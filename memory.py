"""跨手记忆：把每一局的「公开信息」压成对手画像。

为什么单独一层：Jev 每次请求都失忆（官方明确：state 随请求传入，无跨请求记忆，
也不会用你的数据训练）。想让它「记得你」，只能由我们维护一份压缩画像，塞进 state。

同一份数据同时供两个决策后端使用：
  - Jev：`jev.build_state()` 把画像写进每个对手的 `reads` 字段，模型据此判断；
  - 本地兜底：`read_adjust()` 把画像变成跟注门槛 / 诈唬倾向的偏移量。

窗口是滚动的（默认最近 10 局）：只保留最近 N 局，越老的自然淘汰。
体积很小——每局每个玩家约 60 字节，10 局 6 人桌也就 3~4 KB，落盘无压力。
"""
from __future__ import annotations

import json
import os

WINDOW_DEFAULT = 10
MIN_HANDS_FOR_STYLE = 3      # 少于这么多局不下「松/紧」结论，避免一手牌就贴标签

# 动作轨迹缩写：R 加注/下注、C 跟注、K 过牌、F 弃牌
_SEQ_CHAR = {"raise": "R", "allin": "R", "call": "C", "check": "K", "fold": "F"}


class OpponentMemory:
    """最近 N 局的对手画像。window=0 等于关闭（不记、不读、不写盘）。"""

    def __init__(self, window: int = WINDOW_DEFAULT, path: str | None = None):
        self.window = max(0, int(window))
        self.path = path
        self.hands: list[dict] = []        # 每局一项：{玩家名: 单局统计}
        if self.window:
            self.load()

    # ---------- 基础 ----------
    @property
    def enabled(self) -> bool:
        return self.window > 0

    def __len__(self) -> int:
        return len(self.hands)

    def reset(self) -> None:
        self.hands.clear()
        self.save()

    # ---------- 记录 ----------
    def record(self, hand) -> bool:
        """把一局压成统计存进窗口。局必须已结束（有 hand.result）。"""
        if not self.enabled or hand.result is None:
            return False
        rec = {p.name: hand_stats(hand, i) for i, p in enumerate(hand.players)}
        self.hands.append(rec)
        del self.hands[:-self.window]      # 只留最近 N 局
        self.save()
        return True

    # ---------- 查询 ----------
    def profile(self, name: str) -> dict | None:
        rows = [rec[name] for rec in self.hands if name in rec]
        if not rows:
            return None
        return aggregate(rows)

    def reads_for(self, names) -> dict:
        """给决策用的画像集合；没数据的人不放进去，省 token。"""
        out = {}
        for name in names:
            prof = self.profile(name)
            if prof:
                out[name] = prof
        return out

    def describe(self, name: str) -> str:
        prof = self.profile(name)
        if not prof:
            return f"{name} 无记录"
        bits = [f"{prof['hands']}局", prof["style"]]
        bits.append(f"{prof['vpip']:.0%}/{prof['pfr']:.0%}")
        if prof["showdown"]:
            bits.append(f"摊牌{prof['showdown']}次")
        if prof["net"]:
            bits.append(f"net{prof['net']:+d}")
        return f"{name} " + " ".join(bits)

    def summary_line(self, names, limit: int = 4) -> str:
        """局末一行认知摘要；只列有记录的玩家，最多 limit 个。"""
        if not self.enabled:
            return ""
        parts = [self.describe(n) for n in names if self.profile(n)]
        if not parts:
            return ""
        more = "" if len(parts) <= limit else f" …+{len(parts) - limit}"
        tag = f"记忆(最近{self.window}局)"
        return f"{tag} " + " · ".join(parts[:limit]) + more

    # ---------- 落盘 ----------
    def load(self) -> None:
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            self.hands = []                # 存档坏了就当没有，别影响开局
            return
        hands = data.get("hands") if isinstance(data, dict) else None
        if isinstance(hands, list):
            self.hands = [h for h in hands if isinstance(h, dict)][-self.window:]

    def save(self) -> None:
        if not self.path or not self.enabled:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "window": self.window, "hands": self.hands},
                          f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass                            # 只读目录等情况下静默放弃，不影响对局


