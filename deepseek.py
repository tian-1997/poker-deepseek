"""DeepSeek 决策适配器 + 本地兜底 AI + 复盘日志。

约定：模型返回 {action, amount, reason}；牌局中只使用 action，reason 存进日志，
一局结束后由 CLI 的复盘菜单展示。模型输出非法 / 超时 / 无 key 时自动降级到本地
蒙特卡洛兜底 AI，牌局不会卡死。
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request

import jev
import memory
import preflop_table
from cards import Card, evaluate7, fmt_cards, new_deck
from engine import Action

DEFAULT_BASE = "https://api.deepseek.com"
# deepseek-chat / deepseek-reasoner 已于 2026-07-24 下线，现行名字是 deepseek-flash
DEFAULT_MODEL = "deepseek-flash"
CHAT_MODEL_NAMES = {"deepseek-chat", "deepseek-reasoner", "deepseek-flash",
                    "deepseek-v4-flash", "deepseek-v4-pro"}


def is_chat_model(name: str) -> bool:
    """这些名字只对聊天网关有意义，配到 Jev 上会 400，需要自动纠正。"""
    return (name or "").strip().lower() in CHAT_MODEL_NAMES
JEV_BASE = jev.JEV_DEFAULT_BASE
JEV_MODEL = jev.JEV_DEFAULT_MODEL

KEY_PREFIXES = ("apikey_", "tsf_", "sk-", "sk_")


def looks_like_api_key(value: str) -> bool:
    """用户常把 Key 粘进「API 地址」框；这种值绝不能拿去拼 URL。"""
    v = (value or "").strip()
    if not v:
        return False
    if v.startswith(KEY_PREFIXES):
        return True
    return len(v) > 60 and "://" not in v and "/" not in v and " " not in v


def safe_display(value: str) -> str:
    """报错/日志里不要把完整 Key 打出来（用户会截图、会贴聊天）。"""
    v = value or ""
    if looks_like_api_key(v):
        return v[:8] + "…（疑似 API Key，已隐藏）"
    return v

SYSTEM_PROMPT = """你是无限注德州扑克（NLHE）牌桌上的一名玩家，同桌 2~6 人，人数、座位位置和你的风格见局面说明。
只输出一个 json 对象，不要输出任何解释文字、Markdown 或代码块。
格式：{"action":"...","amount":<整数或null>,"reason":"1~2句中文，说明读牌和下注依据"}
action 只能从当前局面给出的合法动作里选。
amount 是「本轮加注到的总投入额」，不是增量；allin / check / call / fold 时填 null。
多人底池里要比单挑更谨慎：对手越多，中等牌力的价值越低，也别轻易诈唬多人。
reason 只用于本局结束后的复盘，牌局进行中不会展示，请写真实判断（牌力、底池赔率、位置、对手数量与倾向）。
下注范围要同时包含价值、半诈唬（听牌）和少量纯诈唬，别只在大牌时才下注；
单挑或处于有利位置时可以更主动地施压。牌力很强时也可以偶尔过牌设陷阱。
不要虚张声势地写没有依据的读牌。"""

# 每个机器人座位一种打法，让多人桌的「位置」有区分度；agg 也影响本地兜底 AI 的阈值
PERSONAS = [
    {"tag": "稳健", "style": "稳健偏紧：只用好牌和大牌型下注，遇到大额下注会果断弃牌。", "agg": -0.03},
    {"tag": "激进", "style": "激进爱施压：经常加注和半诈唬，喜欢用位置优势抢底池。", "agg": 0.08},
    {"tag": "紧凶", "style": "紧凶：起手牌范围窄，但一旦入池就打得很凶，重视价值下注。", "agg": 0.03},
    {"tag": "松凶", "style": "松凶：入池范围宽，喜欢在翻后找机会偷池，诈唬频率偏高。", "agg": 0.10},
    {"tag": "跟注站", "style": "偏被动：很少主动加注，但拿到中等牌力会一直跟注到底。", "agg": -0.08},
]

PERSONA_BY_TAG = {p["tag"]: p for p in PERSONAS}
STYLE_RANDOM = "随机"      # 特殊值：开局时从风格池里随机抽一个（不是真风格）


def persona_for(tag: str | None) -> dict | None:
    """按短名取风格；空值返回 None（表示沿用座位默认轮换）。名字写错直接报错。"""
    if not tag:
        return None
    found = PERSONA_BY_TAG.get(tag)
    if found is None:
        raise ValueError(f"不认识的风格 {tag!r}，可选：{STYLE_RANDOM} / "
                         f"{' / '.join(PERSONA_BY_TAG)}")
    return found

ALIASES = {
    "fold": "fold", "f": "fold", "弃牌": "fold", "弃": "fold",
    "check": "check", "k": "check", "过牌": "check", "过": "check",
    "call": "call", "c": "call", "跟注": "call", "跟": "call",
    "raise": "raise", "r": "raise", "bet": "raise", "b": "raise",
    "加注": "raise", "下注": "raise", "加": "raise",
    "allin": "allin", "all-in": "allin", "all_in": "allin", "a": "allin",
    "全下": "allin", "全压": "allin",
}


def build_prompt(snap: dict) -> str:
    legal = snap["legal"]
    sb, bb = snap["blinds"]
    lines = [
        f"第 {snap['hand_no']} 局 · {snap['street_cn']} · 盲注 {sb}/{bb} · "
        f"{snap.get('n_players', 2)} 人桌 · 你的位置 {snap['position']} · "
        f"未弃牌对手 {snap.get('opponent_count', 1)} 家",
        "你的手牌: " + " ".join(c.code for c in snap["hole"]),
        "公共牌: " + (" ".join(c.code for c in snap["board"]) or "(未发)"),
        f"底池 {snap['pot']} | 本轮当前下注 {legal['current_bet']} | 你本轮已投 {snap['my_round_bet']} | 需跟注 {legal['to_call']}",
        f"你的剩余筹码 {snap['my_stack']}",
        "对手: " + "; ".join(
            f"{o['name']} 筹码{o['stack']} 本轮{o['round_bet']}" +
            (" 已全下" if o["all_in"] else "") + (" 已弃牌" if o["folded"] else "")
            for o in snap["opponents"]
        ),
        "本局行动: " + (" / ".join(snap["history"][-14:]) or "(无)"),
        "合法动作（只能选其中一项）:",
        "- fold 弃牌",
    ]
    if legal["check"]:
        lines.append("- check 过牌")
    else:
        lines.append(f"- call 跟注 {legal['call']}")
    if legal["raise_to"]:
        lo, hi = legal["raise_to"]
        lines.append(f"- raise 加注到 {lo}~{hi} 之间任一整数（amount = 加注后你本轮的总投入额）")
    lines.append(f"- allin 全下（总投入额 {legal['allin']}）")
    lines.append('输出 json：{"action":...,"amount":...,"reason":...}')
    return "\n".join(lines)


def _api_error_text(raw: str) -> str:
    """从网关的错误响应里挖出人话：OpenAI 风格是 {"error":{"message":...}}。"""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw.strip()[:160]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            for key in ("message", "msg", "type", "code"):
                if err.get(key):
                    return str(err[key])[:160]
        for key in ("message", "msg", "detail"):
            if isinstance(data.get(key), str) and data[key]:
                return data[key][:160]
    return raw.strip()[:160]


ACTION_RE = re.compile(r'"action"\s*:\s*"([^"]{1,24})"')
AMOUNT_RE = re.compile(r'"amount"\s*:\s*(-?\d+)')
REASON_RE = re.compile(r'"reason"\s*:\s*"([^"]{0,120})')


def _salvage_action(text: str):
    """JSON 被 max_tokens 截断时，用正则把 action / amount / reason 抢救出来。"""
    if not text:
        return None
    m = ACTION_RE.search(text)
    if not m:
        return None
    dec = {"action": m.group(1)}
    amount = AMOUNT_RE.search(text)
    if amount:
        dec["amount"] = int(amount.group(1))
    reason = REASON_RE.search(text)
    if reason:
        dec["reason"] = reason.group(1)
    return dec


def _content_of(payload: dict, thinking: bool) -> str:
    """取正文；DeepSeek V4 默认开思考，content 可能被思考内容挤空。"""
    choice = ((payload.get("choices") or [{}])[0] or {})
    msg = choice.get("message") or {}
    content = msg.get("content")
    if content:
        return content
    finish = choice.get("finish_reason")
    has_think = bool(msg.get("reasoning_content") or msg.get("reasoning"))
    if has_think:
        raise RuntimeError(
            f"模型只产出了思考内容、正文为空（finish_reason={finish}）："
            "思考模式的 token 预算把正文挤掉了。"
            + ("已请求关闭思考，但这个网关可能不支持 thinking 参数。" if not thinking else
               "可以调大 --thinking 的预算，或关掉思考模式。"))
    raise RuntimeError(f"模型返回的正文为空（finish_reason={finish}，thinking={thinking}）")


def _extract_json(text: str):
    if not text:
        return None
    text = text.strip()
    cand = None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        cand = m.group(1)
    else:
        start = text.find("{")
        if start < 0:
            return None
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        cand = text[start:i + 1]
                        break
    try:
        return json.loads(cand) if cand else None
    except (ValueError, TypeError):
        return None


def _normalize(dec, legal: dict):
    """把模型输出校验/夹逼成引擎可执行的动作；非法返回 None 交给兜底。"""
    if not isinstance(dec, dict):
        return None
    raw = str(dec.get("action", "")).strip().lower()
    kind = ALIASES.get(raw)
    if kind is None:
        for key, val in ALIASES.items():
            if raw.startswith(key) and len(key) >= 2:
                kind = val
                break
    if kind is None:
        return None
    try:
        amount = int(dec["amount"]) if dec.get("amount") is not None else None
    except (ValueError, TypeError):
        amount = None
    reason = str(dec.get("reason", "")).strip()

    if kind in ("check", "call"):
        kind = "call" if legal["to_call"] else "check"
        amount = 0
    elif kind in ("raise", "allin"):
        rng = legal["raise_to"]
        if rng is None:
            return None
        lo, hi = rng
        if kind == "allin" or amount is None:
            amount = hi
        else:
            # 容错：模型偶尔把 amount 当「增量」而不是总额
            if amount < lo and amount > 0 and lo <= amount + legal["current_bet"] <= hi:
                amount += legal["current_bet"]
            amount = max(lo, min(hi, amount))
        kind = "allin" if amount >= hi else "raise"
    return Action(kind, amount), reason


def action_text(kind: str, amount: int, legal: dict) -> str:
    raise_text = f"下注 {amount}" if legal["current_bet"] == 0 else f"加注到 {amount}"
    return {
        "fold": "弃牌",
        "check": "过牌",
        "call": f"跟注 {legal['call']}",
        "raise": raise_text,
        "allin": f"全下 {amount}",
    }.get(kind, kind)


RANK_ORDER = "23456789TJQKA"


def hand_code(hole: list[Card]) -> str:
    """两张底牌 -> 169 手牌代号：对子 AA、同花 AKs、非同花 AKo（大牌在前）。"""
    a, b = hole[0], hole[1]
    if a.rank == b.rank:
        return RANK_ORDER[a.rank - 2] * 2
    hi, lo = (a, b) if a.rank > b.rank else (b, a)
    return (RANK_ORDER[hi.rank - 2] + RANK_ORDER[lo.rank - 2]
            + ("s" if hi.suit == lo.suit else "o"))


def _playability(code: str) -> float:
    """胜率之外的修正：同花、连张更容易兑现，大间隔的高张难打。"""
    if len(code) == 2:
        return 0.0
    hi, lo = RANK_ORDER.index(code[0]), RANK_ORDER.index(code[1])
    gap = hi - lo - 1
    suited = code.endswith("s")
    # 非同花的大间隔高张最容易被压制（K9o 遇到 AK/AQ/KQ 都只有三张活牌），
    # 单看「对随机牌的胜率」会把它抬得过高，这里按间隔罚分。
    bonus = 0.04 if suited else -0.012 * gap
    if gap <= 1 and hi <= 10:              # 连张（Q 以上不算，AK/KQ 本来就不差）
        bonus += 0.02
    if gap >= 4:
        bonus -= 0.02
    return bonus


def preflop_score(code: str) -> float:
    """起手牌强度 0~1：对随机单挑的胜率（查表）+ 可玩性修正。"""
    base = preflop_table.EQUITY.get(code, 0.34)
    return max(0.0, min(1.0, base + _playability(code)))


def _weighted_scores() -> list[float]:
    """1326 个真实组合的强度分布（对子 6 种、同花 4 种、非同花 12 种）。"""
    out: list[float] = []
    for i in range(13):
        for j in range(i, 13):
            lo, hi = RANK_ORDER[i], RANK_ORDER[j]
            if i == j:
                out += [preflop_score(hi * 2)] * 6
            else:
                out += [preflop_score(hi + lo + "s")] * 4
                out += [preflop_score(hi + lo + "o")] * 12
    return sorted(out)


_WEIGHTED = _weighted_scores()

# 各位置的入池比例（按牌力百分位）：越靠后越宽，大盲防守最宽
OPEN_PCT = {"UTG": 0.16, "MP": 0.20, "CO": 0.27, "BTN": 0.42,
            "SB": 0.36, "BB": 0.50, "BTN/SB": 0.44}
DEFEND_PCT = {"UTG": 0.08, "MP": 0.10, "CO": 0.14, "BTN": 0.19,
              "SB": 0.22, "BB": 0.40, "BTN/SB": 0.44}
THREE_BET_PCT = 0.05          # 面对一个加注时反加的比例
CALL_3BET_PCT = 0.045         # 面对 3bet 继续的比例
FOUR_BET_PCT = 0.012          # 面对 3bet 还能加回去的顶级牌


def _cut(pct: float) -> float:
    """「大约前 pct 比例的手牌」的强度分界线。"""
    pct = max(0.002, min(0.998, pct))
    idx = int((1.0 - pct) * (len(_WEIGHTED) - 1))
    return _WEIGHTED[max(0, min(len(_WEIGHTED) - 1, idx))]


def preflop_decision(hole: list[Card], position: str, to_call: int, pot: int,
                     bb: int, n_opp: int = 1, aggression: float = 0.0):
    """按「位置 + 牌力表」给翻前动作，返回 (kind, amount, reason)。

    以前翻前只看「对随机手牌的胜率」，于是 72o 也觉得自己有 34%、什么加注都跟。
    现在按位置定入池线：越靠后越宽，面对加注只留前 40%（大盲）到 8%（枪口），
    面对 3bet 只留前 4.5%。amount 是「本轮加注到的总额」，引擎会再夹进合法区间。
    """
    code = hand_code(hole)
    score = preflop_score(code)
    pos = position if position in OPEN_PCT else "CO"
    widen = max(-0.06, min(0.08, aggression * 0.45))     # 凶的座位范围更宽
    multi = 0.82 if n_opp >= 2 else 1.0                  # 多人底池收紧，别人可能已经有大牌

    def need(pct: float) -> float:
        return _cut(pct * multi + widen)

    if to_call <= 0:                                     # 大盲免费看牌
        if score >= need(OPEN_PCT[pos]):
            return "raise", max(3 * bb, pot), f"{code} 免费看牌但牌力够，主动加注"
        return "check", 0, f"{code} 牌力一般，免费看翻牌"

    if to_call <= bb * 1.25:                             # 无人加注（盲注 / 跛入）
        if score >= need(OPEN_PCT[pos]):
            return "raise", max(3 * bb, to_call + 2 * bb), \
                f"{code} 在{pos}的开池线上（前 {OPEN_PCT[pos]:.0%}），加注"
        return "fold", 0, f"{code} 不够{pos}开池，弃牌"

    if to_call <= bb * 4.5:                              # 面对一次加注
        if score >= need(THREE_BET_PCT):
            return "raise", int(to_call * 3), f"{code} 是顶级牌，3bet 到 {int(to_call * 3)}"
        if score >= need(DEFEND_PCT[pos]):
            return "call", 0, f"{code} 在{pos}的防守线内（前 {DEFEND_PCT[pos]:.0%}），跟注"
        return "fold", 0, f"{code} 面对加注不够{pos}防守线，弃牌"

    if score >= need(FOUR_BET_PCT):                      # 面对 3bet 以上
        return "raise", int(to_call * 2.5), f"{code} 顶级牌，再加注"
    if score >= need(CALL_3BET_PCT):
        return "call", 0, f"{code} 前 {CALL_3BET_PCT:.1%} 的牌，跟 3bet"
    return "fold", 0, f"{code} 不够跟 3bet，弃牌"


def estimate_equity(hole: list[Card], board: list[Card], sims: int, rng,
                    opponents: int = 1, opp_pools: list[list] | None = None) -> float:
    """蒙特卡洛胜率。给了 opp_pools 就从「对手可能拿的牌」里抽，而不是随便抽两张。

    随机抽的假设是「对手拿 72o 和拿 AA 一样常见」，会让胜率整体虚高十几到二十个点
    （K9o 对随机 58%，对上真实加注范围只有 38%）。opp_pools 由 opponent_pools()
    按行动线生成。
    """
    known = set(hole + board)
    deck = [c for c in new_deck() if c not in known]
    need = 5 - len(board)
    opponents = max(1, opponents)
    if need + 2 * opponents > len(deck) or sims <= 0:
        return 0.5
    pools = [p for p in (opp_pools or []) if p]
    score = 0.0
    for _ in range(sims):
        rng.shuffle(deck)
        full = board + deck[:need]
        mine = evaluate7(hole + full)
        best = None
        for k in range(opponents):
            extra = None
            if k < len(pools):
                pool = pools[k]
                for _try in range(10):
                    cand = pool[rng.randrange(len(pool))]
                    if (cand[0] not in known and cand[1] not in known
                            and cand[0] not in full and cand[1] not in full):
                        extra = [cand[0], cand[1]]
                        break
            if extra is None:
                extra = deck[need + 2 * k:need + 2 * k + 2]
            theirs = evaluate7(extra + full)
            if best is None or theirs > best:
                best = theirs
        if mine > best:
            score += 1.0
        elif mine == best:
            score += 0.5
    return score / sims


# 对手这手加注过几次 -> 他的范围大约收缩到前百分之多少
RANGE_BY_RAISES = {0: 0.55, 1: 0.22, 2: 0.06}


def _line_of(snap: dict, name: str) -> dict:
    """从行动记录还原某个对手这手的打法：翻前加注几次、本街是否主动下注。"""
    raises = 0
    aggressive = False
    street = snap.get("street", "preflop")
    for rec in snap.get("actions") or []:
        if rec.get("name") != name:
            continue
        kind = rec.get("kind")
        if rec.get("street") == "preflop" and kind in ("raise", "allin"):
            raises += 1
        if rec.get("street") == street and kind in ("raise", "allin"):
            aggressive = True
    return {"raises": raises, "aggressive": aggressive}


def _range_combos(cut: float, dead: set) -> list[tuple[Card, Card]]:
    """强度够线、且不和已知牌冲突的所有两张组合。"""
    deck = [c for c in new_deck() if c not in dead]
    out = []
    for i, a in enumerate(deck):
        for b in deck[i + 1:]:
            if preflop_score(hand_code([a, b])) >= cut:
                out.append((a, b))
    return out


def _fit_weight(combo: tuple[Card, Card], board: list[Card], aggressive: bool) -> float:
    """按牌面契合度加权：下注的对手更可能真中了，过牌/跟注的更可能是中等牌。"""
    if len(board) < 3:
        return 1.0
    made = evaluate7(list(combo) + board)[0]              # 0 高牌 ... 8 同花顺
    strength = made / 8.0
    draw = draw_strength(list(combo), board)              # 0~1 的听牌强度
    if aggressive:
        return 0.18 + 1.6 * strength + 0.9 * draw
    return max(0.05, 1.0 - 0.55 * strength + 0.5 * draw)


def opponent_pools(snap: dict, rng, size: int = 120) -> list[list[tuple[Card, Card]]]:
    """按每个对手的行动线，抽一批「他这手可能拿的底牌」，供胜率估算使用。"""
    board = list(snap.get("board") or [])
    dead = set(board) | set(snap.get("hole") or [])
    pools: list[list[tuple[Card, Card]]] = []
    for op in snap.get("opponents", []):
        if op.get("folded"):
            continue
        line = _line_of(snap, op.get("name", ""))
        cut = _cut(RANGE_BY_RAISES.get(min(line["raises"], 2), 0.55))
        combos = _range_combos(cut, dead)
        if not combos:
            pools.append([])
            continue
        weights = [_fit_weight(c, board, line["aggressive"]) for c in combos]
        pool = rng.choices(combos, weights=weights, k=size)
        pools.append([(c[0], c[1]) for c in pool])
    return pools


def count_outs(hole: list[Card], board: list[Card]) -> int:
    """还能把牌力提到顺子/同花及以上的牌数（听牌的真正出路）。"""
    if not 3 <= len(board) < 5:
        return 0
    known = set(hole + board)
    current = evaluate7(hole + board)
    outs = 0
    for c in new_deck():
        if c in known:
            continue
        made = evaluate7(hole + board + [c])
        if made[0] >= 4 and made > current:
            outs += 1
    return outs


def implied_odds_ok(to_call: int, pot: int, outs: int, streets_left: int = 2,
                    implied: float = 1.35) -> bool:
    """听牌靠「以后还能赢到的钱」补够赔率时，值得跟一手。"""
    if to_call <= 0:
        return True
    if outs <= 0:
        return False
    draw_eq = min(0.9, outs * (0.04 if streets_left >= 2 else 0.02))
    return draw_eq * (pot + to_call) * implied >= to_call


def bluff_catch_ok(to_call: int, pot: int, eq: float, weak_line: bool,
                   has_showdown_value: bool) -> bool:
    """对手一路被动、河牌才下小注：多半是诈唬，有摊牌价值的牌就该抓。"""
    if to_call <= 0 or not weak_line or not has_showdown_value:
        return False
    if to_call > pot * 0.55:              # 小注才抓；大注是真的强，别硬顶
        return False
    return eq >= 0.14


def _weak_line(snap: dict) -> bool:
    """还在牌里的对手，这手之前有没有主动加注过？没有 = 被动线。"""
    names = {o.get("name") for o in snap.get("opponents", []) if not o.get("folded")}
    street = snap.get("street")
    for rec in snap.get("actions") or []:
        if rec.get("name") in names and rec.get("street") != street \
                and rec.get("kind") in ("raise", "allin"):
            return False
    return True


def _showdown_value(hole: list[Card], board: list[Card]) -> bool:
    """这手牌能不能赢过「什么都没中」的诈唬：一对以上，或者 A/K 高牌。"""
    made = evaluate7(hole + board)
    return made[0] >= 1 or (made[0] == 0 and made[1] >= 13)



# 本地兜底 AI 的诈唬参数。以前它只在胜率 72% 以上才下注、面对下注只会弃/跟，
# 于是「它一下注就说明牌好」——对手（人类）读起来毫不费力。这里把下注范围拆成
# 价值 / 半诈唬 / 纯诈唬三段，并让强牌偶尔过牌设陷阱，下注才不再等于有牌。
BLUFF_BASE = 0.19            # 每次机会的基础诈唬率，再叠加风格偏移
TRAP_HU = 0.24               # 单挑时强牌过牌设陷阱的概率
TRAP_MULTI = 0.10            # 多人底池少打陷阱，免得白送免费牌


def _straight_count(cards) -> int:
    """五连张窗口里已占了几张（4 = 听顺，含卡顺/两头顺）。"""
    ranks = {c.rank for c in cards}
    if 14 in ranks:                 # A 也能当 1 用（A2345）
        ranks = ranks | {1}
    best = 0
    for low in range(1, 11):
        best = max(best, len(ranks & set(range(low, low + 5))))
    return best


def draw_strength(hole, board) -> float:
    """听牌强度 0~1：同花听+顺听 1.0，同花听 .85，顺听 .65。河牌没有听牌。"""
    if len(board) < 3 or len(board) >= 5:
        return 0.0
    cards = list(hole) + list(board)
    suits: dict[str, int] = {}
    for c in cards:
        suits[c.suit] = suits.get(c.suit, 0) + 1
    flush = any(v == 4 for v in suits.values())
    straight = _straight_count(cards) >= 4
    if flush and straight:
        return 1.0
    return 0.85 if flush else 0.65 if straight else 0.0


def bluff_rate(aggression: float, bluff_adj: float = 0.0, scale: float = 1.0) -> float:
    """这个座位「有机会时」下手的概率：松凶约 30%，跟注站约 5%。"""
    raw = BLUFF_BASE + aggression * 1.8 + bluff_adj
    return max(0.03, min(0.50, raw * max(0.0, scale)))


def sized_raise(legal: dict, frac: float):
    """按底池比例给下注/加注额并夹进合法区间；不能加注时返回 None。"""
    if not legal.get("raise_to"):
        return None
    lo, hi = legal["raise_to"]
    amount = legal["current_bet"] + int(max(1, (legal["pot"] + legal["to_call"]) * frac))
    return max(lo, min(hi, amount))


def heuristic_decide(snap: dict, rng, sims: int, aggression: float = 0.0,
                     reads: dict | None = None, plan: dict | None = None,
                     bluff_scale: float = 1.0):
    """无 API / 调用失败时的本地玩家：蒙特卡洛胜率 + 底池赔率 + 风格 + 诈唬。

    reads 是跨手对手画像（memory.py）——和 Jev 共用同一份数据：
    对手紧就抬高跟注门槛，对手爱弃牌就多偷池。

    plan 是「这一局」的作战计划（由 DeepSeekPlayer 每局重建），记录当前是否在
    诈唬、是否已经设过陷阱，用来让诈唬能跨街延续（第二枪、第三枪），也让
    复盘能看出这是一手有计划的偷池，而不是随机抽风。
    """
    legal = snap["legal"]
    n_opp = snap.get("opponent_count", 1)
    street = snap.get("street", "")
    board = snap["board"]
    to_call, pot = legal["to_call"], legal["pot"]
    margin, bluff_adj, read_note = memory.read_adjust(
        reads or {}, [o["name"] for o in snap.get("opponents", [])])
    if street == "preflop":
        # 翻前交给「位置 + 牌力表」：随机抽两张对手牌来算翻前胜率毫无意义，
        # 因为没人会用 72o 加注。这条分支还省掉了每次决策 100 多次模拟。
        # 读牌同样作用于翻前：对手越紧，我们的入池线越窄（margin 为正 = 更谨慎）。
        kind, amount, why = preflop_decision(
            snap["hole"], snap.get("position", "CO"), to_call, pot,
            snap["blinds"][1], n_opp, aggression - margin * 2.5)
        if read_note:
            why = f"{why} · 读牌 {read_note}"
        return Action(kind, amount), why
    # 翻后按对手的行动线收窄范围，再算胜率（不再假设他拿的是随机两张）
    opp_pools = opponent_pools(snap, rng)
    eq = estimate_equity(snap["hole"], board, sims, rng, n_opp, opp_pools=opp_pools)
    odds = to_call / (pot + to_call) if to_call else 0.0
    noise = rng.uniform(-0.04, 0.04)
    a = max(-0.15, min(0.15, aggression))
    plan = plan if plan is not None else {}
    draw = draw_strength(snap["hole"], board)
    # 对手越多，诈唬越危险：摊牌成本随人数上升，成功率却下降
    multi = 1.0 if n_opp <= 1 else max(0.35, 1.0 - 0.18 * (n_opp - 1))
    bluff_p = bluff_rate(a, bluff_adj, bluff_scale)
    detail = f"估胜率 {eq:.0%}({n_opp}家)"
    if read_note:
        detail += f" · {read_note}"
    if draw >= 0.65:
        detail += " · 有听牌"

    def fold(msg: str):
        plan["bluff"] = False
        return Action("fold"), msg

    def check(msg: str):
        return Action("check"), msg

    if to_call == 0:
        if eq > 0.74 - a + noise:
            # 强牌也偶尔过牌：不然「一 bet 就是好牌」太好读
            trap_p = TRAP_HU if n_opp <= 1 else TRAP_MULTI
            if street != "river" and rng.random() < trap_p:
                plan["bluff"] = False
                plan["trapped"] = True
                return check(f"{detail} 牌力够但故意过牌，设个陷阱等他下注")
            target = sized_raise(legal, rng.uniform(0.55, 0.85))
            if target:
                plan["bluff"] = False
                return Action("raise", target), f"{detail} 偏高，做价值下注 {target}"
            return check(f"{detail}，免费看下一张")
        # 半诈唬：有听牌，现在还不强，但下注能立刻赢、也能买到牌
        if draw >= 0.6 and eq < 0.62:
            if rng.random() < bluff_p * (0.55 + draw) * multi:
                target = sized_raise(legal, rng.uniform(0.5, 0.7))
                if target:
                    plan["bluff"] = True
                    return Action("raise", target), \
                        f"{detail} 但听牌不错，半诈唬下注 {target} 施压"
        # 上一街是诈唬的话，这一街按概率继续开第二枪
        if plan.get("bluff"):
            barrel = 0.62 if street != "river" else 0.42
            if rng.random() < barrel:
                target = sized_raise(legal, rng.uniform(0.5, 0.78))
                if target:
                    return Action("raise", target), f"{detail} 延续上一街的进攻，再开一枪 {target}"
            plan["bluff"] = False
        # 纯诈唬：牌基本没戏，但对手也可能没戏，直接偷
        if board and eq < 0.45 and rng.random() < bluff_p * 0.7 * multi:
            target = sized_raise(legal, rng.uniform(0.45, 0.65))
            if target:
                plan["bluff"] = True
                word = "牌面基本没戏" if eq < 0.3 else "牌力一般"
                return Action("raise", target), f"{detail} {word}，但底池空着，直接偷"
        # 薄价值/主动施压：中等牌力偶尔也下注，别只下大牌
        if eq > 0.55 - a and rng.random() < max(0.0, min(0.9, 0.3 + a + bluff_adj)):
            target = sized_raise(legal, rng.uniform(0.4, 0.6))
            if target:
                return Action("raise", target), f"{detail}，主动施压 {target}"
        return check(f"{detail}，免费看下一张")

    # ---- 面对下注 ----
    if eq > 0.72 - a + noise and eq > odds + 0.15:
        target = sized_raise(legal, rng.uniform(0.6, 0.9))
        if target:
            plan["bluff"] = False
            return Action("raise", target), f"{detail} 明显高于赔率 {odds:.0%}，加注要价值到 {target}"
    # 诈唬加注：手里有听牌，或者对手下得很轻（多半是试探），反加回去
    if n_opp <= 2:
        semi = draw >= 0.6 and eq >= 0.25
        cheap = eq < 0.45 and to_call <= max(1, int(pot * 0.35))
        if semi or cheap:
            chance = bluff_p * (0.75 if semi else 0.35) * multi
            if rng.random() < chance:
                target = sized_raise(legal, rng.uniform(0.7, 1.0))
                if target:
                    plan["bluff"] = True
                    why = "用听牌加注施压" if semi else "对手下得轻，反加诈唬"
                    return Action("raise", target), f"{detail} {why}到 {target}"
    if eq < odds - 0.06 - a + margin:
        # 听牌：按「真正的补牌数 + 隐含赔率」决定买不买，而不是死抠当前底池赔率
        outs = count_outs(snap["hole"], board)
        if outs >= 6 and implied_odds_ok(to_call, pot, outs, 5 - len(board)):
            plan["bluff"] = False
            return Action("call"), f"{detail} 差一点，但 {outs} 张补牌有隐含赔率，跟一手买牌"
        # 对手一路被动、河牌才下小注：多半是诈唬，有摊牌价值就抓
        if bluff_catch_ok(to_call, pot, eq, _weak_line(snap),
                          _showdown_value(snap["hole"], board)):
            plan["bluff"] = False
            return Action("call"), f"{detail} 对手一路被动，这里抓诈唬"
        return fold(f"{detail} 低于底池赔率 {odds:.0%}，放弃")
    if eq >= odds - 0.02 - a / 2 + margin:
        word = "覆盖" if eq >= odds else "接近"
        plan["bluff"] = False
        return Action("call"), f"{detail} {word}底池赔率 {odds:.0%}，跟注"
    return fold(f"{detail} 不足以跟注 {to_call}")


class DeepSeekPlayer:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, timeout: float | None = None,
                 offline: bool = False, sims: int = 140, rng=None,
                 trust_config: bool = False, persona: str = "",
                 aggression: float = 0.0, tag: str = "", provider: str = "auto",
                 memory=None, thinking: bool = False, bluff_scale: float = 1.0):
        # 优先级：显式参数 > POKER_* > JEV_* > DEEPSEEK_*（任意 OpenAI 兼容端点都能接）
        env_key = (os.environ.get("POKER_API_KEY") or os.environ.get("JEV_API_KEY")
                   or os.environ.get("DEEPSEEK_API_KEY") or "")
        env_base = (os.environ.get("POKER_BASE_URL") or os.environ.get("JEV_BASE_URL")
                    or os.environ.get("DEEPSEEK_BASE_URL") or "")
        raw_key = (api_key if api_key is not None else env_key).strip()
        custom_key = bool(os.environ.get("JEV_API_KEY") or os.environ.get("POKER_API_KEY") or api_key)

        config_notes: list[str] = []
        # 误填自救：把 API Key 粘进「API 地址」是最常见的配置错误。
        # 不能拿它拼 URL（会得到 unknown url type），更不能把密钥发到莫名其妙的主机。
        if looks_like_api_key(base_url or ""):
            if not raw_key:
                raw_key = base_url.strip()
                config_notes.append("API 地址里填的是 API Key，已自动当作 Key 使用")
            else:
                config_notes.append("API 地址里填的是 API Key，已忽略该地址")
            base_url = None
        if looks_like_api_key(env_base):
            if not raw_key:
                raw_key = env_base.strip()
                config_notes.append("环境变量里的 API 地址其实是 API Key，已自动当作 Key 使用")
            else:
                config_notes.append("环境变量里的 API 地址其实是 API Key，已忽略")
            env_base = ""
        for label, value in (("--base-url", base_url), ("环境变量", env_base)):
            if value and not value.startswith(("http://", "https://")):
                raise ValueError(
                    f"{label} 的 API 地址不是网址：{safe_display(value)}\n"
                    "API 地址必须是 https:// 开头、以 /v1 结尾（别带 /chat/completions）；\n"
                    "API Key 请填到「API Key」输入框或 JEV_API_KEY，不要填进 API 地址。")

        # TypeSafe 官方控制台 (console.typesafe.ai/keys) 发的 key 是 apikey_ 开头；
        # tsf_ 是部分文档里的旧写法，一并兼容。只填 key 也能自动用官方端点 + jev-latest。
        official_key = raw_key.startswith(("apikey_", "tsf_"))
        official_auto = official_key and not (base_url or env_base)
        want_jev_default = (official_auto or provider == "jev") and not (base_url or env_base)
        if custom_key and not (base_url or env_base) and not want_jev_default:
            raise ValueError(
                "检测到自定义 API key，但没有设置 base_url。\n"
                "请设置 JEV_BASE_URL / POKER_BASE_URL，或传 --base-url。\n"
                "TypeSafe Jev 填 https://api.typesafe.ai/v1；聊天模型网关填 https://网关域名/v1。\n"
                "（不设 base_url 会被拒绝，避免密钥误发到 DeepSeek 官方地址。）")
        chosen_base = base_url or env_base or (JEV_BASE if want_jev_default else DEFAULT_BASE)
        default_model = (JEV_MODEL if (want_jev_default or provider == "jev"
                                      or "typesafe.ai" in chosen_base.lower()) else DEFAULT_MODEL)
        chosen_model = (model or os.environ.get("POKER_MODEL") or os.environ.get("JEV_MODEL")
                        or os.environ.get("DEEPSEEK_MODEL") or default_model)
        if want_jev_default and (chosen_model == DEFAULT_MODEL
                                 or is_chat_model(chosen_model)):
            # 旧配置里残留的聊天模型名对 Jev 无效，跟着一起纠正，否则会 400。
            chosen_model = JEV_MODEL
            config_notes.append("模型名是旧的聊天模型，已改为 jev-latest")
        if provider == "auto":
            provider = ("jev" if (official_auto or "typesafe.ai" in chosen_base.lower()
                                  or chosen_model.lower().startswith("jev-")) else "openai")
        self.provider = provider
        self.api_key = raw_key
        self.model = chosen_model
        self.base_url = chosen_base
        self.timeout = timeout if timeout is not None else float(os.environ.get("DEEPSEEK_TIMEOUT", "30"))
        self.offline = offline
        self.sims = sims
        self.rng = rng if rng is not None else random.Random()
        self.persona = persona or "稳健均衡：按牌力和底池赔率做决定。"
        self.aggression = aggression
        self.tag = tag                            # 风格短名，如「激进」
        self.name = ""                            # 座位名，由 CLI 填，复盘标题用
        self.memory = memory                      # 跨手对手画像（Jev 与本地兜底共用）
        self.thinking = thinking                  # 是否让聊天模型先思考（默认关，见 _chat）
        self.bluff_scale = bluff_scale            # 兜底 AI 的诈唬强度倍率（--bluff）
        self._plan: dict = {}                     # 本局作战计划（诈唬是否延续）
        self.hand_log: list[dict] = []
        self.logs: list[dict] = []
        self.stats = {"api": 0, "fallback": 0, "last_error": ""}
        self.config_notes = config_notes
        self.disabled_reason = ""
        self.warned = False
        if not trust_config:
            self._detect_placeholder()

    def _detect_placeholder(self) -> None:
        """模板占位符不能当成真实配置：否则界面写着「已接入 LLM」，实际每次都悄悄降级。"""
        if self.offline or not self.api_key:
            return
        hints = ("xxx", "example", "placeholder", "sk-...", "填入", "填写", "<", ">", "你的",
                 "real_host", "real_key", "real_model", "your_gateway", "your_host",
                 "paste_key", "paste_gateway", "paste_model", "paste_host")
        for label, value in (("API key", self.api_key), ("base_url", self.base_url), ("model", self.model)):
            reason = None
            if any(ord(c) > 127 for c in value):
                reason = "含中文/非 ASCII 字符"
            elif any(h in value.lower() for h in hints):
                reason = "含示例字样"
            if reason:
                self.disabled_reason = f"{label} {reason}，像是模板占位符（{value!r}）"
                self.offline = True
                return

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and not self.offline

    @property
    def kind_label(self) -> str:
        """界面上的来源标签：Jev 是决策模型，不该标成 LLM。"""
        return "Jev" if self.provider == "jev" else "LLM"

    def start_hand(self, hand_no: int) -> None:
        self.hand_log = []
        self._hand_no = hand_no
        self._plan = {"bluff": False, "trapped": False}

    def apply_persona(self, persona: dict) -> None:
        """换一套打法：重开一局时，抽到「随机」的座位靠这个重新定性格。"""
        self.persona = persona["style"]
        self.tag = persona["tag"]
        self.aggression = persona["agg"]

    def end_hand(self, hand_no: int) -> None:
        self.logs.append({"hand_no": hand_no, "decisions": list(self.hand_log)})

    def _chat(self, prompt: str) -> str:
        url = self.base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        try:
            headers["Authorization"].encode("latin-1")
        except UnicodeEncodeError:
            raise ValueError(
                "API key 含中文/非 ASCII 字符，HTTP 请求头无法编码；"
                "请换成真实 key（别把示例占位符当 key）") from None
        system = (SYSTEM_PROMPT + f"\n本桌你是 {self.name or '一名机器人'}，风格：{self.persona}\n"
                  "请把这个风格体现在动作选择里，但不得违反合法动作限制。")
        base = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "stream": False,
        }
        # DeepSeek V4 系列（deepseek-flash / deepseek-v4-*）默认开思考、effort=high，
        # 300 token 的预算会被思考内容吃光，正文直接为空 -> 每次决策都判「不是合法动作」。
        # 所以默认显式关闭思考；--thinking 才打开（慢，但读牌更细）。
        extras = {
            "temperature": 0.8,
            "max_tokens": 1500 if self.thinking else 900,
            "response_format": {"type": "json_object"},
        }
        if self.thinking:
            extras["thinking"] = {"type": "enabled"}
            extras["reasoning_effort"] = "low"
        else:
            extras["thinking"] = {"type": "disabled"}

        # 不同网关支持的字段不一样：400/422 时逐级去掉可选参数重试，
        # 最后一档只留 model / messages / stream，保证老网关也能用。
        variants = [
            extras,
            {k: v for k, v in extras.items() if k != "response_format"},
            {k: v for k, v in extras.items()
             if k not in ("response_format", "thinking", "reasoning_effort")},
            {},
        ]
        last_err = None
        for i, variant in enumerate(variants):
            body = dict(base)
            body.update(variant)
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raw = ""
                try:
                    raw = e.read().decode("utf-8", "replace")
                except Exception:                                  # noqa: BLE001
                    pass
                detail = _api_error_text(raw)
                last_err = RuntimeError(
                    f"HTTP {e.code}" + (f"：{detail}" if detail else f" {e.reason}"))
                # 400/422 = 这组参数不被接受，换更保守的一档；其它（401/402/404/429）直接抛
                if e.code in (400, 422) and i < len(variants) - 1:
                    continue
                raise last_err from None
            return _content_of(payload, self.thinking)
        raise last_err or RuntimeError("请求失败")

    def _reads(self, snap: dict) -> dict:
        """取本桌对手的跨手画像；没有记忆或没有样本时返回空 dict。"""
        if self.memory is None:
            return {}
        return self.memory.reads_for([o["name"] for o in snap.get("opponents", [])])

    def _jev_decide(self, snap: dict):
        """把一次决策变成 Jev 的 state + questions，返回 ((Action, 理由), 原始答案摘要)。"""
        state = jev.build_state(snap, self.persona, self.name, self._reads(snap))
        questions = jev.build_questions(snap["legal"])
        resp = jev.call(self.base_url, self.api_key, self.model,
                        state, questions, self.timeout)
        parsed = jev.parse_answers(resp, snap["legal"])
        raw = json.dumps(resp.get("answers") or {}, ensure_ascii=False)[:300]
        return parsed, raw

    def _jev_check(self) -> str:
        t0 = time.time()
        try:
            resp = jev.call(
                self.base_url, self.api_key, self.model,
                {"note": "connectivity test from a poker CLI"},
                {"ok": {"type": "noul",
                        "instructions": "This state is a connectivity test ping."}},
                self.timeout)
            ms = int((time.time() - t0) * 1000)
            ans = (resp.get("answers") or {}).get("ok") or {}
            return (f"Jev 连通正常 | 端点 {safe_display(jev.resolve_url(self.base_url))} | "
                    f"模型 {self.model} | {ms}ms\n"
                    f"实际版本 {resp.get('model', '?')} | 自检答案 noul={ans.get('noul', '?')}")
        except Exception as e:                                  # noqa: BLE001
            return (f"Jev 连接失败 | 端点 {safe_display(jev.resolve_url(self.base_url))} | 模型 {self.model}\n"
                    f"{type(e).__name__}: {e}\n"
                    "排查：key 是否为 TypeSafe 官方 apikey_ 开头、base_url 是否为 "
                    "https://api.typesafe.ai/v1、模型是否为 jev-latest、账户余额是否充足"
                    "（Jev 无免费额度；控制台 console.typesafe.ai/keys 可查余额与轮换密钥）。")

    def list_models(self) -> str:
        """查 OpenAI 兼容网关的 GET /v1/models，省得手猜 model id。"""
        if not self.api_key:
            return "没有 API key，无法查询模型列表。"
        if not self.base_url.startswith(("http://", "https://")):
            return ("API 地址不是网址：" + safe_display(self.base_url) +
                    "\n多半是把 API Key 粘进了「API 地址」框，请清空该框"
                    "（或填 https://api.typesafe.ai/v1）。")
        root = self.base_url.rstrip("/")
        for suffix in ("/chat/completions", "/systemone", "/models"):
            if root.endswith(suffix):
                root = root[: -len(suffix)]
        if not root.endswith("/v1"):
            root += "/v1"
        url = root + "/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            headers["Authorization"].encode("latin-1")
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            items = payload if isinstance(payload, list) else (
                payload.get("data") or payload.get("models") or [])
            ids = []
            for it in items:
                mid = (it.get("id") or it.get("name") or it.get("model")
                       if isinstance(it, dict) else str(it))
                if mid:
                    ids.append(str(mid))
            if not ids:
                return f"端点 {safe_display(url)} 没返回可用模型 id；请去控制台「模型列表」复制完整 id。"
            lines = [f"可用模型（{safe_display(url)}，共 {len(ids)} 个）："]
            for mid in ids[:60]:
                mark = "  <- Jev 决策模型，不生成文本，需 --provider jev" if mid.lower().startswith("jev") else ""
                lines.append(f"  {mid}{mark}")
            if len(ids) > 60:
                lines.append(f"  ... 还有 {len(ids) - 60} 个")
            lines.append("把想要的 id 填进 JEV_MODEL 或 --model；聊天模型配 --provider openai（默认）。")
            return "\n".join(lines)
        except Exception as e:                                  # noqa: BLE001
            detail = str(e)
            if looks_like_api_key(self.base_url):
                detail = detail.replace(self.base_url, safe_display(self.base_url))
            return (f"查询失败 | {safe_display(url)}\n{type(e).__name__}: {detail}\n"
                    "有些网关不开放 /v1/models。那就去服务商控制台的「模型列表 / 模型广场」"
                    "复制完整 model id（不要填展示名称）。\n"
                    "TypeSafe 官方 Jev 不提供模型列表，固定用 jev-latest；"
                    "填 apikey_ 开头的官方 key 或官方端点即自动切 Jev 协议。")

    def check_connection(self) -> str:
        """启动前自检：确认端点 / key / 模型 / 协议可用。"""
        notes = ("! 配置已自动纠正：" + "；".join(self.config_notes) + "\n"
                 if self.config_notes else "")
        if self.disabled_reason:
            return (notes + "配置疑似示例占位符，已自动切本地 AI：" + self.disabled_reason +
                    "\n换成真实的 JEV_API_KEY / JEV_BASE_URL / JEV_MODEL 后再跑 --check。")
        if not self.enabled:
            return notes + "未启用在线模型（无 API key 或 --offline），当前使用本地兜底 AI。"
        if self.provider == "jev":
            return notes + self._jev_check()
        t0 = time.time()
        try:
            raw = self._chat('请只回复 json：{"ok":true}')
            ms = int((time.time() - t0) * 1000)
            return (notes + f"连通正常 | 端点 {safe_display(self.base_url)} | "
                    f"模型 {self.model} | {ms}ms\n返回片段: {(raw or '')[:120]}")
        except Exception as e:                                  # noqa: BLE001
            detail = str(e)
            for secret in (self.base_url, self.api_key):
                if secret and looks_like_api_key(secret):
                    detail = detail.replace(secret, safe_display(secret))
            hint = ""
            if looks_like_api_key(self.base_url) or not self.base_url.startswith(
                    ("http://", "https://")):
                hint = ("\n! 这个「端点」不是网址：多半是把 API Key 粘进了「API 地址」框。"
                        "清空 API 地址（或填 https://api.typesafe.ai/v1），把 Key 填到 "
                        "「API Key」框 / JEV_API_KEY；官方 apikey_ 开头的 Key 留空地址即自动走 Jev。")
            return (notes + f"连接失败 | 端点 {safe_display(self.base_url)} | 模型 {self.model}\n"
                    f"{type(e).__name__}: {detail}\n"
                    "排查：base_url 是否填到 /v1（别把 /chat/completions 也写进去）、key 是否有效、"
                    "model id 是否与控制台一致（跑 --models 可拉列表）、端点是否支持 chat/completions。\n"
                    "若你的 key 是 TypeSafe Jev 决策模型，改用 --provider jev（走 /v1/systemone）。"
                    + hint)

    def decide(self, snap: dict):
        legal = snap["legal"]
        entry = {
            "hand_no": snap["hand_no"],
            "street_cn": snap["street_cn"],
            "board": fmt_cards(snap["board"], empty="未发"),
            "hole": fmt_cards(snap["hole"]),
            "pot": snap["pot"],
            "to_call": legal["to_call"],
            "my_stack": snap["my_stack"],
            "position": snap["position"],
            "action": None,
            "action_text": "",
            "reason": "",
            "source": "",
            "latency_ms": 0,
            "raw": "",
            "equity": "",
        }
        if self.enabled:
            t0 = time.time()
            try:
                if self.provider == "jev":
                    parsed, raw = self._jev_decide(snap)
                else:
                    raw = self._chat(build_prompt(snap))
                    dec = _extract_json(raw)
                    if dec is None:
                        dec = _salvage_action(raw)   # 输出被截断时按正则抢救
                    parsed = _normalize(dec, legal)
                entry["raw"] = (raw or "")[:300]
                if parsed:
                    action, reason = parsed
                    source = f"Jev {self.model}" if self.provider == "jev" else self.model
                    entry.update(action={"kind": action.kind, "amount": action.amount},
                                 action_text=action_text(action.kind, action.amount, legal),
                                 reason=reason, source=source, valid=True)
                    self.stats["api"] += 1
                else:
                    snippet = " ".join((raw or "").split())[:60]
                    if self.provider == "jev":
                        entry["fallback_note"] = "Jev 未给出合法动作"
                    elif snippet:
                        entry["fallback_note"] = f"模型输出不是合法动作（开头：{snippet}）"
                    else:
                        entry["fallback_note"] = "模型返回空内容"
            except Exception as e:                # 网络 / 鉴权 / 解析异常一律降级
                entry["fallback_note"] = f"{type(e).__name__}: {e}"
                self.stats["last_error"] = str(e)
            entry["latency_ms"] = int((time.time() - t0) * 1000)

        if entry["action"] is None:
            action, reason = heuristic_decide(snap, self.rng, self.sims, self.aggression,
                                              self._reads(snap), self._plan,
                                              self.bluff_scale)
            entry.update(action={"kind": action.kind, "amount": action.amount},
                         action_text=action_text(action.kind, action.amount, legal),
                         reason=reason,
                         source="本地兜底" + (f"：{entry.get('fallback_note', '')}"
                                              if entry.get("fallback_note") else ""),
                         valid=False)
            self.stats["fallback"] += 1

        self.hand_log.append(entry)
        act = Action(entry["action"]["kind"], entry["action"]["amount"])
        return act, entry

    def review_lines(self, hand_no: int | None = None, label: str | None = None) -> list[str]:
        hands = [h for h in self.logs if hand_no is None or h["hand_no"] == hand_no]
        if not hands:
            return ["(还没有复盘记录)"]
        who = label if label is not None else (self.name or "AI")
        out = []
        for h in hands:
            out.append(f"-- 第 {h['hand_no']} 局 · {who} 决策复盘 --")
            if not h["decisions"]:
                out.append("  本局 AI 没有需要决策的时刻")
            for k, d in enumerate(h["decisions"], 1):
                out.append(f"{k}. [{d.get('street_cn', '?')}] 公共 {d.get('board', '--')} | "
                           f"底池 {d.get('pot', 0)} | 需跟 {d.get('to_call', 0)} | "
                           f"手牌 {d.get('hole', '--')} | 位置 {d.get('position', '?')}")
                out.append(f"   动作 {d.get('action_text', '')}")
                out.append(f"   理由 {d.get('reason') or '(模型未给理由)'}")
                tail = f"   来源 {d.get('source', '')}"
                if d.get("latency_ms"):
                    tail += f" · {d['latency_ms']}ms"
                out.append(tail)
        return out
