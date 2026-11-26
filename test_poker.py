"""规则与集成自测：python -m unittest -v test_poker"""
from __future__ import annotations

import json
import os
import random
import tempfile
import unittest

from cards import (Card, card_style, evaluate5, evaluate7, explain_cards,
                   fmt_cards, read_hand, set_card_style, style_legend)
from deepseek import (PERSONAS, DeepSeekPlayer, _extract_json, _normalize,
                      bluff_rate, build_prompt, draw_strength, estimate_equity,
                      heuristic_decide, sized_raise)
from engine import Action, HandResult, HoldemHand, Player, position_name
import jev
from memory import (OpponentMemory, aggregate, hand_stats, read_adjust,
                    style_label)


def h(s: str):
    return [Card.parse(x) for x in s.split()]


class CardTests(unittest.TestCase):
    def test_category_order(self):
        scores = [
            evaluate5(h("As Ks Qs Js Ts")),      # 同花顺
            evaluate5(h("9s 9h 9d 9c 2s")),      # 四条
            evaluate5(h("9s 9h 9d 2c 2s")),      # 葫芦
            evaluate5(h("As 9s 7s 4s 2s")),      # 同花
            evaluate5(h("9s 8h 7d 6c 5s")),      # 顺子
            evaluate5(h("9s 9h 9d 5c 2s")),      # 三条
            evaluate5(h("9s 9h 5d 5c 2s")),      # 两对
            evaluate5(h("9s 9h 7d 5c 2s")),      # 一对
            evaluate5(h("As 9h 7d 5c 2s")),      # 高牌
        ]
        for a, b in zip(scores, scores[1:]):
            self.assertGreater(a, b)

    def test_wheel_straight(self):
        wheel = evaluate5(h("As 2h 3d 4c 5s"))
        six_high = evaluate5(h("6s 2h 3d 4c 5s"))
        self.assertEqual(wheel[0], 4)
        self.assertLess(wheel, six_high)

    def test_best_of_seven(self):
        royal = evaluate5(h("As Ks Qs Js Ts"))
        self.assertEqual(evaluate7(h("As Ks Qs Js Ts 2h 3d")), royal)
        self.assertEqual(evaluate7(h("9s 9h 9d 2c 2s 7h 3d")), evaluate5(h("9s 9h 9d 2c 2s")))

    def test_kicker(self):
        self.assertGreater(evaluate5(h("As Ah Kd Qc Js")), evaluate5(h("As Ah Kd Qc 9s")))

    def test_card_styles_for_humans(self):
        saved = card_style()
        cards = h("9d 2d As Tc")
        try:
            set_card_style("cn")
            self.assertEqual(fmt_cards(cards), "方9 方2 黑A 梅10")
            self.assertIn("方块9", style_legend())
            set_card_style("ascii")
            self.assertEqual(fmt_cards(cards), "9d 2d As Tc")
            set_card_style("pretty")
            self.assertEqual(fmt_cards(cards), "9♦ 2♦ A♠ 10♣")
            self.assertEqual(fmt_cards([]), "--")
        finally:
            set_card_style(saved)

    def test_readout_and_explanation(self):
        saved = card_style()
        try:
            set_card_style("cn")
            hole, board = h("9d 2d"), h("9h 4c 6s")
            self.assertIn("一对 9", read_hand(hole, board))    # 翻后顶对
            self.assertIn("顶对", read_hand(hole, board))
            self.assertIn("同花听牌", read_hand(h("As Ks"), h("2s 7s Qd")))
            self.assertIn("起手", read_hand(h("As Ks"), []))
            self.assertIn("一对A", read_hand(h("As Ah"), []).replace(" ", ""))
            text = "\n".join(explain_cards(hole, board))
            self.assertIn("花色不分大小", text)
            self.assertIn("公共牌", text)
            # 界面换风格不能影响发给模型的标准牌记法
            hand = HoldemHand([Player("你", 1000), Player("AI", 1000)],
                             0, 10, 20, random.Random(1))
            snap = hand.snapshot(1)
            snap["hole"], snap["board"] = hole, board
            set_card_style("pretty")
            self.assertIn("9d 2d", build_prompt(snap))
        finally:
            set_card_style(saved)


class EngineTests(unittest.TestCase):
    def make(self, stacks=(1000, 1000), button=0, seed=1):
        players = [Player("你", stacks[0]), Player("DeepSeek", stacks[1])]
        return HoldemHand(players, button, 10, 20, random.Random(seed))

    def test_heads_up_preflop_order(self):
        hand = self.make()
        self.assertEqual(hand.next_actor(), 0)          # 按钮=小盲，翻前先行动
        self.assertEqual(hand.legal_actions(0)["to_call"], 10)
        self.assertEqual(hand.pot, 30)

    def test_fold_ends_hand_and_conserves_chips(self):
        hand = self.make()
        before = sum(p.stack for p in hand.players) + hand.pot
        hand.apply_action(hand.next_actor(), Action("fold"))
        self.assertTrue(hand.finished)
        self.assertEqual(hand.result.reason, "fold")
        after = sum(p.stack for p in hand.players)
        self.assertEqual(before, after)

    def test_short_allin_does_not_reopen_betting(self):
        # B 只有 140：下大盲 20 后剩 120，全下到 140 属于不足额加注
        hand = self.make(stacks=(1000, 140))
        hand.apply_action(0, Action("raise", 100))     # 足额加注，last_raise=80
        legal_b = hand.legal_actions(1)
        self.assertEqual(legal_b["raise_to"], (140, 140))
        hand.apply_action(1, Action("allin", 140))
        legal_a = hand.legal_actions(0)
        self.assertIsNone(legal_a["raise_to"])         # A 只能跟/弃
        self.assertEqual(legal_a["to_call"], 40)

    def test_side_pot_layers(self):
        players = [Player("A", 300), Player("B", 300), Player("C", 300)]
        hand = HoldemHand(players, 0, 10, 20, random.Random(2))
        for p, v in zip(hand.players, (100, 200, 300)):
            p.total_bet = v
        self.assertEqual(hand._build_pots(), [(300, [0, 1, 2]), (200, [1, 2]), (100, [2])])

    def test_uncalled_overbet_returned(self):
        hand = self.make()
        hand.players[0].total_bet = 300
        hand.players[1].total_bet = 100
        self.assertEqual(hand._build_pots(), [(200, [0, 1]), (200, [0])])

    def test_full_hand_to_showdown(self):
        hand = self.make(seed=7)
        before = sum(p.stack for p in hand.players) + hand.pot
        guard = 0
        while not hand.finished:
            guard += 1
            self.assertLess(guard, 100, "hand did not terminate")
            idx = hand.next_actor()
            if idx is None:
                hand.advance()
                continue
            legal = hand.legal_actions(idx)
            hand.apply_action(idx, Action("call" if legal["to_call"] else "check"))
        self.assertEqual(hand.result.reason, "showdown")
        self.assertEqual(len(hand.board), 5)
        self.assertEqual(before, sum(p.stack for p in hand.players))


class AITests(unittest.TestCase):
    def test_extract_and_normalize(self):
        legal = {"to_call": 20, "current_bet": 60, "raise_to": (120, 940),
                 "call": 20, "check": False, "pot": 90, "allin": 940}
        dec = _extract_json('```json\n{"action":"raise","amount":200,"reason":"牌力强"}\n```')
        act, reason = _normalize(dec, legal)
        self.assertEqual((act.kind, act.amount), ("raise", 200))
        self.assertEqual(reason, "牌力强")
        # 校验 / 夹逼
        self.assertEqual(_normalize({"action": "raise", "amount": 99999}, legal)[0].kind, "allin")
        self.assertEqual(_normalize({"action": "check"}, legal)[0].kind, "call")
        self.assertIsNone(_normalize({"action": "teleport"}, legal))
        # 增量写法容错
        self.assertEqual(_normalize({"action": "bet", "amount": 60}, legal)[0].amount, 120)

    def test_offline_bot_20_hands(self):
        bot = DeepSeekPlayer(offline=True, sims=20, rng=random.Random(3))
        players = [Player("你", 2000), Player("DeepSeek", 2000)]
        total = 4000
        for seed in range(20):
            rng = random.Random(seed)
            hand = HoldemHand(players, seed % 2, 10, 20, rng, seed + 1)
            bot.start_hand(seed + 1)
            guard = 0
            while not hand.finished:
                guard += 1
                self.assertLess(guard, 100)
                idx = hand.next_actor()
                if idx is None:
                    hand.advance()
                    continue
                legal = hand.legal_actions(idx)
                if idx == 0:
                    act = Action("call" if legal["to_call"] else "check")
                else:
                    act, entry = bot.decide(hand.snapshot(idx))
                    self.assertIn("本地兜底", entry["source"])
                    self.assertTrue(entry["reason"])
                hand.apply_action(idx, act)
            bot.end_hand(seed + 1)
            self.assertEqual(sum(p.stack for p in players), total)
        self.assertGreater(len(bot.logs), 0)
        lines = bot.review_lines(1)
        self.assertTrue(any("理由" in x for x in lines))

    def test_placeholder_config_is_refused_instead_of_silent_fallback(self):
        bot = DeepSeekPlayer(api_key="你的key", base_url="https://你的端点/v1",
                             model="模型名", sims=10, rng=random.Random(1))
        self.assertFalse(bot.enabled)                 # 不能假装「已接入 LLM」
        self.assertTrue(bot.disabled_reason)
        self.assertIn("占位符", bot.check_connection())

    def test_non_ascii_key_gets_a_clear_error(self):
        forced = DeepSeekPlayer(api_key="你的key", base_url="http://127.0.0.1:1/v1",
                                model="x", trust_config=True)
        self.assertTrue(forced.enabled)
        with self.assertRaises(ValueError) as cm:     # 在发请求之前就该拦下
            forced._chat("ping")
        self.assertIn("非 ASCII", str(cm.exception))


