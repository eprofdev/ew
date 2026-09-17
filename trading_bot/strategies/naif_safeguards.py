"""فلاتر نايف (@Naifo9x) كصمامات أمان (Filters & Safeguards).

الوظيفة: لا تولّد إشارات شراء، بل تُسقِط الإشارات الخطرة:
  • حظر العقود (Options) نهائياً.
  • إلغاء القنوات السعرية (Linear Regression) في أسهم السنتات بسبب الـ Gaps.
  • فلترة الهيكل المالي (شركة حادة: كاب صغير + فلوت محدود).
  • منع الشراء من العرض (Ask) وفرض أمر محدد على الطلب.
  • رصد بيانات الشورت اللحظية لالتقاط تقفيل الشورت وتفادي فخاخ الاقتراض.
  • تفادي فخاخ جروبات التصريف والمؤشرات التجارية الكاذبة.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..config import BotConfig
from ..indicators import closes, gap_ratio, rsi
from ..models import Candle, MarketSnapshot, RealTimeData, StockMetrics


@dataclass(frozen=True)
class Veto:
    """سبب رفض الصفقة."""

    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - عرض فقط
        return f"{self.code}: {self.message}"


class NaifSafeguards:
    """طبقة الحماية التي تمر عليها كل إشارة قبل التنفيذ."""

    def __init__(self, config: Optional[BotConfig] = None) -> None:
        self.config = config or BotConfig()

    # ── 1) الهيكل المالي ─────────────────────────────────────────────
    def filter_micro_cap_structure(self, stock_metrics: StockMetrics) -> bool:
        """تصفية الأسهم بناءً على الهيكل المالي للشركة لمنع التعليق.

        يرجع True للسهم ذي الهيكل الحاد القابل للانفجار السعري.
        """
        cfg = self.config
        return (
            stock_metrics.market_cap <= cfg.max_market_cap
            and stock_metrics.free_float <= cfg.max_free_float
        )

    # ── 2) إشارة تقفيل الشورت ────────────────────────────────────────
    def check_short_cover_signal(self, ticker: str, real_time_data: RealTimeData) -> str:
        """رصد إشارة تقفيل الشورت الحية (Short Squeeze Trigger).

        مثال سهم VSME: السعر قريب من القاع والشورت ينخفض فجأة بنسبة 50%.
        """
        cfg = self.config
        if (
            real_time_data.price <= cfg.short_squeeze_price_ceiling
            and real_time_data.historical_short_interest >= cfg.short_squeeze_min_historical
        ):
            threshold = real_time_data.historical_short_interest * cfg.short_squeeze_cover_ratio
            if real_time_data.current_short_interest <= threshold:
                return (
                    f"EXECUTE_BUY_ALERT: {ticker} short sellers are covering, expected spike"
                )
        return "HOLD"

    # ── 3) فلاتر RSI مع اختبار الدعم ─────────────────────────────────
    def validate_rsi_and_support(
        self,
        candles_1d: Sequence[Candle],
        candles_4h: Sequence[Candle],
        tested_support_without_high_volume_break: bool,
    ) -> str:
        """مطابقة فلاتر RSI المتعددة مع مناطق اختبار الدعم (نموذج الارتكاز)."""
        cfg = self.config
        rsi_daily = rsi(closes(candles_1d), cfg.rsi_period)
        rsi_4h = rsi(closes(candles_4h), cfg.rsi_period)
        if rsi_daily is None or rsi_4h is None:
            return "WAIT"

        d_lo, d_hi = cfg.rsi_daily_band
        h_lo, h_hi = cfg.rsi_4h_band
        if (d_lo <= rsi_daily <= d_hi) and (h_lo <= rsi_4h <= h_hi):
            if tested_support_without_high_volume_break:
                return "BUY_CONFIRMED"
        return "WAIT"

    # ── 4) القنوات السعرية ───────────────────────────────────────────
    def linear_regression_allowed(self, candles: Sequence[Candle]) -> bool:
        """القنوات السعرية ممنوعة افتراضياً، وممنوعة دائماً مع كثرة الفجوات."""
        if not self.config.allow_linear_regression:
            return False
        return gap_ratio(candles) <= self.config.max_gap_ratio_for_channels

    # ── 5) تسعير الأمر: لا شراء من العرض ─────────────────────────────
    def limit_price(self, realtime: RealTimeData) -> Optional[float]:
        """سعر الأمر المحدد — على الطلب (Bid) أو تحته، وليس على العرض (Ask)."""
        cfg = self.config
        if not cfg.never_buy_the_ask:
            return realtime.ask or realtime.price or None
        reference = realtime.bid or realtime.price
        if not reference:
            return None
        return round(reference * (1 - cfg.limit_offset_pct), 4)

    # ── 6) كل الفحوص مجتمعة ──────────────────────────────────────────
    def run_all(self, snapshot: MarketSnapshot) -> List[Veto]:
        cfg = self.config
        m, rt = snapshot.metrics, snapshot.realtime
        vetoes: List[Veto] = []

        if snapshot.ticker in cfg.blocked_tickers:
            vetoes.append(Veto("BLOCKLIST", "السهم ضمن القائمة السوداء يدوياً"))

        # حظر العقود تماماً لحماية رأس المال
        if m.has_options and not cfg.allow_options:
            vetoes.append(
                Veto("OPTIONS_BLOCKED", "التداول بالعقود محظور — أسهم فقط")
            )

        if not self.filter_micro_cap_structure(m):
            vetoes.append(
                Veto(
                    "STRUCTURE",
                    f"الهيكل غير حاد (كاب {m.market_cap:,.0f} / فلوت {m.free_float:,.0f}) "
                    "— احتمال التعليق مرتفع",
                )
            )

        # سيولة وتنفيذ
        if rt.halted:
            vetoes.append(Veto("HALTED", "السهم موقوف عن التداول"))
        if rt.spread_pct > cfg.max_spread_pct:
            vetoes.append(
                Veto("SPREAD", f"الفرق بين الطلب والعرض {rt.spread_pct:.1%} واسع جداً")
            )
        if m.avg_daily_volume and m.avg_daily_volume < cfg.min_avg_daily_volume:
            vetoes.append(
                Veto("LIQUIDITY", f"متوسط الفوليوم {m.avg_daily_volume:,.0f} أقل من الحد الآمن")
            )

        # تخفيف رأس المال (أهم أسباب التعليق في أسهم السنتات)
        if m.has_active_shelf_offering:
            vetoes.append(Veto("DILUTION", "تسجيل عرض أسهم فعّال (Shelf/ATM) — خطر تخفيف"))
        if m.recent_reverse_split_days is not None and m.recent_reverse_split_days <= 30:
            vetoes.append(
                Veto("REVERSE_SPLIT", f"Reverse Split قبل {m.recent_reverse_split_days} يوماً")
            )

        # فخاخ التصريف والمؤشرات التجارية
        if cfg.block_promoted_tickers and snapshot.promoted_by_groups:
            vetoes.append(Veto("PUMP_GROUP", "السهم مروَّج في جروبات تصريف"))
        if cfg.block_paid_indicator_signals and snapshot.paid_indicator_signal:
            vetoes.append(Veto("PAID_INDICATOR", "إشارة مؤشر تجاري — غير معتمدة"))
        if rt.premarket_change_pct > cfg.max_premarket_spike_pct:
            vetoes.append(
                Veto(
                    "SPIKE_TRAP",
                    f"قفزة {rt.premarket_change_pct:.0%} قبل الافتتاح — شراء من القمة",
                )
            )

        # خطر الشورت/الاقتراض
        if rt.borrow_fee > cfg.max_borrow_fee:
            vetoes.append(
                Veto("BORROW_FEE", f"تكلفة اقتراض {rt.borrow_fee:.0%} — ضغط بيعي حاد")
            )

        return vetoes
