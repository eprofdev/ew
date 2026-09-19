"""اختبارات الفرضية المضادة: اختراق بالزخم."""

from __future__ import annotations

import unittest

from trading_bot import BotConfig, Decision, EnhancedTradingBot
from trading_bot.demo import base_snapshot, candles_1d
from trading_bot.models import Candle
from trading_bot.strategies.momentum_breakout import MomentumBreakout

FOUR_HOURS = 14_400


def build(n=90, base=1.0, drift=0.002, volume=100_000):
    """سلسلة صاعدة بهدوء — أرضية صالحة لاختبار الاختراق."""
    out, price = [], base
    for i in range(n):
        o = price
        c = price * (1 + drift)
        out.append(Candle(i * FOUR_HOURS, o, max(o, c) * 1.004, min(o, c) * 0.996, c, volume))
        price = c
    return out


def breakout_candle(prev_series, high_multiple=1.05, volume=400_000):
    prior_high = max(c.high for c in prev_series[-20:])
    ts = prev_series[-1].ts + FOUR_HOURS
    open_ = prev_series[-1].close
    close = prior_high * high_multiple
    return Candle(ts, open_, close * 1.002, open_ * 0.998, close, volume)


class TestBreakoutDetection(unittest.TestCase):
    def setUp(self):
        self.analyzer = MomentumBreakout(BotConfig())
        self.series = build()

    def test_valid_breakout_is_found(self):
        series = self.series + [breakout_candle(self.series)]
        setup = self.analyzer.analyze(series)
        self.assertTrue(setup.found, setup.rejections)
        self.assertGreater(setup.score, 60)
        self.assertIsNotNone(setup.stop_reference)
        self.assertLess(setup.stop_reference, setup.entry_reference)

    def test_no_breakout_when_below_prior_high(self):
        flat = self.series + [
            Candle(self.series[-1].ts + FOUR_HOURS, 1.0, 1.001, 0.99, 0.995, 400_000)
        ]
        setup = self.analyzer.analyze(flat)
        self.assertFalse(setup.found)
        self.assertTrue(any("لا اختراق" in r for r in setup.rejections))

    def test_low_volume_breakout_is_rejected(self):
        series = self.series + [breakout_candle(self.series, volume=50_000)]
        setup = self.analyzer.analyze(series)
        self.assertFalse(setup.found)
        self.assertTrue(any("اختراق كاذب" in r for r in setup.rejections))

    def test_breakout_inside_downtrend_is_rejected(self):
        falling = build(n=90, drift=-0.004)
        series = falling + [breakout_candle(falling, high_multiple=1.001)]
        setup = self.analyzer.analyze(series)
        self.assertFalse(setup.found)

    def test_short_history_is_rejected(self):
        setup = self.analyzer.analyze(build(n=20))
        self.assertFalse(setup.found)
        self.assertTrue(any("غير كافية" in r for r in setup.rejections))

    def test_stop_uses_atr_distance(self):
        series = self.series + [breakout_candle(self.series)]
        setup = self.analyzer.analyze(series)
        distance = setup.entry_reference - setup.stop_reference
        self.assertAlmostEqual(distance, setup.atr_4h * 2.0, places=4)

    def test_lookback_is_configurable(self):
        strict = MomentumBreakout(BotConfig(breakout_lookback=80))
        series = self.series + [breakout_candle(self.series, high_multiple=1.0005)]
        self.assertFalse(strict.analyze(series).found)


class TestEngineWiring(unittest.TestCase):
    def test_engine_selects_breakout_analyzer(self):
        bot = EnhancedTradingBot(BotConfig(entry_strategy="breakout"))
        self.assertIsInstance(bot.price_action, MomentumBreakout)

    def test_engine_defaults_to_support_bounce(self):
        from trading_bot.strategies import FaisalPriceAction
        self.assertIsInstance(EnhancedTradingBot().price_action, FaisalPriceAction)

    def test_rsi_gate_can_be_disabled(self):
        series = build() + [breakout_candle(build())]
        snapshot = base_snapshot("MOM")
        snapshot.candles_4h = series
        snapshot.candles_1d = candles_1d()
        snapshot.realtime = snapshot.realtime.__class__(
            ticker="MOM", price=series[-1].close,
            bid=series[-1].close * 0.99, ask=series[-1].close * 1.01,
        )
        gated = EnhancedTradingBot(BotConfig(entry_strategy="breakout", use_rsi_gate=True))
        free = EnhancedTradingBot(BotConfig(entry_strategy="breakout", use_rsi_gate=False))
        # بوابة RSI مصمَّمة لشراء الضعف ⇒ تحجب الاختراق
        self.assertIsNot(gated.evaluate(snapshot).decision, Decision.BUY_CONFIRMED)
        self.assertIs(free.evaluate(snapshot).decision, Decision.BUY_CONFIRMED)

    def test_safeguards_still_apply_to_breakout(self):
        snapshot = base_snapshot("MOM")
        snapshot.promoted_by_groups = True
        signal = EnhancedTradingBot(
            BotConfig(entry_strategy="breakout", use_rsi_gate=False)
        ).evaluate(snapshot)
        self.assertIs(signal.decision, Decision.VETO)


if __name__ == "__main__":
    unittest.main()