class MultiwayTests(unittest.TestCase):
    def table(self, n=4, seed=3, button=0):
        players = [Player("你", 1000)] + [Player(f"机器{k}", 1000) for k in range(1, n)]
        return HoldemHand(players, button, 10, 20, random.Random(seed))

    def test_position_names(self):
        self.assertEqual([position_name(i, 4, 0) for i in range(4)],
                         ["BTN", "SB", "BB", "CO"])
        self.assertEqual([position_name(i, 6, 0) for i in range(6)],
                         ["BTN", "SB", "BB", "UTG", "MP", "CO"])
        self.assertEqual(position_name(0, 3, 2), "SB")

    def test_four_handed_preflop_order_and_snapshot(self):
        hand = self.table(4)
        self.assertEqual(hand.next_actor(), 3)          # BB 后一家先行动
        snap = hand.snapshot(3)
        self.assertEqual(snap["n_players"], 4)
        self.assertEqual(snap["position"], "CO")
        self.assertEqual(snap["opponent_count"], 3)
        self.assertIn("4 人桌", build_prompt(snap))

    def test_six_handed_showdown_conserves_chips(self):
        hand = self.table(6, seed=9)
        before = sum(p.stack for p in hand.players) + hand.pot
        guard = 0
        while not hand.finished:
            guard += 1
            self.assertLess(guard, 200, "hand did not terminate")
            idx = hand.next_actor()
            if idx is None:
                hand.advance()
                continue
            legal = hand.legal_actions(idx)
            hand.apply_action(idx, Action("call" if legal["to_call"] else "check"))
        self.assertEqual(hand.result.reason, "showdown")
        self.assertEqual(before, sum(p.stack for p in hand.players))

    def test_more_opponents_lower_equity(self):
        hole = h("As Ah")
        one = estimate_equity(hole, [], 400, random.Random(11), 1)
        three = estimate_equity(hole, [], 400, random.Random(11), 3)
        self.assertGreater(one, three)                  # AA 打 3 家明显低于打 1 家

    def test_persona_reaches_review_and_prompt(self):
        self.assertEqual(len(PERSONAS), 5)
        p = PERSONAS[1]
        bot = DeepSeekPlayer(offline=True, sims=10, rng=random.Random(1),
                             persona=p["style"], aggression=p["agg"], tag=p["tag"])
        bot.name = "机器2"
        bot.start_hand(1)
        bot.hand_log.append({"hand_no": 1, "street_cn": "翻前",
                             "action_text": "加注到 40", "reason": "施压"})
        bot.end_hand(1)
        self.assertIn("机器2", bot.review_lines(1)[0])

    def test_reveal_shows_folded_hands_and_reason_block(self):
        import poker
        players = [Player("你", 1000)] + [Player(f"机器{k}", 1000) for k in range(1, 4)]
        hand = HoldemHand(players, 0, 10, 20, random.Random(6))
        guard = 0
        while not hand.finished and guard < 30:
            guard += 1
            idx = hand.next_actor()
            if idx is None:
                hand.advance()
                continue
            hand.apply_action(idx, Action("fold"))
        self.assertEqual(hand.result.reason, "fold")
        reveal = "\n".join(poker.reveal_lines(hand))
        self.assertIn("已弃牌", reveal)
        for p in players:
            self.assertIn(p.name, reveal)

        bots = {}
        for k in range(1, 4):
            bot = DeepSeekPlayer(offline=True, sims=10, rng=random.Random(k), tag="稳健")
            bot.name = f"机器{k}"
            bot.start_hand(1)
            bot.hand_log.append({"hand_no": 1, "street_cn": "翻前",
                                 "action_text": "跟注 20", "reason": "测试理由"})
            bots[k] = bot
        reasons = "\n".join(poker.reason_lines(bots, 1))
        self.assertIn("-- 下注理由 --", reasons)
        self.assertIn("测试理由", reasons)
        self.assertIn("机器3", reasons)


