"""التشغيل الذاتي الكامل: يعمل يومياً بلا تدخل، ويعرف متى يتوقف عن نفسه.

ما يؤتمته:
    1. تقدّم خط الإنتاج ضمن ميزانية اليوم (يستأنف تلقائياً عبر الأيام).
    2. إعادة الضبط والتحقق خارج العينة كلما اكتمل الاختبار.
    3. **بوابة الترقية**: لا يُعتمد أي إعداد جديد إلا إذا صمد خارج العينة
       بمجال ثقة مصحَّح موجب بالكامل. وإلا يبقى الإعداد الحالي كما هو.
    4. مسح يومي للمرشحين وكتابة التنبيهات في ملف.
    5. تحديث تقرير النتائج تلقائياً.

ما لا يؤتمته عمداً — **إرسال الأوامر**. الاختبار على 113 صفقة أعطى توقعاً
لا يُميَّز عن الصفر، وانقلب سالباً خارج العينة. ربط تنفيذ آلي باستراتيجية
بهذه النتيجة تحويلٌ لرأس المال إلى ضوضاء. المخرَج تنبيهات يقرؤها إنسان.

بوابة الترقية هي جوهر الأمان هنا: بدونها يصبح التشغيل الذاتي آلةً لاكتشاف
الضوضاء — تجرّب حتى تجد رقماً جميلاً بالصدفة ثم تتبنّاه.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .backtest import BacktestConfig
from .config import BotConfig
from .pipeline import Pipeline, PipelineConfig, PipelineState
from .briefing import BriefingBuilder
from .sweep import SweepArm, bonferroni_confidence, run_arm

# التجارب التي يعيد الضبط الذاتي فحصها. أي إضافة هنا توسّع مجال الثقة
# المطلوب تلقائياً (تصحيح Bonferroni) — الثمن العادل لتعدد المحاولات.
DEFAULT_ARMS: List[SweepArm] = [
    SweepArm("الأساس (كما هو)", {}),
    SweepArm("سيولة 20,000", {"min_avg_daily_volume": 20_000}),
    SweepArm("دعم أوسع 5%", {"support_tolerance_pct": 0.05}),
    SweepArm("سقوط من شمعتين", {"falling_sequence_min": 2}),
    SweepArm("RSI موسّع", {"rsi_daily_band": (35.0, 55.0), "rsi_4h_band": (40.0, 60.0)}),
]


@dataclass
class AutoState:
    last_run: str = ""
    last_research: str = ""
    runs: int = 0
    adopted: Dict[str, object] = field(default_factory=dict)
    adopted_at: str = ""
    rejected_promotions: int = 0
    history: List[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "AutoState":
        try:
            return cls(**json.loads(path.read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), "utf-8")


@dataclass
class AutoReport:
    ran_at: str = ""
    pipeline_stage: str = ""
    pipeline_done: bool = False
    credits_today: int = 0
    stopped_reason: str = ""
    research_ran: bool = False
    promotion: str = "لم تُجرَ"
    best_arm: str = ""
    lines: List[str] = field(default_factory=list)

    def text(self) -> str:
        head = [
            "═" * 64,
            f"التشغيل الذاتي — {self.ran_at}",
            "═" * 64,
            f"  المرحلة      : {self.pipeline_stage}" + ("  ✔" if self.pipeline_done else ""),
            f"  رصيد اليوم   : {self.credits_today}",
        ]
        if self.stopped_reason:
            head.append(f"  التوقف       : {self.stopped_reason}")
        head.append(f"  الترقية      : {self.promotion}")
        return "\n".join(head + self.lines)


class AutoRunner:
    """يُشغَّل دورياً؛ كل استدعاء يدفع العمل خطوة ويحفظ كل شيء."""

    def __init__(
        self,
        provider=None,
        config: Optional[PipelineConfig] = None,
        arms: Optional[Sequence[SweepArm]] = None,
        log: Optional[Callable[[str], None]] = None,
        split_fraction: float = 0.6,
        min_trades_to_promote: int = 60,
    ) -> None:
        self.provider = provider
        self.config = config or PipelineConfig()
        self.arms = list(arms if arms is not None else DEFAULT_ARMS)
        self.paths = self.config.paths()
        self.state_path = Path(self.config.data_dir) / "auto_state.json"
        self.state = AutoState.load(self.state_path)
        self._log = log or (lambda message: None)
        self.split_fraction = split_fraction
        self.min_trades_to_promote = min_trades_to_promote

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def bot_config(self) -> BotConfig:
        """الإعداد المعتمد حالياً — لا يتغير إلا عبر بوابة الترقية."""
        base = BotConfig(
            account_equity=self.config.equity,
            risk_per_trade_pct=self.config.risk_pct,
            require_fundamentals=not self.config.research_mode,
        )
        if not self.state.adopted:
            return base
        from dataclasses import replace

        safe = {k: v for k, v in self.state.adopted.items() if hasattr(base, k)}
        return replace(base, **safe)

    # ── دورة واحدة ───────────────────────────────────────────────────
    def run_once(self) -> AutoReport:
        report = AutoReport(ran_at=self._now())
        self.state.runs += 1
        self.state.last_run = report.ran_at

        pipeline = Pipeline(
            provider=self.provider,
            config=self.config,
            progress=lambda stage, message: self._log(f"[{stage}] {message}"),
        )
        outcome = pipeline.run()
        report.pipeline_stage = pipeline.state.stage
        report.pipeline_done = pipeline.state.finished
        report.credits_today = pipeline.state.credits_today
        report.stopped_reason = outcome.stopped_reason

        # البحث لا يُجرى إلا بعد اكتمال الاختبار، ومرة واحدة في اليوم
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if pipeline.state.finished and self.state.last_research != today:
            report.research_ran = True
            self.state.last_research = today
            self._research(report)

        self._build_briefing(report)
        self.state.save(self.state_path)
        self._write_report(report)
        return report

    def _build_briefing(self, report: AutoReport) -> None:
        """الموجز اليومي — المخرَج الفعلي الذي يقرؤه إنسان."""
        candidates = self.paths["candidates"]
        if not candidates.exists():
            return
        try:
            from .briefing_cli import snapshot_for
            from .universe import Universe

            symbols = Universe.load(candidates).symbols[:60]
            snapshots = [
                snapshot for snapshot in
                (snapshot_for(sym, self.paths["cache"], None) for sym in symbols)
                if snapshot is not None
            ]
            if not snapshots:
                return
            from .engine import EnhancedTradingBot

            bot = EnhancedTradingBot(self.bot_config())
            signals = {s.ticker: bot.evaluate(s) for s in snapshots}
            text = BriefingBuilder(self.bot_config()).build(snapshots, signals).to_markdown()
            path = Path(self.config.data_dir) / "briefing.md"
            path.write_text(text, encoding="utf-8")
            report.lines.append(f"\n  الموجز اليومي: {path}")
        except Exception as exc:  # الموجز مخرَج مساعد — لا يُسقط الدورة
            report.lines.append(f"  تعذّر بناء الموجز: {exc}")

    # ── الضبط الذاتي مع بوابة الترقية ────────────────────────────────
    def _symbols(self) -> List[str]:
        cache = self.paths["cache"]
        if not cache.exists():
            return []
        return sorted(p.name[: -len("_4h.csv")] for p in cache.glob("*_4h.csv"))

    def _research(self, report: AutoReport) -> None:
        symbols = self._symbols()
        if not symbols:
            report.lines.append("  لا بيانات مخزّنة للبحث")
            return

        base_bot, base_bt = self.bot_config(), BacktestConfig()
        confidence = bonferroni_confidence(0.95, len(self.arms))

        self._log(f"بحث: {len(self.arms)} تجربة على {len(symbols)} رمزاً")
        in_sample = [
            run_arm(arm, symbols, self.paths["cache"], base_bot, base_bt,
                    window="in", split_fraction=self.split_fraction)
            for arm in self.arms
        ]
        ranked = sorted(in_sample, key=lambda r: r.expectancy, reverse=True)
        best = ranked[0]
        report.best_arm = best.label
        report.lines.append("")
        report.lines.append("  الضبط داخل العينة:")
        for r in ranked:
            report.lines.append(
                f"      {r.label:<22} صفقات {r.trades:>4}  توقع {r.expectancy:>+7.3f}R"
            )

        arm = next(a for a in self.arms if a.label == best.label)
        if not arm.overrides:
            report.promotion = "الأفضل هو الإعداد الحالي — لا تغيير"
            return
        if best.trades < self.min_trades_to_promote:
            report.promotion = (
                f"مرفوضة: العينة {best.trades} صفقة أقل من {self.min_trades_to_promote}"
            )
            self.state.rejected_promotions += 1
            return

        # الحكم على الجزء المحجوب — تجربة واحدة فلا تصحيح إضافي
        out = run_arm(arm, symbols, self.paths["cache"], base_bot, base_bt,
                      window="out", split_fraction=self.split_fraction)
        ci = out.ci(confidence)
        report.lines.append("")
        report.lines.append(
            f"  خارج العينة ({best.label}): صفقات {out.trades} | "
            f"توقع {out.expectancy:+.3f}R | مجال {('[%.3f , %.3f]' % ci) if ci else '—'}"
        )

        decision = {
            "at": self._now(),
            "arm": best.label,
            "in_sample_r": round(best.expectancy, 4),
            "out_sample_r": round(out.expectancy, 4),
            "out_trades": out.trades,
            "ci": list(ci) if ci else None,
        }

        if ci and ci[0] > 0 and out.trades >= self.min_trades_to_promote:
            self.state.adopted = dict(arm.overrides)
            self.state.adopted_at = self._now()
            decision["promoted"] = True
            report.promotion = f"✔ اعتُمد «{best.label}» — صمد خارج العينة"
        else:
            self.state.rejected_promotions += 1
            decision["promoted"] = False
            reason = "المجال يشمل الصفر" if ci else "عينة غير كافية"
            report.promotion = f"✗ رُفض «{best.label}» — {reason}. الإعداد الحالي باقٍ"

        self.state.history.append(decision)
        self.state.history = self.state.history[-50:]

    # ── المخرجات ─────────────────────────────────────────────────────
    def _write_report(self, report: AutoReport) -> None:
        directory = Path(self.config.data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "auto_last_run.txt").write_text(report.text(), "utf-8")
        with (directory / "auto_history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": report.ran_at,
                "stage": report.pipeline_stage,
                "done": report.pipeline_done,
                "credits": report.credits_today,
                "promotion": report.promotion,
                "best_arm": report.best_arm,
            }, ensure_ascii=False) + "\n")

    # ── التشغيل المستمر ──────────────────────────────────────────────
    def run_forever(
        self, sleep_seconds: int = 3600, max_cycles: Optional[int] = None
    ) -> List[AutoReport]:
        """يظل يعمل: كل دورة تدفع العمل، وينام عند نفاد ميزانية اليوم."""
        reports: List[AutoReport] = []
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            report = self.run_once()
            reports.append(report)
            self._log(report.text())
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            # نفاد الميزانية ⇒ ننام حتى تصفيرها مع تغيّر اليوم
            wait = self._seconds_until_reset() if "ميزانية" in report.stopped_reason else sleep_seconds
            self._log(f"انتظار {wait/60:.0f} دقيقة قبل الدورة التالية")
            time.sleep(wait)
        return reports

    @staticmethod
    def _seconds_until_reset() -> int:
        now = datetime.now(timezone.utc)
        tomorrow = now.replace(hour=0, minute=5, second=0, microsecond=0)
        if tomorrow <= now:
            tomorrow = tomorrow.replace(day=now.day) 
            tomorrow = tomorrow.fromtimestamp(tomorrow.timestamp() + 86_400, tz=timezone.utc)
        return max(int((tomorrow - now).total_seconds()), 60)
