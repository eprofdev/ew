"""إعدادات البوت — كل الأرقام هنا قابلة للضبط دون تعديل المنطق."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple


@dataclass
class BotConfig:
    # ── صمامات نايف الأساسية ─────────────────────────────────────────
    allow_options: bool = False            # حظر العقود تماماً لحماية رأس المال
    allow_linear_regression: bool = False  # حظر القنوات السعرية بسبب الـ Gaps
    max_gap_ratio_for_channels: float = 0.10  # فوقها القناة السعرية غير موثوقة

    # فلتر الهيكل المالي (السهم الحاد القابل للانفجار)
    max_market_cap: float = 2_000_000.0
    max_free_float: float = 1_500_000.0
    # True = بيانات الهيكل المجهولة ترفض الصفقة (السلوك الآمن الافتراضي).
    # False = وضع بحثي فقط لقياس السلوك السعري وحده على كون كبير؛ يجب أن
    # يُعلَن بوضوح في أي تقرير لأنه يعطّل أهم صمامات نايف.
    require_fundamentals: bool = True

    # حماية السيولة والتنفيذ
    max_spread_pct: float = 0.06           # 6% فرق بين الطلب والعرض
    min_avg_daily_volume: float = 50_000.0
    never_buy_the_ask: bool = True         # منع الشراء من العرض
    limit_offset_pct: float = 0.0          # 0 = على الطلب تماماً، 0.005 = تحته 0.5%

    # رصد الشورت
    short_squeeze_price_ceiling: float = 1.10
    short_squeeze_min_historical: float = 0.90   # 90% من الفلوت
    short_squeeze_cover_ratio: float = 0.50      # انخفاض الشورت للنصف

    # فلاتر RSI (نموذج الارتكاز)
    rsi_daily_band: Tuple[float, float] = (40.0, 48.0)
    rsi_4h_band: Tuple[float, float] = (48.0, 55.0)
    rsi_period: int = 14

    # ── استراتيجية فيصل: السلوك السعري على 4 ساعات ───────────────────
    support_lookback: int = 60             # عدد شموع 4H للبحث عن الدعوم
    support_tolerance_pct: float = 0.03    # عرض منطقة الدعم ±3%
    min_support_touches: int = 2           # دعم معتبر = لمستان فأكثر
    falling_sequence_min: int = 3          # أقل عدد شموع ساقطة متتالية
    falling_sequence_max: int = 8          # أكثر من ذلك = انهيار وليس تصحيح
    max_drop_pct_in_fall: float = 0.45     # سقوط أعمق = كسر هيكلي
    exhaustion_volume_ratio: float = 0.85  # آخر شمعة ساقطة < 85% من متوسط السقوط
    break_volume_multiple: float = 2.0     # كسر الدعم بفوليوم مضاعف = رفض الإعداد
    rejection_wick_ratio: float = 1.5      # الذيل السفلي ≥ 1.5× الجسم
    min_score_to_buy: float = 60.0

    # ── إدارة المخاطر ────────────────────────────────────────────────
    account_equity: float = 10_000.0
    risk_per_trade_pct: float = 0.01       # 1% من رأس المال لكل صفقة
    max_position_pct_of_equity: float = 0.10
    max_position_pct_of_adv: float = 0.02  # لا تتجاوز 2% من متوسط فوليوم اليوم
    stop_buffer_pct: float = 0.02          # الوقف تحت الدعم بـ 2%
    targets_r_multiples: Tuple[float, ...] = (1.5, 3.0, 5.0)

    # ── فخاخ التصريف والمؤشرات التجارية ──────────────────────────────
    block_promoted_tickers: bool = True
    block_paid_indicator_signals: bool = True
    max_premarket_spike_pct: float = 0.30  # قفزة > 30% قبل الافتتاح = فخ تصريف
    max_borrow_fee: float = 1.00           # تكلفة اقتراض > 100% = خطر شديد

    blocked_tickers: frozenset = field(default_factory=frozenset)
