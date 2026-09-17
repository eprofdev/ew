"""اختبارات مزود Twelve Data — ببيانات مُقلَّدة بأشكال الاستجابة الحقيقية."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trading_bot import Decision, EnhancedTradingBot
from trading_bot.data_feed import build_snapshot
from trading_bot.providers.twelve_data import (
    PlanLimitError,
    RateLimitError,
    ShortInterestStore,
    SymbolNotFoundError,
    TwelveDataError,
    TwelveDataProvider,
    _parse_datetime,
)

# استجابة /time_series الحقيقية: الأحدث أولاً
TIME_SERIES = {
    "meta": {"symbol": "VSME", "interval": "4h"},
    "values": [
        {"datetime": "2026-09-17 09:30:00", "open": "0.935", "high": "0.9402",
         "low": "0.935", "close": "0.9402", "volume": "251"},
        {"datetime": "2026-09-16 13:30:00", "open": "0.99", "high": "1.0051",
         "low": "0.9408", "close": "0.9833", "volume": "27608"},
        {"datetime": "2026-09-16 09:30:00", "open": "0.9481", "high": "1.0404",
         "low": "0.92", "close": "1", "volume": "74366"},
        {"datetime": "2026-09-15 13:30:00", "open": None, "high": None,
         "low": None, "close": None, "volume": None},  # شمعة ناقصة
    ],
    "status": "ok",
}

QUOTE = {
    "symbol": "VSME", "exchange": "NASDAQ", "datetime": "2026-09-17",
    "open": "0.935", "high": "0.9402", "low": "0.935", "close": "0.9402",
    "volume": "251", "previous_close": "0.966", "average_volume": "2871995",
    "is_market_open": True,
}

STATISTICS = {
    "meta": {"symbol": "VSME"},
    "statistics": {
        "valuations_metrics": {"market_capitalization": 1_450_000},
        "stock_statistics": {
            "shares_outstanding": 1_900_000,
            "float_shares": 980_000,
            "avg_10_volume": 1_200_000,
            "shares_short": 401_800,
        },
        "dividends_and_splits": {"last_split_factor": "1:10", "last_split_date": "2026-09-01"},
    },
}


class FakeProvider(TwelveDataProvider):
    """مزود يعيد استجابات ثابتة بدل الشبكة."""

    def __init__(self, *, statistics=STATISTICS, **kwargs):
        kwargs.setdefault("api_key", "test-key")
        kwargs.setdefault("max_requests_per_minute", 0)
        super().__init__(**kwargs)
        self._statistics = statistics
        self.calls = []

    def _request(self, path, params):
        self.calls.append((path, params))
        if path == "time_series":
            return self._check_payload(TIME_SERIES, path)
        if path == "quote":
            return self._check_payload(QUOTE, path)
        if path == "statistics":
            if self._statistics is None:
                raise PlanLimitError("/statistics is available exclusively with pro plans")
            return self._check_payload(self._statistics, path)
        raise AssertionError(f"نقطة غير متوقعة: {path}")


def _store(tmpdir: str) -> ShortInterestStore:
    return ShortInterestStore(path=Path(tmpdir) / "short.json")


class TestParsing(unittest.TestCase):
    def test_parse_datetime_formats(self):
        self.assertEqual(_parse_datetime("2026-09-17"), 1789603200)
        self.assertGreater(_parse_datetime("2026-09-17 09:30:00"), _parse_datetime("2026-09-17"))

    def test_parse_datetime_rejects_garbage(self):
        with self.assertRaises(TwelveDataError):
            _parse_datetime("17/09/2026")

    def test_missing_api_key_raises(self):
        with self.assertRaises(TwelveDataError):
            TwelveDataProvider(api_key="")

    def test_error_payloads_map_to_exceptions(self):
        check = TwelveDataProvider._check_payload
        with self.assertRaises(RateLimitError):
            check({"code": 429, "message": "You have run out of API credits", "status": "error"}, "quote")
        with self.assertRaises(PlanLimitError):
            check({"code": 403, "message": "available exclusively with pro", "status": "error"}, "statistics")
        with self.assertRaises(SymbolNotFoundError):
            check({"code": 404, "message": "symbol not found", "status": "error"}, "quote")
        with self.assertRaises(TwelveDataError):
            check({"code": 400, "message": "bad param", "status": "error"}, "quote")
        with self.assertRaises(TwelveDataError):  # code غير رقمي لا يُسقط المعالج
            check({"code": "n/a", "message": "weird", "status": "error"}, "quote")
        self.assertEqual(check({"status": "ok", "values": []}, "quote"), {"status": "ok", "values": []})


class TestCandles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.provider = FakeProvider(short_store=_store(tmp.name))

    def test_candles_are_sorted_ascending(self):
        candles = self.provider.get_candles("VSME", "4h", 10)
        self.assertTrue(all(a.ts < b.ts for a, b in zip(candles, candles[1:])))

    def test_incomplete_candles_are_dropped(self):
        self.assertEqual(len(self.provider.get_candles("VSME", "4h", 10)), 3)

    def test_values_are_floats(self):
        last = self.provider.get_candles("VSME", "4h", 10)[-1]
        self.assertAlmostEqual(last.close, 0.9402)
        self.assertEqual(last.volume, 251.0)

    def test_candles_are_cached(self):
        self.provider.get_candles("VSME", "4h", 10)
        self.provider.get_candles("VSME", "4h", 10)
        self.assertEqual(sum(1 for c in self.provider.calls if c[0] == "time_series"), 1)


class TestMetrics(unittest.TestCase):
    def test_metrics_from_statistics(self):
        with tempfile.TemporaryDirectory() as d:
            p = FakeProvider(short_store=_store(d))
            m = p.get_metrics("VSME")
        self.assertEqual(m.market_cap, 1_450_000)
        self.assertEqual(m.free_float, 980_000)
        self.assertTrue(m.fundamentals_known)
        self.assertEqual(m.avg_daily_volume, 2_871_995)

    def test_reverse_split_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            m = FakeProvider(short_store=_store(d)).get_metrics("VSME")
        self.assertIsNotNone(m.recent_reverse_split_days)
        self.assertGreaterEqual(m.recent_reverse_split_days, 0)

    def test_forward_split_is_not_flagged(self):
        stats = json.loads(json.dumps(STATISTICS))
        stats["statistics"]["dividends_and_splits"]["last_split_factor"] = "4:1"
        with tempfile.TemporaryDirectory() as d:
            m = FakeProvider(statistics=stats, short_store=_store(d)).get_metrics("VSME")
        self.assertIsNone(m.recent_reverse_split_days)

    def test_plan_limit_leaves_fundamentals_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            p = FakeProvider(statistics=None, short_store=_store(d))
            m = p.get_metrics("VSME")
        self.assertIsNone(m.market_cap)
        self.assertFalse(m.fundamentals_known)
        self.assertTrue(any("Pro" in w for w in p.warnings))

    def test_override_fills_missing_fundamentals(self):
        with tempfile.TemporaryDirectory() as d:
            p = FakeProvider(
                statistics=None,
                short_store=_store(d),
                fundamentals_override={"vsme": {"market_cap": 1_200_000, "free_float": 900_000}},
            )
            m = p.get_metrics("VSME")
        self.assertTrue(m.fundamentals_known)
        self.assertEqual(m.free_float, 900_000)

    def test_override_wins_over_api(self):
        with tempfile.TemporaryDirectory() as d:
            p = FakeProvider(
                short_store=_store(d),
                fundamentals_override={"VSME": {"free_float": 500_000}},
            )
            self.assertEqual(p.get_metrics("VSME").free_float, 500_000)


class TestRealtime(unittest.TestCase):
    def test_no_book_source_means_no_bid(self):
        with tempfile.TemporaryDirectory() as d:
            p = FakeProvider(short_store=_store(d))
            rt = p.get_realtime("VSME")
        self.assertEqual(rt.bid, 0.0)
        self.assertTrue(any("Bid/Ask" in w for w in p.warnings))

    def test_book_source_supplies_bid_ask(self):
        with tempfile.TemporaryDirectory() as d:
            p = FakeProvider(short_store=_store(d), book_source=lambda t: (0.93, 0.95))
            rt = p.get_realtime("VSME")
        self.assertEqual((rt.bid, rt.ask), (0.93, 0.95))

    def test_failing_book_source_does_not_crash(self):
        def boom(_):
            raise RuntimeError("broker down")

        with tempfile.TemporaryDirectory() as d:
            p = FakeProvider(short_store=_store(d), book_source=boom)
            rt = p.get_realtime("VSME")
        self.assertEqual(rt.bid, 0.0)
        self.assertTrue(any("دفتر الأسعار" in w for w in p.warnings))

    def test_short_interest_ratio_of_float(self):
        with tempfile.TemporaryDirectory() as d:
            rt = FakeProvider(short_store=_store(d)).get_realtime("VSME")
        self.assertAlmostEqual(rt.current_short_interest, 401_800 / 980_000, places=6)
        self.assertGreaterEqual(rt.historical_short_interest, rt.current_short_interest)

    def test_premarket_is_zero_while_market_open(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(FakeProvider(short_store=_store(d)).get_realtime("VSME").premarket_change_pct, 0.0)

    def test_premarket_computed_when_market_closed(self):
        quote = dict(QUOTE, is_market_open=False, close="1.449", previous_close="0.966")

        class ClosedMarket(FakeProvider):
            def _request(self, path, params):
                if path == "quote":
                    return self._check_payload(quote, path)
                return super()._request(path, params)

        with tempfile.TemporaryDirectory() as d:
            rt = ClosedMarket(short_store=_store(d)).get_realtime("VSME")
        self.assertAlmostEqual(rt.premarket_change_pct, 0.5, places=2)


class TestShortInterestStore(unittest.TestCase):
    def test_records_and_returns_peak(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(d)
            store.record("VSME", 0.92)
            store.record("VSME", 0.41)
            self.assertAlmostEqual(store.historical("vsme"), 0.92)

    def test_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "short.json"
            ShortInterestStore(path=path).record("VSME", 0.88)
            self.assertAlmostEqual(ShortInterestStore(path=path).historical("VSME"), 0.88)

    def test_old_rows_fall_out_of_window(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "short.json"
            store = ShortInterestStore(path=path, window_days=30)
            store.record("VSME", 0.95, ts=1_000_000)          # قراءة قديمة
            store.record("VSME", 0.30, ts=1_000_000 + 40 * 86_400)
            self.assertAlmostEqual(store.historical("VSME"), 0.30)

    def test_unknown_ticker_is_zero(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_store(d).historical("NOPE"), 0.0)


class TestEndToEnd(unittest.TestCase):
    def test_unknown_fundamentals_are_vetoed(self):
        with tempfile.TemporaryDirectory() as d:
            p = FakeProvider(statistics=None, short_store=_store(d))
            snapshot = build_snapshot(p, "VSME", candles_4h=10, candles_1d=10)
        signal = EnhancedTradingBot().evaluate(snapshot)
        self.assertIs(signal.decision, Decision.VETO)
        self.assertTrue(any("DATA_UNAVAILABLE" in v for v in signal.vetoes))

    def test_snapshot_carries_both_timeframes(self):
        with tempfile.TemporaryDirectory() as d:
            p = FakeProvider(short_store=_store(d))
            snapshot = build_snapshot(p, "VSME", candles_4h=10, candles_1d=10)
        self.assertTrue(snapshot.candles_4h)
        self.assertTrue(snapshot.candles_1d)
        self.assertEqual(snapshot.ticker, "VSME")


if __name__ == "__main__":
    unittest.main()
