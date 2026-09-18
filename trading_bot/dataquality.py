"""فحص جودة البيانات قبل أي تحليل أو backtest.

الدرس المستفاد من AEMD: سلسلة Twelve Data اليومية لم تكن معدّلة للتجزئة
العكسية 1:5 بينما سلسلة 4 ساعات كانت معدّلة — ما خلق قفزة وهمية +442% داخل
البيانات اليومية. أي مؤشر يمرّ فوق هذه القفزة يعطي رقماً بلا معنى.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .models import Candle

# نسب التجزئة الشائعة (عادية وعكسية)
COMMON_RATIOS: Tuple[float, ...] = (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 50, 100)


@dataclass
class DataQualityReport:
    ok: bool = True
    split_gaps: List[int] = field(default_factory=list)   # مواقع الفجوات المشبوهة
    issues: List[str] = field(default_factory=list)
    inferred_ratios: List[float] = field(default_factory=list)

    def __bool__(self) -> bool:  # pragma: no cover - راحة استخدام
        return self.ok


def _nearest_common_ratio(raw: float, tolerance: float = 0.12) -> Optional[float]:
    """أقرب نسبة تجزئة شائعة، أو None إذا كانت الفجوة لا تشبه تجزئة."""
    for ratio in COMMON_RATIOS:
        if abs(raw - ratio) / ratio <= tolerance:
            return float(ratio)
        if abs(raw - 1 / ratio) * ratio <= tolerance:
            return 1.0 / ratio
    return None


def detect_split_gaps(
    candles: Sequence[Candle],
    threshold: float = 0.60,
    max_volume_ratio: float = 3.0,
    volume_lookback: int = 10,
) -> DataQualityReport:
    """رصد القفزات التي يرجّح أنها تجزئة غير معدّلة وليست حركة سوق.

    التمييز الحاسم هو **الفوليوم**، لا السعر وحده:
      • التجزئة حدث محاسبي: السعر يقفز والنشاط لا يتغير (بل يقل بنفس النسبة
        في التجزئة العكسية لأن عدد الأسهم نفسه انخفض).
      • الخبر الحقيقي: السعر يقفز **والفوليوم ينفجر** عشرات أو مئات الأضعاف.

    مثال محفور من AEMD: تجزئة 1:5 في 31 يوليو رافقها انخفاض الفوليوم من
    1.27M إلى 163K، بينما قفزة اندماج 17 سبتمبر (+493%) رافقها ارتفاع من
    42,800 إلى 92.4 مليون سهم. الأولى تصحَّح، والثانية تُترك كما هي.
    """
    report = DataQualityReport()
    for i in range(1, len(candles)):
        prev, cur = candles[i - 1], candles[i]
        if prev.close <= 0:
            continue
        change = (cur.open - prev.close) / prev.close
        if abs(change) < threshold:
            continue

        ratio = _nearest_common_ratio(cur.open / prev.close)
        if ratio is None:
            report.issues.append(
                f"فجوة {change:+.0%} عند الشمعة {i} — لا تطابق أي نسبة تجزئة شائعة"
            )
            continue

        window = [c.volume for c in candles[max(0, i - volume_lookback):i] if c.volume > 0]
        avg_volume = sum(window) / len(window) if window else 0.0
        volume_ratio = (cur.volume / avg_volume) if avg_volume > 0 else None

        if volume_ratio is not None and volume_ratio > max_volume_ratio:
            report.issues.append(
                f"فجوة {change:+.0%} عند الشمعة {i} بفوليوم {volume_ratio:.0f}× المعدل "
                "— حدث سوقي حقيقي وليس تجزئة، لن تُصحَّح"
            )
            continue

        if volume_ratio is None:
            report.issues.append(
                f"فجوة {change:+.0%} عند الشمعة {i} بلا بيانات فوليوم — "
                "يُشتبه بتجزئة لكن لن تُصحَّح تلقائياً"
            )
            report.ok = False
            continue

        report.ok = False
        report.split_gaps.append(i)
        report.inferred_ratios.append(ratio)
        report.issues.append(
            f"فجوة {change:+.0%} عند الشمعة {i} تطابق تجزئة {ratio:g}:1 "
            f"(الفوليوم {volume_ratio:.2f}× المعدل) — البيانات غير معدّلة"
        )
    return report


def adjust_for_splits(
    candles: Sequence[Candle], threshold: float = 0.60, max_volume_ratio: float = 3.0
) -> Tuple[List[Candle], DataQualityReport]:
    """تصحيح السلسلة بضرب ما قبل التجزئة في النسبة المستنتجة.

    يرجع (السلسلة المصححة، التقرير). إذا لم تُرصد تجزئة تُرجع السلسلة كما هي.
    """
    report = detect_split_gaps(candles, threshold, max_volume_ratio)
    if not report.split_gaps:
        return list(candles), report

    out = list(candles)
    # نعالج من الأحدث إلى الأقدم كي تتراكم النسب على كل ما سبقها
    for index, ratio in zip(reversed(report.split_gaps), reversed(report.inferred_ratios)):
        factor = ratio
        for i in range(index):
            c = out[i]
            out[i] = Candle(
                ts=c.ts,
                open=c.open * factor,
                high=c.high * factor,
                low=c.low * factor,
                close=c.close * factor,
                volume=c.volume / factor if factor else c.volume,
            )
    return out, report


def check_series(candles: Sequence[Candle], name: str = "series") -> DataQualityReport:
    """فحص شامل: ترتيب زمني، تكرار، قيم غير منطقية، وتجزئة غير معدّلة."""
    report = detect_split_gaps(candles)

    if len(candles) < 2:
        report.issues.append(f"{name}: سلسلة قصيرة جداً")
        report.ok = False
        return report

    if any(a.ts >= b.ts for a, b in zip(candles, candles[1:])):
        report.ok = False
        report.issues.append(f"{name}: الترتيب الزمني غير تصاعدي أو فيه تكرار")

    for i, c in enumerate(candles):
        if c.high < c.low or c.high < max(c.open, c.close) or c.low > min(c.open, c.close):
            report.ok = False
            report.issues.append(f"{name}: شمعة غير منطقية عند {i} (high/low متعارضان)")
            break
        if min(c.open, c.high, c.low, c.close) <= 0:
            report.ok = False
            report.issues.append(f"{name}: سعر صفري أو سالب عند {i}")
            break
    return report
