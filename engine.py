"""无限注德州扑克牌局引擎：盲注、四轮下注、全下、短码全下不重开加注权、边池、摊牌结算。

引擎是状态机，不主动询问任何人：外部循环调用 next_actor() -> 取决策 -> apply_action()，
没有待行动玩家时调用 advance() 推进街/摊牌。这样 CLI 与 AI 解耦。
支持 N 人（CLI 用 2 人），边池按投入分层，通用可测。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from cards import Card, Deck, describe, evaluate7, fmt_cards

STREETS = ("preflop", "flop", "turn", "river")
STREET_CN = {"preflop": "翻前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}

# 位置按「从按钮位顺时针」排：BTN -> SB -> BB -> UTG -> MP -> CO
POSITION_LABELS = {
    2: ["BTN/SB", "BB"],
    3: ["BTN", "SB", "BB"],
    4: ["BTN", "SB", "BB", "CO"],
    5: ["BTN", "SB", "BB", "UTG", "CO"],
    6: ["BTN", "SB", "BB", "UTG", "MP", "CO"],
}


def position_name(idx: int, n: int, button: int) -> str:
    """座位在本局的相对位置名，供界面和 AI prompt 使用。"""
    labels = POSITION_LABELS.get(n)
    if labels:
        return labels[(idx - button) % n]
    return f"座位{(idx - button) % n + 1}"


@dataclass
class Player:
    name: str
    stack: int
    hole: list[Card] = field(default_factory=list)
    round_bet: int = 0            # 本条街已投入
    total_bet: int = 0            # 本局累计投入（边池用）
    folded: bool = False
    all_in: bool = False
    acted: bool = False

    def reset_hand(self) -> None:
        self.hole = []
        self.round_bet = 0
        self.total_bet = 0
        self.folded = False
        self.all_in = False
        self.acted = False

    @property
    def in_hand(self) -> bool:
        return not self.folded

    @property
    def can_act(self) -> bool:
        return not self.folded and not self.all_in


@dataclass
class Action:
    kind: str                     # fold / check / call / raise / allin
    amount: int = 0               # raise、allin：本轮加注到的「总投入额」


@dataclass
class HandResult:
    board: list[Card]
    payouts: dict[int, int]
    hands: dict[int, tuple]
    reason: str                   # fold / showdown
    winners: list[int]


class HoldemHand:
    def __init__(self, players: list[Player], button: int, sb: int, bb: int, rng=None, hand_no: int = 1):
        self.players = players
        self.n = len(players)
        if self.n < 2:
            raise ValueError("need at least 2 players")
        self.button = button
        self.sb, self.bb = sb, bb
        self.hand_no = hand_no
        self.deck = Deck(rng)
        self.board: list[Card] = []
        self.street = "preflop"
        self.current_bet = 0
        self.last_raise = bb
        self.betting_open = True
        self.action_ptr = 0
        self.history: list[dict] = []
        self.events: list[str] = []
        self.finished = False
        self.result: HandResult | None = None

        # heads-up：按钮位就是小盲，翻前按钮先行动；多人局为标准 SB/BB 位置
        if self.n == 2:
            self._sb_idx, self._bb_idx = button, (button + 1) % 2
        else:
            self._sb_idx = (button + 1) % self.n
            self._bb_idx = (button + 2) % self.n

        for p in players:
            p.reset_hand()
        self._emit(f"-- #{hand_no} -- 按钮 {players[button].name} | 盲注 {sb}/{bb}")
        self._post_blinds()
        self._deal_hole()
        self.action_ptr = self._first_actor("preflop")

    # ---------- 基础 ----------
    @property
    def pot(self) -> int:
        return sum(p.total_bet for p in self.players)

    def _emit(self, text: str) -> None:
        self.events.append(text)

    def _put(self, idx: int, chips: int) -> int:
        p = self.players[idx]
        chips = min(chips, p.stack)
        p.stack -= chips
        p.round_bet += chips
        p.total_bet += chips
        if p.stack == 0:
            p.all_in = True
        return chips

    def _post_blinds(self) -> None:
        a = self._put(self._sb_idx, self.sb)
        b = self._put(self._bb_idx, self.bb)
        self.current_bet = self.bb           # 名义大盲，短码全下也按此跟注
        self.last_raise = self.bb
        self._emit(f"盲注 {self.players[self._sb_idx].name} {a} / {self.players[self._bb_idx].name} {b}")

    def _deal_hole(self) -> None:
        for _ in range(2):
            for k in range(self.n):
                i = (self._sb_idx + k) % self.n
                self.players[i].hole.extend(self.deck.draw(1))

    def _first_actor(self, street: str) -> int:
        start = (self._bb_idx + 1) % self.n if street == "preflop" else (self.button + 1) % self.n
        for k in range(self.n):
            i = (start + k) % self.n
            if self.players[i].can_act:
                return i
        return start

    # ---------- 对外接口 ----------
    def legal_actions(self, idx: int) -> dict:
        p = self.players[idx]
        to_call = max(0, self.current_bet - p.round_bet)
        max_to = p.round_bet + p.stack
        can_raise = self.betting_open and p.stack > 0 and max_to > self.current_bet
        rng = None
        if can_raise:
            lo = min(self.current_bet + self.last_raise, max_to)
            rng = (lo, max_to)
        return {
            "fold": True,
            "check": to_call == 0,
            "call": min(to_call, p.stack) if to_call > 0 else 0,
            "to_call": to_call,
            "raise_to": rng,
            "allin": max_to,
            "current_bet": self.current_bet,
            "pot": self.pot,
        }

    def next_actor(self) -> int | None:
        for k in range(self.n):
            i = (self.action_ptr + k) % self.n
            p = self.players[i]
            if not p.can_act:
                continue
            if not p.acted or p.round_bet < self.current_bet:
                return i
        return None

    def apply_action(self, idx: int, action: Action) -> None:
        if self.finished:
            raise RuntimeError("hand already finished")
        legal = self.legal_actions(idx)
        p = self.players[idx]
        kind = action.kind
        if kind == "bet":
            kind = "raise"

        # 短码全下不重开加注权时，加注/全下自动降级为跟注或过牌
        if kind in ("raise", "allin") and legal["raise_to"] is None:
            kind = "call" if legal["to_call"] else "check"
            action = Action(kind)

        text = ""
        if kind == "fold":
            p.folded = True
            text = "弃牌"
        elif kind == "check":
            if legal["to_call"] != 0:
                raise ValueError("check not legal")
            p.acted = True
            text = "过牌"
        elif kind == "call":
            chips = self._put(idx, legal["call"])
            p.acted = True
            text = ("全下跟注 " if p.all_in else "跟注 ") + str(chips)
        elif kind in ("raise", "allin"):
            lo, hi = legal["raise_to"]
            target = hi if kind == "allin" else min(hi, max(lo, action.amount))
            was_no_bet = self.current_bet == 0
            self._put(idx, target - p.round_bet)
            inc = target - self.current_bet
            if inc >= self.last_raise:                 # 足额加注：重开行动权
                self.last_raise = inc
                self.betting_open = True
                for j, q in enumerate(self.players):
                    if j != idx and q.can_act:
                        q.acted = False
            else:                                     # 短码全下：只剩跟/弃
                self.betting_open = False
            self.current_bet = max(self.current_bet, target)
            p.acted = True
            if p.all_in:
                text = f"全下 {target}"
            elif was_no_bet:
                text = f"下注 {target}"
            else:
                text = f"加注到 {target}"
        else:
            raise ValueError(f"unknown action: {kind}")

        line = f"{p.name} {text}"
        self._emit(line)
        # kind 供复盘/对手画像统计用（text 是给人看的，kind 是给代码看的）
        self.history.append({"street": self.street, "idx": idx, "kind": kind,
                             "total": p.total_bet, "text": line})
        self.action_ptr = (idx + 1) % self.n

        if sum(1 for q in self.players if q.in_hand) == 1:
            self._finish("fold")

    def advance(self) -> None:
        """本条街无人需要行动时，发下一街或跑完公共牌并结算。"""
        if self.finished or self.next_actor() is not None:
            return
        active = [i for i, p in enumerate(self.players) if p.can_act]
        si = STREETS.index(self.street) + 1
        if len(active) >= 2 and si < len(STREETS):
            self.street = STREETS[si]
            self._deal_board()
            for p in self.players:
                p.round_bet = 0
                if p.can_act:
                    p.acted = False
            self.current_bet = 0
            self.last_raise = self.bb
            self.betting_open = True
            self.action_ptr = self._first_actor(self.street)
            self._emit(f"{STREET_CN[self.street]} | 公共 {fmt_cards(self.board)} | 底池 {self.pot}")
        else:
            self._runout()
            self._finish("showdown")

    def _deal_board(self) -> None:
        self.deck.burn()
        n = 3 if self.street == "flop" else 1
        self.board.extend(self.deck.draw(n))

    def _runout(self) -> None:
        while len(self.board) < 5:
            self.deck.burn()
            self.board.extend(self.deck.draw(1))

    # ---------- 结算 ----------
    def _build_pots(self) -> list[tuple[int, list[int]]]:
        contribs = [(i, p.total_bet) for i, p in enumerate(self.players) if p.total_bet > 0]
        pots: list[tuple[int, list[int]]] = []
        prev = 0
        for level in sorted({v for _, v in contribs}):
            amount = (level - prev) * sum(1 for _, v in contribs if v >= level)
            eligible = [i for i, p in enumerate(self.players) if p.in_hand and p.total_bet >= level]
            if amount:
                if not eligible and pots:              # 该层全由弃牌者贡献，并入上一层
                    a, e = pots[-1]
                    pots[-1] = (a + amount, e)
                else:
                    pots.append((amount, eligible))
            prev = level
        merged: list[tuple[int, list[int]]] = []
        for amount, eligible in pots:
            if merged and merged[-1][1] == eligible:
                merged[-1] = (merged[-1][0] + amount, eligible)
            else:
                merged.append((amount, eligible))
        return merged

    def _finish(self, reason: str) -> None:
        if reason == "showdown":
            self._runout()
        payouts = {i: 0 for i in range(self.n)}
        hands: dict[int, tuple] = {}
        winners: list[int] = []

        if reason == "fold":
            w = next(i for i, p in enumerate(self.players) if p.in_hand)
            payouts[w] = self.pot
            winners = [w]
            self._emit(f"{self.players[w].name} 收池 {payouts[w]}（对手弃牌）")
        else:
            for i, p in enumerate(self.players):
                if p.in_hand:
                    hands[i] = evaluate7(p.hole + self.board)
            # 亮牌统一交给 CLI 的「局末翻牌」块（含弃牌者底牌），此处不重复打印
            for amount, eligible in self._build_pots():
                if not eligible:
                    continue
                best = max(hands[i] for i in eligible)
                pool = [i for i in eligible if hands[i] == best]
                share, extra = divmod(amount, len(pool))
                for i in pool:
                    payouts[i] += share
                order = sorted(pool, key=lambda i: (i - self.button) % self.n)
                for i in order[:extra]:
                    payouts[i] += 1
                winners = sorted(set(winners + pool))
                self._emit(f"底池 {amount} -> {'/'.join(self.players[i].name for i in pool)} ({describe(best)})")

        for i, p in enumerate(self.players):
            p.stack += payouts[i]
        self._emit("筹码 " + " | ".join(f"{p.name} {p.stack}" for p in self.players))
        self.result = HandResult(list(self.board), payouts, hands, reason, winners)
        self.finished = True

    # ---------- 给 AI / 复盘的快照 ----------
    def snapshot(self, idx: int) -> dict:
        p = self.players[idx]
        legal = self.legal_actions(idx)
        return {
            "hand_no": self.hand_no,
            "street": self.street,
            "street_cn": STREET_CN[self.street],
            "board": list(self.board),
            "hole": list(p.hole),
            "pot": legal["pot"],
            "current_bet": legal["current_bet"],
            "to_call": legal["to_call"],
            "my_stack": p.stack,
            "my_round_bet": p.round_bet,
            "blinds": (self.sb, self.bb),
            "position": position_name(idx, self.n, self.button),
            "n_players": self.n,
            "opponent_count": max(1, sum(1 for j, q in enumerate(self.players)
                                         if j != idx and not q.folded)),
            "is_button": idx == self.button,
            "opponents": [
                {"name": q.name, "stack": q.stack, "round_bet": q.round_bet,
                 "folded": q.folded, "all_in": q.all_in}
                for j, q in enumerate(self.players) if j != idx
            ],
            "legal": legal,
            # 结构化行动记录：AI 靠它判断「谁加注过、谁本街主动下注」来收窄对手范围。
            # 只有名字/街/动作类型，不含任何底牌信息。
            "actions": [{"name": self.players[h["idx"]].name, "street": h["street"],
                         "kind": h["kind"]} for h in self.history],
            "history": [h["text"] for h in self.history],
        }
