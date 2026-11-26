"""本地 AI 强度基准：旧版（_baseline_deepseek）与新版（deepseek）同桌对打。

两个用法：
  python _bench.py --spots              # 关键牌例对照：同一副牌，旧版和新版分别怎么打
  python _bench.py --selfplay 400 --sims 25   # A/B 自对弈，报 bb/100 与 95% 置信区间

为什么要 A/B 自对弈而不是「感觉变强了」：两个版本坐同一张桌、轮换按钮、
同一副牌，筹码在两人之间流动，长期净输赢的差就是强度差。
注意方差：几百手的置信区间仍在 ±20 bb/100 量级，只能看方向和量级。
"""
from __future__ import annotations

import argparse
import random
import statistics

import _baseline_deepseek as old_mod
import deepseek as new_mod
from cards import Card
from engine import HoldemHand, Player


def h(text: str):
    return [Card.parse(x) for x in text.split()]


class Policy:
    """把 heuristic_decide 包成「一个会打牌的东西」，带自己的一局作战计划。"""

    def __init__(self, mod, label: str, agg: float = 0.0, sims: int = 25, seed: int = 1):
        self.mod = mod
        self.label = label
        self.agg = agg
        self.sims = sims
        self.rng = random.Random(seed)
        self.plan = {"bluff": False, "trapped": False}

    def start_hand(self) -> None:
        self.plan = {"bluff": False, "trapped": False}

    def reseed(self, seed: int) -> None:
        """同一副牌打两遍时，让两边的随机流也对称，差值才只剩策略差异。"""
        self.rng = random.Random(seed)

    def decide(self, snap: dict):
        return self.mod.heuristic_decide(snap, self.rng, self.sims, self.agg,
                                         None, self.plan, 1.0)


def play_hand(policies, stacks, button, sb, bb, rng, hand_no):
    players = [Player(f"S{i}", stacks[i]) for i in range(len(stacks))]
    hand = HoldemHand(players, button, sb, bb, rng, hand_no)
    for p in policies:
        p.start_hand()
    guard = 0
    while not hand.finished:
        pos = hand.next_actor()
        if pos is None:
            hand.advance()
            continue
        guard += 1
        if guard > 200:
            raise RuntimeError("一手牌超过 200 次行动，引擎或策略有问题")
        action, _reason = policies[pos].decide(hand.snapshot(pos))
        hand.apply_action(pos, action)
    return [p.stack for p in players], hand


def selfplay(hands: int, sims: int, stack: int, sb: int, bb: int, seed: int) -> None:
    """同一副牌打两遍（座位对调），把「牌运」从差值里抵掉，这是复式扑克的做法。"""
    labels = ("旧版", "新版")
    policies = {s: Policy(old_mod if s == "旧版" else new_mod, s, 0.0, sims, seed) for s in labels}
    diffs: list[float] = []
    mix = {s: {"preflop_raise": 0, "preflop_call": 0, "preflop_fold": 0,
               "postflop_raise": 0, "postflop_call": 0, "postflop_fold": 0} for s in labels}

    for k in range(hands):
        net_sum = {s: 0.0 for s in labels}
        for variant in (0, 1):
            # variant 0：旧版坐 0/2；variant 1：旧版坐 1/3（按钮不变，位置被镜像）
            order = labels if variant == 0 else labels[::-1]
            seats = [order[i % 2] for i in range(4)]           # 旧新 旧新 / 新旧 新旧
            seat_policies = [policies[s] for s in seats]
            for s in labels:
                policies[s].reseed(seed * 7919 + k * 31 + variant)
            nets, hand = play_hand(seat_policies, [stack] * 4, k % 4, sb, bb,
                                   random.Random(seed * 1000 + k), k + 1)
            for i, s in enumerate(seats):
                net_sum[s] += nets[i] - stack
                for rec in hand.history:
                    if rec["idx"] != i:
                        continue
                    street = "preflop" if rec["street"] == "preflop" else "postflop"
                    bucket = "raise" if rec["kind"] in ("raise", "allin") else rec["kind"]
                    if bucket in ("raise", "call", "fold"):
                        mix[s][f"{street}_{bucket}"] += 1
        # 两手一起看，净输赢为零和；记「新版 - 旧版」这一个样本
        diffs.append((net_sum["新版"] - net_sum["旧版"]) / 2)

    def report(series, label, show_ci=True):
        mean = statistics.fmean(series)
        sd = statistics.pstdev(series) if len(series) > 1 else 0.0
        se = sd / max(1, len(series)) ** 0.5
        text = f"  {label:<10} {mean / bb * 100:>8.1f} bb/100"
        if show_ci:
            lo, hi = mean - 1.96 * se, mean + 1.96 * se
            text += f"   95% CI [{lo / bb * 100:+.1f}, {hi / bb * 100:+.1f}]"
        print(text)
        return mean

    print(f"A/B 自对弈：{hands} 手 × 2 遍（座位对调）· {sims} 次模拟/决策 · "
          f"盲注 {sb}/{bb} · 4 人桌（每版 2 座）\n")
    report(diffs, "版本差")
    print("  （正 = 新版赢旧版。两个版本代码相同时应为 0 上下浮动，可用来验证基准本身无偏）")

    print("\n动作构成（次数 / 占比）")
    for s in labels:
        m = mix[s]
        pre_total = m["preflop_raise"] + m["preflop_call"] + m["preflop_fold"]
        post_total = m["postflop_raise"] + m["postflop_call"] + m["postflop_fold"]

        def pct(a, b):
            return f"{a / b:>5.0%}" if b else "  n/a"

        print(f"  {s}: 翻前 加注 {pct(m['preflop_raise'], pre_total)} / "
              f"跟注 {pct(m['preflop_call'], pre_total)} / 弃牌 {pct(m['preflop_fold'], pre_total)}"
              f"   |   翻后 下注 {pct(m['postflop_raise'], post_total)} / "
              f"跟注 {pct(m['postflop_call'], post_total)} / 弃牌 {pct(m['postflop_fold'], post_total)}")


