"""اختبارات الكون ومحرك المحفظة (بلا شبكة)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trading_bot.backtest import BacktestConfig
from trading_bot.config import BotConfig
from trading_bot.csvio import load_candles_csv, save_candles_csv
from trading_bot.demo import candles_1d, candles_4h
from trading_bot.portfolio import (
    PooledTrade,
    PortfolioBacktester,
    PortfolioResult,
    SymbolOutcome,
    estimate_cost,
)
from trading_bot.universe import (
    SURVIVORSHIP_WARNING,
    Universe,
    UniverseEntry,
    fetch_universe,
)

STOCKS_PAYLOAD = {
    "data": [
        {"symbol": "AACB", "name": "Artius II", "exchange": "NASDAQ",
         "mic_code": "XNMS", "type": "Common Stock", "country": "United States"},
        {"symbol": "AAPL", "name": "Apple Inc", "exchange": "NASDAQ",
         "mic_code": "XNGS", "type": "Common Stock", "country": "United States"},
        {"symbol": "ZZZW", "name": "Some Warrant", "exchange": "NASDAQ",
         "mic_code": "XNMS", "type": "Warrant", "country": "United States"},
        {"symbol": "XYZL", "name": "Foreign Co", "exchange": "NASDAQ",
         "mic_code": "XNMS", "type": "Common Stock", "country": "Canada"},
    ],
    "count": 4,
    "status": "ok",
}


class FakeProvider:
    def __init__(self):
        self.calls = []

    def _request(self, path, params):
        self.calls.append((path, params))
        return STOCKS_PAYLOAD


class TestUniverse(unittest.TestCase):
    def setUp(self):
        self.universe = fetch_universe(FakeProvider(), exchange="NASDAQ")

    def test_fetch_parses_entries(self):
        self.assertEqual(len(self.universe), 4)
        self.assertIn("AAPL", self.universe.symbols)

    def test_survivorship_warning_is_always_attached(self):
        self.assertIn(SURVIVORSHIP_WARNING, self.universe.notes)

    def test_filter_keeps_common_us_stocks_only(self):
        filtered = self.universe.filter()
        self.assertEqual(sorted(filtered.symbols), ["AACB", "AAPL"])

    def test_filter_can_exclude_symbols(self):
        filtered = self.universe.filter(exclude={"AAPL"})
        self.assertNotIn("AAPL", filtered.symbols)

    def test_extra_symbols_counter_survivorship(self):
        universe = fetch_universe(FakeProvider(), extra_symbols=["AMED", "aapl"])
        self.assertIn("AMED", universe.symbols)
        self.assertEqual(universe.symbols.count("AAPL"), 1)   # لا تكرار

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "u.json"
            self.universe.save(path)
            loaded = Universe.load(path)
        self.assertEqual(loaded.symbols, self.universe.symbols)
        self.assertEqual(loaded.notes, self.universe.notes)


class TestCostEstimate(unittest.TestCase):
    def test_free_plan_full_universe_is_days(self):
        cost = estimate_cost(3683, requests_per_minute=8)
        self.assertGreater(cost["days_by_daily_limit"], 9)
        self.assertEqual(cost["bottleneck"], "API")

    def test_compute_is_not_the_bottleneck(self):
        cost = estimate_cost(3683, requests_per_minute=8)
        self.assertLess(cost["compute_hours"], cost["hours"])


class TestPortfolioAggregation(unittest.TestCase):
    def setUp(self):
        self.result = PortfolioResult()
        for i, r in enumerate([1.5, -1.0, 2.0, -1.0, 0.5]):
            self.result.pooled.append(
                PooledTrade(
                    symbol=f"S{i}", entry_ts=1000 + i * 100, exit_ts=1050 + i * 100,
                    r_multiple=r, pnl=r * 100, exit_reason="target1" if r > 0 else "stop",
                )
            )

    def test_pooled_metrics(self):
        self.assertEqual(self.result.total_trades, 5)
        self.assertAlmostEqual(self.result.expectancy_r, 0.4, places=6)
        self.assertAlmostEqual(self.result.win_rate, 0.6, places=6)
        self.assertAlmostEqual(self.result.profit_factor, 400 / 200, places=6)

    def test_small_sample_is_flagged(self):
        self.assertFalse(self.result.is_statistically_usable)
        self.assertIn("الحد الأدنى", self.result.summary())

    def test_confidence_interval_is_deterministic(self):
        first = self.result.expectancy_ci()
        second = self.result.expectancy_ci()
        self.assertEqual(first, second)
        self.assertLess(first[0], self.result.expectancy_r)
        self.assertGreater(first[1], self.result.expectancy_r)

    def test_portfolio_simulation_respects_concurrency(self):
        # كل الصفقات متداخلة زمنياً ⇒ سقف 1 يعني تنفيذ واحدة فقط
        for t in self.result.pooled:
            t.entry_ts, t.exit_ts = 1000, 9999
        sim = self.result.simulate_portfolio(max_concurrent=1)
        self.assertEqual(sim["trades_taken"], 1)
        self.assertEqual(sim["trades_skipped_full"], 4)

    def test_portfolio_simulation_compounds(self):
        sim = self.result.simulate_portfolio(starting_equity=10_000, risk_per_trade_pct=0.01,
                                             max_concurrent=10)
        self.assertEqual(sim["trades_taken"], 5)
        self.assertGreater(sim["ending_equity"], 10_000)   # مجموع R موجب
        self.assertGreaterEqual(sim["max_drawdown_pct"], 0.0)

    def test_aggregate_funnel_sums_symbols(self):
        self.result.outcomes = [
            SymbolOutcome(symbol="A", funnel={"no_setup": 10, "confirmed": 1}),
            SymbolOutcome(symbol="B", funnel={"no_setup": 5, "vetoed": 3}),
        ]
        self.assertEqual(
            self.result.aggregate_funnel(), {"no_setup": 15, "confirmed": 1, "vetoed": 3}
        )

    def test_research_mode_is_declared_in_report(self):
        self.result.research_mode = True
        self.assertIn("وضع بحثي", self.result.summary())


class TestPortfolioRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"
        save_candles_csv(self.cache / "TEST_4h.csv", candles_4h())
        save_candles_csv(self.cache / "TEST_1d.csv", candles_1d())
        self.runner = PortfolioBacktester(
            bot_config=BotConfig(require_fundamentals=False),
            backtest_config=BacktestConfig(warmup_bars=10),
            cache_dir=self.cache,
        )

    def test_reads_from_cache_without_provider(self):
        outcome = self.runner.run_symbol("TEST")
        self.assertIsNone(outcome.error)
        self.assertGreater(outcome.bars_tested, 0)

    def test_missing_symbol_is_reported_not_raised(self):
        outcome = self.runner.run_symbol("NOPE")
        self.assertIsNotNone(outcome.error)
        self.assertIn("NOPE", outcome.error)

    def test_run_collects_outcomes(self):
        result = self.runner.run(["TEST", "NOPE"])
        self.assertEqual(result.symbols_tested, 1)
        self.assertEqual(len(result.symbols_failed), 1)

    def test_checkpoint_preserves_trades_on_resume(self):
        checkpoint = Path(self.tmp.name) / "ck.jsonl"
        first = self.runner.run(["TEST"], checkpoint=checkpoint)
        second = self.runner.run(["TEST"], checkpoint=checkpoint, resume=True)
        self.assertEqual(second.total_trades, first.total_trades)
        self.assertAlmostEqual(second.expectancy_r, first.expectancy_r, places=6)
        self.assertTrue(any("استئناف" in w for w in second.warnings))

    def test_checkpoint_rows_are_valid_json(self):
        checkpoint = Path(self.tmp.name) / "ck2.jsonl"
        self.runner.run(["TEST"], checkpoint=checkpoint)
        rows = [json.loads(line) for line in checkpoint.read_text("utf-8").splitlines()]
        self.assertEqual(rows[0]["symbol"], "TEST")
        self.assertIn("trade_rows", rows[0])

    def test_stop_after_failures_halts_early(self):
        result = self.runner.run(["A", "B", "C", "D"], stop_after_failures=2)
        self.assertEqual(len(result.outcomes), 2)
        self.assertTrue(any("توقف" in w for w in result.warnings))

    def test_fundamentals_are_applied(self):
        runner = PortfolioBacktester(
            bot_config=BotConfig(),
            cache_dir=self.cache,
            fundamentals={"TEST": {"market_cap": 1_400_000, "free_float": 900_000}},
        )
        metrics = runner.metrics_for("TEST")
        self.assertEqual(metrics.market_cap, 1_400_000)
        self.assertTrue(metrics.fundamentals_known)

    def test_unknown_fundamentals_stay_unknown(self):
        self.assertFalse(self.runner.metrics_for("TEST").fundamentals_known)


if __name__ == "__main__":
    unittest.main()
