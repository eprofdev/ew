"""اختبارات التشغيل الذاتي — وجوهرها بوابة الترقية."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trading_bot.auto import DEFAULT_ARMS, AutoRunner, AutoState
from trading_bot.config import BotConfig
from trading_bot.csvio import save_candles_csv
from trading_bot.demo import candles_1d, candles_4h
from trading_bot.pipeline import PipelineConfig
from trading_bot.sweep import ArmResult, SweepArm
from trading_bot.universe import Universe, UniverseEntry


def make_runner(tmp: Path, arms=None, **kwargs):
    universe = Universe(entries=[
        UniverseEntry(symbol="AAA", name="AAA Inc", exchange="NASDAQ",
                      mic_code="XNCM", type="Common Stock", country="United States")
    ])
    universe.save(tmp / "universe.json")
    config = PipelineConfig(data_dir=tmp, research_mode=True, daily_credit_budget=0)
    return AutoRunner(provider=None, config=config, arms=arms, **kwargs)


class TestAutoState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "auto.json"
            AutoState(runs=3, adopted={"min_avg_daily_volume": 0}).save(path)
            loaded = AutoState.load(path)
        self.assertEqual(loaded.runs, 3)
        self.assertEqual(loaded.adopted["min_avg_daily_volume"], 0)

    def test_corrupt_state_is_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "auto.json"
            path.write_text("تالف", encoding="utf-8")
            self.assertEqual(AutoState.load(path).runs, 0)


class TestAdoptedConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = make_runner(Path(self.tmp.name))

    def test_defaults_when_nothing_adopted(self):
        self.assertEqual(self.runner.bot_config().min_avg_daily_volume,
                         BotConfig().min_avg_daily_volume)

    def test_adopted_overrides_are_applied(self):
        self.runner.state.adopted = {"min_avg_daily_volume": 12_345}
        self.assertEqual(self.runner.bot_config().min_avg_daily_volume, 12_345)

    def test_unknown_keys_are_ignored_not_crashing(self):
        self.runner.state.adopted = {"not_a_field": 1, "min_avg_daily_volume": 99}
        self.assertEqual(self.runner.bot_config().min_avg_daily_volume, 99)


class TestPromotionGate(unittest.TestCase):
    """البوابة هي ما يمنع التشغيل الذاتي من أن يصبح آلة لاصطياد الضوضاء."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cache = Path(self.tmp.name) / "cache"
        save_candles_csv(cache / "AAA_4h.csv", candles_4h())
        save_candles_csv(cache / "AAA_1d.csv", candles_1d())
        self.arms = [SweepArm("الأساس (كما هو)", {}),
                     SweepArm("مرشّح", {"min_avg_daily_volume": 0})]
        self.runner = make_runner(Path(self.tmp.name), arms=self.arms,
                                  min_trades_to_promote=5)

    def _run_research(self, in_results, out_result):
        from trading_bot.auto import AutoReport

        report = AutoReport(ran_at="test")
        with mock.patch("trading_bot.auto.run_arm") as run_arm:
            run_arm.side_effect = list(in_results) + [out_result]
            self.runner._research(report)
        return report

    def test_promotes_only_when_out_of_sample_ci_is_positive(self):
        winner = ArmResult(label="مرشّح", trades=40, wins=30, r_values=[1.0] * 40)
        base = ArmResult(label="الأساس (كما هو)", trades=40, wins=10, r_values=[-0.5] * 40)
        out = ArmResult(label="مرشّح", trades=40, wins=30, r_values=[1.0] * 40)
        report = self._run_research([base, winner], out)
        self.assertIn("اعتُمد", report.promotion)
        self.assertEqual(self.runner.state.adopted, {"min_avg_daily_volume": 0})

    def test_rejects_when_out_of_sample_includes_zero(self):
        winner = ArmResult(label="مرشّح", trades=40, wins=30, r_values=[1.0, -1.0] * 20)
        base = ArmResult(label="الأساس (كما هو)", trades=40, wins=10, r_values=[-0.5] * 40)
        out = ArmResult(label="مرشّح", trades=40, wins=20, r_values=[1.0, -1.0] * 20)
        report = self._run_research([base, winner], out)
        self.assertIn("رُفض", report.promotion)
        self.assertEqual(self.runner.state.adopted, {})
        self.assertEqual(self.runner.state.rejected_promotions, 1)

    def test_rejects_small_sample_before_touching_holdout(self):
        winner = ArmResult(label="مرشّح", trades=3, wins=3, r_values=[1.0, 1.0, 1.0])
        base = ArmResult(label="الأساس (كما هو)", trades=3, wins=0, r_values=[-1.0] * 3)
        report = self._run_research([base, winner], ArmResult(label="unused"))
        self.assertIn("مرفوضة", report.promotion)
        self.assertEqual(self.runner.state.adopted, {})

    def test_no_change_when_baseline_wins(self):
        base = ArmResult(label="الأساس (كما هو)", trades=40, wins=30, r_values=[1.0] * 40)
        winner = ArmResult(label="مرشّح", trades=40, wins=10, r_values=[-1.0] * 40)
        report = self._run_research([base, winner], ArmResult(label="unused"))
        self.assertIn("لا تغيير", report.promotion)
        self.assertEqual(self.runner.state.adopted, {})

    def test_decision_is_recorded_in_history(self):
        winner = ArmResult(label="مرشّح", trades=40, wins=30, r_values=[1.0] * 40)
        base = ArmResult(label="الأساس (كما هو)", trades=40, wins=10, r_values=[-0.5] * 40)
        self._run_research([base, winner], ArmResult(label="مرشّح", trades=40, wins=30,
                                                     r_values=[1.0] * 40))
        self.assertEqual(len(self.runner.state.history), 1)
        self.assertTrue(self.runner.state.history[0]["promoted"])


class TestRunCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.runner = make_runner(self.dir)

    def test_run_once_writes_outputs_and_advances_counter(self):
        report = self.runner.run_once()
        self.assertEqual(self.runner.state.runs, 1)
        self.assertTrue((self.dir / "auto_last_run.txt").exists())
        self.assertTrue((self.dir / "auto_history.jsonl").exists())
        self.assertIn("التشغيل الذاتي", report.text())

    def test_research_runs_at_most_once_a_day(self):
        with mock.patch.object(AutoRunner, "_research") as research:
            self.runner.state.last_research = ""
            json.dump({"stage": "report", "finished": True, "quote_cursor": 0,
                       "credits_today": 0, "credits_date": "", "credits_total": 0,
                       "started_at": 0, "updated_at": 0, "notes": []},
                      (self.dir / "pipeline_state.json").open("w"))
            self.runner.run_once()
            self.runner.run_once()
        self.assertEqual(research.call_count, 1)

    def test_default_arms_include_baseline(self):
        self.assertTrue(any(not arm.overrides for arm in DEFAULT_ARMS))

    def test_reset_of_budget_waits_at_least_a_minute(self):
        self.assertGreaterEqual(AutoRunner._seconds_until_reset(), 60)


if __name__ == "__main__":
    unittest.main()
