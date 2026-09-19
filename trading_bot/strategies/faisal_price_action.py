"""استراتيجية فيصل (@kisar_): تحليل سلوك سعري للشموع الساقطة والدعوم على فريم 4 ساعات.

الفكرة باختصار:
  1) نحدد مناطق الدعم من القيعان المحورية على فريم 4 ساعات (لمستان فأكثر).
  2) نبحث عن سلسلة شموع ساقطة تتجه نحو الدعم (تصحيح وليس انهيار).
  3) نتأكد أن السقوط "استُنزف": الفوليوم يتناقص مع نزول السعر.
  4) نطلب اختبار الدعم بذيل رفض (Rejection) ثم شمعة انعكاسية تغلق فوق الدعم.
  5) نرفض الإعداد نهائياً إذا كُسر الدعم بفوليوم مضاعف (كسر حقيقي وليس اختبار).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import List, Optional, Sequence

from ..config import BotConfig
from ..indicators import atr, average_volume, pivot_lows
from ..models import Candle


@dataclass(frozen=True)
class SupportZone:
    """منطقة دعم أفقية مبنية على تجميع القيعان المتقاربة."""

    low: float
    high: float
    touches: int
    last_touch_index: int

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    @property
    def strength(self) -> float:
        """قوة الدعم: تزداد باللمسات وتشبع عند 4 لمسات."""
        return min(self.touches / 4.0, 1.0)


@dataclass
class PriceActionSetup:
    """نتيجة تحليل السلوك السعري على 4 ساعات."""

    found: bool = False
    score: float = 0.0
    zone: Optional[SupportZone] = None
    entry_reference: Optional[float] = None   # إغلاق شمعة الانعكاس
    stop_reference: Optional[float] = None    # قاع منطقة الاختبار
    atr_4h: Optional[float] = None
    reasons: List[str] = field(default_factory=list)
    rejections: List[str] = field(default_factory=list)
    tested_support_without_high_volume_break: bool = False


class FaisalPriceAction:
    """محلّل السلوك السعري للشموع الساقطة والدعوم (فريم 4 ساعات)."""

    def __init__(self, config: Optional[BotConfig] = None) -> None:
        self.config = config or BotConfig()

    # ── الدعوم ────────────────────────────────────────────────────────
    def find_support_zones(self, candles: Sequence[Candle]) -> List[SupportZone]:
        """تجميع القيعان المحورية المتقاربة في مناطق دعم."""
        cfg = self.config
        window = list(candles[-cfg.support_lookback:])
        if len(window) < 10:
            return []

        lows = [(i, window[i].low) for i in pivot_lows(window)]
        if not lows:
            return []

        lows.sort(key=lambda p: p[1])
        clusters: List[List[tuple]] = []
        for idx, low in lows:
            for cluster in clusters:
                anchor = cluster[0][1]
                if abs(low - anchor) / max(anchor, 1e-9) <= cfg.support_tolerance_pct:
                    cluster.append((idx, low))
                    break
            else:
                clusters.append([(idx, low)])

        zones: List[SupportZone] = []
        for cluster in clusters:
            if len(cluster) < cfg.min_support_touches:
                continue
            values = [low for _, low in cluster]
            base = mean(values)
            zones.append(
                SupportZone(
                    low=min(values) * (1 - cfg.support_tolerance_pct / 2),
                    high=max(base, max(values)) * (1 + cfg.support_tolerance_pct / 2),
                    touches=len(cluster),
                    last_touch_index=max(idx for idx, _ in cluster),
                )
            )
        zones.sort(key=lambda z: z.mid, reverse=True)
        return zones

    # ── الشموع الساقطة ────────────────────────────────────────────────
    def falling_sequence(self, candles: Sequence[Candle]) -> List[Candle]:
        """آخر سلسلة شموع هابطة قبل شمعة الانعكاس الحالية."""
        if len(candles) < 3:
            return []
        seq: List[Candle] = []
        # نتجاهل الشمعة الأخيرة لأنها شمعة الاختبار/الانعكاس المحتملة
        for candle in reversed(candles[:-1]):
            if candle.is_bearish or candle.close < candle.open:
                seq.append(candle)
            elif seq and candle.body_ratio < 0.2:
                seq.append(candle)  # دوجي داخل السقوط لا يكسر التسلسل
            else:
                break
            if len(seq) >= self.config.falling_sequence_max:
                break
        seq.reverse()
        return seq

    def is_exhausted(self, sequence: Sequence[Candle]) -> bool:
        """استنزاف البائعين: فوليوم آخر شمعة ساقطة أقل من متوسط السلسلة."""
        if len(sequence) < 2:
            return False
        avg = mean(c.volume for c in sequence)
        if avg <= 0:
            return False
        return sequence[-1].volume <= avg * self.config.exhaustion_volume_ratio

    # ── الانعكاس ─────────────────────────────────────────────────────
    @staticmethod
    def is_hammer(candle: Candle, wick_ratio: float) -> bool:
        return (
            candle.lower_wick >= max(candle.body, 1e-9) * wick_ratio
            and candle.upper_wick <= candle.body
            and candle.body_ratio <= 0.5
        )

    @staticmethod
    def is_bullish_engulfing(prev: Candle, cur: Candle) -> bool:
        return (
            prev.is_bearish
            and cur.is_bullish
            and cur.close >= prev.open
            and cur.open <= prev.close
        )

    def reversal_kind(self, prev: Candle, cur: Candle) -> Optional[str]:
        if self.is_hammer(cur, self.config.rejection_wick_ratio):
            return "شمعة مطرقة (ذيل رفض عند الدعم)"
        if self.is_bullish_engulfing(prev, cur):
            return "ابتلاع شرائي فوق الدعم"
        if cur.is_bullish and cur.close > prev.high:
            return "إغلاق فوق قمة شمعة السقوط الأخيرة"
        return None

    def broke_support_on_volume(
        self, candles: Sequence[Candle], zone: SupportZone
    ) -> bool:
        """كسر حقيقي للدعم: إغلاق تحت المنطقة بفوليوم مضاعف."""
        avg_vol = average_volume(candles, min(20, len(candles))) or 0.0
        for candle in candles[-self.config.falling_sequence_max:]:
            broke = candle.close < zone.low
            heavy = avg_vol > 0 and candle.volume >= avg_vol * self.config.break_volume_multiple
            if broke and heavy:
                return True
        return False

    # ── التحليل الكامل ───────────────────────────────────────────────
    def analyze(self, candles_4h: Sequence[Candle]) -> PriceActionSetup:
        cfg = self.config
        setup = PriceActionSetup()

        if len(candles_4h) < 20:
            setup.rejections.append("بيانات 4H غير كافية للتحليل")
            return setup

        zones = self.find_support_zones(candles_4h)
        if not zones:
            setup.rejections.append("لا توجد منطقة دعم معتبرة (لمستان فأكثر)")
            return setup

        last = candles_4h[-1]
        prev = candles_4h[-2]

        zone = self._nearest_zone_below(zones, last)
        if zone is None:
            if last.close < min(z.low for z in zones):
                setup.rejections.append("الإغلاق تحت جميع مناطق الدعم — كسر هيكلي")
            else:
                setup.rejections.append("السعر بعيد عن أقرب دعم — لا يوجد ارتكاز")
            return setup
        setup.zone = zone
        setup.atr_4h = atr(candles_4h, min(14, len(candles_4h) - 1))

        # 1) كسر الدعم بفوليوم = رفض فوري
        if self.broke_support_on_volume(candles_4h, zone):
            setup.rejections.append("كسر الدعم بفوليوم مضاعف — الهيكل تالف")
            return setup
        setup.tested_support_without_high_volume_break = True

        score = 0.0

        # 2) سلسلة الشموع الساقطة
        sequence = self.falling_sequence(candles_4h)
        if len(sequence) < cfg.falling_sequence_min:
            setup.rejections.append(
                f"لا توجد سلسلة شموع ساقطة كافية ({len(sequence)} < {cfg.falling_sequence_min})"
            )
            return setup

        drop = (sequence[0].high - min(c.low for c in sequence)) / max(sequence[0].high, 1e-9)
        if drop > cfg.max_drop_pct_in_fall:
            setup.rejections.append(
                f"عمق السقوط {drop:.0%} يتجاوز الحد — انهيار وليس تصحيحاً"
            )
            return setup
        score += 20
        setup.reasons.append(
            f"سلسلة {len(sequence)} شموع ساقطة بعمق {drop:.0%} نحو الدعم"
        )

        # 3) استنزاف البائعين
        if self.is_exhausted(sequence):
            score += 20
            setup.reasons.append("تناقص الفوليوم مع السقوط — استنزاف البائعين")
        else:
            setup.reasons.append("تنبيه: الفوليوم لم يتناقص بوضوح أثناء السقوط")

        # 4) اختبار الدعم فعلياً
        tested = zone.contains(last.low) or zone.contains(prev.low)
        if not tested:
            setup.rejections.append("لم يحدث اختبار فعلي لمنطقة الدعم بعد")
            return setup
        score += 20 * (0.5 + 0.5 * zone.strength)
        setup.reasons.append(
            f"اختبار دعم {zone.low:.4f}–{zone.high:.4f} بعدد {zone.touches} لمسات"
        )

        # 5) شمعة الانعكاس والإغلاق فوق الدعم
        kind = self.reversal_kind(prev, last)
        if kind is None:
            setup.score = min(score, 55.0)
            setup.rejections.append("لا توجد شمعة انعكاسية مؤكدة بعد — مراقبة")
            return setup
        if last.close < zone.low:
            setup.rejections.append("الإغلاق تحت منطقة الدعم — لا دخول")
            return setup

        score += 25
        setup.reasons.append(kind)

        # 6) مكافأة إضافية إذا جاء الانعكاس بفوليوم أعلى من السقوط
        avg_fall_vol = mean(c.volume for c in sequence) if sequence else 0.0
        if avg_fall_vol > 0 and last.volume >= avg_fall_vol:
            score += 15
            setup.reasons.append("فوليوم شمعة الانعكاس أعلى من متوسط السقوط")

        setup.found = True
        setup.score = min(score, 100.0)
        setup.entry_reference = last.close
        setup.stop_reference = min(last.low, prev.low, zone.low)
        return setup

    def _nearest_zone_below(
        self, zones: Sequence[SupportZone], last: Candle
    ) -> Optional[SupportZone]:
        """أقرب دعم يلامسه السعر الحالي أو يقف فوقه مباشرة."""
        cfg = self.config
        candidates = [z for z in zones if z.low <= last.close * (1 + cfg.support_tolerance_pct)]
        if not candidates:
            return None
        zone = max(candidates, key=lambda z: z.mid)
        distance = (last.close - zone.high) / max(last.close, 1e-9)
        if distance > cfg.support_tolerance_pct * 2:
            return None
        return zone
