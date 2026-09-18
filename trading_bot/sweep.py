"""ضبط المعاملات مع حماية من التفصيل على المقاس (Overfitting).

الخطر الذي تعالجه هذه الوحدة: تجريب عشرين إعداداً على نفس البيانات ثم اختيار
أفضلها ليس اكتشافاً بل **انتقاءً للضوضاء**. مع عشرين تجربة، احتمال أن يبدو
أحدها «ممتازاً» بالصدفة وحدها مرتفع جداً.

الحماية بثلاث طبقات:

1) **تقسيم زمني (Walk-forward)**: الضبط على الجزء الأقدم من التاريخ،
   والحكم على الجزء الأحدث الذي لم تره أي تجربة. الإعداد الذي ينجح في
   الأول ويفشل في الثاني هو ضوضاء لا حافة.

2) **تصحيح تعدد الاختبارات**: كلما زادت التجارب اتسع مجال الثقة المطلوب.
   نطبّق تصحيح Bonferroni على مستوى الثقة.

3) **الإعلان الصريح**: كل تقرير يذكر عدد التجارب، فلا يُقرأ الأفضل منها
   كأنه التجربة الوحيدة.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import mean, pstdev
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .backtest import BacktestConfig, Backtester
from .config import BotConfig
from .csvio import load_candles_csv
from .dataquality import adjust_for_splits
from .models import Candle, StockMetrics


@dataclass
class SweepArm:
    """تجربة واحدة: اسم + تعديل على الإعدادات."""

    label: str
    overrides: Dict[str, object]

    def apply(self, base: BotConfig) -> BotConfig:
        return replace(base, **self.overrides)


@dataclass
class ArmResult:
    label: str
    trades: int = 0
    wins: int = 0
    total_r: float = 0.0
    r_values: List[float] = field(default_factory=list)
    funnel: Dict[str, int] = field(default_factory=dict)

    @property
    def expectancy(self) -> float:
        return mean(self.r_values) if self.r_values else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def stdev(self) -> float:
        return pstdev(self.r_values) if len(self.r_values) > 1 else 0.0

    def ci(self, confidence: float = 0.95, resamples: int = 1500, seed: int = 11):
        import random

        if len(self.r_values) < 2:
            return None
        rng = random.Random(seed)
        means = sorted(
            mean(rng.choice(self.r_values) for _ in self.r_values) for _ in range(resamples)
        )
        lo = means[int((1 - confidence) / 2 * resamples)]
        hi = means[min(int((1 + confidence) / 2 * resamples), resamples - 1)]
        return lo, hi


def load_symbol(cache_dir: Path, symbol: str) -> Optional[Tuple[List[Candle], List[Candle]]]:
    path_4h = cache_dir / f"{symbol}_4h.csv"
    path_1d = cache_dir / f"{symbol}_1d.csv"
    if not (path_4h.exists() and path_1d.exists()):
        return None
    c4, _ = adjust_for_splits(load_candles_csv(path_4h))
    c1, _ = adjust_for_splits(load_candles_csv(path_1d))
    return c4, c1


def split_series(
    candles: Sequence[Candle], fraction: float
) -> Tuple[List[Candle], List[Candle]]:
    """تقسيم زمني: (الأقدم للضبط، الأحدث للحكم)."""
    cut = int(len(candles) * fraction)
    return list(candles[:cut]), list(candles[cut:])


def run_arm(
    arm: SweepArm,
    symbols: Sequence[str],
    cache_dir: Path,
    base_bot: BotConfig,
    base_bt: BacktestConfig,
    window: str = "all",
    split_fraction: float = 0.6,
) -> ArmResult:
    """يشغّل تجربة واحدة على كل الرموز ويجمع صفقاتها."""
    result = ArmResult(label=arm.label)
    config = arm.apply(base_bot)

    for symbol in symbols:
        loaded = load_symbol(cache_dir, symbol)
        if loaded is None:
            continue
        c4, c1 = loaded
        if window == "in":
            c4, _ = split_series(c4, split_fraction)
        elif window == "out":
            _, c4 = split_series(c4, split_fraction)
        if len(c4) <= base_bt.warmup_bars + 2:
            continue

        outcome = Backtester(config, base_bt).run(
            symbol, c4, c1, StockMetrics(ticker=symbol)
        )
        for key, value in outcome.funnel.items():
            result.funnel[key] = result.funnel.get(key, 0) + value
        for trade in outcome.closed:
            result.r_values.append(trade.r_multiple)
            result.trades += 1
            if trade.pnl > 0:
                result.wins += 1
            result.total_r += trade.r_multiple
    return result


def bonferroni_confidence(base: float, arms: int) -> float:
    """توسيع مستوى الثقة بعدد التجارب — الثمن العادل لتعدد المحاولات."""
    alpha = (1 - base) / max(arms, 1)
    return 1 - alpha


def format_table(results: Sequence[ArmResult], confidence: float, arms: int) -> str:
    adjusted = bonferroni_confidence(confidence, arms)
    lines = [
        f"{'الإعداد':<28}{'صفقات':>7}{'فوز':>6}{'توقع':>9}{'مجموع R':>10}"
        f"{'مجال الثقة المصحَّح':>26}",
        "─" * 86,
    ]
    for r in results:
        ci = r.ci(adjusted)
        ci_text = f"[{ci[0]:+.3f} , {ci[1]:+.3f}]" if ci else "—"
        verdict = ""
        if ci and ci[0] > 0:
            verdict = " ✔"
        elif ci and ci[1] < 0:
            verdict = " ✗"
        lines.append(
            f"{r.label:<28}{r.trades:>7}{r.win_rate:>6.0%}{r.expectancy:>+9.3f}"
            f"{r.total_r:>+10.1f}{ci_text:>26}{verdict}"
        )
    lines.append("")
    lines.append(
        f"مستوى الثقة مصحَّح بـ Bonferroni لـ {arms} تجربة: "
        f"{adjusted:.3%} بدل {confidence:.0%}"
    )
    return "\n".join(lines)