# ---------- 单局统计 ----------
def hand_stats(hand, idx: int) -> dict:
    """从一局的动作历史里提取某个玩家的画像素材（全部是公开信息）。"""
    player = hand.players[idx]
    st = {"vpip": False, "pfr": False, "aggr": 0, "calls": 0, "folds": 0,
          "faced": 0, "folded_to_bet": 0, "showdown": False, "won": False,
          "net": 0, "seq": ""}
    agg_per_street: dict[str, int] = {}
    seq: list[str] = []

    for h in hand.history:
        street = h.get("street")
        kind = h.get("kind", "")
        aggressive = kind in ("raise", "allin")
        if h.get("idx") == idx:
            if street == "preflop":
                if kind in ("call", "raise", "allin"):
                    st["vpip"] = True          # 盲注不算主动投入
                if aggressive:
                    st["pfr"] = True
            if aggressive:
                st["aggr"] += 1
            elif kind == "call":
                st["calls"] += 1
            elif kind == "fold":
                st["folds"] += 1
            # 本街此前已有人下注/加注，才算「面对下注」
            if kind in ("fold", "call") and agg_per_street.get(street, 0):
                st["faced"] += 1
                if kind == "fold":
                    st["folded_to_bet"] += 1
            if kind in _SEQ_CHAR:
                seq.append(_SEQ_CHAR[kind])
        if aggressive:
            agg_per_street[street] = agg_per_street.get(street, 0) + 1

    result = hand.result
    if result is not None:
        st["showdown"] = idx in (result.hands or {})
        st["won"] = idx in (result.winners or [])
        st["net"] = (result.payouts or {}).get(idx, 0) - player.total_bet
    st["seq"] = "".join(seq)[:8]
    return st


def aggregate(rows: list[dict]) -> dict:
    """把窗口内的多局统计合成一份画像。"""
    n = len(rows)
    aggr = sum(r.get("aggr", 0) for r in rows)
    calls = sum(r.get("calls", 0) for r in rows)
    faced = sum(r.get("faced", 0) for r in rows)
    folded = sum(r.get("folded_to_bet", 0) for r in rows)
    vpip = sum(1 for r in rows if r.get("vpip")) / n
    pfr = sum(1 for r in rows if r.get("pfr")) / n
    fold_to_bet = round(folded / faced, 2) if faced else None
    return {
        "hands": n,
        "vpip": round(vpip, 2),
        "pfr": round(pfr, 2),
        # 侵略指数：主动下注/加注 与 跟注 的比值，+1 是拉普拉斯平滑
        "af": round((aggr + 1) / (calls + 1), 2),
        "fold_to_bet": fold_to_bet,
        "showdown": sum(1 for r in rows if r.get("showdown")),
        "won": sum(1 for r in rows if r.get("won")),
        "net": sum(r.get("net", 0) for r in rows),
        "style": style_label(vpip, pfr, n),
        "recent": [r.get("seq", "") for r in rows[-2:] if r.get("seq")],
    }


def style_label(vpip: float, pfr: float, hands: int) -> str:
    """松/紧 × 凶/被动，只用于给人看的提示和给模型的一句话先验。"""
    if hands < MIN_HANDS_FOR_STYLE:
        return "样本少"
    loose = vpip >= 0.42
    tight = vpip <= 0.26
    aggressive = pfr >= 0.20
    passive = pfr < 0.10
    if not loose and not tight:
        return "均衡偏凶" if aggressive else "均衡偏被动" if passive else "均衡"
    if aggressive:
        return "松凶" if loose else "紧凶"
    if passive:
        return "松被动" if loose else "紧被动"
    return "松" if loose else "紧"


def read_adjust(reads: dict, opponents: list[str] | None = None):
    """把画像变成兜底 AI 的两个偏移量：(跟注门槛偏移, 诈唬倾向偏移, 读牌说明)。

    门槛为正 = 更谨慎（弃得更早）；诈唬为正 = 更愿意偷池。
    读牌说明会拼进决策理由，方便复盘时看出「它为什么突然变凶」。
    """
    if not reads:
        return 0.0, 0.0, ""
    rows = [reads[n] for n in (opponents or reads) if n in reads]
    rows = [r for r in rows if r.get("hands", 0) >= MIN_HANDS_FOR_STYLE]
    if not rows:
        return 0.0, 0.0, ""
    vpip = sum(r["vpip"] for r in rows) / len(rows)
    pfr = sum(r["pfr"] for r in rows) / len(rows)
    fold_to_bet = [r["fold_to_bet"] for r in rows if r.get("fold_to_bet") is not None]
    ftb = sum(fold_to_bet) / len(fold_to_bet) if fold_to_bet else None

    margin = 0.0
    notes = []
    if vpip <= 0.26:                       # 对手紧：他们的下注更可能是真牌，尊重
        margin += 0.04
        notes.append("对手偏紧")
    elif vpip >= 0.42:                     # 对手松：范围宽，我们的跟注门槛可以降
        margin -= 0.05
        notes.append("对手偏松")
    bluff = 0.0
    if ftb is not None and ftb >= 0.55:    # 爱弃牌 → 多偷
        bluff += 0.12
        notes.append("对手爱弃")
    elif ftb is not None and ftb <= 0.30:  # 跟注站 → 少偷多价值
        bluff -= 0.15
        notes.append("对手爱跟")
    if pfr >= 0.30:                        # 极度爱加注：别轻易给免费牌，也别无脑跟
        margin += 0.02
        notes.append("对手爱加注")
    return round(margin, 3), round(bluff, 3), ("读牌 " + "/".join(notes) if notes else "")
