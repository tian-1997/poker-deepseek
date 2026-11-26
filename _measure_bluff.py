"""离线测量本地 AI 的下注范围是否还「一 bet 就是好牌」。

做法：随机造翻后局面 -> 估算胜率 -> 记录 heuristic_decide 的动作，
最后按胜率分桶统计「下注率」和「下注里的牌力构成」。
"""
import random
import sys

from cards import new_deck
from deepseek import PERSONAS, estimate_equity, heuristic_decide

SPOTS = int(sys.argv[1]) if len(sys.argv) > 1 else 400
BUCKETS = [(0.0, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 1.01)]


def make_snap(rng: random.Random) -> dict:
    deck = new_deck()
    rng.shuffle(deck)
    n_opp = rng.choice([1, 1, 2, 3])
    board_n = rng.choice([3, 3, 4, 4, 5])
    hole, board = deck[:2], deck[2:2 + board_n]
    pot = rng.choice([60, 90, 120, 180, 240, 360])
    street = {3: "flop", 4: "turn", 5: "river"}[board_n]
    # 一半「没人下注（可过牌）」，一半「面对下注」
    if rng.random() < 0.5:
        current_bet, to_call = 0, 0
    else:
        current_bet = rng.choice([20, 40, 60, 80, 120])
        to_call = current_bet
    stack = 1000
    my_round = 0
    max_to = my_round + stack
    lo = min(current_bet + 20, max_to)
    return {
        "hand_no": 1, "street": street, "street_cn": street, "board": board, "hole": hole,
        "pot": pot, "current_bet": current_bet, "to_call": to_call, "my_stack": stack,
        "my_round_bet": my_round, "blinds": (10, 20), "position": "CO",
        "n_players": n_opp + 1, "opponent_count": n_opp, "is_button": False,
        "opponents": [{"name": f"机器{i+1}", "stack": stack, "round_bet": current_bet,
                       "folded": i >= n_opp, "all_in": False} for i in range(n_opp)],
        "legal": {"fold": True, "check": to_call == 0, "call": to_call,
                  "to_call": to_call, "raise_to": (lo, max_to) if max_to > current_bet else None,
                  "allin": max_to, "current_bet": current_bet, "pot": pot},
        "history": [],
    }


def bucket_of(eq: float) -> int:
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= eq < hi:
            return i
    return len(BUCKETS) - 1


def run(tag: str, agg: float, spots: int = SPOTS) -> dict:
    rng = random.Random(20261111)
    stats = {"bets": 0, "checks": 0, "calls": 0, "folds": 0, "spots": 0}
    by_bucket = {i: {"n": 0, "bet": 0} for i in range(len(BUCKETS))}
    bet_eqs: list[float] = []
    for _ in range(spots):
        snap = make_snap(rng)
        eq = estimate_equity(snap["hole"], snap["board"], 110, rng, snap["opponent_count"])
        act, _reason = heuristic_decide(snap, rng, 70, agg, None, {"bluff": False}, 1.0)
        stats["spots"] += 1
        b = bucket_of(eq)
        by_bucket[b]["n"] += 1
        key = {"raise": "bets", "check": "checks", "call": "calls", "fold": "folds"}.get(act.kind)
        if key:
            stats[key] += 1
        if act.kind == "raise":
            by_bucket[b]["bet"] += 1
            bet_eqs.append(eq)
    stats["by_bucket"] = by_bucket
    stats["bet_eqs"] = bet_eqs
    return stats


def main() -> None:
    print(f"每个风格 {SPOTS} 个随机翻后局面（一半可过牌 / 一半面对下注）\n")
    head = "风格     总下注率 | " + " ".join(f"{lo:.2f}-{hi:.2f}" for lo, hi in BUCKETS)
    print(head)
    print("-" * len(head))
    all_bets: list[float] = []
    for p in PERSONAS:
        st = run(p["tag"], p["agg"])
        line = f"{p['tag']:<6} {st['bets']/st['spots']:>7.0%} | "
        cells = []
        for i in range(len(BUCKETS)):
            n, b = st["by_bucket"][i]["n"], st["by_bucket"][i]["bet"]
            cells.append(f"{b/n:>9.0%}" if n else " " * 9)
        print(line + " ".join(cells))
        all_bets.extend(st["bet_eqs"])

    print("\n把所有风格的下注混在一起看「下注里的牌力构成」")
    total = len(all_bets)
    weak = sum(1 for e in all_bets if e < 0.35)
    mid = sum(1 for e in all_bets if 0.35 <= e < 0.65)
    strong = sum(1 for e in all_bets if e >= 0.65)
    print(f"  下注 {total} 次：胜率<35% 的诈唬 {weak/total:.0%} | 35~65% 的中等/半诈唬 "
          f"{mid/total:.0%} | >=65% 的价值 {strong/total:.0%}")
    print(f"  => 看到下注时，他是诈唬的概率约 {weak/total:.0%}（改造前接近 0%）")


if __name__ == "__main__":
    main()