class JevTests(unittest.TestCase):
    """TypeSafe Jev 是决策模型，协议与 chat/completions 完全不同。"""
    @staticmethod
    def legal(to_call=20, raise_to=(40, 200)):
        return {"to_call": to_call, "call": to_call, "check": to_call == 0,
                "current_bet": 20, "pot": 60, "raise_to": raise_to, "allin": 200}

    def test_resolve_url(self):
        import jev
        for base in ("https://api.typesafe.ai", "https://api.typesafe.ai/v1",
                     "https://api.typesafe.ai/v1/",
                     "https://api.typesafe.ai/v1/chat/completions"):
            self.assertEqual(jev.resolve_url(base),
                             "https://api.typesafe.ai/v1/systemone")

    def test_questions_only_offer_legal_actions(self):
        import jev
        q = jev.build_questions(self.legal())
        self.assertIn("call", q["action"]["criteria"])
        self.assertNotIn("check", q["action"]["criteria"])
        self.assertIn("sizing", q)
        q2 = jev.build_questions(self.legal(to_call=0, raise_to=None))
        self.assertEqual(set(q2["action"]["criteria"]), {"fold", "check"})
        self.assertNotIn("sizing", q2)

    def test_parse_choice_and_reason(self):
        import jev
        resp = {"answers": {
            "action": {"choice": "call", "probabilities": {"call": 0.72, "fold": 0.28},
                       "confidence": 0.81},
            "sizing": {"score": 1.0}}}
        act, reason = jev.parse_answers(resp, self.legal())
        self.assertEqual((act.kind, act.amount), ("call", 0))
        self.assertIn("概率 72%", reason)
        self.assertIn("置信 0.81", reason)
        self.assertIn("次选 弃牌", reason)

    def test_parse_illegal_choice_falls_back(self):
        import jev
        self.assertIsNone(jev.parse_answers(
            {"answers": {"action": {"choice": "check"}}}, self.legal()))
        self.assertIsNone(jev.parse_answers({"answers": {}}, self.legal()))
        self.assertIsNone(jev.parse_answers("not a dict", self.legal()))

    def test_sizing_maps_into_legal_range(self):
        import jev
        for score in (0, 1, 2, 3, 4):
            resp = {"answers": {
                "action": {"choice": "raise", "probabilities": {"raise": 0.6},
                           "confidence": 0.7},
                "sizing": {"score": score}}}
            act, reason = jev.parse_answers(resp, self.legal())
            self.assertTrue(40 <= act.amount <= 200)
            self.assertIn("尺度", reason)
        resp = {"answers": {"action": {"choice": "allin", "confidence": 0.9},
                            "sizing": {"score": 0}}}
        act, _ = jev.parse_answers(resp, self.legal())
        self.assertEqual((act.kind, act.amount), ("allin", 200))

    def test_tsf_key_autoconfig(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            bot = DeepSeekPlayer(api_key="tsf_abc", trust_config=True)
        self.assertEqual(bot.provider, "jev")
        self.assertEqual(bot.base_url, "https://api.typesafe.ai/v1")
        self.assertEqual(bot.model, "jev-latest")
        self.assertEqual(bot.kind_label, "Jev")

    def test_official_apikey_prefix_autoconfig(self):
        """官方控制台 console.typesafe.ai/keys 发的是 apikey_ 前缀，不是 tsf_。"""
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            bot = DeepSeekPlayer(api_key="apikey_demo", trust_config=True)
        self.assertEqual(bot.provider, "jev")
        self.assertEqual(bot.base_url, "https://api.typesafe.ai/v1")
        self.assertEqual(bot.model, "jev-latest")

    def test_official_endpoint_autodetects_jev(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            bot = DeepSeekPlayer(api_key="sk_demo", base_url="https://api.typesafe.ai/v1",
                                 model="jev-latest", trust_config=True)
        self.assertEqual(bot.provider, "jev")
        self.assertEqual(bot.kind_label, "Jev")

    def test_chat_gateway_stays_openai(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            bot = DeepSeekPlayer(api_key="sk_abc", base_url="https://gw.example.com/v1",
                                 model="deepseek-chat", trust_config=True)
        self.assertEqual(bot.provider, "openai")
        self.assertEqual(bot.kind_label, "LLM")

    def test_english_placeholders_detected(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            for base, model in (("https://REAL_HOST/v1", "REAL_MODEL_ID"),
                                ("https://PASTE_GATEWAY_HOST/v1", "PASTE_MODEL_ID")):
                bot = DeepSeekPlayer(api_key="sk_abc", base_url=base, model=model)
                self.assertTrue(bot.disabled_reason, f"{base} should be a placeholder")
                self.assertFalse(bot.enabled)


class ConfigGuardTests(unittest.TestCase):
    """把 API Key 粘进「API 地址」时，不能拼出 apikey_xxx/chat/completions。"""

    def test_key_in_base_url_corrected_to_official_jev(self):
        import os
        from unittest.mock import patch
        from deepseek import JEV_BASE, JEV_MODEL
        fake = "apikey_FAKE1234567890abcdef"
        with patch.dict(os.environ, {}, clear=True):
            bot = DeepSeekPlayer(api_key=fake, base_url=fake,
                                 model="deepseek-chat", trust_config=True)
        self.assertEqual(bot.provider, "jev")
        self.assertEqual(bot.base_url, JEV_BASE)
        self.assertEqual(bot.model, JEV_MODEL)
        self.assertTrue(bot.enabled)
        self.assertTrue(any("Key" in n for n in bot.config_notes))

    def test_key_in_env_base_without_key_is_reused(self):
        import os
        from unittest.mock import patch
        fake = "apikey_FAKE1234567890abcdef"
        with patch.dict(os.environ, {"JEV_BASE_URL": fake}, clear=True):
            bot = DeepSeekPlayer(trust_config=True)
        self.assertEqual(bot.api_key, fake)
        self.assertEqual(bot.provider, "jev")
        self.assertEqual(bot.base_url, "https://api.typesafe.ai/v1")

    def test_non_url_base_rejected_with_clear_error(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "不是网址"):
                DeepSeekPlayer(api_key="sk_abc", base_url="gateway.local/v1",
                               trust_config=True)

    def test_check_connection_never_leaks_key(self):
        import os
        from unittest.mock import patch
        fake = "apikey_FAKE1234567890abcdef"
        with patch.dict(os.environ, {}, clear=True):
            bot = DeepSeekPlayer(api_key=fake, trust_config=True)
        bot.base_url = fake          # 绕过纠错，模拟手工注入的脏配置
        bot.provider = "openai"
        out = bot.check_connection()
        self.assertNotIn(fake, out)
        self.assertIn("已隐藏", out)
        self.assertIn("unknown url type", out)

    def test_normal_urls_not_mistaken_for_key(self):
        from deepseek import looks_like_api_key
        for url in ("https://api.typesafe.ai/v1", "https://gw.example.com/v1",
                    "http://127.0.0.1:8000/v1"):
            self.assertFalse(looks_like_api_key(url), url)


class SeatSelectionTests(unittest.TestCase):
    """启动器要能勾选任意座位走模型，CLI 必须支持任意组合。"""

    def test_llm_bots_compat(self):
        import poker
        self.assertEqual(poker.resolve_online_seats(None, 1, 3), {1})
        self.assertEqual(poker.resolve_online_seats(None, 0, 3), set())
        self.assertEqual(poker.resolve_online_seats(None, 3, 3), {1, 2, 3})

    def test_arbitrary_seats(self):
        import poker
        self.assertEqual(poker.resolve_online_seats("1,3,5", 1, 5), {1, 3, 5})
        self.assertEqual(poker.resolve_online_seats(" 2 , 4 ", 1, 5), {2, 4})
        self.assertEqual(poker.resolve_online_seats("3,3", 1, 5), {3})
        self.assertEqual(poker.resolve_online_seats("", 1, 5), set())

    def test_bad_seats_rejected(self):
        import poker
        for spec in ("1,x", "0", "6", "1,9"):
            with self.assertRaises(ValueError):
                poker.resolve_online_seats(spec, 1, 5)


class SeatStyleModelTests(unittest.TestCase):
    """每个座位可以有自己的风格和模型：1=松凶 / 2=模型名。"""

    def test_seat_map_parsing(self):
        import poker
        self.assertEqual(poker.parse_seat_map("1=松凶,3=跟注站", 3, "--seat-styles"),
                         {1: "松凶", 3: "跟注站"})
        self.assertEqual(poker.parse_seat_map("", 3, "--seat-styles"), {})
        self.assertEqual(poker.parse_seat_map(None, 3, "--seat-styles"), {})

    def test_seat_map_rejects_bad_input(self):
        import poker
        for spec in ("1", "0=松凶", "4=松凶", "1="):
            with self.assertRaises(ValueError):
                poker.parse_seat_map(spec, 3, "--seat-styles")

    def test_persona_lookup(self):
        from deepseek import PERSONA_BY_TAG, persona_for
        self.assertIsNone(persona_for(""))
        self.assertIsNone(persona_for(None))
        self.assertEqual(persona_for("松凶"), PERSONA_BY_TAG["松凶"])
        self.assertGreater(persona_for("松凶")["agg"], persona_for("跟注站")["agg"])
        with self.assertRaises(ValueError):
            persona_for("乱写的风格")

    def test_random_style_sampled_without_repeats(self):
        """「随机」= 开局从风格池里抽，尽量一桌不重样，且同 seed 可复现。"""
        import random
        import poker
        from deepseek import PERSONAS

        spec = {1: "随机", 2: "随机", 3: "随机"}
        first = poker.assign_personas(spec, 3, random.Random(1))
        again = poker.assign_personas(spec, 3, random.Random(1))
        self.assertEqual([p["tag"] for p in first.values()],
                         [p["tag"] for p in again.values()])
        self.assertEqual(len({p["tag"] for p in first.values()}), 3)

        fixed = poker.assign_personas({}, 3, random.Random(1))
        # 没指定风格 = 默认随机：一样从池子里抽，且同 seed 可复现
        self.assertEqual([p["tag"] for p in fixed.values()],
                         [p["tag"] for p in poker.assign_personas({}, 3, random.Random(1)).values()])
        self.assertEqual(len({p["tag"] for p in fixed.values()}), 3)

        # 人比风格多时才会重样，但不能因为池子空了而崩
        crowd = poker.assign_personas({k: "随机" for k in range(1, 6)}, 5,
                                      random.Random(2))
        self.assertEqual(sorted(p["tag"] for p in crowd.values()),
                         sorted(p["tag"] for p in PERSONAS))

    def test_style_hidden_until_exit(self):
        """局内输出不出现任何风格名，整场打完退出时才揭晓。"""
        import contextlib
        import io
        import sys
        from unittest.mock import patch

        import poker
        from deepseek import PERSONAS

        calls = {"n": 0}

        def fake_read(prompt, singles, numeric=False):
            calls["n"] += 1
            self.assertLess(calls["n"], 400)
            if "下一局" in prompt:
                return ("n", "") if calls["n"] < 8 else ("q", "")
            if "f弃" in prompt:
                return "f", ""
            return "q", ""

        argv = ["poker.py", "--bots", "3", "--offline", "--seat-styles", "1=随机",
                "--sims", "12", "--seed", "5", "--memory", "0"]
        out = io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(poker, "read_cmd", fake_read), \
                contextlib.redirect_stdout(out):
            self.assertEqual(poker.main(), 0)
        text = out.getvalue()
        playing, reveal = text.split("风格揭晓", 1)
        for persona in PERSONAS:
            self.assertNotIn(persona["tag"], playing)
        self.assertIn("机器1 本地", playing)      # 座位行只剩名字 + 来源
        self.assertTrue(any(p["tag"] in reveal for p in PERSONAS))


class TournamentTests(unittest.TestCase):
    """淘汰赛：筹码见底的座位出局，一直打到只剩一个人。"""

    def _players(self, *stacks):
        return [Player(f"座位{i}", s) for i, s in enumerate(stacks)]

    def test_busted_seat_is_out_and_ranked(self):
        import poker
        players = self._players(1000, 0, 500, 300)
        left, gone = poker.knockout(players, [0, 1, 2, 3])
        self.assertEqual(left, [0, 2, 3])
        self.assertEqual(gone, [(1, 4)])          # 4 人桌第一个出局 = 第 4 名

    def test_nobody_busted_keeps_table(self):
        import poker
        players = self._players(10, 10)
        left, gone = poker.knockout(players, [0, 1])
        self.assertEqual(left, [0, 1])
        self.assertEqual(gone, [])

    def test_simultaneous_busts_share_rank(self):
        import poker
        players = self._players(0, 0, 800)
        left, gone = poker.knockout(players, [0, 1, 2])
        self.assertEqual(left, [2])
        self.assertEqual(gone, [(0, 2), (1, 2)])  # 同时出局，名次相同

    def test_button_skips_eliminated_seat(self):
        import poker
        self.assertEqual(poker.advance_button([0, 2, 3], 0, 4), 2)
        self.assertEqual(poker.advance_button([0, 2, 3], 3, 4), 0)
        # 按钮位本人被淘汰：顺延到他后面第一个还在的座位
        self.assertEqual(poker.advance_button([0, 3], 2, 4), 3)
        self.assertEqual(poker.advance_button([2], 0, 4), 2)

    def test_blind_level_doubles_every_n_hands(self):
        import argparse
        import poker
        args = argparse.Namespace(sb=10, bb=20, blind_up=3)
        self.assertEqual(poker.blind_level(args, 1), (10, 20))
        self.assertEqual(poker.blind_level(args, 3), (10, 20))
        self.assertEqual(poker.blind_level(args, 4), (20, 40))
        self.assertEqual(poker.blind_level(args, 7), (40, 80))
        args.blind_up = 0
        self.assertEqual(poker.blind_level(args, 99), (10, 20))

    def test_offline_tournament_ends_with_one_player_left(self):
        """端到端：真人每手弃牌，机器人互殴，淘汰赛必须收敛到只剩一个人或真人出局。"""
        import contextlib
        import io
        import sys
        import poker

        calls = {"n": 0}

        def fake_read(prompt, singles, numeric=False):
            calls["n"] += 1
            self.assertLess(calls["n"], 3000, "淘汰赛没有收敛，疑似死循环")
            if "下一局" in prompt:
                return "n", ""
            if "f弃" in prompt:
                return "f", ""
            return "q", ""                      # 只剩复盘/导出/退出

        argv = ["poker.py", "--bots", "3", "--offline", "--mode", "tournament",
                "--blind-up", "2", "--stack", "300", "--sb", "10", "--bb", "20",
                "--sims", "12", "--seed", "3", "--no-reasons", "--memory", "0"]
        out = io.StringIO()
        from unittest.mock import patch
        with patch.object(sys, "argv", argv), patch.object(poker, "read_cmd", fake_read):
            with contextlib.redirect_stdout(out):
                self.assertEqual(poker.main(), 0)
        text = out.getvalue()
        self.assertIn("淘汰赛", text)
        self.assertIn("出局（第 ", text)
        self.assertIn("名）", text)


class ConnectionFeedbackTests(unittest.TestCase):
    """「到底连没连上」必须在界面上一眼可查，不能只靠 --check。"""

    def _bot(self, provider="jev"):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            return DeepSeekPlayer(api_key="apikey_demo", trust_config=True,
                                  provider=provider)

    def test_link_line_names_protocol_and_latency(self):
        import poker
        bot = self._bot()
        line = poker.link_line(bot, {"latency_ms": 312, "valid": True})
        self.assertIn("连通确认", line)
        self.assertIn("Jev", line)
        self.assertIn("312ms", line)
        self.assertIn("/v1/systemone", line)
        self.assertIn("概率/置信", line)

    def test_link_line_for_chat_gateway(self):
        import os
        import poker
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            bot = DeepSeekPlayer(api_key="sk_abc", base_url="https://gw.example.com/v1",
                                 model="deepseek-chat", trust_config=True)
        line = poker.link_line(bot, {"latency_ms": 95})
        self.assertIn("/chat/completions", line)
        self.assertIn("LLM", line)

    def test_reason_lines_mark_per_decision_source(self):
        import poker
        bot = self._bot()
        bot.name, bot.tag = "机器1", "稳健"
        bot.start_hand(1)
        bot.hand_log.extend([
            {"hand_no": 1, "street_cn": "翻前", "action_text": "跟注 10",
             "reason": "Jev 选 跟注 · 概率 48%", "valid": True},
            {"hand_no": 1, "street_cn": "翻牌", "action_text": "过牌",
             "reason": "估胜率 12%，免费看下一张", "valid": False},
        ])
        out = "\n".join(poker.reason_lines({1: bot}, 1))
        self.assertIn("（Jev·1步降级）", out)
        self.assertIn("Jev 选 跟注", out)
        self.assertIn("[降级本地]", out)
        self.assertEqual(out.count("[降级本地]"), 1)

    def test_offline_seat_has_no_fallback_suffix(self):
        import os
        import poker
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            bot = DeepSeekPlayer(offline=True, tag="激进")
        bot.name = "机器2"
        bot.start_hand(1)
        bot.hand_log.append({"hand_no": 1, "street_cn": "翻前", "action_text": "过牌",
                             "reason": "估胜率 27%，免费看下一张", "valid": False})
        out = "\n".join(poker.reason_lines({2: bot}, 1))
        self.assertIn("（本地）", out)
        self.assertNotIn("[降级本地]", out)


class ChatGatewayTests(unittest.TestCase):
    """DeepSeek V4 系列默认开思考，正文可能空着回来：
    这类「看着连上了、其实每次都在降级」的坑必须能被测出来。"""

    def _bot(self, thinking=False):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            return DeepSeekPlayer(api_key="sk-test-1234567890",
                                  base_url="https://gw.example.com/v1",
                                  model="deepseek-flash", trust_config=True,
                                  provider="openai", offline=False, thinking=thinking)

    def _patch(self, responder):
        """responder(body) -> dict（JSON 响应）或抛 HTTPError。"""
        from unittest.mock import patch
        import deepseek

        class _Resp:
            def __init__(self, obj):
                self._data = json.dumps(obj).encode("utf-8")

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake(req, timeout=None):
            body = json.loads(req.data.decode("utf-8"))
            return _Resp(responder(body))

        return patch.object(deepseek.urllib.request, "urlopen", fake)

    def test_thinking_disabled_by_default(self):
        """默认必须显式关掉思考，不然 300 token 会被思考内容吃光。"""
        seen = {}

        def responder(body):
            seen.update(body)
            return {"choices": [{"message": {"content": '{"action":"call","amount":null,'
                                                       '"reason":"赔率合适"}'},
                                 "finish_reason": "stop"}]}

        bot = self._bot()
        with self._patch(responder):
            self.assertIn("call", bot._chat("hi"))
        self.assertEqual(seen["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", seen)
        self.assertGreaterEqual(seen["max_tokens"], 600)

    def test_thinking_mode_opt_in(self):
        seen = {}

        def responder(body):
            seen.update(body)
            return {"choices": [{"message": {"content": '{"action":"check","amount":null,'
                                                       '"reason":"免费看牌"}'},
                                 "finish_reason": "stop"}]}

        bot = self._bot(thinking=True)
        with self._patch(responder):
            bot._chat("hi")
        self.assertEqual(seen["thinking"], {"type": "enabled"})
        self.assertEqual(seen["reasoning_effort"], "low")

    def test_empty_content_with_reasoning_is_explained(self):
        """content 空、reasoning_content 有值 = 思考把预算吃光，要说人话。"""
        import deepseek
        payload = {"choices": [{"message": {"content": "", "reasoning_content": "让我想想…"},
                                "finish_reason": "length"}]}
        with self.assertRaises(RuntimeError) as ctx:
            deepseek._content_of(payload, thinking=False)
        self.assertIn("思考内容", str(ctx.exception))
        self.assertIn("length", str(ctx.exception))

    def test_http_error_message_is_surfaced(self):
        """400 不能只说 Bad Request：网关给的 error.message 要原样带出来。"""
        import io
        import urllib.error

        def responder(_body):
            raise urllib.error.HTTPError(
                "https://gw.example.com/v1/chat/completions", 400, "Bad Request", {},
                io.BytesIO(json.dumps(
                    {"error": {"message": "Model Not Exist"}}).encode("utf-8")))

        bot = self._bot()
        with self._patch(responder):
            with self.assertRaises(RuntimeError) as ctx:
                bot._chat("hi")
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("Model Not Exist", str(ctx.exception))

    def test_truncated_json_salvaged(self):
        """被 max_tokens 截断的 JSON 也尽量救回来，别整手都丢给本地 AI。"""
        from deepseek import _salvage_action
        dec = _salvage_action('{"action": "raise", "amount": 120, "reason": "施压')
        self.assertEqual(dec["action"], "raise")
        self.assertEqual(dec["amount"], 120)
        self.assertEqual(_salvage_action("完全不是 json"), None)

    def test_decide_records_fallback_reason(self):
        """降级时 fallback_note 要能说明白为什么，别只写「不是合法动作」。"""
        from engine import HoldemHand, Player
        players = [Player("你", 1000), Player("机器1", 1000)]
        hand = HoldemHand(players, 0, 10, 20, random.Random(1), 1)
        snap = hand.snapshot(1)

        bot = self._bot()
        bot.name = "机器1"
        bot.start_hand(1)
        payload = {"choices": [{"message": {"content": "", "reasoning_content": "想"},
                                "finish_reason": "length"}]}
        with self._patch(lambda _b: payload):
            action, entry = bot.decide(snap)
        self.assertFalse(entry["valid"])
        self.assertIn("思考内容", entry["fallback_note"])
        self.assertIn(entry["action"]["kind"], ("fold", "call", "check", "raise", "allin"))


class LauncherUiTests(unittest.TestCase):
    """XP 启动器：命令拼装 / 座位联动 / 环境变量不泄漏（无 PyQt5 时跳过）。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        import os
        from importlib.machinery import SourceFileLoader
        try:
            import PyQt5.QtWidgets  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("未安装 PyQt5，跳过 GUI 测试")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launcher.pyw")
        spec = importlib.util.spec_from_loader(
            "launcher_ui", SourceFileLoader("launcher_ui", path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.ui = mod

    def setUp(self):
        self.w = self.ui.Launcher()
        self.w.saved = {}          # 屏蔽本地已保存配置，保证测试可复现
        self.w.key.setText("apikey_fake_for_test")

    @staticmethod
    def _value(args, flag):
        return args[args.index(flag) + 1]

    def _online(self):
        """默认是纯本地模式；在线相关用例显式打开开关，保持用例本意。"""
        self.w.enable.setChecked(True)
        self.w.refresh()
        return self.w

    def test_default_command(self):
        """开箱默认：5 人桌 + 淘汰赛 + 纯本地 AI（不联网、不花钱）。"""
        args, _env, warn = self.w.collect()
        self.assertEqual(self._value(args, "--bots"), "5")
        self.assertEqual(self._value(args, "--mode"), "tournament")
        self.assertEqual(self._value(args, "--blind-up"), "10")
        self.assertIn("--offline", args)
        self.assertNotIn("--llm-seats", args)
        self.assertIn("全部座位走本地 AI", warn)

    def test_memory_window_arg(self):
        """启动器必须显式传记忆窗口：默认 10，调成 0 就是关闭。"""
        args, _env, _warn = self.w.collect()
        self.assertEqual(self._value(args, "--memory"), "10")
        self.w.memory.setValue(0)
        args, _env, _warn = self.w.collect()
        self.assertEqual(self._value(args, "--memory"), "0")

    def test_rect_controls_keep_xp_shape(self):
        """用户要求：窗口/控件保持 Windows XP 的矩形形态，霓虹只体现在配色和边框。"""
        self.assertEqual(self.ui.FRAME_RADIUS, 0.0)
        self.assertNotIn("border-radius", self.ui.QSS)

    def test_tournament_mode_args(self):
        """默认就是淘汰赛（带涨盲）；切成现金局才不传 --mode / --blind-up。"""
        args, _env, _warn = self.w.collect()
        self.assertEqual(self._value(args, "--mode"), "tournament")
        self.assertEqual(self._value(args, "--blind-up"), "10")

        self.w.mode.setCurrentIndex(1)          # 切到现金局
        args, _env, warn = self.w.collect()
        self.assertNotIn("--mode", args)
        self.assertNotIn("--blind-up", args)
        self.assertNotIn("淘汰赛", warn)

        self.w.blind_up.setValue(0)          # 0 = 不涨盲，别传上去
        args, _env, _warn = self.w.collect()
        self.assertNotIn("--blind-up", args)

    def test_seat_style_and_model_args(self):
        """每个座位单独选风格/模型，只把非默认的座位写进命令行。"""
        self._online()
        self.w.seat_styles[0].setCurrentIndex(self.ui.STYLE_TAGS.index("松凶"))
        self.w.seat_styles[2].setCurrentIndex(self.ui.STYLE_TAGS.index("跟注站"))
        self.w.seat_models[0].setCurrentText("jev-latest")
        args, _env, _warn = self.w.collect()
        self.assertEqual(self._value(args, "--seat-styles"), "1=松凶,3=跟注站")
        self.assertEqual(self._value(args, "--seat-models"), "1=jev-latest")

        for combo in self.w.seat_styles:      # 全默认时不该出现多余参数
            combo.setCurrentIndex(0)
        self.w.seat_models[0].setCurrentText(self.ui.SEAT_FOLLOW)
        args, _env, _warn = self.w.collect()
        self.assertNotIn("--seat-styles", args)
        self.assertNotIn("--seat-models", args)

    def test_seat_style_tags_match_backend(self):
        """界面上的风格名必须和后端 PERSONAS 完全一致，否则 CLI 会报错退出。"""
        from deepseek import PERSONAS, STYLE_RANDOM
        self.assertEqual(self.ui.STYLE_TAGS[0], STYLE_RANDOM)      # 默认就是「随机」
        self.assertEqual(self.ui.STYLE_TAGS[1:], [p["tag"] for p in PERSONAS])
        self.assertEqual(len(self.ui.STYLE_LABELS), len(self.ui.STYLE_TAGS))

    def test_random_is_default_and_stays_implicit(self):
        """每个座位默认「随机」，且随机不写进命令行（后端默认就是随机抽签）。"""
        from deepseek import STYLE_RANDOM
        for combo in self.w.seat_styles:
            self.assertEqual(combo.currentIndex(), 0)
            self.assertEqual(self.ui.STYLE_TAGS[combo.currentIndex()], STYLE_RANDOM)
        args, _env, _warn = self.w.collect()
        self.assertNotIn("--seat-styles", args)
        self.w.seat_styles[1].setCurrentIndex(self.ui.STYLE_TAGS.index("跟注站"))
        args, _env, _warn = self.w.collect()
        self.assertEqual(self._value(args, "--seat-styles"), "2=跟注站")

    def test_fixed_style_reaches_command_line(self):
        """写死的风格要传给 --seat-styles；随机是默认值，不必写。"""
        self.w.seat_styles[0].setCurrentIndex(self.ui.STYLE_TAGS.index("松凶"))
        args, _env, _warn = self.w.collect()
        self.assertEqual(self._value(args, "--seat-styles"), "1=松凶")

    def test_dead_model_name_is_flagged(self):
        """deepseek-chat/reasoner 已于 2026-07-24 下线，界面要当场提示改 deepseek-flash。"""
        self._online()
        self.w.key.setText("sk-abcdef123456")
        self.w.base.setText("https://api.deepseek.com")
        self.w.model.setText("deepseek-chat")
        _args, env, warn = self.w.collect()
        self.assertEqual(env.get("JEV_MODEL"), "deepseek-chat")   # 不擅自改用户输入
        self.assertIn("下线", warn)
        self.assertIn("deepseek-flash", warn)

        self.w.model.setText("deepseek-flash")                    # 现行名字不该报警
        _args, _env, warn = self.w.collect()
        self.assertNotIn("下线", warn)

    def test_arrow_assets_substituted(self):
        """QSS 里的箭头/对勾占位符必须替换成真实图片路径：
        Fusion 自带的箭头在白色 QSS 下会被染白，浅色输入框里根本看不见。"""
        qss = self.ui.styled_qss()
        for token in ("@UP@", "@DOWN@", "@CHECK@"):
            self.assertNotIn(token, qss)
        self.assertIn(".png", qss)

    def test_neon_flow_animates_only_when_visible(self):
        """霓虹流水灯只在窗口可见时空转，隐藏后不能白烧 CPU。"""
        self.w._phase = 0.4
        self.w._tick()
        self.assertEqual(self.w._phase, 0.4)
        self.w.show()
        self.w._tick()
        self.assertNotEqual(self.w._phase, 0.4)
        self.w.hide()

    def test_any_seat_subset(self):
        w = self._online()
        w.bots.setValue(5)
        for cb in w.seat_boxes:
            cb.setChecked(False)
        w.seat_boxes[1].setChecked(True)
        w.seat_boxes[3].setChecked(True)
        args, _env, warn = self.w.collect()
        self.assertEqual(self._value(args, "--bots"), "5")
        self.assertEqual(self._value(args, "--llm-seats"), "2,4")
        self.assertIn("其余 3 个本地 AI", warn)

    def test_shrinking_keeps_one_online_seat(self):
        w = self._online()
        w.bots.setValue(5)
        for cb in w.seat_boxes:
            cb.setChecked(False)
        w.seat_boxes[3].setChecked(True)
        w.seat_boxes[4].setChecked(True)
        w.bots.setValue(2)          # 原来勾选的 4、5 号位被裁掉
        seats = [i + 1 for i, cb in enumerate(w.seat_boxes) if cb.isChecked()]
        self.assertEqual(len(seats), 1)
        self.assertLessEqual(seats[0], 2)
        args, _env, _warn = w.collect()
        self.assertEqual(self._value(args, "--llm-seats"), str(seats[0]))

    def test_offline_flags_and_clean_env(self):
        from unittest.mock import patch
        w = self.w
        w.enable.setChecked(False)
        w.cards.setCurrentIndex(1)       # ascii
        w.reasons.setChecked(False)
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "should-not-leak"},
                        clear=False):
            args, env, warn = w.collect()
        self.assertIn("--offline", args)
        self.assertNotIn("--llm-seats", args)
        self.assertEqual(self._value(args, "--cards"), "ascii")
        self.assertIn("--no-reasons", args)
        self.assertIn("不产生 API 费用", warn)
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        for k in env:
            self.assertFalse(k.startswith(("JEV_", "POKER_", "DEEPSEEK_")))

    def test_provider_numbers_and_key_only_in_env(self):
        w = self._online()
        w.provider.setCurrentIndex(2)   # openai
        w.key.setText("sk-fake-for-test")   # 聊天网关的 key，不是 Jev 前缀
        w.sb.setText("5")
        w.bb.setText("10")
        w.stack.setText("500")
        args, env, warn = w.collect()
        self.assertEqual(self._value(args, "--provider"), "openai")
        self.assertEqual(self._value(args, "--sb"), "5")
        self.assertEqual(self._value(args, "--bb"), "10")
        self.assertEqual(self._value(args, "--stack"), "500")
        self.assertIn("需要填 API 地址", warn)
        self.assertEqual(env["JEV_API_KEY"], "sk-fake-for-test")
        self.assertNotIn("sk-fake-for-test", " ".join(args))

    def test_powershell_entry(self):
        cmd = self.ui.Launcher.powershell_cmd(["--bots", "2"])
        self.assertEqual(cmd[0], "powershell")
        self.assertIn("-File", cmd)
        self.assertEqual(cmd[cmd.index("-File") + 1], self.ui.PLAY_PS1)
        self.assertEqual(cmd[-2:], ["--bots", "2"])

    def test_key_pasted_into_api_address_is_corrected(self):
        w = self._online()
        fake = "apikey_FAKE1234567890abcdef"
        w.key.setText("")
        w.base.setText(fake)
        w.model.setText("deepseek-chat")
        w.provider.setCurrentIndex(2)     # 故意选聊天网关，纠错后应回到自动识别
        args, env, warn = w.collect()
        self.assertEqual(env.get("JEV_API_KEY"), fake)
        self.assertNotIn("JEV_BASE_URL", env)
        self.assertNotIn("JEV_MODEL", env)
        self.assertNotIn("--provider", args)
        self.assertIn("自动纠正", warn)
        # 界面上要真的搬走：地址框清空、Key 进密码框、红框提示撤掉
        self.assertEqual(w.base.text(), "")
        self.assertEqual(w.key.text(), fake)
        self.assertEqual(w.base.styleSheet(), "")

    def test_valid_api_address_clears_warning(self):
        w = self._online()
        w.base.setText("https://api.typesafe.ai/v1")
        _args, env, warn = w.collect()
        self.assertEqual(env.get("JEV_BASE_URL"), "https://api.typesafe.ai/v1")
        self.assertNotIn("自动纠正", warn)
        self.assertEqual(w.base.styleSheet(), "")

    def test_game_cmd_prefers_bundled_exe(self):
        """分享包里优先跑 PokerGame.exe，源码模式下才回落到 play.ps1。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = os.path.join(tmp, "PokerGame.exe")
            open(fake_exe, "wb").close()
            old = self.ui.GAME_EXE
            try:
                self.ui.GAME_EXE = fake_exe
                cmd = self.ui.Launcher.game_cmd(["--bots", "2"])
                self.assertEqual(cmd[0], fake_exe)
                self.assertEqual(cmd[1:], ["--bots", "2"])
                # 没有 exe 时回落到 PowerShell + play.ps1
                self.ui.GAME_EXE = os.path.join(tmp, "missing.exe")
                self.assertEqual(self.ui.Launcher.game_cmd(["x"])[0], "powershell")
            finally:
                self.ui.GAME_EXE = old

    def test_config_template_has_no_key(self):
        """分享用的模板必须留空 Key，避免把自己的 key 发出去。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "poker-config.sample.bat")
        text = open(path, encoding="utf-8", errors="ignore").read()
        self.assertIn("JEV_API_KEY", text)
        for line in text.splitlines():
            if "JEV_API_KEY" in line and line.strip().upper().startswith("SET"):
                value = line.split("=", 1)[1].strip().strip('"')
                self.assertEqual(value, "")


class MemoryTests(unittest.TestCase):
    """跨手记忆：统计口径、滚动窗口、落盘、以及与两个决策后端的对接。"""

    @staticmethod
    def stub(history, payouts, hands=None, winners=None, bets=(0, 0)):
        players = [Player("你", 1000), Player("机器1", 1000)]
        for p, b in zip(players, bets):
            p.total_bet = b
        result = HandResult([], payouts, hands or {}, "fold", winners or [])

        class _Stub:
            pass

        s = _Stub()
        s.history = history
        s.players = players
        s.result = result
        return s

    def test_vpip_pfr_and_fold_to_bet(self):
        history = [
            {"street": "preflop", "idx": 0, "kind": "raise"},
            {"street": "preflop", "idx": 1, "kind": "call"},
            {"street": "flop", "idx": 1, "kind": "check"},
            {"street": "flop", "idx": 0, "kind": "raise"},
            {"street": "flop", "idx": 1, "kind": "fold"},
        ]
        hand = self.stub(history, {0: 40, 1: 0}, bets=(20, 20))
        hero = hand_stats(hand, 0)
        bot = hand_stats(hand, 1)
        self.assertTrue(hero["vpip"])
        self.assertTrue(hero["pfr"])
        self.assertEqual((hero["aggr"], hero["calls"]), (2, 0))
        self.assertTrue(bot["vpip"])
        self.assertFalse(bot["pfr"])
        self.assertEqual(bot["calls"], 1)
        self.assertEqual(bot["folds"], 1)
        # 只有「本街此前有人下注」才算面对下注：翻前跟加注 + 翻牌弃给下注
        self.assertEqual(bot["faced"], 2)
        self.assertEqual(bot["folded_to_bet"], 1)
        self.assertEqual(bot["seq"], "CKF")

    def test_blind_check_is_not_vpip(self):
        """大盲免费看牌不算主动入池，否则统计全被拉高。"""
        hand = self.stub([{"street": "preflop", "idx": 1, "kind": "check"}],
                         {1: 20}, bets=(10, 20))
        self.assertFalse(hand_stats(hand, 1)["vpip"])

    def test_window_rolls_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mem.json")
            mem = OpponentMemory(2, path)
            for _ in range(3):
                mem.record(self.stub([{"street": "preflop", "idx": 0, "kind": "call"}],
                                     {0: 20}, bets=(20, 0)))
            self.assertEqual(len(mem), 2)             # 只留最近 2 局
            self.assertEqual(mem.profile("你")["hands"], 2)
            reloaded = OpponentMemory(2, path)        # 重开一局仍记得
            self.assertEqual(len(reloaded), 2)
            self.assertEqual(reloaded.profile("你")["vpip"], 1.0)

    def test_zero_window_disables_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mem.json")
            mem = OpponentMemory(0, path)
            self.assertFalse(mem.enabled)
            self.assertFalse(mem.record(self.stub([], {0: 20}, bets=(20, 0))))
            self.assertEqual(len(mem), 0)
            self.assertFalse(os.path.exists(path))    # 关闭时连文件都不建

    def test_corrupt_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mem.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ broken json")
            mem = OpponentMemory(5, path)
            self.assertEqual(len(mem), 0)
            self.assertIsNone(mem.profile("你"))

    def test_style_label(self):
        self.assertEqual(style_label(0.5, 0.3, 10), "松凶")
        self.assertEqual(style_label(0.5, 0.05, 10), "松被动")
        self.assertEqual(style_label(0.15, 0.22, 10), "紧凶")
        self.assertEqual(style_label(0.0, 0.0, 10), "紧被动")
        self.assertEqual(style_label(0.0, 0.0, 1), "样本少")

    def test_read_adjust_signs(self):
        tight = {"机器1": {"hands": 10, "vpip": 0.15, "pfr": 0.12, "fold_to_bet": 0.7}}
        loose = {"机器1": {"hands": 10, "vpip": 0.55, "pfr": 0.10, "fold_to_bet": 0.2}}
        m_tight, bluff_tight, note_tight = read_adjust(tight, ["机器1"])
        m_loose, bluff_loose, note_loose = read_adjust(loose, ["机器1"])
        self.assertGreater(m_tight, 0)            # 对手紧 -> 跟注更谨慎
        self.assertGreater(bluff_tight, 0)        # 对手爱弃 -> 多偷
        self.assertLess(m_loose, 0)               # 对手松 -> 跟注门槛降低
        self.assertLess(bluff_loose, 0)           # 跟注站 -> 少偷
        self.assertIn("紧", note_tight)
        self.assertIn("松", note_loose)
        # 样本太少时不做任何调整
        self.assertEqual(read_adjust({"机器1": {"hands": 1, "vpip": 0.9,
                                                "pfr": 0.9, "fold_to_bet": 0.1}}),
                         (0.0, 0.0, ""))

    def test_jev_state_carries_reads(self):
        players = [Player("你", 1000), Player("机器1", 1000)]
        hand = HoldemHand(players, button=0, sb=10, bb=20)
        snap = hand.snapshot(0)
        reads = {"机器1": {"hands": 8, "vpip": 0.5, "pfr": 0.25, "af": 1.8,
                           "fold_to_bet": 0.3, "showdown": 2, "won": 3,
                           "net": 40, "style": "松凶", "recent": ["RC"]}}
        state = jev.build_state(snap, "稳健", "你", reads)
        self.assertEqual(state["opponents"][0]["reads"]["style"], "松凶")
        self.assertIn("reads_note", state)        # 有画像才附口径说明
        plain = jev.build_state(snap, "稳健", "你")
        self.assertNotIn("reads", plain["opponents"][0])
        self.assertNotIn("reads_note", plain)

    def test_heuristic_uses_reads(self):
        players = [Player("你", 1000), Player("机器1", 1000)]
        hand = HoldemHand(players, button=0, sb=10, bb=20)
        snap = hand.snapshot(0)
        reads = {"机器1": {"hands": 10, "vpip": 0.55, "pfr": 0.12,
                           "fold_to_bet": 0.2, "style": "松被动"}}
        _act, reason = heuristic_decide(snap, random.Random(0), 40, 0.0, reads)
        self.assertIn("读牌", reason)
        _act, plain = heuristic_decide(snap, random.Random(0), 40, 0.0, None)
        self.assertNotIn("读牌", plain)

    def test_heuristic_unchanged_without_memory(self):
        """没有画像时，兜底 AI 必须和加记忆之前一模一样（种子相同则动作相同）。"""
        players = [Player("你", 1000), Player("机器1", 1000)]
        hand = HoldemHand(players, button=0, sb=10, bb=20)
        snap = hand.snapshot(0)
        a1, r1 = heuristic_decide(snap, random.Random(7), 40, 0.0, None)
        a2, r2 = heuristic_decide(snap, random.Random(7), 40, 0.0, {})
        self.assertEqual((a1.kind, a1.amount, r1), (a2.kind, a2.amount, r2))


class BluffTests(unittest.TestCase):
    """本地兜底 AI 不能「一下注就说明牌好」：要有诈唬、半诈唬和陷阱。"""

    @staticmethod
    def spot(hole: str, board: str, to_call: int = 0, pot: int = 120,
             stack: int = 1000, n_opp: int = 1):
        board_n = len(board.split()) if board.strip() else 0
        street = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}[board_n]
        lo = min(to_call + 20, stack)
        return {
            "hand_no": 1, "street": street, "street_cn": street,
            "board": h(board) if board.strip() else [], "hole": h(hole),
            "pot": pot, "current_bet": to_call, "to_call": to_call,
            "my_stack": stack, "my_round_bet": 0, "blinds": (10, 20),
            "position": "CO", "n_players": n_opp + 1, "opponent_count": n_opp,
            "is_button": False,
            "opponents": [{"name": f"机器{i+1}", "stack": stack, "round_bet": to_call,
                           "folded": i >= n_opp, "all_in": False} for i in range(n_opp)],
            "legal": {"fold": True, "check": to_call == 0, "call": to_call,
                      "to_call": to_call, "raise_to": (lo, stack) if stack > to_call else None,
                      "allin": stack, "current_bet": to_call, "pot": pot},
            "history": [],
        }

    def test_draw_strength(self):
        self.assertAlmostEqual(draw_strength(h("As Ks"), h("Qs 7s 2h")), 0.85)   # 同花听
        self.assertAlmostEqual(draw_strength(h("9h 8d"), h("Ts 7c 2h")), 0.65)   # 两头顺
        self.assertEqual(draw_strength(h("As Kd"), h("Qh 7s 2c")), 0.0)          # 没听牌
        self.assertEqual(draw_strength(h("9h 8d"), h("Ts 7c 2h 3s 4d")), 0.0)    # 河牌无听牌
        self.assertEqual(draw_strength(h("As Ks"), []), 0.0)                     # 翻前无听牌

    def test_bluff_rate_follows_style(self):
        tags = {p["tag"]: p["agg"] for p in PERSONAS}
        self.assertGreater(bluff_rate(tags["松凶"]), bluff_rate(tags["稳健"]))
        self.assertGreater(bluff_rate(tags["激进"]), bluff_rate(tags["跟注站"]))
        self.assertEqual(bluff_rate(tags["松凶"], 0.0, 0.0), 0.03)   # 倍率 0 也不能完全不打
        self.assertGreater(bluff_rate(tags["稳健"], 0.12), bluff_rate(tags["稳健"]))

    def test_sized_raise_stays_legal(self):
        legal = {"current_bet": 40, "to_call": 40, "pot": 200, "raise_to": (60, 300)}
        for frac in (0.1, 0.5, 1.0, 5.0):
            amount = sized_raise(legal, frac)
            self.assertGreaterEqual(amount, 60)
            self.assertLessEqual(amount, 300)
        self.assertIsNone(sized_raise({"raise_to": None, "current_bet": 0,
                                       "to_call": 0, "pot": 10}, 0.5))

    def _count(self, snap, trials=240, aggression=0.10):
        kinds = {"raise": 0, "check": 0, "call": 0, "fold": 0}
        for seed in range(trials):
            act, _ = heuristic_decide(snap, random.Random(seed), 30, aggression,
                                      None, {"bluff": False}, 1.0)
            kinds[act.kind] += 1
        return kinds

    def test_weak_hand_sometimes_bets(self):
        """72o 在 A K 9 的牌面上基本没戏，但也要偶尔偷——否则下注=好牌。"""
        snap = self.spot("7d 2c", "Ah Kd 9s")
        kinds = self._count(snap)
        self.assertGreater(kinds["raise"], 0, "弱牌从来不偷，一眼就被看穿")
        self.assertGreater(kinds["check"], kinds["raise"], "偷太频繁就变成疯子了")

    def test_strong_hand_sometimes_checks(self):
        """三条 A 也要偶尔过牌设陷阱，让「下注=有牌」不成立。"""
        snap = self.spot("As Ah", "Ad 7c 2s")
        kinds = self._count(snap, aggression=-0.03)
        self.assertGreater(kinds["check"], 0, "强牌永远下注，等于把牌写给对手看")
        self.assertGreater(kinds["raise"], kinds["check"], "陷阱是点缀，不是主旋律")

    def test_draw_raises_into_a_bet(self):
        """同花听面对下注时要会半诈唬加注，而不是只会弃/跟。"""
        snap = self.spot("Ks Qs", "Js 8s 2h", to_call=40, pot=160, n_opp=1)
        kinds = self._count(snap, aggression=0.10)
        self.assertGreater(kinds["raise"], 0)
        self.assertGreater(kinds["raise"] + kinds["call"], 0)

    def test_bluff_plan_carries_to_next_street(self):
        """上一街诈唬过，下一街继续开第二枪的概率应该明显更高。"""
        snap = self.spot("7d 2c", "Ah Kd 9s 3c")
        cold = self._count(snap, trials=200)
        warm = {"raise": 0, "check": 0, "call": 0, "fold": 0}
        for seed in range(200):
            act, _ = heuristic_decide(snap, random.Random(seed), 30, 0.10, None,
                                      {"bluff": True}, 1.0)
            warm[act.kind] += 1
        self.assertGreater(warm["raise"], cold["raise"])


class HiddenInfoTests(unittest.TestCase):
    """机器人不能看到别人的底牌——快照、提示词、Jev state 三个出口都不能漏。"""

    def test_bot_never_sees_opponent_hole_cards(self):
        players = [Player(n, 1000) for n in ("你", "机器1", "机器2", "机器3")]
        hand = HoldemHand(players, button=0, sb=10, bb=20)     # 构造即发牌
        codes = [[c.code for c in p.hole] for p in players]

        for seat in range(4):
            snap = hand.snapshot(seat)
            others = [c for i, hh in enumerate(codes) if i != seat for c in hh]
            payloads = {
                "snapshot": json.dumps(snap, ensure_ascii=False, default=str),
                "prompt": build_prompt(snap),
                "jev_state": json.dumps(jev.build_state(snap, "稳健", f"座位{seat}"),
                                        ensure_ascii=False, default=str),
            }
            # 自己的底牌当然要在（不然没牌可打）
            for code in codes[seat]:
                self.assertIn(code, payloads["prompt"])
            # 别人的底牌一张都不能出现
            for where, text in payloads.items():
                for code in others:
                    self.assertNotIn(code, text, f"{where} 泄漏了别人的底牌 {code}")
            # 对手对象里只允许公开字段
            self.assertEqual(set(snap["opponents"][0]),
                             {"name", "stack", "round_bet", "folded", "all_in"})
            self.assertNotIn("hole", json.dumps(snap["opponents"], default=str))


class RestartTests(unittest.TestCase):
    """局末按 0 重开一场：配置不变，筹码/座位/复盘归零，随机座位重新抽风格。"""

    def _table(self, n: int = 3, stack: int = 1000):
        from deepseek import DeepSeekPlayer
        players = [Player("你", stack)] + [Player(f"机器{k}", stack) for k in range(1, n + 1)]
        bots = {}
        for k in range(1, n + 1):
            b = DeepSeekPlayer(offline=True, sims=4, trust_config=True)
            b.name = players[k].name
            bots[k] = b
        return players, bots

    def test_zero_key_means_restart(self):
        import poker
        for raw in ("0", "重开", "重来", "重新开始", "restart", "Reset"):
            self.assertEqual(poker.parse_line(raw)[0], "0")
        self.assertEqual(poker.parse_line("n")[0], "n")      # 下一局照旧

    def test_restart_resets_stacks_and_reviews(self):
        import poker
        players, bots = self._table()
        players[0].stack = 0                                  # 被打空的座位也要能回桌
        players[1].stack = 120
        players[2].hole = h("As Ah")
        bots[2].logs.append({"hand_no": 1, "decisions": []})
        bots[1].stats["api"] = 9

        active = poker.restart_table(players, bots, 800, 3)
        self.assertEqual(active, [0, 1, 2, 3])
        self.assertEqual([p.stack for p in players], [800] * 4)
        self.assertEqual(players[2].hole, [])                 # 手牌不会带进新的一场
        self.assertEqual(bots[2].logs, [])
        self.assertEqual(bots[1].stats["api"], 0)

    def test_reroll_changes_random_seats_only(self):
        import random
        import poker
        from deepseek import PERSONA_BY_TAG

        players, bots = self._table()
        bots[1].apply_persona(PERSONA_BY_TAG["松凶"])         # 写死的风格
        bots[2].apply_persona(PERSONA_BY_TAG["稳健"])         # 随机抽到的
        bots[3].apply_persona(PERSONA_BY_TAG["激进"])
        changed = poker.reroll_random_styles(
            bots, {1: "松凶", 2: "随机", 3: "随机"}, random.Random(7))

        self.assertEqual(sorted(changed), [2, 3])
        self.assertEqual(bots[1].tag, "松凶")                  # 指定风格的座位不动
        self.assertNotEqual(bots[2].tag, "稳健")               # 随机座位换一个打法
        self.assertNotEqual(bots[3].tag, "激进")
        self.assertNotEqual(bots[2].tag, bots[3].tag)          # 同时尽量不重样
        self.assertEqual(changed[2], bots[2].tag)

    def test_unspecified_style_counts_as_random(self):
        import random
        import poker
        from deepseek import PERSONA_BY_TAG

        players, bots = self._table()
        for k in (1, 2, 3):
            bots[k].apply_persona(PERSONA_BY_TAG["稳健"])
        changed = poker.reroll_random_styles(bots, {}, random.Random(3))
        self.assertEqual(sorted(changed), [1, 2, 3])           # 默认就是随机，全部重抽
        for k in (1, 2, 3):
            self.assertNotEqual(bots[k].tag, "稳健")

    def test_restart_from_end_menu(self):
        """真的在局末按 0：打印重开提示，并从第 1 局重新开始。"""
        import contextlib
        import io
        import sys
        from unittest.mock import patch

        import poker

        seen = {"end": 0}

        def fake_read(prompt, singles, numeric=False):
            if "下一局" in prompt:
                seen["end"] += 1
                return ("0", "") if seen["end"] == 1 else ("q", "")
            if "f弃" in prompt:
                return "f", ""
            return "q", ""

        argv = ["poker.py", "--bots", "2", "--offline", "--sims", "8",
                "--seed", "9", "--memory", "0"]
        out = io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(poker, "read_cmd", fake_read), \
                contextlib.redirect_stdout(out):
            self.assertEqual(poker.main(), 0)
        text = out.getvalue()
        self.assertIn("已按当前配置重开", text)
        self.assertEqual(text.count("第 1 局结束"), 2)          # 局号真的回到 1

    def test_restart_after_tournament_over(self):
        """淘汰赛打完（出冠军）后也能直接 0 重开，不用退出重进。"""
        import contextlib
        import io
        import sys
        from unittest.mock import patch

        import poker

        seen = {"over": 0, "calls": 0}

        def fake_read(prompt, singles, numeric=False):
            seen["calls"] += 1
            self.assertLess(seen["calls"], 900)
            if "下一局" in prompt:                 # 还在打，继续下一手
                return "n", ""
            if "重开一局" in prompt:               # 淘汰赛结束后的菜单
                seen["over"] += 1
                return ("0", "") if seen["over"] == 1 else ("q", "")
            if "f弃" in prompt:
                return "c", ""
            return "q", ""

        argv = ["poker.py", "--bots", "1", "--offline", "--sims", "6", "--seed", "2",
                "--mode", "tournament", "--stack", "20", "--memory", "0"]
        out = io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(poker, "read_cmd", fake_read), \
                contextlib.redirect_stdout(out):
            self.assertEqual(poker.main(), 0)
        text = out.getvalue()
        self.assertIn("冠军是", text)                          # 第一场确实打完了
        self.assertIn("已按当前配置重开", text)
        self.assertGreaterEqual(text.count("第 1 局结束"), 2)


class StrengthTests(unittest.TestCase):
    """决策强度：翻前按位置/牌力表打，翻后按「对手真实范围」而不是随机两张牌。"""

    @staticmethod
    def spot(hole: str, board: str = "", to_call: int = 0, pot: int = 30,
             position: str = "CO", n_opp: int = 1, opp_raised: bool = False,
             stack: int = 1000):
        board_cards = h(board) if board.strip() else []
        street = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}[len(board_cards)]
        lo = min(max(to_call, 20) + 20, stack)
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
            "actions": ([{"name": "机器1", "street": "preflop", "kind": "raise"}]
                        if opp_raised else []),
            "history": [],
        }

    def _rates(self, snap, trials=40, aggression=0.0):
        counts = {"raise": 0, "call": 0, "fold": 0, "check": 0}
        for seed in range(trials):
            act, _ = heuristic_decide(snap, random.Random(seed), 24, aggression,
                                      None, {"bluff": False}, 1.0)
            counts[act.kind] = counts.get(act.kind, 0) + 1
        return {k: v / trials for k, v in counts.items()}

    def test_preflop_score_ranks_hands_sensibly(self):
        from deepseek import preflop_score
        pairs = [("AA", "KK"), ("KK", "QQ"), ("QQ", "JJ"),
                 ("AKs", "AKo"), ("AKo", "KQo"), ("K9o", "72o"), ("76s", "72o")]
        for strong, weak in pairs:
            self.assertGreater(preflop_score(strong), preflop_score(weak),
                               f"牌力顺序错了：{strong} 应强于 {weak}")
        self.assertAlmostEqual(preflop_score("AA"), 0.85, places=2)

    def test_preflop_folds_trash_when_unopened(self):
        """72o 在枪口位没人入池时不该跟注看翻牌。"""
        snap = self.spot("7d 2c", to_call=20, pot=30, position="UTG")
        rates = self._rates(snap)
        self.assertGreaterEqual(rates["fold"], 0.95, f"72o UTG 还在入池：{rates}")

    def test_preflop_opens_and_three_bets_premium(self):
        from deepseek import preflop_decision
        open_snap = self.spot("As Ah", to_call=20, pot=30, position="CO")
        kind, amount, _ = preflop_decision(open_snap["hole"], "CO", 20, 30, 20, 1, 0.0)
        self.assertEqual(kind, "raise")
        self.assertGreaterEqual(amount, 40)              # 至少加到 2bb 以上

        vs_raise = self.spot("As Ah", to_call=60, pot=90, position="CO", opp_raised=True)
        kind, _, _ = preflop_decision(vs_raise["hole"], "CO", 60, 90, 20, 1, 0.0)
        self.assertEqual(kind, "raise")                  # AA 面对加注要反加

    def test_preflop_does_not_call_three_bet_with_k9o(self):
        """K9o 面对 3bet 就该走——旧版这里 80% 跟注，是最大的漏斗。"""
        snap = self.spot("Kd 9c", to_call=180, pot=240, position="CO", opp_raised=True)
        rates = self._rates(snap)
        self.assertGreaterEqual(rates["fold"], 0.9, f"K9o 还在跟 3bet：{rates}")

    def test_preflop_big_blind_defends_wide(self):
        """大盲面对常规加注，K9o 有位置赔率可以防守，不能一弃了之。"""
        snap = self.spot("Kd 9c", to_call=40, pot=70, position="BB", opp_raised=True)
        rates = self._rates(snap)
        self.assertLess(rates["fold"], 0.4, f"大盲弃得太狠：{rates}")

    def test_opponent_pools_narrow_after_a_raise(self):
        """对手加注过，他的范围里就不该出现 72o 这类牌。"""
        from deepseek import opponent_pools, preflop_score
        snap = self.spot("Kd 9c", to_call=60, pot=90, position="CO", opp_raised=True)
        pools = opponent_pools(snap, random.Random(1), size=120)
        self.assertEqual(len(pools), 1)
        pool = pools[0]
        self.assertGreaterEqual(len(pool), 20)
        codes = {_canon(a, b) for a, b in pool}
        self.assertNotIn("72o", codes)
        weakest = min(codes, key=preflop_score)
        self.assertGreater(preflop_score(weakest), preflop_score("72o"))

    def test_equity_shrinks_when_opponent_has_a_real_range(self):
        """同一个 K9o，对随机手牌 58%，对上真实加注范围只有 38% 左右。"""
        from deepseek import estimate_equity, opponent_pools
        hole, rng = h("Kd 9c"), random.Random(5)
        snap = self.spot("Kd 9c", to_call=60, pot=90, position="CO", opp_raised=True)
        pools = opponent_pools(snap, random.Random(5), size=200)
        random_eq = estimate_equity(hole, [], 1200, rng, 1)
        range_eq = estimate_equity(hole, [], 1200, rng, 1, opp_pools=pools)
        self.assertLess(range_eq, random_eq - 0.08,
                        f"范围没起作用：随机 {random_eq:.2f} vs 范围 {range_eq:.2f}")

    def test_count_outs_counts_draws(self):
        from deepseek import count_outs
        self.assertEqual(count_outs(h("As Ks"), h("Qs 7s 2h")), 9)      # 同花听
        self.assertEqual(count_outs(h("9h 8d"), h("Ts 7c 2h")), 8)      # 两头顺
        self.assertEqual(count_outs(h("9h 8d"), h("Ts 6c 2h")), 4)      # 卡顺（缺 7）
        self.assertEqual(count_outs(h("7d 2c"), h("Ah Kd 9s")), 0)      # 没戏
        self.assertEqual(count_outs(h("As Ks"), h("Qs 7s 2h 3d 4c")), 0)  # 河牌无听牌

    def test_weak_line_gets_bluff_caught(self):
        """对手一路过牌、河牌才下小注，多半是诈唬，A 高也要敢抓。"""
        from deepseek import bluff_catch_ok
        self.assertTrue(bluff_catch_ok(to_call=60, pot=240, eq=0.22,
                                       weak_line=True, has_showdown_value=True))
        self.assertFalse(bluff_catch_ok(to_call=60, pot=240, eq=0.22,
                                        weak_line=False, has_showdown_value=True))
        self.assertFalse(bluff_catch_ok(to_call=200, pot=240, eq=0.22,
                                        weak_line=True, has_showdown_value=True))

    def test_range_equity_reaches_decision(self):
        """K9o 在 CO 面对加注，用上范围后不该再像旧版那样 85% 跟注。"""
        snap = self.spot("Kd 9c", to_call=60, pot=90, position="CO", opp_raised=True)
        rates = self._rates(snap)
        self.assertGreater(rates["fold"], 0.5, f"范围没传到决策上：{rates}")


def _canon(a, b) -> str:
    """两张牌 -> 169 手牌代号（AA / AKs / AKo）。"""
    hi, lo = sorted((a.rank, b.rank), reverse=True)
    r = "23456789TJQKA"
    if hi == lo:
        return r[hi - 2] * 2
    return r[hi - 2] + r[lo - 2] + ("s" if a.suit == b.suit else "o")


if __name__ == "__main__":
    unittest.main()
