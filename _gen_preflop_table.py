"""生成 preflop_table.py：169 手起手牌「单挑一个随机对手」的胜率。

离线跑一次，结果入库（preflop_table.EQUITY），运行时直接查表，不再现算。
    python _gen_preflop_table.py
"""
import random

from cards import Card, evaluate7, new_deck

RANKS = "23456789TJQKA"
SIM = 2000
rng = random.Random(20261125)


def equity_vs_random(hole, sims: int = SIM) -> float:
    known = set(hole)
    deck = [x for x in new_deck() if x not in known]
    score = 0.0
    for _ in range(sims):
        rng.shuffle(deck)
        board = deck[:5]
        mine = evaluate7(hole + board)
        theirs = evaluate7(deck[5:7] + board)
        score += 1.0 if mine > theirs else 0.5 if mine == theirs else 0.0
    return score / sims


table: dict[str, float] = {}
for i in range(13):                 # 低牌索引
    for j in range(i, 13):          # 高牌索引（相等即对子）
        lo, hi = RANKS[i], RANKS[j]
        if i == j:
            table[hi * 2] = round(equity_vs_random(
                [Card(i + 2, "s"), Card(i + 2, "h")]), 3)
        else:
            table[hi + lo + "s"] = round(equity_vs_random(
                [Card(j + 2, "s"), Card(i + 2, "s")]), 3)
            table[hi + lo + "o"] = round(equity_vs_random(
                [Card(j + 2, "s"), Card(i + 2, "d")]), 3)

lines = ['"""169 手起手牌对随机单挑的胜率（由 _gen_preflop_table.py 生成，勿手改）。"""',
         "EQUITY = {"]
for k in sorted(table):
    lines.append(f'    "{k}": {table[k]:.3f},')
lines.append("}")
open("preflop_table.py", "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("wrote preflop_table.py with", len(table), "hands")
print("AA", table["AA"], "KK", table["KK"], "AKs", table["AKs"],
      "K9o", table["K9o"], "76s", table["76s"], "72o", table["72o"])
