"""اختبارات الموجز اليومي — المخرَج الذي يقرؤه إنسان."""

from __future__ import annotations

import unittest
from dataclasses import replace

from trading_bot.briefing import BLIND_SPOTS, BriefingBuilder, DailyBriefing
from trading_bot.config import BotConfig
from trading_bot.demo import base_snapshot
from trading_bot.engine import EnhancedTradingBot
from trading_bot.models import StockMetrics
from trading_bot.screener import Screener, ScreenerConfig


def liquid(snapshot, volume=9_000_000):
    snapshot.realtime = replace(snapshot.realtime, session_volume=volume)
    return snapshot


class TestUnknownFloatHandling(unittest.TestCase):
    """المسار الآلي يرفض عند الجهل؛ المسار البشري يحذّر ويعرض."""

    def setUp(self):
        self.snap = liquid(base_snapshot("VSME"))
        self.snap.metrics = StockMetrics(ticker="VSME", avg_daily_volume=1_200_000)

    def test_automated_path_rejects_unknown_float(self):
        candidate = Screener().score(self.snap)
        self.assertEqual(candidate.rejected, "الفلوت غير معروف")

    def test_human_path_passes_with_warning(self):
        candidate = Screener(ScreenerConfig(require_known_float=False)).score(self.snap)
        self.assertTrue(candidate.passed)
        self.assertTrue(any("الفلوت غير معروف" in p for p in candidate.penalties))

    def test_known_oversized_float_still_rejected(self):
        self.snap.metrics = replace(self.snap.metrics, free_float=90_000_000)
        candidate = Screener(ScreenerConfig(require_known_float=False)).score(self.snap)
        self.assertIsNotNone(candidate.rejected)


class TestBriefingContent(unittest.TestCase):
    def setUp(self):
        self.snap = liquid(base_snapshot("VSME"))
        self.builder = BriefingBuilder(BotConfig(require_fundamentals=False))
        bot = EnhancedTradingBot(BotConfig(require_fundamentals=False))
        self.briefing = self.builder.build([self.snap], {"VSME": bot.evaluate(self.snap)})

    def test_candidate_is_listed_with_context(self):
        self.assertEqual(len(self.briefing.items), 1)
        item = self.briefing.items[0]
        self.assertEqual(item.ticker, "VSME")
        self.assertGreater(item.rvol, 0)
        self.assertIsNotNone(item.rsi_daily)
        self.assertIsNotNone(item.support)

    def test_sizing_is_precomputed_for_the_human(self):
        item = self.briefing.items[0]
        self.assertGreater(item.shares_at_1pct, 0)
        self.assertLess(item.suggested_stop, item.price)
        self.assertAlmostEqual(item.risk_per_share, item.price - item.suggested_stop, places=3)

    def test_markdown_declares_it_is_not_advice(self):
        text = self.briefing.to_markdown()
        self.assertIn("موجز قرار لا توصية", text)
        self.assertIn("القرار قرارك", text)

    def test_markdown_lists_every_blind_spot(self):
        text = self.briefing.to_markdown()
        for spot in BLIND_SPOTS:
            self.assertIn(spot, text)

    def test_markdown_includes_discipline_rules(self):
        text = self.briefing.to_markdown()
        self.assertIn("لا تلاحق قفزة", text)
        self.assertIn("لا تشترِ من العرض", text)

    def test_extended_move_is_a_red_flag(self):
        self.snap.realtime = replace(self.snap.realtime, premarket_change_pct=0.60)
        briefing = self.builder.build([self.snap])
        self.assertTrue(any("الملاحقة" in f for f in briefing.items[0].red_flags))

    def test_wide_spread_warns(self):
        self.snap.realtime = replace(self.snap.realtime, bid=1.00, ask=1.06)
        briefing = self.builder.build([self.snap])
        self.assertTrue(any("فرق طلب/عرض" in w for w in briefing.items[0].warnings))

    def test_rejections_are_shown_not_hidden(self):
        weak = liquid(base_snapshot("WEAK"), volume=1_000)
        briefing = self.builder.build([self.snap, weak])
        self.assertEqual(len(briefing.rejected), 1)
        self.assertIn("WEAK", briefing.to_markdown())

    def test_items_sorted_by_score(self):
        second = liquid(base_snapshot("OTHER"))
        second.realtime = replace(second.realtime, price=4.90, session_volume=2_500_000)
        briefing = self.builder.build([second, self.snap])
        scores = [i.score for i in briefing.items]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestEmptyBriefing(unittest.TestCase):
    def test_no_candidates_is_stated_plainly(self):
        weak = liquid(base_snapshot("WEAK"), volume=1_000)
        text = BriefingBuilder(BotConfig()).build([weak]).to_markdown()
        self.assertIn("لا مرشحين اليوم", text)
        self.assertIn("يوم بلا صفقة قرار صحيح", text)

    def test_empty_briefing_still_carries_checklist(self):
        text = DailyBriefing(date="2026-01-01", scanned=0).to_markdown()
        self.assertIn("تحقق بنفسك", text)


if __name__ == "__main__":
    unittest.main()
