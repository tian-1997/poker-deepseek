"""TypeSafe Jev（System One 决策模型）适配器。

Jev 不是 LLM：它不生成文本，只把 state + typed questions 变成结构化 answers。
官方协议：POST https://api.typesafe.ai/v1/systemone
    {"model":"jev-latest","state":{...},"questions":{...}}
    -> {"model":"jev-1.14.0","answers":{...},"usage":{...}}

因此这里：
  - choice 选动作（fold/check/call/raise/allin），选项只放当前合法动作；
  - score 选下注尺度「档位」，具体金额由代码算（官方明确要求：算术留在代码里）；
  - probabilities / confidence 直接当作「下注理由」，比让模型编一段理由更可解释。
"""
from __future__ import annotations

import json
import urllib.request

from engine import Action

JEV_DEFAULT_BASE = "https://api.typesafe.ai/v1"
JEV_DEFAULT_MODEL = "jev-latest"

ACTION_CN = {"fold": "弃牌", "check": "过牌", "call": "跟注",
             "raise": "加注", "allin": "全下"}
SIZE_CN = ["最小加注", "半池", "一个底池", "重注", "全下"]
SIZE_INSTRUCTIONS = (
    "If this player raises, how large should the TOTAL bet for this round be, "
    "relative to the pot? Only judge the appropriate size tier; the exact chip "
    "amount will be computed in code."
)


