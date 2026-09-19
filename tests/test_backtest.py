"""اختبارات محرك الـ backtest وحارس جودة البيانات."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from trading_bot.backtest import BacktestConfig, Backtester, Fill, Trade, _day_start
from trading_bot.config import BotConfig
from trading_bot.csvio import load_candles_csv
from trading_bot.dataquality import adjust_for_splits, check_series, detect_split_gaps
from trading_bot.demo import candles_1d, candles_4h
from trading_bot.models import Candle, StockMetrics

FOUR_HOURS = 14_400


def mk(i, o, h, l, c, v=100_000):
    return Candle(i * FOUR_HOURS, o, h, l, c, v)


class TestSplitDetection(unittest.TestCase):
    def test_reverse_split_is_detected_by_quiet_volume(self):
        # حالة AEMD الحقيقية: 1:5 بفوليوم هادئ
        cs = [mk(i, 0.66, 0.70, 0.63, 0.66, 200_000) for i in range(10)]
        cs.append(mk(10, 3.30, 3.75, 3.10, 3.52, 180_000))
        report = detect_split_gaps(cs)
        self.assertFalse(report.ok)
        self.assertEqual(report.inferred_ratios, [5.0])

    def test_news_gap_is_not_treated_as_split(self):
        # حالة اندماج AEMD: نفس حجم القفزة لكن بفوليوم منفجر
        cs = [mk(i, 1.43, 1.50, 1.41, 1.43, 42_800) for i in range(10)]
        cs.append(mk(10, 8.48, 9.50, 6.01, 6.78, 92_381_100))
        report = detect_split_gaps(cs)
        self.assertEqual(report.split_gaps, [])
        self.assertTrue(any("حدث سوقي حقيقي" in m for m in report.issues))

    def test_adjust_scales_pre_split_bars(self):
        cs = [mk(i, 0.66, 0.70, 0.63, 0.66, 200_000) for i in range(10)]
        cs.append(mk(10, 3.30, 3.75, 3.10, 3.52, 180_000))
        adjusted, report = adjust_for_splits(cs)
        self.assertAlmostEqual(adjusted[0].close, 3.30, places=2)
        self.assertAlmostEqual(adjusted[-1].close, 3.52, places=2)   # ما بعد التجزئة لا يتغير
        self.assertAlmostEqual(adjusted[0].volume, 40_000, places=0)  # الفوليوم يُقسم

    def test_clean_series_needs_no_adjustment(self):
        adjusted, report = adjust_for_splits(candles_4h())
        self.assertEqual(report.split_gaps, [])
        self.assertEqual(len(adjusted), len(candles_4h()))

    def test_check_series_flags_bad_ordering(self):
        cs = [mk(2, 1, 1.1, 0.9, 1), mk(1, 1, 1.1, 0.9, 1)]
        self.assertFalse(check_series(cs, "test").ok)

    def test_check_series_flags_impossible_candle(self):
        cs = [mk(i, 1, 1.1, 0.9, 1) for i in range(5)]
        cs[3] = Candle(3 * FOUR_HOURS, 1.0, 0.5, 1.2, 1.0, 100)  # high < low
        self.assertFalse(check_series(cs, "test").ok)


class TestTradeMath(unittest.TestCase):
    def test_r_multiple_and_pnl(self):
        t = Trade(ticker="X", entry_ts=0, entry=1.00, stop=0.90, targets=[1.15], shares=100)
        t.exits.append(Fill(1, 1.15, 100, "target1"))
        self.assertAlmostEqual(t.pnl, 15.0, places=6)
        self.assertAlmostEqual(t.r_multiple, 1.5, places=6)
        self.assertFalse(t.is_open)

    def test_commission_reduces_pnl(self):
        t = Trade(ticker="X", entry_ts=0, entry=1.00, stop=0.90, targets=[1.15], shares=100)
        t.commission = 5.0
        t.exits.append(Fill(1, 1.15, 100, "target1"))
        self.assertAlmostEqual(t.pnl, 10.0, places=6)

    def test_partial_exit_keeps_trade_open(self):
        t = Trade(ticker="X", entry_ts=0, entry=1.0, stop=0.9, targets=[1.1, 1.3], shares=100)
        t.exits.append(Fill(1, 1.10, 50, "target1"))
        self.assertTrue(t.is_open)


class TestNoLookahead(unittest.TestCase):
    def test_daily_visible_excludes_current_day(self):
        bt = Backtester()
        daily = candles_1d()
        bar = Candle(daily[-1].ts + 3600, 1, 1, 1, 1, 1)   # داخل نفس يوم آخر شمعة
        visible = bt._daily_visible(daily, bar)
        self.assertNotIn(daily[-1], visible)
        self.assertIn(daily[-2], visible)

    def test_day_start_is_utc_midnight(self):
        ts = int(datetime(2026, 9, 17, 13, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(
            _day_start(ts),
            int(datetime(2026, 9, 17, tzinfo=timezone.utc).timestamp()),
        )


class TestBacktestRun(unittest.TestCase):
    def setUp(self):
        self.metrics = StockMetrics(
            ticker="TEST", market_cap=1_450_000, free_float=980_000, avg_daily_volume=1_200_000
        )

    def test_insufficient_data_is_reported(self):
        result = Backtester().run("TEST", candles_4h()[:5], candles_1d(), self.metrics)
        self.assertEqual(len(result.trades), 0)
        self.assertTrue(any("غير كافية" in w for w in result.warnings))

    def test_run_completes_and_reports_funnel(self):
        bt = Backtester(BotConfig(account_equity=10_000), BacktestConfig(warmup_bars=10))
        result = bt.run("TEST", candles_4h(), candles_1d(), self.metrics)
        self.assertGreater(result.bars_tested, 0)
        self.assertTrue(result.funnel)
        self.assertIn(result.ticker, result.summary())

    def test_unknown_fundamentals_produce_vetoes(self):
        bt = Backtester(BotConfig(), BacktestConfig(warmup_bars=10))
        result = bt.run("TEST", candles_4h(), candles_1d(), StockMetrics(ticker="TEST"))
        self.assertEqual(len(result.trades), 0)
        self.assertIn("DATA_UNAVAILABLE", result.veto_counts)
        self.assertEqual(result.funnel.get("vetoed"), result.bars_tested)

    def test_stop_is_assumed_before_target(self):
        """شمعة تلمس الوقف والهدف معاً ⇒ يُحتسب الوقف (الافتراض الأسوأ)."""
        bt = Backtester(BotConfig(), BacktestConfig())
        trade = Trade(ticker="X", entry_ts=0, entry=1.0, stop=0.9, targets=[1.2], shares=100)
        # نحاكي منطق الحلقة: الشمعة تلمس 0.85 و 1.25
        bar = mk(1, 1.0, 1.25, 0.85, 1.20)
        self.assertLessEqual(bar.low, trade.stop)
        self.assertGreaterEqual(bar.high, trade.targets[0])
        # المحرك يفحص الوقف أولاً — نتحقق من الترتيب في الشيفرة
        import inspect
        source = inspect.getsource(Backtester.run)
        self.assertLess(source.index('"stop"'), source.index('f"target'))

    def test_metrics_on_empty_result(self):
        result = Backtester().run("TEST", candles_4h()[:3], candles_1d(), self.metrics)
        self.assertEqual(result.win_rate, 0.0)
        self.assertEqual(result.expectancy_r, 0.0)
        self.assertEqual(result.max_drawdown_pct, 0.0)


class TestRealAEMDFixture(unittest.TestCase):
    """اختبار على بيانات AEMD الحقيقية المحفوظة في المستودع."""

    def test_daily_fixture_contains_unadjusted_split(self):
        daily = load_candles_csv("data/AEMD_1d.csv")
        report = detect_split_gaps(daily)
        self.assertIn(5.0, report.inferred_ratios)          # التجزئة 1:5 مرصودة
        self.assertTrue(any("حدث سوقي حقيقي" in m for m in report.issues))  # والاندماج ليس تجزئة

    def test_backtest_runs_on_real_micro_cap_data(self):
        c4 = load_candles_csv("data/AEMD_4h.csv")
        c1, _ = adjust_for_splits(load_candles_csv("data/AEMD_1d.csv"))
        result = Backtester(BotConfig(), BacktestConfig(warmup_bars=40)).run(
            "AEMD", c4, c1, StockMetrics(ticker="AEMD", market_cap=30_000_000, free_float=9_000_000)
        )
        self.assertGreater(result.bars_tested, 0)
        self.assertIn("STRUCTURE", result.veto_counts)      # الكاب أكبر من حد نايف


if __name__ == "__main__":
    unittest.main()
