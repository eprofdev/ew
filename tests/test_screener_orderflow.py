"""اختبارات المرشّح وطبقة تدفق الأوامر (Bookmap)."""

from __future__ import annotations

import unittest
from dataclasses import replace

from trading_bot.demo import base_snapshot
from trading_bot.orderflow import BookmapFeed, OrderBook, OrderFlowGuard
from trading_bot.orderflow.bookmap import OrderFlowConfig
from trading_bot.screener import Candidate, Screener, ScreenerConfig


def liquid(snapshot, volume=9_000_000):
    snapshot.realtime = replace(snapshot.realtime, session_volume=volume)
    return snapshot


class TestScreener(unittest.TestCase):
    def setUp(self):
        self.screener = Screener()
        self.snap = liquid(base_snapshot("VSME"))

    def test_good_candidate_passes(self):
        candidate = self.screener.score(self.snap)
        self.assertTrue(candidate.passed)
        self.assertGreater(candidate.score, 40)

    def test_rvol_uses_average_daily_volume(self):
        self.assertAlmostEqual(self.screener.rvol(self.snap), 9_000_000 / 1_200_000, places=6)

    def test_absolute_volume_gate_comes_first(self):
        self.snap.realtime = replace(self.snap.realtime, session_volume=100_000)
        self.assertIn("فوليوم الجلسة", self.screener.score(self.snap).rejected)

    def test_low_rvol_is_rejected(self):
        # فوليوم مطلق كافٍ (600K > 500K) لكنه نصف المعدل اليومي ⇒ RVOL = 0.5×
        self.snap.realtime = replace(self.snap.realtime, session_volume=600_000)
        self.assertIn("الفوليوم النسبي", self.screener.score(self.snap).rejected)

    def test_price_outside_band_is_rejected(self):
        self.snap.realtime = replace(self.snap.realtime, price=12.0, session_volume=9_000_000)
        self.assertIn("خارج النطاق", self.screener.score(self.snap).rejected)

    def test_unknown_float_is_rejected(self):
        self.snap.metrics = replace(self.snap.metrics, free_float=None)
        self.assertEqual(self.screener.score(self.snap).rejected, "الفلوت غير معروف")

    def test_huge_float_is_rejected(self):
        self.snap.metrics = replace(self.snap.metrics, free_float=80_000_000)
        self.assertIn("الفلوت", self.screener.score(self.snap).rejected)

    def test_extended_move_is_penalised(self):
        base = self.screener.score(self.snap).score
        self.snap.realtime = replace(self.snap.realtime, premarket_change_pct=0.85)
        extended = self.screener.score(self.snap)
        self.assertLess(extended.score, base)
        self.assertTrue(any("ممتد" in p for p in extended.penalties))

    def test_promoted_ticker_is_penalised(self):
        base = self.screener.score(self.snap).score
        self.snap.promoted_by_groups = True
        self.assertLess(self.screener.score(self.snap).score, base)

    def test_reverse_split_is_penalised(self):
        base = self.screener.score(self.snap).score
        self.snap.metrics = replace(self.snap.metrics, recent_reverse_split_days=20)
        self.assertLess(self.screener.score(self.snap).score, base)

    def test_rank_sorts_by_score_and_drops_rejects(self):
        weak = liquid(base_snapshot("WEAK"), volume=50_000)     # RVOL منخفض ⇒ مرفوض
        ranked = self.screener.rank([weak, self.snap])
        self.assertEqual([c.ticker for c in ranked], ["VSME"])

    def test_report_keeps_rejects_for_diagnosis(self):
        weak = liquid(base_snapshot("WEAK"), volume=50_000)
        report = self.screener.report([weak, self.snap])
        self.assertEqual(len(report), 2)
        self.assertFalse(report[-1].passed)

    def test_config_is_tunable(self):
        strict = Screener(ScreenerConfig(min_rvol=100))
        self.assertFalse(strict.score(self.snap).passed)


class TestOrderBook(unittest.TestCase):
    def setUp(self):
        self.book = OrderBook()

    def test_best_prices(self):
        self.book.update(True, 1.00, 500)
        self.book.update(True, 1.02, 300)
        self.book.update(False, 1.05, 400)
        self.book.update(False, 1.08, 900)
        self.assertEqual(self.book.best_bid, (1.02, 300))
        self.assertEqual(self.book.best_ask, (1.05, 400))

    def test_zero_size_removes_level(self):
        self.book.update(True, 1.00, 500)
        self.book.update(True, 1.00, 0)
        self.assertEqual(self.book.best_bid, (0.0, 0.0))

    def test_imbalance_sign(self):
        self.book.update(True, 1.00, 9_000)
        self.book.update(False, 1.05, 1_000)
        self.assertGreater(self.book.imbalance(), 0.5)

    def test_empty_book_imbalance_is_zero(self):
        self.assertEqual(self.book.imbalance(), 0.0)