# ---------- 关键牌例对照 ----------

def spot_snap(hole: str, board: str, to_call: int, pot: int, position: str,
              n_opp: int = 1, opp_raised: bool = False, stack: int = 1000) -> dict:
    board_cards = h(board) if board.strip() else []
    street = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}[len(board_cards)]
    lo = min(to_call + 20, stack)
    return {
        "hand_no": 1, "street": street, "street_cn": street,
        "board": board_cards, "hole": h(hole),
        "pot": pot, "current_bet": to_call, "to_call": to_call,
        "my_stack": stack, "my_round_bet": 0, "blinds": (10, 20),
        "position": position, "n_players": n_opp + 1, "opponent_count": n_opp,
        "is_button": position == "BTN",
        "opponents": [{"name": f"机器{i+1}", "stack": stack, "round_bet": to_call,
                       "folded": i >= n_opp, "all_in": False} for i in range(n_opp)],
        "legal": {"fold": True, "check": to_call == 0, "call": to_call,
                  "to_call": to_call, "raise_to": (lo, stack) if stack > to_call else None,
                  "allin": stack, "current_bet": to_call, "pot": pot},
        "actions": ([{"name": "机器1", "street": "preflop", "kind": "raise"}] if opp_raised else []),
        "history": [],
    }


SPOTS = [
    ("翻前 72o UTG 无人入池", dict(hole="7d 2c", board="", to_call=20, pot=30, position="UTG")),
    ("翻前 72o BTN 无人入池", dict(hole="7d 2c", board="", to_call=20, pot=30, position="BTN")),
    ("翻前 AA 面对加注", dict(hole="As Ah", board="", to_call=60, pot=90, position="CO", opp_raised=True)),
    ("翻前 K9o BB 面对加注", dict(hole="Kd 9c", board="", to_call=40, pot=70, position="BB", opp_raised=True)),
    ("翻前 K9o CO 面对加注", dict(hole="Kd 9c", board="", to_call=60, pot=90, position="CO", opp_raised=True)),
    ("翻前 K9o 面对 3bet", dict(hole="Kd 9c", board="", to_call=180, pot=240, position="CO", opp_raised=True)),
    ("翻后 中对 面对 1/3 底池", dict(hole="9d 9c", board="Kh 7s 2d", to_call=40, pot=120, position="BB", opp_raised=True)),
    ("翻后 同花听 面对 1/2 底池", dict(hole="As Ks", board="Qs 7s 2h", to_call=60, pot=120, position="CO", opp_raised=True)),
    ("翻后 坚果同花 河牌 无人下注", dict(hole="Ah 3h", board="Kh 9h 2s Js 7h", to_call=0, pot=240, position="CO")),
    ("翻后 无牌 河牌 面对 1/2 底池", dict(hole="7d 2c", board="Kh Qs 9d 5c 3h", to_call=120, pot=240, position="CO", opp_raised=True)),
]


def spots(trials: int, sims: int) -> None:
    print(f"关键牌例：每个局面各跑 {trials} 次，看两个版本的动作分布\n")
    for title, kw in SPOTS:
        snap = spot_snap(**kw)
        row = []
        for label, mod in (("旧版", old_mod), ("新版", new_mod)):
            counts = {"raise": 0, "call": 0, "fold": 0, "check": 0}
            sizes = []
            for seed in range(trials):
                act, _ = mod.heuristic_decide(snap, random.Random(seed), sims, 0.0,
                                             None, {"bluff": False}, 1.0)
                counts[act.kind] = counts.get(act.kind, 0) + 1
                if act.kind == "raise":
                    sizes.append(act.amount)
            top = max(counts, key=counts.get)
            cn = {"raise": "下注/加注", "call": "跟注", "fold": "弃牌", "check": "过牌"}[top]
            extra = f" {round(statistics.fmean(sizes))}" if sizes else ""
            row.append(f"{cn}{extra} {counts[top] / trials:.0%}")
        print(f"  {title:<26} 旧版 {row[0]:<16} 新版 {row[1]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spots", action="store_true", help="只跑关键牌例对照")
    ap.add_argument("--selfplay", type=int, default=400, help="自对弈手数，默认 400")
    ap.add_argument("--sims", type=int, default=25, help="每次决策的蒙特卡洛次数，默认 25")
    ap.add_argument("--stack", type=int, default=1000)
    ap.add_argument("--sb", type=int, default=10)
    ap.add_argument("--bb", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument("--trials", type=int, default=60, help="牌例每个局面重复次数")
    args = ap.parse_args()

    spots(args.trials, args.sims)
    if not args.spots:
        print()
        selfplay(args.selfplay, args.sims, args.stack, args.sb, args.bb, args.seed)


if __name__ == "__main__":
    main()
