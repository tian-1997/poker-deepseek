"""命令行德州扑克 · 你 + 多个 AI 机器人同桌（单机）。

界面刻意做得很短：牌桌状态 + 一行动作提示，单键操作，x 一键清屏。
默认 6 人桌（你 + 5 个机器人，各有位置与风格）；--bots 1 回到单挑。
默认淘汰赛（tournament：筹码见底就出局，一直打到只剩一个人）；--mode cash 换成
现金局（筹码见底可以补筹码接着打）。
默认全部座位走本地 AI（不联网）；要用在线模型加 --llm-bots 或 --llm-seats。
对手可走：OpenAI 兼容聊天模型（chat/completions）、TypeSafe Jev 决策模型
（/v1/systemone，--provider jev），或纯离线本地兜底 AI。
每个座位可以单独指定风格（--seat-styles 1=松凶）和模型（--seat-models 1=jev-latest）。
局末自动亮出所有底牌；默认列出下注理由（--no-reasons 可关），r 可看详细复盘。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# PyInstaller 冻结后 __file__ 在 _MEI 临时目录，存档要落在 exe 旁边。
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable))
           if getattr(sys, "frozen", False) else
           os.path.dirname(os.path.abspath(__file__)))


def set_console_title(title: str = "Windows PowerShell") -> None:
    """控制台伪装成 PowerShell，并把代码页固定为 UTF-8。

    打包成 exe 后由自己设置标题，不依赖 play.ps1；代码页固定后，
    即使对方系统是英文/其他区域，中文牌面也不会变乱码。
    """
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleOutputCP(65001)
        k.SetConsoleCP(65001)
        k.SetConsoleTitleW(title)
    except Exception:
        pass

from cards import (CARD_STYLES, describe, explain_cards, fmt_cards,      # noqa: E402
                   read_hand, set_card_style, style_legend)
from deepseek import (PERSONAS, STYLE_RANDOM, DeepSeekPlayer,   # noqa: E402
                      persona_for, safe_display)
from engine import (Action, HoldemHand, Player, STREET_CN,      # noqa: E402
                    position_name)
from memory import WINDOW_DEFAULT, OpponentMemory                # noqa: E402

HAS_MSVCRT = False
if os.name == "nt" and sys.stdin.isatty():
    try:
        import msvcrt
        HAS_MSVCRT = True
    except ImportError:
        pass

PENDING: list[str] = []
HERO = 0                                          # 0 号座位永远是人类
MODE_CASH = "cash"                                # 现金局：筹码见底可以补筹码接着打
MODE_TOURNEY = "tournament"                       # 淘汰赛：筹码见底就出局，直到只剩一人

# 局内默认不显示座位风格：知道谁松谁紧等于白送读牌信息。
# 想调试就在启动时加 --show-styles 把风格标回来。
STYLE_VISIBLE = False


def bot_label(bot) -> str:
    """局内显示用的座位名：默认只有名字，不暴露风格。"""
    return f"{bot.name}·{bot.tag}" if STYLE_VISIBLE else bot.name


def style_reveal(bots: dict) -> None:
    """整场打完（退出 / 出冠军）才揭晓每个座位是什么风格。"""
    if STYLE_VISIBLE or not bots:
        return
    seats = " | ".join(f"{b.name} {b.tag}" for b in bots.values())
    print(f"\n-- 风格揭晓 -- {seats}")

HELP = [
    "f 弃牌(认输这局) | c 跟注/过牌 | r<总额> 加注到该总额(不是加多少) | a 全下",
    "底池=本局桌上已下的总筹码 | 需跟 0=可免费过牌 | 需跟 N=补 N 才能继续",
    "四轮：翻前(只有底牌) -> 翻牌(发3张公共) -> 转牌(第4张) -> 河牌(第5张) -> 比牌",
    "x 一键清屏，伪装成 PowerShell 提示符，按任意键恢复（AI 思考时也能用）",
    "局末自动亮出所有底牌 + 每个机器人的下注理由（--no-reasons 可关）；r 看带耗时/来源的详细复盘",
    "座位名后括号是位置：BTN按钮 SB小盲 BB大盲 UTG/MP/CO 靠前位；按钮每局顺时针轮",
    "淘汰赛是默认模式：筹码见底即出局并报出第 N 名，一直打到只剩一个人（--mode cash 换现金局）",
    "机器人各自的风格（松凶/跟注站…）局内不显示，退出时才揭晓；--show-styles 可照旧标出来",
    "局末 0 = 按当前配置重开一场（筹码归位，随机座位的风格重新抽签）| s 导出复盘 | q 退出",
]


def _getch() -> str:
    if PENDING:
        return PENDING.pop(0)
    if HAS_MSVCRT:
        return msvcrt.getwch()
    return sys.stdin.read(1)


def parse_line(raw: str, numeric: bool = False):
    raw = raw.strip().lower().replace(" ", "")
    if not raw:
        return "", ""
    m = re.match(r"^(加注到|加注|raise|r)(\d*)$", raw)
    if m:
        return "r", m.group(2) if numeric else ""
    alias = {
        "弃牌": "f", "弃": "f", "fold": "f",
        "过牌": "c", "过": "c", "check": "c", "跟注": "c", "跟": "c", "call": "c",
        "全下": "a", "全压": "a", "allin": "a", "all-in": "a",
        "下一局": "n", "复盘": "r", "导出": "s", "退出": "q", "quit": "q",
        "帮助": "?", "清屏": "x", "藏": "x",
        "重开": "0", "重来": "0", "重新开始": "0", "restart": "0", "reset": "0",
    }
    if raw in alias:
        return alias[raw], ""
    if raw[0] in "fcaxnrsq?h0":
        return raw[0], ""
    return "?", ""


def read_cmd(prompt: str, singles: str, numeric: bool = False):
    """Windows 控制台走单键即时响应；其它环境按行读取。返回 (命令, 数字参数)。"""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if not HAS_MSVCRT:
        line = sys.stdin.readline()
        if not line:
            return "q", ""
        if not sys.stdin.isatty():                  # 管道输入没有终端回显，补一个换行
            sys.stdout.write("\n")
        cmd, arg = parse_line(line, numeric)
        if cmd and cmd not in singles:
            return "", ""
        return cmd, arg

    ch = _getch()
    if ch in ("\x00", "\xe0"):
        _getch()
        return "", ""
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch in "\r\n":
        sys.stdout.write("\n")
        return "", ""
    ch = ch.lower()
    if ch in singles:
        sys.stdout.write(ch)
        arg = ""
        if numeric and ch == "r":
            buf = ""
            while True:
                c = _getch()
                if c in "\r\n":
                    break
                if c == "\x08":
                    if buf:
                        buf = buf[:-1]
                        sys.stdout.write("\b \b")
                    continue
                if c.isdigit():
                    buf += c
                    sys.stdout.write(c)
                sys.stdout.flush()
            arg = buf
        sys.stdout.write("\n")
        sys.stdout.flush()
        return ch, arg

    # 未知首字符：按行读完，兼容中文 / 长命令
    rest = ch
    while True:
        c = _getch()
        if c in "\r\n":
            break
        if c == "\x08":
            rest = rest[:-1]
            continue
        rest += c
    sys.stdout.write("\n")
    return parse_line(rest, numeric)


def panic(state_fn) -> None:
    """老板键：清屏 + 伪装提示符，按任意键恢复并重绘状态。"""
    os.system("cls" if os.name == "nt" else "clear")
    sys.stdout.write("\n" * 8 + r"PS C:\Users\PC> ")
    sys.stdout.flush()
    _getch()
    sys.stdout.write("\n")
    state_fn()


def decide_with_panic(bot: DeepSeekPlayer, snap: dict, state_fn):
    """AI 决策放后台线程，主线程轮询老板键，等待期间也能一键隐藏。"""
    box: dict = {}

    def work():
        try:
            box["result"] = bot.decide(snap)
        except Exception as e:                                  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    panicked = False
    while t.is_alive():
        t.join(0.08)
        if HAS_MSVCRT and msvcrt.kbhit():
            ch = msvcrt.getwch().lower()
            if ch == "x":
                panic(state_fn)
                panicked = True
            else:
                PENDING.append(ch)                             # 提前按的动作键不丢
    if "error" in box:
        raise box["error"]
    return box["result"], panicked


# ---------- 极简界面 ----------
def seat_line(hand: HoldemHand) -> str:
    parts = []
    for i, p in enumerate(hand.players):
        pos = position_name(i, hand.n, hand.button)
        tag = "·弃" if p.folded else ("·全下" if p.all_in else "")
        parts.append(f"{p.name}({pos}{tag}) {p.stack}")
    return "座位 " + " | ".join(parts)


def state_line(hand: HoldemHand) -> str:
    me = hand.players[HERO]
    legal = hand.legal_actions(HERO)
    return (f"#{hand.hand_no} {STREET_CN[hand.street]} | 底池 {hand.pot} | "
            f"{hand.n}人桌 | 按钮 {hand.players[hand.button].name} | 需跟 {legal['to_call']}\n"
            f"手牌 [{fmt_cards(me.hole)}]  公共 [{fmt_cards(hand.board, empty='未发')}]  "
            f"->  {read_hand(me.hole, hand.board)}\n"
            f"{seat_line(hand)}")


def reveal_lines(hand: HoldemHand) -> list[str]:
    """局末翻牌：所有人的底牌（含弃牌者）、牌型、赢家筹码。"""
    result = hand.result
    if result is None:
        return []
    out = ["-- 翻牌 --", "公共 " + fmt_cards(hand.board)]
    for i, p in enumerate(hand.players):
        if not p.hole:
            continue
        marks = []
        if p.folded:
            marks.append("已弃牌")
        if p.all_in:
            marks.append("全下")
        if i in result.winners:
            marks.append(f"★赢 {result.payouts[i]}")
        if not p.folded and i in result.hands:
            marks.append(describe(result.hands[i]))
        out.append(f"{p.name} [{fmt_cards(p.hole)}] " + " ".join(marks))
    return out


def reason_lines(bots: dict, hand_no: int) -> list[str]:
    """局末不用按键，直接列出每个机器人本局每一步的下注理由。"""
    out = ["-- 下注理由 --"]
    for idx in sorted(bots):
        bot = bots[idx]
        ds = [d for d in bot.hand_log if d["hand_no"] == hand_no]
        if not ds:
            out.append(f"{bot_label(bot)}：本局无需决策")
            continue
        # 按「每条决策的真实来源」标注，而不是整个座位一刀切：
        # 一次网络抖动降级后，本局其余步骤仍是在线模型做的。
        api_n = sum(1 for d in ds if d.get("valid"))
        if api_n == len(ds):
            src = bot.kind_label
        elif api_n:
            src = f"{bot.kind_label}·{len(ds) - api_n}步降级"
        else:
            src = "本地"
        out.append(f"{bot_label(bot)}（{src}）：")
        for d in ds:
            line = f"  {d['street_cn']} {d['action_text']} -> {d['reason'] or '(未给理由)'}"
            if bot.enabled and not d.get("valid"):
                line += " [降级本地]"
            out.append(line)
    return out


def link_line(bot, entry: dict) -> str:
    """首次在线模型成功响应时的一次性确认，避免「没跑 --check」时看不出连没连上。"""
    proto = "/v1/systemone" if bot.provider == "jev" else "/chat/completions"
    tail = "局末理由带概率/置信" if bot.provider == "jev" else "局末 r 可看理由"
    return (f"! 连通确认 · {bot.kind_label} {bot.model} 已响应 "
            f"({entry.get('latency_ms', 0)}ms) · {bot.name} 走在线模型 · {proto} · {tail}")


def action_prompt(hand: HoldemHand) -> str:
    legal = hand.legal_actions(HERO)
    parts = ["f弃", "c过牌" if legal["to_call"] == 0 else f"c跟{legal['call']}"]
    if legal["raise_to"]:
        lo, hi = legal["raise_to"]
        parts.append(f"r<额>加注{lo}-{hi}")
    parts += ["a全下", "x藏", "?帮助"]
    return state_line(hand) + "\n[ " + " / ".join(parts) + " ] > "


def drain(hand: HoldemHand, cursor: int) -> int:
    for line in hand.events[cursor:]:
        print(line)
    sys.stdout.flush()
    return len(hand.events)


def build_action(cmd: str, arg: str, legal: dict) -> Action:
    if cmd == "f":
        return Action("fold")
    if cmd == "c":
        return Action("call" if legal["to_call"] else "check")
    if cmd == "a":
        return Action("allin", legal["allin"])
    if cmd == "r":
        rng = legal["raise_to"]
        if not rng:
            raise ValueError("对手短码全下，当前只能跟注或弃牌")
        lo, hi = rng
        if arg:
            target = int(arg)
            if not lo <= target <= hi:
                raise ValueError(f"加注额需在 {lo}~{hi}（填加注到的总投入额，不是增量）")
        else:
            target = lo
        return Action("raise" if target < hi else "allin", target)
    raise ValueError("未知命令，按 ? 看帮助")


def banner(bots: dict, args, mem=None) -> None:
    sample = bots[min(bots)]
    host = urlparse(sample.base_url).netloc or safe_display(sample.base_url)
    online = sum(1 for b in bots.values() if b.enabled)
    status = (f"{sample.kind_label} {sample.model} @ {host} · {online}/{len(bots)} 座位在线"
              if online else "本地兜底 AI")
    mode_cn = ("淘汰赛（筹码见底即出局，打到只剩一人）" if args.mode == MODE_TOURNEY
               else "现金局（筹码见底可补筹码）")
    print(f"德州扑克 {len(bots) + 1} 人桌（你 + {len(bots)} 机器人） | "
          f"{mode_cn} | 盲注 {args.sb}/{args.bb} | 起始筹码 {args.stack}")
    if online:
        print(status)
    print("热键 f弃 c跟/过 r加注 a全下 x藏 ?帮助   |   局末自动翻牌 + 下注理由，按 0 重开一场")
    print(style_legend())
    seats = " | ".join(
        f"{bot_label(b)} {b.kind_label + '·' + b.model if b.enabled else '本地'}"
        for b in bots.values())
    print(f"座位 你(人类) | {seats}")
    if args.mode == MODE_TOURNEY and args.blind_up > 0:
        print(f"盲注每 {args.blind_up} 局翻倍（Lv1 {args.sb}/{args.bb}）")
    if getattr(args, "thinking", False):
        print("思考模式已开启：每次决策会先推理一会儿（更慢，但读牌更细）")
    notes = next((b.config_notes for b in bots.values() if getattr(b, "config_notes", None)), [])
    if notes:
        print("! 配置已自动纠正：" + "；".join(notes))
    disabled = next((b.disabled_reason for b in bots.values() if b.disabled_reason), "")
    if disabled:
        print(f"! {disabled} -> 已切本地 AI（换真实配置后重开）")
    elif not online:
        print("接入：TypeSafe Jev 只需 $env:JEV_API_KEY='apikey_...'；聊天网关再填 JEV_BASE_URL / JEV_MODEL")
    if mem is not None and mem.enabled:
        kept = f"，已带上次的 {len(mem)} 局" if len(mem) else ""
        print(f"记忆 机器人记住最近 {mem.window} 局的对手画像{kept}（--memory 0 可关，--forget 清零）")
    sys.stdout.flush()


def save_reviews(bots: dict) -> None:
    path = os.path.join(APP_DIR, "reviews.json")
    data = {"seats": [
        {"name": b.name, "tag": b.tag, "model": b.model,
         "stats": b.stats, "hands": b.logs}
        for b in bots.values()
    ]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    total = sum(len(b.logs) for b in bots.values())
    print(f"复盘已保存 {path}（{len(bots)} 个座位，共 {total} 局记录）")


def check_rebuy(players: list[Player], stack: int) -> bool:
    for p in players:
        if p.stack <= 0:
            print(f"{p.name} 筹码见底。")
            cmd, _ = read_cmd("[ b补充筹码继续 / q退出 ] > ", "bq")
            if cmd == "q":
                return False
            p.stack = stack
            print(f"{p.name} 补充到 {stack}")
    return True


def knockout(players: list[Player], active: list[int]) -> tuple[list[int], list[tuple[int, int]]]:
    """淘汰赛结算：筹码为 0 的座位出局。

    返回 (仍在桌上的座位号, [(出局座位号, 名次)])。同一手一起出局的人名次相同，
    名次 = 剩下的人 + 1（4 人桌第一个出局即第 4 名）。
    """
    busted = [s for s in active if players[s].stack <= 0]
    if not busted:
        return list(active), []
    left = [s for s in active if players[s].stack > 0]
    rank = len(left) + 1
    return left, [(s, rank) for s in busted]


def advance_button(active: list[int], seat: int, n_seats: int) -> int:
    """按钮顺时针移到下一个还在桌上的座位；按钮位本人被淘汰时继续顺延。"""
    if not active:
        return seat
    if len(active) == 1:
        return active[0]
    for step in range(1, n_seats + 1):
        cand = (seat + step) % n_seats
        if cand in active:
            return cand
    return active[0]


def parse_seat_map(spec: str | None, n_bots: int, label: str) -> dict[int, str]:
    """解析 `1=jev-latest,3=gpt-4o` 这种「座位号=值」的表，用于每座位模型/风格。"""
    if not spec:
        return {}
    out: dict[int, str] = {}
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"{label} 的格式是 座位号=值（如 1=松凶），收到 {chunk!r}")
        left, value = chunk.split("=", 1)
        try:
            seat = int(left)
        except ValueError as e:
            raise ValueError(f"{label} 里的座位号 {left!r} 不是数字") from e
        if not 1 <= seat <= n_bots:
            raise ValueError(f"{label} 里的座位 {seat} 不存在（本桌只有 1~{n_bots} 号机器人）")
        if not value:
            raise ValueError(f"{label} 里座位 {seat} 的值是空的")
        out[seat] = value
    return out


def assign_personas(style_map: dict[int, str], n_bots: int, picker) -> dict[int, dict]:
    """座位号 -> 这局实际上桌的风格（persona）。

    默认就是「随机」：没指定、或明确写了随机的座位，都从打乱后的风格池里抽，
    尽量一桌不重样（人比风格多时才会重）；只有写死了风格名的座位才固定下来。
    抽签用独立的随机源：别去动发牌用的 rng，否则同一个 --seed 的牌序会变。
    """
    pool = list(PERSONAS)
    picker.shuffle(pool)
    out: dict[int, dict] = {}
    for k in range(1, n_bots + 1):
        tag = style_map.get(k)
        if tag and tag != STYLE_RANDOM:
            out[k] = persona_for(tag) or PERSONAS[(k - 1) % len(PERSONAS)]
        else:
            out[k] = pool.pop() if pool else picker.choice(PERSONAS)
    return out


def reroll_random_styles(bots: dict, style_map: dict[int, str], picker) -> dict[int, str]:
    """重开一局时给「随机」的座位重新抽风格，写死风格的座位保持不动。

    刻意避开这个座位上一场用过的打法：否则重开一次看起来像什么都没变。
    返回 {座位号: 新风格短名}，给界面/输出用（局内仍然不显示风格）。
    """
    seats = [k for k in sorted(bots)
             if not style_map.get(k) or style_map.get(k) == STYLE_RANDOM]
    if not seats:
        return {}
    pool = list(PERSONAS)
    picker.shuffle(pool)
    changed: dict[int, str] = {}
    for k in seats:
        fresh = [p for p in pool if p["tag"] != bots[k].tag]
        persona = (fresh or pool or PERSONAS)[0]
        if persona in pool:
            pool.remove(persona)
        bots[k].apply_persona(persona)
        changed[k] = persona["tag"]
    return changed


def restart_table(players: list[Player], bots: dict, stack: int, n_bots: int) -> list[int]:
    """按当前配置重开一场：筹码归位、所有座位回桌、机器人复盘清零。

    只重置「这一场」的状态。跨手记忆（OpponentMemory）故意保留——
    那是它对你的长期认知，重开一局不该把你变回陌生人。
    """
    for p in players:
        p.stack = stack
        p.reset_hand()
    for b in bots.values():
        b.logs.clear()
        b.hand_log.clear()
        b.stats = {"api": 0, "fallback": 0, "last_error": ""}
        b.warned = False
    return list(range(n_bots + 1))


def blind_level(args, hand_no: int) -> tuple[int, int]:
    """锦标赛按局数涨盲：--blind-up N 表示每 N 局盲注翻倍（0 = 不涨）。"""
    step = getattr(args, "blind_up", 0) or 0
    if step <= 0:
        return args.sb, args.bb
    level = 1 + (hand_no - 1) // step
    return args.sb * 2 ** (level - 1), args.bb * 2 ** (level - 1)


def parse_args():
    ap = argparse.ArgumentParser(description="命令行德州扑克 · 你 + 多个 AI 机器人同桌（单机）")
    ap.add_argument("--stack", type=int, default=1000, help="起始筹码，默认 1000")
    ap.add_argument("--sb", type=int, default=10, help="小盲，默认 10")
    ap.add_argument("--bb", type=int, default=20, help="大盲，默认 20")
    ap.add_argument("--model", default=None,
                    help="模型名，默认 deepseek-flash（DeepSeek 官方现行模型；旧名 deepseek-chat 已于 2026-07-24 下线）")
    ap.add_argument("--base-url", default=None, help="API 地址，默认 https://api.deepseek.com")
    ap.add_argument("--timeout", type=float, default=None, help="单次请求超时秒数，默认 30")
    ap.add_argument("--offline", action="store_true", help="强制本地兜底 AI，不联网")
    ap.add_argument("--api-key", default=None, help="API key（建议改用环境变量，避免留在命令历史里）")
    ap.add_argument("--check", action="store_true", help="只做一次 API 连通性自检然后退出")
    ap.add_argument("--cards", choices=CARD_STYLES, default="cn",
                    help="牌面样式：cn 中文(默认，如 方9 红A) / ascii 英文缩写(9d) / pretty 花色符号(9♦)")
    ap.add_argument("--pretty", action="store_true", help="兼容旧参数，等价于 --cards pretty")
    ap.add_argument("--sims", type=int, default=140, help="兜底 AI 蒙特卡洛次数，默认 140")
    ap.add_argument("--bluff", type=float, default=None,
                    help="本地 AI 的诈唬强度倍率，默认 1.0（也可用 POKER_BLUFF 环境变量）；"
                         "0.5 更老实，2 更疯——基数由座位风格决定，跟注站即使 2 也很少偷")
    ap.add_argument("--seed", type=int, default=None, help="固定随机种子，便于复现")
    ap.add_argument("--trust-config", action="store_true",
                    help="跳过占位符检测（确认 key/端点/模型都是真实值时使用）")
    ap.add_argument("--bots", type=int, default=5,
                    help="机器人座位数 1~5，默认 5（你 + 5 机器人 = 6 人桌）；--bots 1 回到单挑")
    ap.add_argument("--llm-bots", type=int, default=0,
                    help="其中几个座位走在线模型，默认 0（全部本地 AI，不联网不花钱）；"
                         "设成 --bots 的值可让全部机器人走在线模型")
    ap.add_argument("--llm-seats", default=None,
                    help="指定哪些座位走在线模型，逗号分隔，如 1,3,5；优先于 --llm-bots。"
                         "留空字符串或一个都不填 = 全部座位本地 AI")
    ap.add_argument("--provider", choices=("auto", "openai", "jev"), default="auto",
                    help="协议：auto 自动识别(默认) / openai 聊天模型(chat/completions) / "
                         "jev TypeSafe 决策模型(/v1/systemone)")
    ap.add_argument("--no-reasons", action="store_true",
                    help="局末不自动打印下注理由（翻牌照常显示），需要时按 r 看详细复盘")
    ap.add_argument("--models", action="store_true",
                    help="列出 base_url 上可用的 model id 后退出（不知道模型名时先跑这个）")
    ap.add_argument("--thinking", action="store_true",
                    help="让聊天模型先思考再出牌（DeepSeek V4 默认是开着的，本程序默认关掉："
                         "思考会把输出预算吃光、每次决策也慢几秒；想读牌更细再打开）")
    ap.add_argument("--memory", type=int, default=WINDOW_DEFAULT, metavar="N",
                    help=f"跨手记忆窗口：机器人记住最近 N 局的对手画像，默认 {WINDOW_DEFAULT}；"
                         "0 = 关闭（不记不存，机器人对每一局都失忆）")
    ap.add_argument("--forget", action="store_true",
                    help="清空已保存的对手画像后开始（默认会接着上次的窗口继续记）")
    ap.add_argument("--mode", choices=(MODE_CASH, MODE_TOURNEY), default=MODE_TOURNEY,
                    help="tournament 淘汰赛(默认)：筹码见底的座位直接出局，一直打到只剩一个人；"
                         "cash 现金局：筹码见底可以补筹码接着打")
    ap.add_argument("--blind-up", type=int, default=0, metavar="N",
                    help="每 N 局盲注翻倍，默认 0 = 不涨（淘汰赛建议 8~12 局，否则短码磨很久；"
                         "启动器里默认就是 10）")
    ap.add_argument("--seat-styles", default=None,
                    help="给指定座位单独指定风格，逗号分隔，如 1=紧凶,3=松凶；"
                         f"可选：{STYLE_RANDOM} / {'/'.join(p['tag'] for p in PERSONAS)}"
                         f"（{STYLE_RANDOM} = 开局随机抽一个，一桌尽量不重样）")
    ap.add_argument("--show-styles", action="store_true",
                    help="局内把每个座位的风格标出来（默认隐藏：知道谁松谁紧等于白送读牌）")
    ap.add_argument("--seat-models", default=None,
                    help="给指定座位单独指定模型，逗号分隔，如 1=jev-latest,3=gpt-4o-mini；"
                         "没写的座位沿用全局模型")
    args = ap.parse_args()
    if args.bluff is None:
        try:
            args.bluff = float(os.environ.get("POKER_BLUFF") or 1.0)
        except ValueError:
            args.bluff = 1.0
    args.bluff = max(0.0, min(4.0, args.bluff))
    return args


def resolve_online_seats(spec: str | None, llm_bots: int, n_bots: int) -> set[int]:
    """把 --llm-seats / --llm-bots 解析成「哪些机器人座位走在线模型」。"""
    if spec is None:
        return set(range(1, llm_bots + 1))
    try:
        seats = {int(x) for x in spec.replace(" ", "").split(",") if x}
    except ValueError as e:
        raise ValueError("--llm-seats 只接受逗号分隔的座位号，如 1,3,5") from e
    bad = sorted(seats - set(range(1, n_bots + 1)))
    if bad:
        raise ValueError(f"座位 {','.join(map(str, bad))} 不存在（本桌只有 1~{n_bots} 号机器人）")
    return seats


def main() -> int:
    global STYLE_VISIBLE
    set_console_title()
    args = parse_args()
    STYLE_VISIBLE = args.show_styles
    set_card_style("pretty" if args.pretty else args.cards)
    if not 1 <= args.bots <= 5:
        print("配置错误：--bots 需在 1~5 之间（最多 6 人桌）")
        return 2
    if not 0 <= args.llm_bots <= args.bots:
        print(f"配置错误：--llm-bots 需在 0~{args.bots} 之间")
        return 2
    if args.blind_up < 0:
        print("配置错误：--blind-up 不能是负数（0 = 不涨盲）")
        return 2
    try:
        online_seats = resolve_online_seats(args.llm_seats, args.llm_bots, args.bots)
        style_map = parse_seat_map(args.seat_styles, args.bots, "--seat-styles")
        model_map = parse_seat_map(args.seat_models, args.bots, "--seat-models")
        for tag in style_map.values():
            if tag != STYLE_RANDOM:
                persona_for(tag)                # 风格名打错就在启动时失败，别等开局才崩
    except ValueError as e:
        print(f"配置错误：{e}")
        return 2
    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    if not 0 <= args.memory <= 200:
        print("配置错误：--memory 需在 0~200 之间（0 = 关闭跨手记忆）")
        return 2
    mem = OpponentMemory(args.memory, os.path.join(APP_DIR, "poker-memory.json"))
    if args.forget:
        mem.reset()

    players = ([Player("你", args.stack)] +
               [Player(f"机器{k}", args.stack) for k in range(1, args.bots + 1)])
    bots: dict[int, DeepSeekPlayer] = {}       # 键是座位号（1~N），淘汰出局后也不变
    # 抽「随机」风格用独立随机源，避免扰动发牌序列（同 seed 仍可复现同一局）
    style_rng = (random.Random(args.seed * 31 + 7) if args.seed is not None
                 else random.Random())
    personas = assign_personas(style_map, args.bots, style_rng)
    try:
        for k in range(1, args.bots + 1):
            # 指定了风格就用它，没写的按座位轮换，保证一桌人的打法有区分度
            persona = personas[k]
            # 每个座位独立的随机源，避免多个机器人走出完全一样的决策序列
            sub_rng = (random.Random(args.seed * 1000 + k)
                       if args.seed is not None else random.Random())
            b = DeepSeekPlayer(api_key=args.api_key,
                               model=model_map.get(k) or args.model,
                               base_url=args.base_url,
                               timeout=args.timeout, sims=args.sims, rng=sub_rng,
                               offline=args.offline or k not in online_seats,
                               trust_config=args.trust_config,
                               persona=persona["style"], aggression=persona["agg"],
                               tag=persona["tag"], provider=args.provider, memory=mem,
                               thinking=getattr(args, "thinking", False),
                               bluff_scale=getattr(args, "bluff", 1.0))
            b.name = players[k].name
            bots[k] = b
    except ValueError as e:
        print(f"配置错误：{e}")
        return 2

    if args.models:
        print(bots[min(bots)].list_models())
        return 0
    if args.check:
        probe = next((b for b in bots.values() if b.enabled), bots[1])
        print(probe.check_connection())
        return 0
    banner(bots, args, mem)

    hand_no = 1
    n_seats = len(players)
    active = list(range(n_seats))              # 还坐在桌上的座位号（淘汰赛会缩短）
    button_seat = 0                            # 用座位号记按钮，删人后仍然对得上
    level_seen = 1
    linked = False
    tourney_over = False
    try:
        while True:
            if args.mode == MODE_CASH and not check_rebuy(players, args.stack):
                break
            sb, bb = blind_level(args, hand_no)
            if args.blind_up > 0:
                level = 1 + (hand_no - 1) // args.blind_up
                if level != level_seen:
                    print(f"! 盲注升级到 Lv{level}：{sb}/{bb}")
                    level_seen = level
            hand_seats = list(active)
            hand = HoldemHand([players[s] for s in hand_seats],
                              hand_seats.index(button_seat), sb, bb, rng, hand_no)
            for b in bots.values():
                b.start_hand(hand_no)
            cursor = drain(hand, 0)

            while not hand.finished:
                pos = hand.next_actor()
                if pos is None:
                    hand.advance()
                    cursor = drain(hand, cursor)
                    continue
                idx = hand_seats[pos]
                if idx == HERO:
                    cmd, arg = read_cmd(action_prompt(hand), "fcaxr?h0", numeric=True)
                    if cmd == "x":
                        panic(lambda: print(state_line(hand)))
                        continue
                    if cmd in ("?", "h"):
                        print("\n".join(HELP))
                        print("-- 当前这手牌 --")
                        print("\n".join(explain_cards(hand.players[HERO].hole, hand.board)))
                        print("速读：" + read_hand(hand.players[HERO].hole, hand.board))
                        continue
                    if cmd == "q":
                        style_reveal(bots)
                        return 0
                    if cmd == "0":
                        print("  ! 手牌还在打，重开要等这一手结束（局末按 0）")
                        continue
                    try:
                        action = build_action(cmd, arg, hand.legal_actions(HERO))
                    except ValueError as e:
                        print(f"  ! {e}")
                        continue
                else:
                    bot = bots[idx]
                    snap = hand.snapshot(pos)
                    (action, entry), _panicked = decide_with_panic(
                        bot, snap, lambda h=hand: print(state_line(h)))
                    if not linked and bot.enabled and entry.get("valid"):
                        print(link_line(bot, entry))
                        linked = True
                    if bot.enabled and not entry.get("valid", False) and not bot.warned:
                        note = entry.get("fallback_note", "无响应")
                        print(f"! {bot.name} 的 {bot.kind_label} 未响应（{note}），该座位已切本地 AI")
                        bot.warned = True
                hand.apply_action(pos, action)
                cursor = drain(hand, cursor)

            for b in bots.values():
                b.end_hand(hand_no)
            mem.record(hand)
            print(f"-- 第 {hand_no} 局结束 --")
            print("\n".join(reveal_lines(hand)))

            # 淘汰赛：这一手打完谁筹码归零谁出局，直到只剩一个人
            if args.mode == MODE_TOURNEY:
                active, gone = knockout(players, active)
                for seat, rank in gone:
                    who = "你" if seat == HERO else players[seat].name
                    print(f"! {who} 筹码见底，出局（第 {rank} 名）")
                if len(active) == 1:
                    print(f"★ 锦标赛结束 · 冠军是 {players[active[0]].name}")
                    tourney_over = True
                elif HERO not in active:
                    print("你已被淘汰，本场到此为止（0 重开一局 / r 复盘 / s 导出 / q 退出）")
                    tourney_over = True

            if not args.no_reasons:
                # 只列还在桌上的座位：出局的机器人这手没决策，不必刷「本局无需决策」
                print("\n".join(reason_lines(
                    {s: bots[s] for s in active if s in bots}, hand_no)))
            seen = mem.summary_line([p.name for p in players])
            if seen:
                print(seen)
            while True:
                restart = False
                if tourney_over:
                    cmd, _ = read_cmd("[ 0重开一局 / r详细复盘 / s导出 / q退出 ] > ", "0rsq")
                else:
                    cmd, _ = read_cmd("[ n下一局 / 0重开一局 / r详细复盘 / s导出 / q退出 ] > ",
                                      "n0rsq")
                if cmd == "n":
                    break
                if cmd == "0":
                    restart = True
                    break
                if cmd == "q":
                    style_reveal(bots)
                    return 0
                if cmd == "r":
                    lines = []
                    for k in sorted(bots):
                        b = bots[k]
                        lines += b.review_lines(hand_no, label=bot_label(b))
                    print("\n".join(lines))
                elif cmd == "s":
                    save_reviews(bots)
                elif cmd == "":
                    continue
                else:
                    print("  ! 只支持 " + ("0 / r / s / q" if tourney_over
                                          else "n / 0 / r / s / q"))
            if restart:
                active = restart_table(players, bots, args.stack, args.bots)
                changed = reroll_random_styles(bots, style_map, style_rng)
                hand_no = 1
                button_seat = 0
                level_seen = 1
                tourney_over = False
                tail = f"，{len(changed)} 个随机座位重新抽了风格" if changed else ""
                print(f"\n-- 已按当前配置重开：筹码回到 {args.stack}，从第 1 局开始{tail} --")
                continue
            if tourney_over:
                break
            hand_no += 1
            button_seat = advance_button(active, button_seat, n_seats)
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。")
    style_reveal(bots)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