class TestBookmapFeed(unittest.TestCase):
    def setUp(self):
        self.feed = BookmapFeed("VSME", OrderFlowConfig(stale_seconds=1e9))
        for price, size in [(1.05, 3_000), (1.04, 4_000), (1.03, 5_000)]:
            self.feed.on_depth(True, price, size)
        for price, size in [(1.06, 800), (1.07, 900), (1.08, 40_000)]:
            self.feed.on_depth(False, price, size)

    def test_book_source_returns_bid_ask(self):
        source = self.feed.as_book_source()
        self.assertEqual(source("VSME"), (1.05, 1.06))

    def test_book_source_rejects_other_ticker(self):
        with self.assertRaises(ValueError):
            self.feed.as_book_source()("AAPL")

    def test_stale_feed_refuses_to_price(self):
        stale = BookmapFeed("VSME", OrderFlowConfig(stale_seconds=0.0))
        stale.on_depth(True, 1.0, 100, ts=1.0)
        with self.assertRaises(RuntimeError):
            stale.as_book_source()("VSME")

    def test_ask_wall_detected_against_other_levels(self):
        self.assertEqual(self.feed.snapshot().ask_wall, 1.08)

    def test_absorption_needs_volume_without_price_move(self):
        for _ in range(12):
            self.feed.on_trade(1.05, 1_000, False)
        self.assertEqual(self.feed.snapshot().absorption_price, 1.05)

    def test_no_absorption_when_price_runs(self):
        for i in range(12):
            self.feed.on_trade(1.05 + i * 0.01, 1_000, False)
        self.assertIsNone(self.feed.snapshot().absorption_price)

    def test_iceberg_needs_repeated_refills(self):
        for _ in range(4):
            self.feed.on_depth(True, 1.05, 0)
            self.feed.on_depth(True, 1.05, 3_000)
        self.assertIn(1.05, self.feed.snapshot().iceberg_prices)

    def test_aggressor_ratio_from_tape(self):
        for _ in range(8):
            self.feed.on_trade(1.06, 500, True)
        for _ in range(2):
            self.feed.on_trade(1.05, 500, False)
        self.assertAlmostEqual(self.feed.snapshot().aggressor_ratio, 0.8, places=6)


class TestOrderFlowGuard(unittest.TestCase):
    def setUp(self):
        self.guard = OrderFlowGuard()
        self.feed = BookmapFeed("VSME", OrderFlowConfig(stale_seconds=1e9))

    def test_empty_book_is_vetoed(self):
        vetoes, _ = self.guard.evaluate(self.feed.snapshot())
        self.assertTrue(any("BOOK_EMPTY" in v for v in vetoes))

    def test_thin_bid_is_vetoed(self):
        self.feed.on_depth(True, 1.05, 100)
        self.feed.on_depth(False, 1.06, 5_000)
        vetoes, _ = self.guard.evaluate(self.feed.snapshot())
        self.assertTrue(any("THIN_BID" in v for v in vetoes))

    def test_ask_wall_is_vetoed(self):
        for price, size in [(1.05, 9_000), (1.04, 9_000)]:
            self.feed.on_depth(True, price, size)
        for price, size in [(1.06, 500), (1.07, 500), (1.08, 60_000)]:
            self.feed.on_depth(False, price, size)
        vetoes, _ = self.guard.evaluate(self.feed.snapshot())
        self.assertTrue(any("ASK_WALL" in v for v in vetoes))

    def test_healthy_book_confirms(self):
        for price, size in [(1.05, 20_000), (1.04, 15_000)]:
            self.feed.on_depth(True, price, size)
        self.feed.on_depth(False, 1.06, 4_000)
        for _ in range(9):
            self.feed.on_trade(1.06, 900, True)
        vetoes, confirmations = self.guard.evaluate(self.feed.snapshot())
        self.assertEqual(vetoes, [])
        self.assertTrue(confirmations)

    def test_addon_module_imports_without_bookmap_package(self):
        from trading_bot.orderflow import bookmap_addon
        self.assertTrue(callable(bookmap_addon.book_source_for_provider))
        self.assertIn("bid", bookmap_addon.current_order_flow())


if __name__ == "__main__":
    unittest.main()