def resolve_url(base_url: str) -> str:
    """无论 base_url 填 https://api.typesafe.ai 还是 .../v1，都落到 /v1/systemone。"""
    root = (base_url or JEV_DEFAULT_BASE).rstrip("/")
    for suffix in ("/chat/completions", "/systemone"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
    if not root.endswith("/v1"):
        root += "/v1"
    return root + "/systemone"


def legal_choices(legal: dict) -> list[str]:
    opts = ["fold", "check" if legal["to_call"] == 0 else "call"]
    if legal["raise_to"]:
        opts += ["raise", "allin"]
    return opts


READS_NOTE = (
    "Each opponent may carry `reads`: stats over their last few hands. "
    "vpip = share of hands where they voluntarily put chips in preflop; pfr = raised preflop; "
    "af = (bets+raises)/calls; fold_to_bet = how often they fold when facing a bet; "
    "style is a coarse label; recent is their last action sequences (R raise, C call, K check, F fold). "
    "Use it to widen or narrow your calling range, but do not over-trust small samples."
)


def build_state(snap: dict, persona: str, name: str, reads: dict | None = None) -> dict:
    """state 用 JSON 对象；牌用标准 9d 记法，只放与这次决策相关的信息。

    reads 是 memory.py 维护的跨手画像：Jev 本身无记忆，只能靠这个字段「记得」对手。
    """
    legal = snap["legal"]
    reads = reads or {}
    opponents = []
    used = False
    for o in snap["opponents"]:
        item = {"name": o["name"], "stack": o["stack"], "bet_this_round": o["round_bet"],
                "all_in": o["all_in"], "folded": o["folded"]}
        prof = reads.get(o["name"])
        if prof:
            item["reads"] = prof
            used = True
        opponents.append(item)
    state = {
        "game": "No-limit Texas Hold'em poker",
        "hand_number": snap["hand_no"],
        "street": snap.get("street_cn", ""),
        "table_size": snap.get("n_players", 2),
        "seat": name,
        "position": snap["position"],
        "playing_style": persona,
        "my_hole_cards": [c.code for c in snap["hole"]],
        "community_cards": [c.code for c in snap["board"]],
        "pot": legal["pot"],
        "current_bet_this_round": legal["current_bet"],
        "my_bet_this_round": snap["my_round_bet"],
        "chips_to_call": legal["to_call"],
        "my_remaining_stack": snap["my_stack"],
        "opponents": opponents,
        "action_history_this_hand": snap["history"][-14:],
        "legal_actions": legal_choices(legal),
        "raise_total_range": list(legal["raise_to"]) if legal["raise_to"] else None,
    }
    if used:
        state["reads_note"] = READS_NOTE
    return state


def build_questions(legal: dict) -> dict:
    """一次请求并行问两题：动作 + 尺度。选项描述写清边界，避免模型选非法动作。"""
    descriptions = {
        "fold": "Give up the hand now and forfeit any claim on the pot",
        "check": "Stay in the hand without adding chips; only legal when nothing is owed",
        "call": "Match the current bet exactly to stay in the hand",
        "raise": "Increase the bet to a chosen total amount for value or as a bluff",
        "allin": "Commit every remaining chip this round",
    }
    opts = legal_choices(legal)
    questions = {
        "action": {
            "type": "choice",
            "instructions": (
                "You are this poker player at this exact decision point. "
                "Given your cards, the board, the pot odds, your table position, "
                "the number of opponents still in the hand and your playing style, "
                "which single legal action maximizes your expected value right now? "
                "Consider that more opponents in the pot demand stronger hands."
            ),
            "criteria": {k: descriptions[k] for k in opts},
        }
    }
    if legal["raise_to"]:
        questions["sizing"] = {
            "type": "score",
            "instructions": SIZE_INSTRUCTIONS,
            "criteria": [
                "Minimum legal raise only",
                "About half the pot",
                "About one pot",
                "A large overbet of roughly two pots",
                "All remaining chips",
            ],
        }
    return questions


def call(base_url: str, api_key: str, model: str, state: dict,
         questions: dict, timeout: float = 30.0) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        headers["Authorization"].encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError(
            "API key 含中文/非 ASCII 字符，HTTP 请求头无法编码；请换成真实 key") from None
    body = {"model": model, "state": state, "questions": questions}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(resolve_url(base_url), data=data,
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_answers(resp: dict, legal: dict):
    """把 Jev 的 answers 变成 (Action, 中文理由)；无法解析时返回 None 交给本地兜底。"""
    if not isinstance(resp, dict):
        return None
    answers = resp.get("answers") or {}
    a = answers.get("action") or {}
    kind = str(a.get("choice") or "").strip().lower()
    if kind not in legal_choices(legal):
        return None

    probs = a.get("probabilities") or {}
    conf = a.get("confidence")
    try:
        p = float(probs.get(kind)) if probs.get(kind) is not None else None
    except (TypeError, ValueError):
        p = None
    try:
        conf_f = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf_f = None

    bits = [f"Jev 选 {ACTION_CN[kind]}"]
    if p is not None:
        bits.append(f"概率 {p:.0%}")
    if conf_f is not None:
        bits.append(f"置信 {conf_f:.2f}")
    ranked = []
    for k, v in probs.items():
        try:
            ranked.append((float(v), str(k)))
        except (TypeError, ValueError):
            continue
    ranked.sort(reverse=True)
    alt = next(((v, k) for v, k in ranked if k != kind), None)   # 概率次高的另一个选项
    if alt:
        bits.append(f"次选 {ACTION_CN.get(alt[1], alt[1])} {alt[0]:.0%}")
    if conf_f is not None and conf_f < 0.5:
        bits.append("低置信")

    amount = 0
    if kind in ("raise", "allin"):
        rng = legal["raise_to"]
        if not rng:
            return None
        lo, hi = rng
        raw = (answers.get("sizing") or {}).get("score")
        try:
            level = max(0, min(4, int(round(float(raw))))) if raw is not None else 2
        except (TypeError, ValueError):
            level = 2
        pot = legal["pot"]
        cb = legal["current_bet"]
        steps = [lo, cb + max(1, pot // 2), cb + max(1, pot),
                 cb + max(1, pot * 2), hi]
        if kind == "allin":
            amount, level = hi, 4
        else:
            amount = max(lo, min(hi, steps[level]))
            if amount >= hi:
                kind, level = "allin", 4
        bits.append(f"尺度 {SIZE_CN[level]}")

    return Action(kind, amount), " · ".join(bits)
