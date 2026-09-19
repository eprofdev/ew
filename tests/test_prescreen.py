"""اختبارات الترشيح المسبق — الطبقة التي تحمي رصيد الـ API."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trading_bot.prescreen import (
    MIC_CAPITAL_MARKET,
    MIC_GLOBAL_SELECT,
    PreScreenConfig,
    PreScreener,
    estimate_prescreen,
)
from trading_bot.universe import Universe, UniverseEntry

QUOTES = {
    "GOOD": {"symbol": "GOOD", "close": "1.80", "previous_close": "1.75",
             "average_volume": "1200000", "fifty_two_week_low": "1.10",
             "fifty_two_week_high": "9.00"},
    "PRICEY": {"symbol": "PRICEY", "close": "42.00", "previous_close": "41.00",
               "average_volume": "3000000", "fifty_two_week_low": "10",
               "fifty_two_week_high": "50"},
    "THIN": {"symbol": "THIN", "close": "2.00", "previous_close": "2.00",
             "average_volume": "12000", "fifty_two_week_low": "1",
             "fifty_two_week_high": "5"},
    "EXTENDED": {"symbol": "EXTENDED", "close": "3.00", "previous_close": "1.00",
                 "average_volume": "5000000", "fifty_two_week_low": "0.80",
                 "fifty_two_week_high": "3.10"},
    "TOPPED": {"symbol": "TOPPED", "close": "4.80", "previous_close": "4.75",
               "average_volume": "900000", "fifty_two_week_low": "1.00",
               "fifty_two_week_high": "5.00"},
    "BROKEN": {"error": "symbol not found"},
}


class FakeQuoteProvider:
    def __init__(self, quotes=None):
        self.quotes = quotes if quotes is not None else QUOTES
        self.credits_used = 0
        self.warnings = []
        self.batches = []

    def get_quotes(self, symbols):
        self.batches.append(list(symbols))
        self.credits_used += len(symbols)
        return {s: self.quotes.get(s, {"error": "مفقود"}) for s in symbols}


def entry(symbol, mic=MIC_CAPITAL_MARKET, name="Some Co", type_="Common Stock",
          country="United States"):
    return UniverseEntry(symbol=symbol, name=name, exchange="NASDAQ", mic_code=mic,
                         type=type_, country=country)


class TestStage0(unittest.TestCase):
    """المرحلة المجانية — لا تستهلك رصيداً إطلاقاً."""

    def setUp(self):
        self.screener = PreScreener()
        self.universe = Universe(entries=[
            entry("SMALL"),
            entry("BIG", mic=MIC_GLOBAL_SELECT),
            entry("SPAC1", name="Artius II Acquisition Corp"),
            entry("WARR", name="Something Warrant"),
            entry("FOREIGN", country="Canada"),
            entry("FUND", type_="ETF"),
        ])

    def test_keeps_capital_market_only(self):
        kept = [e.symbol for e in self.screener.stage0(self.universe)]
        self.assertEqual(kept, ["SMALL"])

    def test_excludes_spacs_by_name(self):
        kept = [e.symbol for e in self.screener.stage0(self.universe)]
        self.assertNotIn("SPAC1", kept)

    def test_tier_is_configurable(self):
        screener = PreScreener(PreScreenConfig(allowed_mics=(MIC_GLOBAL_SELECT,)))
        kept = [e.symbol for e in screener.stage0(self.universe)]
        self.assertEqual(kept, ["BIG"])

    def test_all_tiers_when_empty(self):
        screener = PreScreener(PreScreenConfig(allowed_mics=()))
        kept = [e.symbol for e in screener.stage0(self.universe)]
        self.assertIn("BIG", kept)
        self.assertIn("SMALL", kept)

    def test_stage0_costs_nothing(self):
        result = self.screener.run(self.universe, provider=None)
        self.assertEqual(result.credits_used, 0)
        self.assertTrue(any("المرحلة 0 فقط" in w for w in result.warnings))


class TestStage1(unittest.TestCase):
    def setUp(self):
        self.screener = PreScreener(PreScreenConfig(batch_size=3))
        self.provider = FakeQuoteProvider()
        self.entries = [entry(s) for s in
                        ["GOOD", "PRICEY", "THIN", "EXTENDED", "TOPPED", "BROKEN"]]

    def test_only_valid_candidate_passes(self):
        candidates = self.screener.stage1(self.provider, self.entries)
        passed = [c.symbol for c in candidates if c.passed]
        self.assertEqual(passed, ["GOOD"])

    def test_each_rejection_has_its_reason(self):
        by_symbol = {c.symbol: c for c in self.screener.stage1(self.provider, self.entries)}
        self.assertTrue(by_symbol["PRICEY"].rejected.startswith("PRICE"))
        self.assertTrue(by_symbol["THIN"].rejected.startswith("VOLUME"))
        self.assertTrue(by_symbol["EXTENDED"].rejected.startswith("EXTENDED"))
        self.assertTrue(by_symbol["TOPPED"].rejected.startswith("RANGE"))
        self.assertTrue(by_symbol["BROKEN"].rejected.startswith("NO_QUOTE"))

    def test_credits_are_one_per_symbol(self):
        self.screener.stage1(self.provider, self.entries)
        self.assertEqual(self.provider.credits_used, len(self.entries))

    def test_batching_splits_requests(self):
        self.screener.stage1(self.provider, self.entries)
        self.assertEqual([len(b) for b in self.provider.batches], [3, 3])

    def test_range_position_computed(self):
        candidate = self.screener.judge_quote(entry("GOOD"), QUOTES["GOOD"])
        self.assertAlmostEqual(candidate.range_position, (1.80 - 1.10) / (9.00 - 1.10), places=4)

    def test_nested_fifty_two_week_shape_is_supported(self):
        quote = {"close": "2.00", "previous_close": "2.00", "average_volume": "900000",
                 "fifty_two_week": {"low": "1.00", "high": "5.00"}}
        candidate = self.screener.judge_quote(entry("NEST"), quote)
        self.assertAlmostEqual(candidate.range_position, 0.25, places=4)

    def test_missing_range_does_not_reject(self):
        quote = {"close": "2.00", "previous_close": "2.00", "average_volume": "900000"}
        self.assertTrue(self.screener.judge_quote(entry("NORANGE"), quote).passed)

    def test_max_candidates_stops_early(self):
        screener = PreScreener(PreScreenConfig(batch_size=1, max_candidates=1))
        provider = FakeQuoteProvider({s: QUOTES["GOOD"] for s in ["A", "B", "C", "D"]})
        screener.stage1(provider, [entry(s) for s in ["A", "B", "C", "D"]])
        self.assertLess(provider.credits_used, 4)   # توقف قبل استنزاف الرصيد


class TestFullRun(unittest.TestCase):
    def setUp(self):
        self.universe = Universe(entries=[
            entry("GOOD"), entry("PRICEY"), entry("THIN"),
            entry("BIG", mic=MIC_GLOBAL_SELECT),   # يسقط مجاناً في المرحلة 0
        ])
        self.provider = FakeQuoteProvider()
        self.result = PreScreener().run(self.universe, self.provider)

    def test_stage0_saves_credits_before_stage1(self):
        self.assertEqual(len(self.result.stage0_survivors), 3)
        self.assertEqual(self.result.credits_used, 3)   # لم يُدفع شيء عن BIG

    def test_saved_credits_are_reported(self):
        self.assertGreater(self.result.credits_saved, 0)

    def test_result_converts_to_universe(self):
        universe = self.result.to_universe()
        self.assertEqual(universe.symbols, ["GOOD"])
        with tempfile.TemporaryDirectory() as d:
            path = universe.save(Path(d) / "c.json")
            self.assertEqual(Universe.load(path).symbols, ["GOOD"])

    def test_rejection_reasons_are_counted(self):
        reasons = self.result.rejection_reasons()
        self.assertEqual(sum(reasons.values()), 2)

    def test_summary_mentions_both_stages(self):
        summary = self.result.summary()
        self.assertIn("المرحلة 0", summary)
        self.assertIn("المرحلة 1", summary)


class TestEstimate(unittest.TestCase):
    def test_real_nasdaq_numbers(self):
        estimate = estimate_prescreen(4509, 1415)
        self.assertGreater(estimate["saved_pct"], 0.75)
        self.assertLess(estimate["prescreen_days_free_plan"], estimate["naive_days_free_plan"])

    def test_no_savings_when_nothing_filtered(self):
        estimate = estimate_prescreen(100, 100, expected_pass_rate=1.0)
        self.assertLessEqual(estimate["saved_credits"], 0)


if __name__ == "__main__":
    unittest.main()


class TestSweepGuards(unittest.TestCase):
    """حماية الضبط من التفصيل على المقاس."""

    def test_bonferroni_widens_with_more_arms(self):
        from trading_bot.sweep import bonferroni_confidence
        self.assertAlmostEqual(bonferroni_confidence(0.95, 1), 0.95, places=6)
        self.assertGreater(bonferroni_confidence(0.95, 8), bonferroni_confidence(0.95, 2))
        self.assertLess(bonferroni_confidence(0.95, 20), 1.0)

    def test_split_series_is_chronological_and_disjoint(self):
        from trading_bot.models import Candle
        from trading_bot.sweep import split_series
        candles = [Candle(i, 1, 1, 1, 1, 1) for i in range(100)]
        first, second = split_series(candles, 0.6)
        self.assertEqual(len(first), 60)
        self.assertEqual(len(second), 40)
        self.assertLess(first[-1].ts, second[0].ts)
        self.assertEqual(len(first) + len(second), len(candles))

    def test_arm_overrides_only_named_fields(self):
        from trading_bot.config import BotConfig
        from trading_bot.sweep import SweepArm
        base = BotConfig(account_equity=5_000, min_avg_daily_volume=50_000)
        tuned = SweepArm("x", {"min_avg_daily_volume": 0}).apply(base)
        self.assertEqual(tuned.min_avg_daily_volume, 0)
        self.assertEqual(tuned.account_equity, 5_000)

    def test_arm_result_metrics(self):
        from trading_bot.sweep import ArmResult
        arm = ArmResult(label="t", trades=4, wins=2, r_values=[1.0, -1.0, 2.0, -1.0])
        self.assertAlmostEqual(arm.expectancy, 0.25, places=6)
        self.assertAlmostEqual(arm.win_rate, 0.5, places=6)
        self.assertIsNotNone(arm.ci())

    def test_ci_needs_two_values(self):
        from trading_bot.sweep import ArmResult
        self.assertIsNone(ArmResult(label="t", r_values=[1.0]).ci())
