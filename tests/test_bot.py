"""اختبارات وحدة للبوت المدمج (stdlib unittest — بدون تبعيات)."""

from __future__ import annotations

import unittest
from dataclasses import replace

from trading_bot import BotConfig, Candle, Decision, EnhancedTradingBot, RealTimeData
from trading_bot.demo import base_snapshot, candles_1d, candles_4h
from trading_bot.indicators import gap_ratio, rsi
from trading_bot.strategies import FaisalPriceAction, NaifSafeguards


class TestIndicators(unittest.TestCase):
    def test_rsi_all_gains_is_100(self):
        self.assertEqual(rsi([1 + i * 0.1 for i in range(20)]), 100.0)

    def test_rsi_needs_enough_data(self):
        self.assertIsNone(rsi([1, 2, 3], period=14))

    def test_rsi_in_expected_range(self):
        value = rsi([c.close for c in candles_1d()])
        self.assertIsNotNone(value)
        self.assertTrue(40 <= value <= 48, value)

    def test_gap_ratio_detects_gaps(self):
        # كل شمعة تفتح بعيداً عن إغلاق سابقتها (فجوة 50%)
        gappy = [
            Candle(i, level, level * 1.02, level * 0.98, level, 100)
            for i, level in enumerate(1.0 + 0.5 * (i % 2) for i in range(10))
        ]
        self.assertGreater(gap_ratio(gappy), 0.5)
        smooth = [Candle(i, 1.0, 1.02, 0.98, 1.0, 100) for i in range(10)]
        self.assertEqual(gap_ratio(smooth), 0.0)


class TestFaisalPriceAction(unittest.TestCase):
    def setUp(self):
        self.pa = FaisalPriceAction()

    def test_finds_support_zone_with_multiple_touches(self):
        zones = self.pa.find_support_zones(candles_4h())
        self.assertTrue(zones)
        self.assertGreaterEqual(zones[0].touches, 2)

    def test_detects_valid_setup(self):
        setup = self.pa.analyze(candles_4h())
        self.assertTrue(setup.found)
        self.assertGreater(setup.score, 60)
        self.assertTrue(setup.tested_support_without_high_volume_break)

    def test_heavy_volume_break_invalidates_setup(self):
        cs = candles_4h()
        cs[-1] = Candle(cs[-1].ts, 1.015, 1.02, 0.88, 0.89, 6_000_000)
        setup = self.pa.analyze(cs)
        self.assertFalse(setup.found)

    def test_short_history_is_rejected(self):
        self.assertFalse(self.pa.analyze(candles_4h()[:5]).found)

    def test_hammer_and_engulfing_detection(self):
        hammer = Candle(0, 1.00, 1.02, 0.90, 1.01, 100)
        self.assertTrue(self.pa.is_hammer(hammer, 1.5))
        prev = Candle(0, 1.10, 1.11, 1.00, 1.01, 100)
        cur = Candle(1, 1.00, 1.15, 0.99, 1.12, 100)
        self.assertTrue(self.pa.is_bullish_engulfing(prev, cur))


