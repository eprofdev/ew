"""مرشّح الأسهم: كيف نختار *ماذا* نراقب أصلاً قبل أن نحلل *متى* ندخل.

منهجية القمع (Funnel) بثلاث طبقات — الترتيب مقصود لأن الأرخص يسبق الأغلى:

    الطبقة 1 — استبعاد صلب (Hard gates)
        نطاق السعر، سيولة دنيا، حجم الفلوت، واستبعاد ما لا يُتداول أصلاً.
        غرضها تقليص آلاف الرموز إلى عشرات بأقل تكلفة بيانات.

    الطبقة 2 — ترتيب بالنقاط (Ranking)
        الفوليوم النسبي (RVOL) هو العامل الأقوى إجماعاً لحركة اليوم،
        يليه ضيق الفلوت، ثم الموقع من المدى السنوي، ثم وقود الشورت.

    الطبقة 3 — عقوبات المخاطر (Penalties)
        تجزئة عكسية حديثة، عرض أسهم فعّال، ترويج جروبات، وامتداد سعري
        بالفعل (لا تشترِ ما ارتفع 40% اليوم — هذا شراء من القمة).

تحذير منهجي صريح: أوزان الترتيب **فرضية وليست نتيجة مثبتة**. الحكم عليها من
`backtest.py` وحده. غيّرها في `ScreenerConfig` واختبر، ولا تثق بأي رقم هنا
لأنه "يبدو معقولاً".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .models import MarketSnapshot


@dataclass
class ScreenerConfig:
    # ── الطبقة 1: استبعاد صلب ────────────────────────────────────────
    min_price: float = 0.50
    max_price: float = 5.00
    sweet_spot: Tuple[float, float] = (1.0, 3.0)   # النطاق الأكثر حركة
    min_session_volume: float = 500_000.0
    min_rvol: float = 2.0
    max_float: float = 25_000_000.0
    max_spread_pct: float = 0.06
    # True = الفلوت المجهول يُسقط الرمز (المسار الآلي، فشل آمن).
    # False = يمرّ مع تحذير صريح (المسار البشري: القرار لك، فلا تُحجب عنك
    # المعلومة — لكن جهل الفلوت يبقى معروضاً كنقطة عمياء).
    require_known_float: bool = True

    # ── الطبقة 2: أوزان الترتيب (المجموع 100) ────────────────────────
    weight_rvol: float = 35.0
    weight_float: float = 25.0
    weight_price_band: float = 10.0
    weight_range_position: float = 15.0
    weight_short_fuel: float = 15.0

    # ── الطبقة 3: عقوبات ─────────────────────────────────────────────
    penalty_reverse_split: float = 25.0
    penalty_shelf: float = 20.0
    penalty_promoted: float = 40.0
    penalty_extended: float = 30.0
    extended_threshold: float = 0.30   # ارتفاع اليوم الذي يُعدّ امتداداً

    min_score: float = 40.0


@dataclass
class Candidate:
    ticker: str
    score: float
    rvol: float = 0.0
    reasons: List[str] = field(default_factory=list)
    penalties: List[str] = field(default_factory=list)
    rejected: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.rejected is None


class Screener:
    """يرتّب قائمة مراقبة بدل أن يفحص السوق كله عشوائياً."""

    def __init__(self, config: Optional[ScreenerConfig] = None) -> None:
        self.config = config or ScreenerConfig()

    # ── الطبقة 1 ─────────────────────────────────────────────────────
    def hard_gate(self, snapshot: MarketSnapshot) -> Optional[str]:
        cfg = self.config
        m, rt = snapshot.metrics, snapshot.realtime

        if rt.halted:
            return "موقوف عن التداول"
        if not (cfg.min_price <= rt.price <= cfg.max_price):
            return f"السعر {rt.price:.2f} خارج النطاق {cfg.min_price}–{cfg.max_price}"
        if m.free_float is None and cfg.require_known_float:
            return "الفلوت غير معروف"
        if m.free_float is not None and m.free_float > cfg.max_float:
            return f"الفلوت {m.free_float:,.0f} أكبر من الحد"
        if rt.session_volume < cfg.min_session_volume:
            return f"فوليوم الجلسة {rt.session_volume:,.0f} أقل من الحد"
        if rt.spread_pct > cfg.max_spread_pct:
            return f"الفرق طلب/عرض {rt.spread_pct:.1%} واسع"

        rvol = self.rvol(snapshot)
        if rvol < cfg.min_rvol:
            return f"الفوليوم النسبي {rvol:.1f}× أقل من {cfg.min_rvol}×"
        return None

    @staticmethod
    def rvol(snapshot: MarketSnapshot) -> float:
        """الفوليوم النسبي: فوليوم الجلسة ÷ المتوسط اليومي."""
        adv = snapshot.metrics.avg_daily_volume
        return snapshot.realtime.session_volume / adv if adv else 0.0

    # ── الطبقة 2 + 3 ─────────────────────────────────────────────────
    def score(self, snapshot: MarketSnapshot) -> Candidate:
        cfg = self.config
        m, rt = snapshot.metrics, snapshot.realtime
        candidate = Candidate(ticker=snapshot.ticker, score=0.0, rvol=self.rvol(snapshot))

        rejection = self.hard_gate(snapshot)
        if rejection:
            candidate.rejected = rejection
            return candidate

        score = 0.0

        # الفوليوم النسبي — تشبع عند 10×
        rvol_score = min(candidate.rvol / 10.0, 1.0) * cfg.weight_rvol
        score += rvol_score
        candidate.reasons.append(f"فوليوم نسبي {candidate.rvol:.1f}×")

        if m.free_float is None:
            candidate.penalties.append("الفلوت غير معروف — تحقق منه بنفسك قبل الدخول")

        # ضيق الفلوت — كلما صغر زادت حدة الحركة
        if m.free_float and m.free_float > 0:
            tightness = max(0.0, 1.0 - (m.free_float / cfg.max_float))
            score += tightness * cfg.weight_float
            candidate.reasons.append(f"فلوت {m.free_float:,.0f} سهم")

        # النطاق السعري الأكثر حركة
        low, high = cfg.sweet_spot
        if low <= rt.price <= high:
            score += cfg.weight_price_band
            candidate.reasons.append(f"السعر {rt.price:.2f} داخل النطاق الأنشط")

        # الموقع من مدى الشموع الأخيرة: نفضّل القريب من القاع لا من القمة
        position = self._range_position(snapshot)
        if position is not None:
            score += (1.0 - position) * cfg.weight_range_position
            candidate.reasons.append(f"الموقع من المدى الأخير {position:.0%} (0%=قاع)")

        # وقود الشورت
        if rt.current_short_interest > 0:
            fuel = min(rt.current_short_interest / 0.30, 1.0)
            score += fuel * cfg.weight_short_fuel
            candidate.reasons.append(f"شورت {rt.current_short_interest:.0%} من الفلوت")

        # ── العقوبات ─────────────────────────────────────────────────
        if m.recent_reverse_split_days is not None and m.recent_reverse_split_days <= 90:
            score -= cfg.penalty_reverse_split
            candidate.penalties.append(
                f"تجزئة عكسية قبل {m.recent_reverse_split_days} يوماً"
            )
        if m.has_active_shelf_offering:
            score -= cfg.penalty_shelf
            candidate.penalties.append("عرض أسهم فعّال (تخفيف)")
        if snapshot.promoted_by_groups or snapshot.paid_indicator_signal:
            score -= cfg.penalty_promoted
            candidate.penalties.append("ترويج جروبات أو مؤشر تجاري")
        if rt.premarket_change_pct > cfg.extended_threshold:
            score -= cfg.penalty_extended
            candidate.penalties.append(
                f"ممتد {rt.premarket_change_pct:.0%} بالفعل — شراء من القمة"
            )

        candidate.score = max(0.0, min(score, 100.0))
        if candidate.score < cfg.min_score:
            candidate.rejected = f"النقاط {candidate.score:.0f} أقل من الحد {cfg.min_score:.0f}"
        return candidate

    @staticmethod
    def _range_position(snapshot: MarketSnapshot, lookback: int = 60) -> Optional[float]:
        """موقع السعر بين قاع وقمة آخر N شمعة (0 = عند القاع)."""
        candles = list(snapshot.candles_4h)[-lookback:]
        if len(candles) < 5:
            return None
        low = min(c.low for c in candles)
        high = max(c.high for c in candles)
        if high <= low:
            return None
        return max(0.0, min((snapshot.realtime.price - low) / (high - low), 1.0))

    def rank(self, snapshots: Sequence[MarketSnapshot]) -> List[Candidate]:
        """يرجع المرشحين الناجحين مرتبين بالأقوى أولاً."""
        scored = [self.score(s) for s in snapshots]
        passed = [c for c in scored if c.passed]
        passed.sort(key=lambda c: c.score, reverse=True)
        return passed

    def report(self, snapshots: Sequence[MarketSnapshot]) -> List[Candidate]:
        """كل المرشحين بما فيهم المرفوضون — لفهم سبب خلو القائمة."""
        scored = [self.score(s) for s in snapshots]
        scored.sort(key=lambda c: (c.passed, c.score), reverse=True)
        return scored
