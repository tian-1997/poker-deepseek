"""牌、牌堆与七选五牌力评估。纯标准库，零依赖。"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

RANK_CHARS = "23456789TJQKA"          # rank 2..14
SUIT_CHARS = "shdc"                   # spade heart diamond club
SUIT_SYMBOLS = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
SUIT_CN = {"s": "黑桃", "h": "红桃", "d": "方块", "c": "梅花"}
SUIT_ABBR = {"s": "黑", "h": "红", "d": "方", "c": "梅"}
CATEGORY_CN = ["高牌", "一对", "两对", "三条", "顺子", "同花", "葫芦", "四条", "同花顺"]
CARD_STYLES = ("cn", "ascii", "pretty")

# 界面牌面风格。默认中文：不依赖终端字体，PowerShell 5.1 也不乱码。
# 注意：Card.__str__ 始终给 AI / 复盘用标准 code（如 9d），不受界面风格影响。
_style = "cn"


def set_card_style(style: str = "cn") -> None:
    global _style
    if style not in CARD_STYLES:
        raise ValueError(f"unknown card style: {style}")
    _style = style


def card_style() -> str:
    return _style


def use_ascii(flag: bool = True) -> None:
    """兼容旧接口：True=英文缩写，False=花色符号。"""
    set_card_style("ascii" if flag else "pretty")


def rank_text(rank: int) -> str:
    if rank == 10:
        return "10"
    return {11: "J", 12: "Q", 13: "K", 14: "A"}.get(rank, str(rank))


def fmt_card(c: "Card") -> str:
    """给「人」看的牌面：方9 / 9d / 9♦。"""
    if _style == "ascii":
        return c.code
    if _style == "pretty":
        return rank_text(c.rank) + SUIT_SYMBOLS[c.suit]
    return SUIT_ABBR[c.suit] + rank_text(c.rank)


def fmt_cards(cards, sep: str = " ", empty: str = "--") -> str:
    return sep.join(fmt_card(c) for c in cards) if cards else empty


def style_legend() -> str:
    """一行牌面读法对照，启动时和帮助里都能看。"""
    if _style == "ascii":
        return ("牌面 9d=方块9 9h=红桃9 As=黑桃A 2c=梅花2 | "
                "s黑桃 h红桃 d方块 c梅花 | T=10 J/Q/K/A依次变大")
    if _style == "pretty":
        return "牌面 9♦=方块9 | ♠黑桃 ♥红桃 ♦方块 ♣梅花 | 2~10 < J < Q < K < A(最大)"
    return ("牌面 方9=方块9 红9=红桃9 黑A=黑桃A 梅K=梅花K | "
            "黑=黑桃 红=红桃 方=方块 梅=梅花 | 2~10 < J < Q < K < A(最大)")


@dataclass(frozen=True)
class Card:
    rank: int                          # 2..14，14 = A
    suit: str                          # s/h/d/c

    def __str__(self) -> str:
        if _style == "pretty":
            return rank_text(self.rank) + SUIT_SYMBOLS[self.suit]
        return self.code                          # AI prompt / 赛场标准记法

    @property
    def code(self) -> str:
        return RANK_CHARS[self.rank - 2] + self.suit

    @staticmethod
    def parse(code: str) -> "Card":
        code = code.strip()
        r, s = code[0].upper(), code[1].lower()
        if r not in RANK_CHARS or s not in SUIT_CHARS:
            raise ValueError(f"bad card: {code}")
        return Card(RANK_CHARS.index(r) + 2, s)


def new_deck() -> list[Card]:
    return [Card(r, s) for s in SUIT_CHARS for r in range(2, 15)]


class Deck:
    def __init__(self, rng=None):
        import random
        self.rng = rng if rng is not None else random.Random()
        self.cards = new_deck()
        self.rng.shuffle(self.cards)

    def draw(self, n: int = 1) -> list[Card]:
        if n > len(self.cards):
            raise RuntimeError("deck exhausted")
        out = self.cards[:n]
        del self.cards[:n]
        return out

    def burn(self) -> None:
        if self.cards:
            self.cards.pop(0)


def evaluate5(cards: list[Card]) -> tuple:
    """返回可比较的元组：(类别, 踢脚...)。类别 8=同花顺 ... 0=高牌。"""
    if len(cards) != 5:
        raise ValueError("evaluate5 needs exactly 5 cards")
    ranks = sorted((c.rank for c in cards), reverse=True)
    uniq = sorted(set(ranks), reverse=True)
    flush = len({c.suit for c in cards}) == 1

    straight_high = 0
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight_high = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:       # A2345 轮子
            straight_high = 5

    counts: dict[int, int] = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    by_count = sorted(((n, r) for r, n in counts.items()), reverse=True)

    if flush and straight_high:
        return (8, straight_high)
    if by_count[0][0] == 4:
        quad = by_count[0][1]
        return (7, quad, max(r for r in uniq if r != quad))
    if by_count[0][0] == 3 and by_count[1][0] == 2:
        return (6, by_count[0][1], by_count[1][1])
    if flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if by_count[0][0] == 3:
        trip = by_count[0][1]
        kick = sorted((r for r in uniq if r != trip), reverse=True)
        return (3, trip, *kick)
    if by_count[0][0] == 2 and by_count[1][0] == 2:
        hi, lo = sorted((by_count[0][1], by_count[1][1]), reverse=True)
        return (2, hi, lo, max(r for r in uniq if r not in (hi, lo)))
    if by_count[0][0] == 2:
        pair = by_count[0][1]
        kick = sorted((r for r in uniq if r != pair), reverse=True)
        return (1, pair, *kick)
    return (0, *ranks)


def evaluate7(cards: list[Card]) -> tuple:
    """从 5/6/7 张牌里取最佳 5 张。"""
    if len(cards) < 5:
        raise ValueError("need at least 5 cards")
    return max(evaluate5(list(c)) for c in combinations(cards, 5))


def best5(cards: list[Card]) -> list[Card]:
    return max((list(c) for c in combinations(cards, 5)), key=evaluate5)


def describe(score: tuple) -> str:
    """短而可判胜负的牌型名，例如 '一对 8' / '两对 A/2'。"""
    c = score[0]
    r = rank_text
    if c == 8:
        return f"同花顺 {r(score[1])}高"
    if c == 7:
        return f"四条 {r(score[1])}"
    if c == 6:
        return f"葫芦 {r(score[1])}带{r(score[2])}"
    if c == 5:
        return f"同花 {r(score[1])}高"
    if c == 4:
        return f"顺子 {r(score[1])}高"
    if c == 3:
        return f"三条 {r(score[1])}"
    if c == 2:
        return f"两对 {r(score[1])}/{r(score[2])}"
    if c == 1:
        return f"一对 {r(score[1])}"
    return f"高牌 {r(score[1])}"


def _straight_draw(ranks) -> bool:
    """是否有 4 张连续点数、只差 1 张成顺（A 可当 1 或 14）。"""
    vals = set(ranks)
    if 14 in vals:
        vals.add(1)
    for lo in range(1, 11):
        window = set(range(lo, lo + 5))
        if len(window & vals) >= 4 and len(window - vals) == 1:
            return True
    return False


def read_hand(hole: list["Card"], board: list["Card"]) -> str:
    """给新手的一句话速读：我现在凑成了什么、大概好不好、还差什么。"""
    if len(hole) < 2:
        return ""
    hi, lo = sorted((c.rank for c in hole[:2]), reverse=True)
    suited = hole[0].suit == hole[1].suit

    if len(board) < 3:                              # 翻前：还没公共牌，只看起手牌
        if hi == lo:
            tag = "强" if hi >= 10 else "中上"
            return f"起手 一对{rank_text(hi)} · {tag}"
        label = f"{rank_text(hi)}/{rank_text(lo)} " + ("同花" if suited else "不同花")
        if hi - lo == 1:
            label += "连牌"
        if (hi == 14 and lo >= 10) or (suited and hi == 14):
            tag = "强"
        elif hi >= 12 or suited or hi - lo <= 2:
            tag = "中等"
        else:
            tag = "偏弱"
        return f"起手 {label} · {tag}"

    score = evaluate7(hole[:2] + board)
    cat = score[0]
    if cat >= 4:
        tag = "很强"
    elif cat == 3:
        tag = "强"
    elif cat == 2:
        tag = "中上"
    elif cat == 1:
        top = max(c.rank for c in board)
        if score[1] == top:
            tag = "顶对·中上"
        elif score[1] >= 11:
            tag = "中等"
        else:
            tag = "小对子·偏弱（公共牌有更大的牌）"
    else:
        tag = "偏弱"
    line = f"当前 {describe(score)} · {tag}"

    if len(board) < 5:                              # 还有牌没发，提示听牌
        all_cards = hole[:2] + board
        hints = []
        if any(sum(1 for c in all_cards if c.suit == s) == 4 for s in SUIT_CHARS):
            hints.append("同花听牌(差1张)")
        if cat < 4 and _straight_draw([c.rank for c in all_cards]):
            hints.append("顺子听牌")
        if hints:
            line += " · " + "/".join(hints)
    return line


def explain_cards(hole: list["Card"], board: list["Card"]) -> list[str]:
    """按 ? 时的白话讲解：牌从哪来、怎么比。"""
    legend = style_legend()
    prefix = "牌面 "
    if legend.startswith(prefix):
        legend = legend[len(prefix):]
    out = ["读法：" + legend]
    if hole:
        out.append("你的底牌：" + "、".join(
            SUIT_CN[c.suit] + rank_text(c.rank) for c in hole[:2]) + "（只有你能用）")
    out.append("公共牌：" + ("、".join(SUIT_CN[c.suit] + rank_text(c.rank) for c in board)
                          if board else "还没发") + "（桌上都能用，最多 5 张）")
    out.append("比牌：从「2 张底牌 + 公共牌」里挑最好的 5 张，"
               "对子 < 两对 < 三条 < 顺子 < 同花 < 葫芦 < 四条 < 同花顺")
    out.append("花色不分大小（黑桃不比梅花大）；同牌型时比点数大小")
    return out
