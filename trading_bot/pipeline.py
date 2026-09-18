"""خط إنتاج كامل: من كون السوق إلى حكم إحصائي — بأمر واحد يستأنف نفسه.

المشكلة التي يحلها: التشغيل الكامل على الخطة المجانية يستغرق يومين مقطّعين
بحد 800 رصيد يومياً. بلا حالة محفوظة تعني كل مقاطعة البدء من الصفر.

المراحل الخمس، كل واحدة تُحفظ فور اكتمالها ولا تُعاد:

    1. universe    — قائمة رموز السوق            (رصيد واحد)
    2. prescreen0  — تصفية مجانية بطبقة السوق     (صفر رصيد)
    3. prescreen1  — اقتباس لكل رمز              (رصيد/رمز، يستأنف من موضعه)
    4. backtest    — تاريخ الناجين فقط            (رصيدان/رمز، يستأنف)
    5. report      — التقرير النهائي              (صفر رصيد)

ميزانية الرصيد اليومية محترمة: عند بلوغها يتوقف الخط ويحفظ موضعه بدل أن
يصطدم بالجدار. ونفاد الرصيد اليومي من المزوّد يُعامَل كتوقف نظيف لا كعطل.
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
from .portfolio import PortfolioBacktester, PortfolioResult
from .prescreen import MIC_CAPITAL_MARKET, PreScreenConfig, PreScreener
from .universe import SURVIVORSHIP_WARNING, Universe, fetch_universe

STAGES = ("universe", "prescreen0", "prescreen1", "backtest", "report")


@dataclass
class PipelineConfig:
    data_dir: Path = Path("data")
    exchange: str = "NASDAQ"
    country: str = "United States"

    # الترشيح
    tier: Sequence[str] = (MIC_CAPITAL_MARKET,)
    min_price: float = 0.50
    max_price: float = 5.00
    min_avg_volume: float = 300_000.0
    max_range_position: float = 0.60
    max_today_change: float = 0.25
    max_candidates: Optional[int] = 300
    quote_batch: int = 50

    # الاختبار
    bars_4h: int = 1000
    bars_1d: int = 400
    equity: float = 10_000.0
    risk_pct: float = 0.01
    research_mode: bool = False
    max_concurrent: int = 5

    # الميزانية
    daily_credit_budget: int = 800
    stop_after_failures: int = 25
    retry_failed_quotes: bool = False   # أعد جلب ما فشل سابقاً

    def paths(self) -> Dict[str, Path]:
        d = Path(self.data_dir)
        return {
            "state": d / "pipeline_state.json",
            "universe": d / "universe.json",
            "stage0": d / "stage0.json",
            "quotes": d / "quotes.jsonl",
            "candidates": d / "candidates.json",
            "checkpoint": d / "backtest.jsonl",
            "report": d / "report.json",
            "cache": d / "cache",
        }


@dataclass
class PipelineState:
    stage: str = "universe"
    quote_cursor: int = 0              # موضع التوقف في المرحلة 1
    credits_today: int = 0
    credits_date: str = ""
    credits_total: int = 0
    started_at: float = 0.0
    updated_at: float = 0.0
    finished: bool = False
    notes: List[str] = field(default_factory=list)

    @staticmethod
    def today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def roll_day(self) -> None:
        """ميزانية الرصيد يومية — تُصفَّر مع تغيّر اليوم."""
        if self.credits_date != self.today():
            self.credits_date = self.today()
            self.credits_today = 0

    def spend(self, credits: int) -> None:
        self.roll_day()
        self.credits_today += credits
        self.credits_total += credits

    def remaining(self, budget: int) -> int:
        self.roll_day()
        return max(budget - self.credits_today, 0)

    @classmethod
    def load(cls, path: Path) -> "PipelineState":
        try:
            return cls(**json.loads(path.read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            return cls(started_at=time.time())

    def save(self, path: Path) -> None:
        self.updated_at = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), "utf-8")


@dataclass
class PipelineOutcome:
    state: PipelineState
    stopped_reason: str = ""
    universe_size: int = 0
    stage0_size: int = 0
    candidates: int = 0
    portfolio: Optional[PortfolioResult] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.state.finished

    def summary(self) -> str:
        lines = ["═" * 68, "خط الإنتاج", "═" * 68,
                 f"  المرحلة الحالية : {self.state.stage}"
                 + ("  (مكتمل)" if self.state.finished else "")]
        if self.universe_size:
            lines.append(f"  الكون           : {self.universe_size:,}")
        if self.stage0_size:
            lines.append(f"  بعد الترشيح 0   : {self.stage0_size:,}")
        if self.candidates:
            lines.append(f"  المرشحون        : {self.candidates:,}")
        lines += [
            f"  رصيد اليوم      : {self.state.credits_today:,}",
            f"  رصيد إجمالي     : {self.state.credits_total:,}",
        ]
        if self.stopped_reason:
            lines.append(f"  سبب التوقف      : {self.stopped_reason}")
        for warning in self.warnings:
            lines.append(f"  ⚠ {warning}")
        if self.portfolio is not None:
            lines += ["", self.portfolio.summary()]
        return "\n".join(lines)


class BudgetExhausted(RuntimeError):
    """بلغت ميزانية الرصيد اليومية — توقف نظيف لا عطل."""


class Pipeline:
    """يشغّل المراحل بالترتيب، ويستأنف من حيث توقف."""

    def __init__(
        self,
        provider=None,
        config: Optional[PipelineConfig] = None,
        progress: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.provider = provider
        self.config = config or PipelineConfig()
        self.paths = self.config.paths()
        self.state = PipelineState.load(self.paths["state"])
        self._progress = progress
        self._credits_mark = getattr(provider, "credits_used", 0) if provider else 0

    # ── أدوات ────────────────────────────────────────────────────────
    def log(self, stage: str, message: str) -> None:
        if self._progress:
            self._progress(stage, message)

    def _sync_credits(self) -> None:
        """يرحّل ما استهلكه المزوّد فعلاً إلى ميزانية الحالة."""
        if self.provider is None:
            return
        used = getattr(self.provider, "credits_used", 0)
        delta = used - self._credits_mark
        if delta > 0:
            self.state.spend(delta)
            self._credits_mark = used

    def _check_budget(self, need: int = 1) -> None:
        self._sync_credits()
        if self.state.remaining(self.config.daily_credit_budget) < need:
            raise BudgetExhausted(
                f"بلغت ميزانية اليوم ({self.config.daily_credit_budget:,} رصيد) — "
                "أعد التشغيل غداً وسيُستأنف من موضعه"
            )

    def _advance(self, stage: str) -> None:
        self.state.stage = stage
        self.state.save(self.paths["state"])

    def reset(self) -> None:
        """يمسح الحالة ليبدأ من الصفر (لا يمسح الذاكرة المؤقتة للشموع)."""
        for key in ("state", "stage0", "quotes", "candidates", "checkpoint"):
            path = self.paths[key]
            if path.exists():
                path.unlink()
        self.state = PipelineState(started_at=time.time())

    # ── المراحل ──────────────────────────────────────────────────────
    def stage_universe(self) -> Universe:
        path = self.paths["universe"]
        if path.exists():
            universe = Universe.load(path)
            self.log("universe", f"من الملف: {len(universe):,} رمزاً")
            return universe
        if self.provider is None:
            raise FileNotFoundError("لا يوجد كون محفوظ ولا مزوّد متصل")
        self._check_budget(1)
        universe = fetch_universe(
            self.provider, exchange=self.config.exchange, country=self.config.country
        )
        universe.save(path)
        self._sync_credits()
        self.log("universe", f"جُلب {len(universe):,} رمزاً")
        return universe

    def stage_prescreen0(self, universe: Universe) -> Universe:
        path = self.paths["stage0"]
        if path.exists():
            kept = Universe.load(path)
            self.log("prescreen0", f"من الملف: {len(kept):,} رمزاً")
            return kept
        screener = PreScreener(self._prescreen_config())
        entries = screener.stage0(universe)
        kept = Universe(entries=entries, source="prescreen0", fetched_at=time.time(),
                        notes=[SURVIVORSHIP_WARNING])
        kept.save(path)
        self.log("prescreen0", f"{len(universe):,} ← {len(kept):,} بصفر رصيد")
        return kept

    def _prescreen_config(self) -> PreScreenConfig:
        cfg = self.config
        return PreScreenConfig(
            allowed_mics=tuple(cfg.tier),
            country=cfg.country,
            min_price=cfg.min_price,
            max_price=cfg.max_price,
            min_avg_volume=cfg.min_avg_volume,
            max_range_position=cfg.max_range_position,
            max_today_change=cfg.max_today_change,
            batch_size=cfg.quote_batch,
            max_candidates=cfg.max_candidates,
        )

    def stage_prescreen1(self, stage0: Universe) -> Universe:
        """اقتباس لكل رمز — يستأنف من موضعه ويحترم الميزانية.

        نخزّن **الاقتباس الخام** لا نتيجة الحكم عليه. الفرق جوهري: تعديل
        الفلاتر بعد ذلك يُعاد تقييمه من الملف بصفر رصيد، بدل أن تبقى الرموز
        محكوماً عليها بفلتر قديم — وهو خطأ صامت يفسد كل ضبط لاحق.
        """
        cfg = self.config
        screener = PreScreener(self._prescreen_config())
        entries = list(stage0.entries)
        by_symbol = {e.symbol: e for e in entries}
        quotes_path = self.paths["quotes"]
        cached = self._load_raw_quotes(quotes_path)

        missing = [e.symbol for e in entries if e.symbol not in cached]
        if cfg.retry_failed_quotes and missing and self.state.quote_cursor >= len(entries):
            self.log("prescreen1", f"إعادة محاولة {len(missing)} رمزاً لم يُجلب")
            self.state.quote_cursor = 0

        # 1) جلب ما ينقص فقط (مع احترام الميزانية والاستئناف)
        while self.state.quote_cursor < len(entries):
            passed_now = self._count_passed(screener, cached, by_symbol)
            if cfg.max_candidates and passed_now >= cfg.max_candidates:
                self.log("prescreen1", f"بلغ سقف المرشحين ({cfg.max_candidates})")
                self.state.quote_cursor = len(entries)
                break

            chunk = [
                e for e in
                entries[self.state.quote_cursor : self.state.quote_cursor + cfg.quote_batch]
                if e.symbol not in cached
            ]
            step = min(cfg.quote_batch, len(entries) - self.state.quote_cursor)
            if chunk:
                if self.provider is None:
                    raise FileNotFoundError("المرحلة 1 تحتاج مزوّداً متصلاً للرموز غير المخزّنة")
                self._check_budget(len(chunk))
                quotes = self.provider.get_quotes([e.symbol for e in chunk])
                failures = 0
                with quotes_path.open("a", encoding="utf-8") as fh:
                    for entry in chunk:
                        quote = quotes.get(entry.symbol) or {"error": "لا استجابة"}
                        if "error" in quote:
                            # لا نخزّن الفشل: دفعة فاشلة واحدة كانت ستحكم على
                            # رموزها بالإعدام أبداً — تُعاد محاولتها بـ retry_failed
                            failures += 1
                            continue
                        cached[entry.symbol] = quote
                        fh.write(json.dumps(
                            {"symbol": entry.symbol, "quote": quote}, ensure_ascii=False
                        ) + "\n")
                if failures:
                    self.log("prescreen1", f"فشل جلب {failures} رمزاً في هذه الدفعة")

            self.state.quote_cursor += step
            self._sync_credits()
            self.state.save(self.paths["state"])
            self.log(
                "prescreen1",
                f"[{self.state.quote_cursor}/{len(entries)}] "
                f"مرشحون: {self._count_passed(screener, cached, by_symbol)} "
                f"| رصيد اليوم: {self.state.credits_today:,}",
            )

        # 2) الحكم على الجميع بالفلاتر الحالية — من الملف، بصفر رصيد
        passed_rows = [
            candidate
            for symbol, quote in cached.items()
            if symbol in by_symbol
            and (candidate := screener.judge_quote(by_symbol[symbol], quote)).passed
        ]
        universe = Universe(
            entries=[by_symbol[c.symbol] for c in passed_rows[: cfg.max_candidates or None]],
            source="prescreen1",
            fetched_at=time.time(),
            notes=[SURVIVORSHIP_WARNING],
        )
        universe.save(self.paths["candidates"])
        if missing:
            self.log(
                "prescreen1",
                f"{len(missing)} رمزاً بلا اقتباس — أعد التشغيل بـ --retry-failed",
            )
        return universe

    @staticmethod
    def _count_passed(screener: PreScreener, cached: Dict[str, dict], by_symbol: dict) -> int:
        return sum(
            1
            for symbol, quote in cached.items()
            if symbol in by_symbol and screener.judge_quote(by_symbol[symbol], quote).passed
        )

    @staticmethod
    def _load_raw_quotes(path: Path) -> Dict[str, dict]:
        """الاقتباسات الخام المخزّنة مفهرسةً بالرمز."""
        if not path.exists():
            return {}
        out: Dict[str, dict] = {}
        for line in path.read_text("utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            symbol = row.get("symbol")
            quote = row.get("quote")
            # صفوف الخطأ (من نسخ أقدم أو دفعات فاشلة) ليست بيانات — نتجاهلها
            # كي تُعاد محاولتها بدل أن تُعدم الرمز إلى الأبد.
            if symbol and isinstance(quote, dict) and "error" not in quote:
                out[symbol] = quote
        return out

    def stage_backtest(self, candidates: Universe, fundamentals: Optional[dict] = None) -> PortfolioResult:
        cfg = self.config
        runner = PortfolioBacktester(
            provider=self.provider,
            bot_config=BotConfig(
                account_equity=cfg.equity,
                risk_per_trade_pct=cfg.risk_pct,
                require_fundamentals=not cfg.research_mode,
            ),
            backtest_config=BacktestConfig(),
            cache_dir=self.paths["cache"],
            bars_4h=cfg.bars_4h,
            bars_1d=cfg.bars_1d,
            fundamentals=fundamentals or {},
        )

        def on_symbol(index: int, total: int, outcome) -> None:
            self._sync_credits()
            self.state.save(self.paths["state"])
            if index % 10 == 0 or outcome.trades:
                self.log(
                    "backtest",
                    f"[{index}/{total}] {outcome.symbol}: "
                    f"{outcome.error or f'{outcome.trades} صفقة'} "
                    f"| رصيد اليوم: {self.state.credits_today:,}",
                )
            if self.state.remaining(cfg.daily_credit_budget) < 2 and self.provider is not None:
                raise BudgetExhausted("بلغت ميزانية اليوم أثناء جلب التاريخ")

        return runner.run(
            candidates.symbols,
            checkpoint=self.paths["checkpoint"],
            resume=True,
            progress=on_symbol,
            stop_after_failures=cfg.stop_after_failures,
        )

    # ── التشغيل الكامل ───────────────────────────────────────────────
    def run(self, fundamentals: Optional[dict] = None) -> PipelineOutcome:
        outcome = PipelineOutcome(state=self.state)
        if not self.state.started_at:
            self.state.started_at = time.time()

        try:
            universe = self.stage_universe()
            outcome.universe_size = len(universe)
            self._advance("prescreen0")

            stage0 = self.stage_prescreen0(universe)
            outcome.stage0_size = len(stage0)
            self._advance("prescreen1")

            candidates = self.stage_prescreen1(stage0)
            outcome.candidates = len(candidates)
            if not candidates.symbols:
                outcome.stopped_reason = "لا مرشحين — وسّع الفلاتر أو راجع التقرير"
                self._advance("report")
                self.state.finished = True
                self.state.save(self.paths["state"])
                return outcome
            self._advance("backtest")

            result = self.stage_backtest(candidates, fundamentals)
            outcome.portfolio = result
            self._save_report(outcome, result)
            self._advance("report")
            self.state.finished = True
            self.state.save(self.paths["state"])

        except (BudgetExhausted, FileNotFoundError) as exc:
            # توقف متوقَّع (نفاد ميزانية أو مرحلة تحتاج اتصالاً): نحفظ الموضع
            # ونرجع بهدوء بدل أن ننهار بأثر استدعاء مخيف.
            outcome.stopped_reason = str(exc)
            self._sync_credits()
            self.state.save(self.paths["state"])
        except Exception as exc:  # نحفظ الموضع قبل أن نُعيد رفع الخطأ
            self._sync_credits()
            self.state.save(self.paths["state"])
            outcome.stopped_reason = f"{type(exc).__name__}: {exc}"
            raise

        outcome.warnings.append(SURVIVORSHIP_WARNING)
        if self.config.research_mode:
            outcome.warnings.append("وضع بحثي: فلتر الهيكل المالي معطّل")
        return outcome

    def _save_report(self, outcome: PipelineOutcome, result: PortfolioResult) -> None:
        sim = result.simulate_portfolio(
            starting_equity=self.config.equity,
            risk_per_trade_pct=self.config.risk_pct,
            max_concurrent=self.config.max_concurrent,
        )
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "universe_size": outcome.universe_size,
            "stage0_size": outcome.stage0_size,
            "candidates": outcome.candidates,
            "credits_total": self.state.credits_total,
            "symbols_tested": result.symbols_tested,
            "total_trades": result.total_trades,
            "win_rate": round(result.win_rate, 4),
            "expectancy_r": round(result.expectancy_r, 4),
            "expectancy_ci": result.expectancy_ci(),
            "statistically_usable": result.is_statistically_usable,
            "research_mode": result.research_mode,
            "funnel": result.aggregate_funnel(),
            "vetoes": result.aggregate_vetoes(),
            "portfolio": {k: v for k, v in sim.items() if k != "curve"},
            "survivorship_warning": SURVIVORSHIP_WARNING,
            "per_symbol": [o.to_json() for o in result.outcomes if o.trades > 0],
        }
        self.paths["report"].parent.mkdir(parents=True, exist_ok=True)
        self.paths["report"].write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
        )
