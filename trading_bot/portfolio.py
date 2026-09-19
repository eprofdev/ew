"""تشغيل الـ backtest على كون كامل من الأسهم وتجميع النتيجة.

لماذا لا نكتفي بجمع نتائج الرموز؟ لأن جمع منحنيات رأس المال خطأ شائع: لا
يمكنك فتح 40 صفقة متزامنة بـ 1% مخاطرة لكل واحدة. لذلك نفصل مستويين:

  • **التجميع الإحصائي**: كل الصفقات في سلة واحدة لقياس التوقع بوحدات R
    ومجال ثقته. هذا هو الحكم على الاستراتيجية نفسها.
  • **محاكاة المحفظة**: ترتيب الصفقات زمنياً مع سقف للصفقات المتزامنة،
    فينتج منحنى رأس مال واقعي. هذا هو الحكم على قابلية التطبيق.

كلاهما مطلوب: استراتيجية بتوقع ممتاز قد تكون غير قابلة للتطبيق لأن كل
إشاراتها تأتي في نفس اللحظة.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, pstdev
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .backtest import BacktestConfig, Backtester, Trade
from .config import BotConfig
from .csvio import load_candles_csv, save_candles_csv
from .dataquality import adjust_for_splits
from .models import Candle, StockMetrics

MIN_TRADES_FOR_INFERENCE = 30


@dataclass
class SymbolOutcome:
    symbol: str
    bars_tested: int = 0
    trades: int = 0
    wins: int = 0
    expectancy_r: float = 0.0
    total_r: float = 0.0
    net_pnl: float = 0.0
    error: Optional[str] = None
    funnel: Dict[str, int] = field(default_factory=dict)
    veto_counts: Dict[str, int] = field(default_factory=dict)
    data_notes: List[str] = field(default_factory=list)
    # تفاصيل الصفقات تُحفظ كي لا يفقدها الاستئناف — بدونها ينهار التجميع
    trade_rows: List[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "symbol": self.symbol,
            "bars_tested": self.bars_tested,
            "trades": self.trades,
            "wins": self.wins,
            "expectancy_r": round(self.expectancy_r, 4),
            "total_r": round(self.total_r, 4),
            "net_pnl": round(self.net_pnl, 2),
            "error": self.error,
            "funnel": self.funnel,
            "veto_counts": self.veto_counts,
            "data_notes": self.data_notes,
            "trade_rows": self.trade_rows,
        }


@dataclass
class PooledTrade:
    symbol: str
    entry_ts: int
    exit_ts: int
    r_multiple: float
    pnl: float
    exit_reason: str


@dataclass
class PortfolioResult:
    outcomes: List[SymbolOutcome] = field(default_factory=list)
    pooled: List[PooledTrade] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    research_mode: bool = False
    warnings: List[str] = field(default_factory=list)

    # ── تجميع إحصائي ─────────────────────────────────────────────────
    @property
    def symbols_tested(self) -> int:
        return sum(1 for o in self.outcomes if o.error is None)

    @property
    def symbols_failed(self) -> List[SymbolOutcome]:
        return [o for o in self.outcomes if o.error is not None]

    @property
    def symbols_with_trades(self) -> int:
        return sum(1 for o in self.outcomes if o.trades > 0)

    @property
    def total_trades(self) -> int:
        return len(self.pooled)

    @property
    def win_rate(self) -> float:
        if not self.pooled:
            return 0.0
        return sum(1 for t in self.pooled if t.pnl > 0) / len(self.pooled)

    @property
    def expectancy_r(self) -> float:
        return mean(t.r_multiple for t in self.pooled) if self.pooled else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.pooled if t.pnl > 0)
        pains = abs(sum(t.pnl for t in self.pooled if t.pnl <= 0))
        if pains == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / pains

    @property
    def r_stdev(self) -> float:
        values = [t.r_multiple for t in self.pooled]
        return pstdev(values) if len(values) > 1 else 0.0

    @property
    def is_statistically_usable(self) -> bool:
        return self.total_trades >= MIN_TRADES_FOR_INFERENCE

    def expectancy_ci(self, confidence: float = 0.95, resamples: int = 2000,
                      seed: int = 7) -> Optional[Tuple[float, float]]:
        values = [t.r_multiple for t in self.pooled]
        if len(values) < 2:
            return None
        rng = random.Random(seed)
        means = sorted(mean(rng.choice(values) for _ in values) for _ in range(resamples))
        return (
            means[int((1 - confidence) / 2 * resamples)],
            means[int((1 + confidence) / 2 * resamples) - 1],
        )

    def aggregate_funnel(self) -> Dict[str, int]:
        total: Dict[str, int] = {}
        for outcome in self.outcomes:
            for key, value in outcome.funnel.items():
                total[key] = total.get(key, 0) + value
        return total

    def aggregate_vetoes(self) -> Dict[str, int]:
        total: Dict[str, int] = {}
        for outcome in self.outcomes:
            for key, value in outcome.veto_counts.items():
                total[key] = total.get(key, 0) + value
        return total

    # ── محاكاة محفظة واقعية ──────────────────────────────────────────
    def simulate_portfolio(
        self,
        starting_equity: float = 10_000.0,
        risk_per_trade_pct: float = 0.01,
        max_concurrent: int = 5,
    ) -> dict:
        """منحنى رأس مال واقعي بسقف للصفقات المتزامنة.

        كل صفقة تخاطر بنسبة ثابتة من رأس المال الحالي، وتُرفض الإشارة إن كانت
        الفتحات ممتلئة — وهذا ما يحدث فعلاً عند التطبيق.
        """
        equity = starting_equity
        peak = starting_equity
        max_dd = 0.0
        open_until: List[int] = []
        taken = skipped = 0
        curve: List[Tuple[int, float]] = [(0, starting_equity)]

        for trade in sorted(self.pooled, key=lambda t: t.entry_ts):
            open_until = [ts for ts in open_until if ts > trade.entry_ts]
            if len(open_until) >= max_concurrent:
                skipped += 1
                continue
            risk_amount = equity * risk_per_trade_pct
            equity += trade.r_multiple * risk_amount
            open_until.append(trade.exit_ts)
            taken += 1
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)
            curve.append((trade.exit_ts, equity))

        return {
            "starting_equity": starting_equity,
            "ending_equity": equity,
            "return_pct": (equity / starting_equity - 1) if starting_equity else 0.0,
            "max_drawdown_pct": max_dd,
            "trades_taken": taken,
            "trades_skipped_full": skipped,
            "max_concurrent": max_concurrent,
            "curve": curve,
        }

    # ── التقرير ──────────────────────────────────────────────────────
    def summary(self, top: int = 10) -> str:
        lines = ["═" * 70, "نتيجة اختبار الكون الكامل", "═" * 70]
        duration = (self.finished_at - self.started_at) if self.finished_at else 0.0
        lines += [
            f"  رموز مختبرة        : {self.symbols_tested}  (فشل: {len(self.symbols_failed)})",
            f"  رموز أنتجت صفقات   : {self.symbols_with_trades}",
            f"  إجمالي الصفقات     : {self.total_trades}",
            f"  زمن التشغيل        : {duration/60:.1f} دقيقة",
        ]
        if self.pooled:
            pf = "∞" if self.profit_factor == float("inf") else f"{self.profit_factor:.2f}"
            lines += [
                f"  نسبة الربح         : {self.win_rate:.0%}",
                f"  التوقع لكل صفقة    : {self.expectancy_r:+.3f}R",
                f"  الانحراف المعياري  : {self.r_stdev:.2f}R",
                f"  عامل الربح         : {pf}",
                f"  مجموع R            : {sum(t.r_multiple for t in self.pooled):+.1f}R",
            ]
            ci = self.expectancy_ci()
            if ci:
                lines.append(f"  مجال ثقة 95%       : [{ci[0]:+.3f}R , {ci[1]:+.3f}R]")
                if ci[0] <= 0 <= ci[1]:
                    lines.append("  ⛔ المجال يشمل الصفر — لم تثبت أفضلية إحصائية")
                elif ci[0] > 0:
                    lines.append("  ✔ المجال كله موجب — أفضلية إحصائية قائمة على هذه العينة")
            if not self.is_statistically_usable:
                lines.append(
                    f"  ⛔ العينة {self.total_trades} صفقة — الحد الأدنى {MIN_TRADES_FOR_INFERENCE}"
                )
        else:
            lines.append("  لا توجد صفقات إطلاقاً — راجع قمع الفرص أدناه")

        funnel = self.aggregate_funnel()
        if funnel:
            lines.append("  قمع الفرص المجمّع:")
            for key, value in sorted(funnel.items(), key=lambda kv: -kv[1]):
                lines.append(f"      {key:<20} {value:>10,}")
        vetoes = self.aggregate_vetoes()
        if vetoes:
            top_vetoes = sorted(vetoes.items(), key=lambda kv: -kv[1])[:6]
            lines.append("  أسباب الرفض: " + ", ".join(f"{k}×{v:,}" for k, v in top_vetoes))

        ranked = sorted(
            [o for o in self.outcomes if o.trades > 0], key=lambda o: o.total_r, reverse=True
        )
        if ranked:
            lines += ["", f"  أفضل {min(top, len(ranked))} رموز بمجموع R:"]
            for outcome in ranked[:top]:
                lines.append(
                    f"      {outcome.symbol:<8} صفقات {outcome.trades:>3}  "
                    f"مجموع {outcome.total_r:>+7.1f}R  توقع {outcome.expectancy_r:>+6.2f}R"
                )

        if self.research_mode:
            lines += ["", "  ⚠ وضع بحثي: فلتر الهيكل المالي معطّل — النتيجة لا تمثل البوت الحقيقي"]
        for warning in self.warnings:
            lines.append(f"  ⚠ {warning}")
        return "\n".join(lines)


def _pooled_from(outcome: SymbolOutcome) -> List[PooledTrade]:
    """تحويل صفوف صفقات الرمز إلى صفقات مجمّعة (يعمل مع المحسوب والمستأنف)."""
    return [
        PooledTrade(
            symbol=outcome.symbol,
            entry_ts=int(row.get("entry_ts", 0)),
            exit_ts=int(row.get("exit_ts", 0)),
            r_multiple=float(row.get("r", 0.0)),
            pnl=float(row.get("pnl", 0.0)),
            exit_reason=str(row.get("reason", "")),
        )
        for row in outcome.trade_rows
    ]


class PortfolioBacktester:
    """يشغّل الاختبار على قائمة رموز مع تخزين مؤقت واستئناف."""

    def __init__(
        self,
        provider=None,
        bot_config: Optional[BotConfig] = None,
        backtest_config: Optional[BacktestConfig] = None,
        cache_dir: str | Path = "data/cache",
        bars_4h: int = 1000,
        bars_1d: int = 400,
        fundamentals: Optional[Dict[str, dict]] = None,
    ) -> None:
        self.provider = provider
        self.bot_config = bot_config or BotConfig()
        self.backtest_config = backtest_config or BacktestConfig()
        self.cache_dir = Path(cache_dir)
        self.bars_4h = bars_4h
        self.bars_1d = bars_1d
        self.fundamentals = {k.upper(): v for k, v in (fundamentals or {}).items()}

    # ── البيانات ─────────────────────────────────────────────────────
    def _cache_paths(self, symbol: str) -> Tuple[Path, Path]:
        return (
            self.cache_dir / f"{symbol}_4h.csv",
            self.cache_dir / f"{symbol}_1d.csv",
        )

    def load_candles(self, symbol: str) -> Tuple[List[Candle], List[Candle]]:
        """من الذاكرة المؤقتة إن وُجدت، وإلا من المزوّد ثم تُحفظ."""
        path_4h, path_1d = self._cache_paths(symbol)
        if path_4h.exists() and path_1d.exists():
            return load_candles_csv(path_4h), load_candles_csv(path_1d)
        if self.provider is None:
            raise FileNotFoundError(f"لا توجد بيانات مخزّنة لـ {symbol} ولا مزوّد متصل")

        c4 = list(self.provider.get_candles(symbol, "4h", self.bars_4h))
        c1 = list(self.provider.get_candles(symbol, "1day", self.bars_1d))
        if c4:
            save_candles_csv(path_4h, c4)
        if c1:
            save_candles_csv(path_1d, c1)
        return c4, c1

    def metrics_for(self, symbol: str) -> StockMetrics:
        row = self.fundamentals.get(symbol.upper(), {})
        return StockMetrics(
            ticker=symbol,
            market_cap=row.get("market_cap"),
            free_float=row.get("free_float"),
            avg_daily_volume=row.get("avg_daily_volume", 0.0) or 0.0,
            has_active_shelf_offering=bool(row.get("has_active_shelf_offering", False)),
            recent_reverse_split_days=row.get("recent_reverse_split_days"),
        )

    # ── رمز واحد ─────────────────────────────────────────────────────
    def run_symbol(self, symbol: str) -> SymbolOutcome:
        outcome = SymbolOutcome(symbol=symbol)
        try:
            c4, c1 = self.load_candles(symbol)
        except Exception as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
            return outcome

        if len(c4) <= self.backtest_config.warmup_bars + 2:
            outcome.error = f"بيانات 4H غير كافية ({len(c4)} شمعة)"
            return outcome

        c4, report_4h = adjust_for_splits(c4)
        c1, report_1d = adjust_for_splits(c1)
        outcome.data_notes = report_4h.issues + report_1d.issues

        result = Backtester(self.bot_config, self.backtest_config).run(
            symbol, c4, c1, self.metrics_for(symbol)
        )
        outcome.bars_tested = result.bars_tested
        outcome.funnel = dict(result.funnel)
        outcome.veto_counts = dict(result.veto_counts)
        closed = result.closed
        outcome.trades = len(closed)
        outcome.wins = len(result.wins)
        outcome.expectancy_r = result.expectancy_r
        outcome.total_r = sum(t.r_multiple for t in closed)
        outcome.net_pnl = result.net_pnl
        outcome.trade_rows = [
            {
                "entry_ts": t.entry_ts,
                "exit_ts": t.exits[-1].ts if t.exits else t.entry_ts,
                "r": round(t.r_multiple, 6),
                "pnl": round(t.pnl, 4),
                "reason": t.exit_reason,
            }
            for t in closed
        ]
        return outcome

    # ── الكون كله ────────────────────────────────────────────────────
    def run(
        self,
        symbols: Sequence[str],
        checkpoint: Optional[str | Path] = None,
        resume: bool = True,
        progress: Optional[Callable[[int, int, SymbolOutcome], None]] = None,
        stop_after_failures: int = 0,
    ) -> PortfolioResult:
        result = PortfolioResult(
            started_at=time.time(),
            research_mode=not self.bot_config.require_fundamentals,
        )
        done: Dict[str, dict] = {}
        checkpoint_path = Path(checkpoint) if checkpoint else None
        if checkpoint_path and resume and checkpoint_path.exists():
            for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    done[row["symbol"]] = row
                except (ValueError, KeyError):
                    continue
            result.warnings.append(f"استئناف: {len(done)} رمزاً محسوباً مسبقاً")

        if checkpoint_path:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

        consecutive_failures = 0
        for index, symbol in enumerate(symbols, start=1):
            if symbol in done:
                row = done[symbol]
                outcome = SymbolOutcome(
                    trade_rows=row.get("trade_rows", []),
                    symbol=symbol,
                    bars_tested=row.get("bars_tested", 0),
                    trades=row.get("trades", 0),
                    wins=row.get("wins", 0),
                    expectancy_r=row.get("expectancy_r", 0.0),
                    total_r=row.get("total_r", 0.0),
                    net_pnl=row.get("net_pnl", 0.0),
                    error=row.get("error"),
                    funnel=row.get("funnel", {}),
                    veto_counts=row.get("veto_counts", {}),
                    data_notes=row.get("data_notes", []),
                )
                result.outcomes.append(outcome)
                result.pooled.extend(_pooled_from(outcome))
                continue

            outcome = self.run_symbol(symbol)
            result.outcomes.append(outcome)
            result.pooled.extend(_pooled_from(outcome))

            if checkpoint_path:
                with checkpoint_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(outcome.to_json(), ensure_ascii=False) + "\n")

            if outcome.error:
                consecutive_failures += 1
                if stop_after_failures and consecutive_failures >= stop_after_failures:
                    result.warnings.append(
                        f"توقف بعد {consecutive_failures} إخفاقاً متتالياً — تحقق من المفتاح أو الحدود"
                    )
                    break
            else:
                consecutive_failures = 0

            if progress:
                progress(index, len(symbols), outcome)

        result.finished_at = time.time()
        return result


# قياس فعلي على هذه الآلة: 1160 شمعة 4 ساعات لرمز واحد = 0.37 ثانية
SECONDS_PER_1000_BARS = 0.32


def estimate_cost(
    symbol_count: int,
    requests_per_symbol: int = 2,
    requests_per_minute: int = 8,
    daily_limit: Optional[int] = 800,
    bars_per_symbol: int = 1000,
) -> dict:
    """تقدير صادق للتكلفة قبل تشغيل شيء قد يستغرق أياماً.

    القيد الحقيقي هو حدود الـ API لا المعالج: الحساب لكون ناسداك كامل أقل من
    نصف ساعة، بينما جلب بياناته على الخطة المجانية يتجاوز تسعة أيام.
    """
    total = symbol_count * requests_per_symbol
    minutes = total / max(requests_per_minute, 1)
    compute_seconds = symbol_count * bars_per_symbol / 1000 * SECONDS_PER_1000_BARS
    return {
        "requests": total,
        "minutes": minutes,
        "hours": minutes / 60,
        "days_by_daily_limit": (total / daily_limit) if daily_limit else 0.0,
        "compute_hours": compute_seconds / 3600,
        "bottleneck": "API" if minutes * 60 > compute_seconds else "CPU",
    }
