"""المحرك المدمج: استراتيجية فيصل تولّد الإشارة، وفلاتر نايف تعطيها الإذن.

ترتيب التنفيذ:
    1) صمامات نايف (Hard Vetoes)  → أي رفض يوقف كل شيء.
    2) سلوك فيصل السعري على 4H   → هل يوجد إعداد ارتداد من دعم؟
    3) فلاتر RSI المزدوجة        → نموذج الارتكاز.
    4) إشارة تقفيل الشورت        → مضاعِف قوة (وليست سبب دخول وحيداً).
    5) إدارة المخاطر والتسعير     → أمر محدد على الطلب، وقف، وأهداف.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from .config import BotConfig
from .models import Decision, MarketSnapshot, RealTimeData, Signal, StockMetrics
from .risk import RiskManager
from .strategies.faisal_price_action import FaisalPriceAction
from .strategies.naif_safeguards import NaifSafeguards


class EnhancedTradingBot:
    """التحديث التكتيكي للبوت: سلوك سعري + صمامات أمان."""

    def __init__(self, config: Optional[BotConfig] = None) -> None:
        self.config = config or BotConfig()
        # صمامات نايف الأساسية (محفوظة كخصائص للتوافق مع النسخة السابقة)
        self.allow_options = self.config.allow_options
        self.allow_linear_regression = self.config.allow_linear_regression

        self.price_action = FaisalPriceAction(self.config)
        self.safeguards = NaifSafeguards(self.config)
        self.risk = RiskManager(self.config)

    # ── واجهات متوافقة مع النسخة الأصلية ─────────────────────────────
    def filter_micro_cap_structure(self, stock_metrics: StockMetrics) -> bool:
        return self.safeguards.filter_micro_cap_structure(stock_metrics)

    def check_short_cover_signal(self, ticker: str, real_time_data: RealTimeData) -> str:
        return self.safeguards.check_short_cover_signal(ticker, real_time_data)

    def validate_rsi_and_support(self, snapshot: MarketSnapshot) -> str:
        setup = self.price_action.analyze(snapshot.candles_4h)
        return self.safeguards.validate_rsi_and_support(
            snapshot.candles_1d,
            snapshot.candles_4h,
            setup.tested_support_without_high_volume_break,
        )

    # ── التقييم الكامل ───────────────────────────────────────────────
    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        signal = Signal(ticker=snapshot.ticker, decision=Decision.WAIT)

        # 1) صمامات الأمان أولاً — لا تحليل قبل الحماية
        vetoes = self.safeguards.run_all(snapshot)
        if vetoes:
            signal.decision = Decision.VETO
            signal.vetoes = [str(v) for v in vetoes]
            return signal

        # 2) السلوك السعري على 4 ساعات
        setup = self.price_action.analyze(snapshot.candles_4h)
        signal.score = setup.score
        signal.reasons.extend(setup.reasons)

        if not setup.found:
            signal.decision = Decision.WATCH if setup.score > 0 else Decision.WAIT
            signal.notes.extend(setup.rejections)
            return signal

        # 3) فلاتر RSI المزدوجة (نموذج الارتكاز)
        rsi_state = self.safeguards.validate_rsi_and_support(
            snapshot.candles_1d,
            snapshot.candles_4h,
            setup.tested_support_without_high_volume_break,
        )
        if rsi_state != "BUY_CONFIRMED":
            signal.decision = Decision.WATCH
            signal.notes.append("RSI خارج نطاق الارتكاز (يومي 40-48 / 4 ساعات 48-55)")
            return signal
        signal.reasons.append("RSI مطابق لنموذج الارتكاز على الفريمين")

        # 4) تقفيل الشورت: مضاعِف قوة وليس سبباً وحيداً
        short_state = self.check_short_cover_signal(snapshot.ticker, snapshot.realtime)
        if short_state.startswith("EXECUTE_BUY_ALERT"):
            signal.score = min(signal.score + 15, 100.0)
            signal.reasons.append(
                f"تقفيل شورت مرصود: انخفاض من {snapshot.realtime.historical_short_interest:.0%} "
                f"إلى {snapshot.realtime.current_short_interest:.0%} من الفلوت"
            )

        if signal.score < self.config.min_score_to_buy:
            signal.decision = Decision.WATCH
            signal.notes.append(
                f"قوة الإعداد {signal.score:.0f} أقل من الحد {self.config.min_score_to_buy:.0f}"
            )
            return signal

        # 5) التسعير وإدارة المخاطر
        limit = self.safeguards.limit_price(snapshot.realtime)
        if limit is None:
            signal.decision = Decision.WATCH
            signal.notes.append("لا يوجد سعر طلب صالح — ممنوع الشراء من العرض")
            return signal

        plan = self.risk.build_plan(limit, setup.stop_reference or limit, snapshot.metrics)
        if plan is None:
            signal.decision = Decision.WATCH
            signal.notes.append("تعذر بناء خطة مخاطرة صالحة (وقف/كمية غير منطقية)")
            return signal

        signal.decision = Decision.BUY_CONFIRMED
        signal.entry_limit = plan.entry
        signal.stop_loss = plan.stop
        signal.targets = plan.targets
        signal.position_size = plan.size
        signal.notes.extend(plan.notes)
        signal.notes.append("أمر محدد على الطلب (Bid) — ممنوع الشراء من العرض")
        if not self.safeguards.linear_regression_allowed(snapshot.candles_4h):
            signal.notes.append("القنوات السعرية معطّلة لهذا السهم (فجوات سعرية)")
        return signal

    def scan(self, snapshots: Iterable[MarketSnapshot]) -> List[Signal]:
        """فحص قائمة أسهم وإرجاع الإشارات مرتبة بالأقوى أولاً."""
        signals = [self.evaluate(s) for s in snapshots]
        signals.sort(
            key=lambda s: (s.decision is Decision.BUY_CONFIRMED, s.score), reverse=True
        )
        return signals
