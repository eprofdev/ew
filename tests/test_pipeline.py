"""اختبارات خط الإنتاج: الحالة، الميزانية، الاستئناف، وتلوّث الذاكرة."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trading_bot.csvio import save_candles_csv
from trading_bot.demo import candles_1d, candles_4h
from trading_bot.pipeline import (
    STAGES,
    BudgetExhausted,
    Pipeline,
    PipelineConfig,
    PipelineState,
)
from trading_bot.prescreen import MIC_CAPITAL_MARKET
from trading_bot.universe import Universe, UniverseEntry

GOOD_QUOTE = {
    "symbol": "GOOD", "close": "1.80", "previous_close": "1.75",
    "average_volume": "1200000", "fifty_two_week_low": "1.10",
    "fifty_two_week_high": "9.00",
}


def quote_for(symbol, price="1.80"):
    return dict(GOOD_QUOTE, symbol=symbol, close=price)


class FakeProvider:
    """مزوّد وهمي يحصي الأرصدة ويسمح بمحاكاة الفشل."""

    def __init__(self, failing=(), stocks=None):
        self.credits_used = 0
        self.warnings = []
        self.failing = set(failing)
        self.stocks = stocks or []
        self.fetched = []

    def _request(self, path, params, credits=1):
        self.credits_used += credits
        if path == "stocks":
            return {"data": self.stocks, "status": "ok"}
        raise AssertionError(path)

    def get_quotes(self, symbols):
        self.credits_used += len(symbols)
        self.fetched.extend(symbols)
        return {
            s: ({"error": "فشل"} if s in self.failing else quote_for(s))
            for s in symbols
        }

    def get_candles(self, symbol, interval, limit):
        return candles_4h() if interval == "4h" else candles_1d()


def make_universe(symbols):
    return Universe(entries=[
        UniverseEntry(symbol=s, name=f"{s} Inc", exchange="NASDAQ",
                      mic_code=MIC_CAPITAL_MARKET, type="Common Stock",
                      country="United States")
        for s in symbols
    ])


class TestPipelineState(unittest.TestCase):
    def test_daily_budget_resets_with_the_date(self):
        state = PipelineState(credits_today=700, credits_date="2020-01-01")
        state.roll_day()
        self.assertEqual(state.credits_today, 0)
        self.assertEqual(state.credits_date, PipelineState.today())

    def test_spend_tracks_both_counters(self):
        state = PipelineState()
        state.spend(5)
        state.spend(3)
        self.assertEqual(state.credits_today, 8)
        self.assertEqual(state.credits_total, 8)

    def test_remaining_never_negative(self):
        state = PipelineState()
        state.spend(900)
        self.assertEqual(state.remaining(800), 0)

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            state = PipelineState(stage="backtest", quote_cursor=42)
            state.save(path)
            self.assertEqual(PipelineState.load(path).quote_cursor, 42)

    def test_corrupt_state_falls_back_to_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            path.write_text("{ليس JSON", encoding="utf-8")
            self.assertEqual(PipelineState.load(path).stage, "universe")


class PipelineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        make_universe(["AAA", "BBB", "CCC", "DDD"]).save(self.data / "universe.json")

    def config(self, **kwargs):
        defaults = dict(
            data_dir=self.data, research_mode=True, quote_batch=2,
            max_candidates=None, min_avg_volume=1000, max_range_position=1.0,
        )
        defaults.update(kwargs)
        return PipelineConfig(**defaults)


class TestStages(PipelineTestBase):
    def test_full_run_completes(self):
        provider = FakeProvider()
        outcome = Pipeline(provider, self.config()).run()
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.candidates, 4)
        self.assertIsNotNone(outcome.portfolio)

    def test_report_file_is_written(self):
        Pipeline(FakeProvider(), self.config()).run()
        report = json.loads((self.data / "report.json").read_text("utf-8"))
        self.assertIn("expectancy_r", report)
        self.assertIn("survivorship_warning", report)
        self.assertTrue(report["research_mode"])

    def test_universe_is_reused_not_refetched(self):
        provider = FakeProvider()
        Pipeline(provider, self.config()).run()
        self.assertNotIn("stocks", [c for c in provider.fetched])
        self.assertEqual(provider.credits_used, 4)   # اقتباسات فقط

    def test_stage0_costs_nothing(self):
        provider = FakeProvider()
        pipeline = Pipeline(provider, self.config())
        pipeline.stage_prescreen0(Universe.load(self.data / "universe.json"))
        self.assertEqual(provider.credits_used, 0)

    def test_stages_are_ordered(self):
        self.assertEqual(STAGES[0], "universe")
        self.assertEqual(STAGES[-1], "report")


class TestBudget(PipelineTestBase):
    def test_stops_when_budget_is_reached(self):
        provider = FakeProvider()
        outcome = Pipeline(provider, self.config(daily_credit_budget=2)).run()
        self.assertFalse(outcome.complete)
        self.assertIn("ميزانية", outcome.stopped_reason)
        self.assertLessEqual(provider.credits_used, 2)

    def test_resume_continues_from_cursor(self):
        provider = FakeProvider()
        first = Pipeline(provider, self.config(daily_credit_budget=2))
        first.run()
        cursor = first.state.quote_cursor
        self.assertEqual(cursor, 2)

        second = Pipeline(FakeProvider(), self.config(daily_credit_budget=800))
        outcome = second.run()
        self.assertTrue(outcome.complete)
        self.assertEqual(second.state.quote_cursor, 4)

    def test_resume_does_not_refetch_cached_symbols(self):
        Pipeline(FakeProvider(), self.config(daily_credit_budget=2)).run()
        provider = FakeProvider()
        Pipeline(provider, self.config(daily_credit_budget=800)).run()
        self.assertNotIn("AAA", provider.fetched)   # مخزّن مسبقاً
        self.assertIn("CCC", provider.fetched)

    def test_budget_stop_is_not_an_exception(self):
        outcome = Pipeline(FakeProvider(), self.config(daily_credit_budget=0)).run()
        self.assertFalse(outcome.complete)


class TestQuoteCache(PipelineTestBase):
    def test_failed_quotes_are_not_cached(self):
        provider = FakeProvider(failing=["BBB"])
        Pipeline(provider, self.config()).run()
        rows = [json.loads(l) for l in (self.data / "quotes.jsonl").read_text("utf-8").splitlines()]
        self.assertNotIn("BBB", [r["symbol"] for r in rows])

    def test_retry_failed_refetches_only_missing(self):
        Pipeline(FakeProvider(failing=["BBB"]), self.config()).run()
        provider = FakeProvider()
        Pipeline(provider, self.config(retry_failed_quotes=True)).run()
        self.assertEqual(provider.fetched, ["BBB"])

    def test_filters_are_rejudged_free_from_cache(self):
        """تضييق الفلاتر بعد الجلب يُعاد تقييمه بصفر رصيد."""
        Pipeline(FakeProvider(), self.config()).run()
        provider = FakeProvider()
        outcome = Pipeline(provider, self.config(max_price=1.00)).run()
        self.assertEqual(provider.credits_used, 0)     # لا طلب جديد
        self.assertEqual(outcome.candidates, 0)        # ومع ذلك تغيّر الحكم

    def test_widening_filters_also_free(self):
        Pipeline(FakeProvider(), self.config(max_price=1.00)).run()
        provider = FakeProvider()
        outcome = Pipeline(provider, self.config(max_price=5.00)).run()
        self.assertEqual(provider.credits_used, 0)
        self.assertEqual(outcome.candidates, 4)

    def test_error_rows_in_file_are_ignored(self):
        path = self.data / "quotes.jsonl"
        path.write_text(json.dumps({"symbol": "AAA", "quote": {"error": "قديم"}}) + "\n", "utf-8")
        self.assertEqual(Pipeline._load_raw_quotes(path), {})


class TestReset(PipelineTestBase):
    def test_reset_clears_state_but_keeps_candle_cache(self):
        pipeline = Pipeline(FakeProvider(), self.config())
        pipeline.run()
        cache_file = self.data / "cache" / "AAA_4h.csv"
        save_candles_csv(cache_file, candles_4h())
        pipeline.reset()
        self.assertFalse((self.data / "pipeline_state.json").exists())
        self.assertFalse((self.data / "candidates.json").exists())
        self.assertTrue(cache_file.exists())
        self.assertEqual(pipeline.state.quote_cursor, 0)


class TestOffline(PipelineTestBase):
    def test_offline_stops_cleanly_when_quotes_missing(self):
        outcome = Pipeline(None, self.config()).run()
        self.assertFalse(outcome.complete)
        self.assertIn("مزوّد", outcome.stopped_reason)

    def test_offline_works_once_quotes_are_cached(self):
        Pipeline(FakeProvider(), self.config()).run()
        for symbol in ["AAA", "BBB", "CCC", "DDD"]:
            save_candles_csv(self.data / "cache" / f"{symbol}_4h.csv", candles_4h())
            save_candles_csv(self.data / "cache" / f"{symbol}_1d.csv", candles_1d())
        outcome = Pipeline(None, self.config()).run()
        self.assertTrue(outcome.complete)


if __name__ == "__main__":
    unittest.main()