class TestNaifSafeguards(unittest.TestCase):
    def setUp(self):
        self.guard = NaifSafeguards()
        self.snap = base_snapshot()

    def test_micro_cap_structure_accepts_sharp_structure(self):
        self.assertTrue(self.guard.filter_micro_cap_structure(self.snap.metrics))

    def test_micro_cap_structure_rejects_large_float(self):
        big = replace(self.snap.metrics, market_cap=95_000_000, free_float=40_000_000)
        self.assertFalse(self.guard.filter_micro_cap_structure(big))

    def test_short_cover_signal_fires(self):
        out = self.guard.check_short_cover_signal("VSME", self.snap.realtime)
        self.assertTrue(out.startswith("EXECUTE_BUY_ALERT"))

    def test_short_cover_signal_holds_when_price_too_high(self):
        rt = replace(self.snap.realtime, price=3.20)
        self.assertEqual(self.guard.check_short_cover_signal("VSME", rt), "HOLD")

    def test_short_cover_signal_holds_without_enough_cover(self):
        rt = replace(self.snap.realtime, current_short_interest=0.85)
        self.assertEqual(self.guard.check_short_cover_signal("VSME", rt), "HOLD")

    def test_rsi_and_support_confirms(self):
        self.assertEqual(
            self.guard.validate_rsi_and_support(
                self.snap.candles_1d, self.snap.candles_4h, True
            ),
            "BUY_CONFIRMED",
        )

    def test_rsi_and_support_waits_without_support_test(self):
        self.assertEqual(
            self.guard.validate_rsi_and_support(
                self.snap.candles_1d, self.snap.candles_4h, False
            ),
            "WAIT",
        )

    def test_linear_regression_blocked_by_default(self):
        self.assertFalse(self.guard.linear_regression_allowed(self.snap.candles_4h))

    def test_never_buy_the_ask(self):
        price = self.guard.limit_price(self.snap.realtime)
        self.assertEqual(price, self.snap.realtime.bid)
        self.assertLess(price, self.snap.realtime.ask)

    def test_options_are_blocked(self):
        self.snap.metrics = replace(self.snap.metrics, has_options=True)
        codes = {v.code for v in self.guard.run_all(self.snap)}
        self.assertIn("OPTIONS_BLOCKED", codes)

    def test_wide_spread_and_halt_are_blocked(self):
        self.snap.realtime = replace(self.snap.realtime, bid=1.00, ask=1.40, halted=True)
        codes = {v.code for v in self.guard.run_all(self.snap)}
        self.assertIn("SPREAD", codes)
        self.assertIn("HALTED", codes)

    def test_dilution_flags_are_blocked(self):
        self.snap.metrics = replace(
            self.snap.metrics, has_active_shelf_offering=True, recent_reverse_split_days=5
        )
        codes = {v.code for v in self.guard.run_all(self.snap)}
        self.assertIn("DILUTION", codes)
        self.assertIn("REVERSE_SPLIT", codes)

    def test_pump_trap_is_blocked(self):
        self.snap.promoted_by_groups = True
        self.snap.paid_indicator_signal = True
        self.snap.realtime = replace(self.snap.realtime, premarket_change_pct=0.90)
        codes = {v.code for v in self.guard.run_all(self.snap)}
        self.assertIn("PUMP_GROUP", codes)
        self.assertIn("PAID_INDICATOR", codes)
        self.assertIn("SPIKE_TRAP", codes)

    def test_clean_snapshot_has_no_vetoes(self):
        self.assertEqual(self.guard.run_all(self.snap), [])


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.bot = EnhancedTradingBot(BotConfig(account_equity=10_000))
        self.snap = base_snapshot()

    def test_defaults_match_tactical_update(self):
        self.assertFalse(self.bot.allow_options)
        self.assertFalse(self.bot.allow_linear_regression)

    def test_full_setup_is_confirmed(self):
        signal = self.bot.evaluate(self.snap)
        self.assertIs(signal.decision, Decision.BUY_CONFIRMED)
        self.assertTrue(signal.is_actionable)
        self.assertEqual(signal.entry_limit, self.snap.realtime.bid)
        self.assertLess(signal.stop_loss, signal.entry_limit)
        self.assertEqual(len(signal.targets), 3)
        self.assertGreater(signal.position_size, 0)

    def test_risk_per_trade_is_capped(self):
        signal = self.bot.evaluate(self.snap)
        risk = signal.position_size * (signal.entry_limit - signal.stop_loss)
        self.assertLessEqual(risk, self.bot.config.account_equity * 0.01 + 1e-6)

    def test_position_value_respects_equity_cap(self):
        signal = self.bot.evaluate(self.snap)
        value = signal.position_size * signal.entry_limit
        self.assertLessEqual(value, self.bot.config.account_equity * 0.10 + 1e-6)

    def test_veto_short_circuits_analysis(self):
        self.snap.promoted_by_groups = True
        signal = self.bot.evaluate(self.snap)
        self.assertIs(signal.decision, Decision.VETO)
        self.assertEqual(signal.score, 0.0)
        self.assertFalse(signal.is_actionable)

    def test_missing_bid_prevents_entry(self):
        self.snap.realtime = RealTimeData(ticker="VSME", price=0.0, bid=0.0, ask=1.2)
        signal = self.bot.evaluate(self.snap)
        self.assertIsNot(signal.decision, Decision.BUY_CONFIRMED)

    def test_scan_orders_confirmed_first(self):
        weak = base_snapshot("WEAK")
        weak.candles_4h = candles_4h()[:6]
        signals = self.bot.scan([weak, self.snap])
        self.assertEqual(signals[0].ticker, "VSME")

    def test_backward_compatible_helpers(self):
        self.assertTrue(self.bot.filter_micro_cap_structure(self.snap.metrics))
        self.assertTrue(
            self.bot.check_short_cover_signal("VSME", self.snap.realtime).startswith(
                "EXECUTE_BUY_ALERT"
            )
        )
        self.assertEqual(self.bot.validate_rsi_and_support(self.snap), "BUY_CONFIRMED")


if __name__ == "__main__":
    unittest.main()
