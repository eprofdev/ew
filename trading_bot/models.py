"""نماذج البيانات المشتركة بين استراتيجية السلوك السعري وصمامات الأمان."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence


class Decision(str, Enum):
    """القرار النهائي الصادر عن المحرك."""

    BUY_CONFIRMED = "BUY_CONFIRMED"      # اكتملت شروط السلوك السعري ومرّت كل الفلاتر
    WATCH = "WATCH"                      # الإعداد يتشكل لكنه ناقص تأكيد
    WAIT = "WAIT"                        # لا يوجد إعداد
    VETO = "VETO"                        # صمام أمان رفض الصفقة


@dataclass(frozen=True)
class Candle:
    """شمعة واحدة على أي فريم."""

    ts: int          # طابع زمني (epoch seconds) لبداية الشمعة
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return max(self.high - self.low, 1e-9)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_ratio(self) -> float:
        """نسبة الجسم إلى مدى الشمعة (0 = دوجي، 1 = ماربوزو)."""
        return self.body / self.range


@dataclass(frozen=True)
class StockMetrics:
    """الهيكل المالي للشركة — يستخدم في فلتر نايف للأسهم الحادة."""

    ticker: str
    market_cap: float
    free_float: float
    shares_outstanding: float = 0.0
    avg_daily_volume: float = 0.0
    is_otc: bool = False
    has_options: bool = False
    # مؤشرات التخفيف (Dilution) التي تُعلّق السهم
    has_active_shelf_offering: bool = False   # تسجيل S-1/S-3 فعّال
    recent_reverse_split_days: Optional[int] = None  # عدد الأيام منذ آخر Reverse Split


@dataclass(frozen=True)
class RealTimeData:
    """بيانات لحظية: السعر، العمق، والشورت."""

    ticker: str
    price: float
    bid: float = 0.0
    ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    # نسب الشورت كنسبة من الفلوت (0.90 = 90%)
    historical_short_interest: float = 0.0
    current_short_interest: float = 0.0
    borrow_fee: float = 0.0          # تكلفة الاقتراض السنوية (0.35 = 35%)
    shares_available_to_short: Optional[float] = None
    session_volume: float = 0.0
    premarket_change_pct: float = 0.0  # تغير ما قبل الافتتاح (لرصد التصريف)
    halted: bool = False

    @property
    def spread(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return 0.0
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return 0.0
        mid = (self.ask + self.bid) / 2
        return self.spread / mid if mid else 0.0

    @property
    def short_drop_ratio(self) -> float:
        """نسبة انخفاض الشورت مقارنة بالتاريخي (0.5 = انخفض للنصف)."""
        if self.historical_short_interest <= 0:
            return 1.0
        return self.current_short_interest / self.historical_short_interest


@dataclass
class MarketSnapshot:
    """كل ما يحتاجه المحرك لتقييم سهم واحد في لحظة معينة."""

    ticker: str
    metrics: StockMetrics
    realtime: RealTimeData
    candles_4h: Sequence[Candle] = field(default_factory=list)
    candles_1d: Sequence[Candle] = field(default_factory=list)
    # إشارات خارجية اختيارية (جروبات/مؤشرات تجارية) لرصد الفخاخ
    promoted_by_groups: bool = False
    paid_indicator_signal: bool = False


@dataclass
class Signal:
    """مخرجات المحرك بعد دمج الاستراتيجيتين."""

    ticker: str
    decision: Decision
    score: float = 0.0                       # 0..100 قوة الإعداد السعري
    entry_limit: Optional[float] = None       # سعر الأمر المحدد (لا شراء من العرض)
    stop_loss: Optional[float] = None
    targets: List[float] = field(default_factory=list)
    position_size: int = 0
    reasons: List[str] = field(default_factory=list)   # أسباب داعمة
    vetoes: List[str] = field(default_factory=list)    # أسباب الرفض
    notes: List[str] = field(default_factory=list)     # ملاحظات/تحذيرات

    @property
    def is_actionable(self) -> bool:
        return self.decision is Decision.BUY_CONFIRMED and not self.vetoes

    def to_alert(self) -> str:
        """نص التنبيه الجاهز للإرسال (تيليجرام/واتساب)."""
        head = f"[{self.decision.value}] {self.ticker} | score={self.score:.0f}"
        if self.decision is Decision.VETO:
            body = "\n".join(f"  ⛔ {v}" for v in self.vetoes)
            return f"{head}\n{body}"
        lines = [head]
        if self.entry_limit is not None:
            lines.append(f"  دخول (Limit): {self.entry_limit:.4f}")
        if self.stop_loss is not None:
            lines.append(f"  وقف: {self.stop_loss:.4f}")
        if self.targets:
            lines.append("  أهداف: " + ", ".join(f"{t:.4f}" for t in self.targets))
        if self.position_size:
            lines.append(f"  الكمية: {self.position_size}")
        lines += [f"  ✔ {r}" for r in self.reasons]
        lines += [f"  ⚠ {n}" for n in self.notes]
        return "\n".join(lines)
