"""الفرضية المضادة: شراء القوة بدل الضعف (Momentum Breakout).

لماذا هذه الاستراتيجية موجودة أصلاً؟ لأن استراتيجية فيصل تشتري **الضعف**:
شموع ساقطة تختبر دعماً. وهذا رهان على الارتداد العكسي (mean reversion).
لكن الأدلة المنشورة على الشركات الصغيرة تميل إلى العكس: الزخم يستمر،
والقوة تجلب قوة. وثلاثة انقلابات خارج العينة في يوم واحد ترجّح أن المشكلة
في الفكرة لا في معايرتها.

فاختبار الفرضية المضادة ليس ترفاً: إما أن يجد حافة في الاتجاه المعاكس، أو
يؤكد أن السوق نفسه (أسهم سنتات، فريم 4 ساعات) لا يحمل حافة بسيطة أياً كان
اتجاهها. كلا الجوابين يستحق المعرفة.

الشروط:
  1) إغلاق فوق أعلى قمة في آخر N شمعة (اختراق حقيقي لا لمسة).
  2) فوليوم الاختراق أعلى من متوسطه بمضاعف (وإلا فهو اختراق كاذب).
  3) السعر فوق متوسطه المتحرك (سياق صاعد لا ارتداد ميت).
  4) الوقف تحت الاختراق بمسافة ATR — لا تحت دعم بعيد.
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..config import BotConfig
from ..indicators import atr, average_volume, sma
from ..models import Candle
from .faisal_price_action import PriceActionSetup, SupportZone


class MomentumBreakout:
    """محلّل اختراق بالزخم — يملأ نفس `PriceActionSetup` فيقبله المحرك كما هو."""

    def __init__(self, config: Optional[BotConfig] = None) -> None:
        self.config = config or BotConfig()

    def analyze(self, candles_4h: Sequence[Candle]) -> PriceActionSetup:
        cfg = self.config
        setup = PriceActionSetup()
        lookback = cfg.breakout_lookback
        needed = lookback + cfg.breakout_trend_period + 2

        if len(candles_4h) < needed:
            setup.rejections.append(f"بيانات غير كافية للاختراق ({len(candles_4h)} < {needed})")
            return setup

        last = candles_4h[-1]
        window = candles_4h[-(lookback + 1) : -1]
        prior_high = max(c.high for c in window)
        setup.atr_4h = atr(candles_4h, min(14, len(candles_4h) - 1))

        # صمام الأمان نفسه يعمل هنا: الاختراق بلا كسر دعم بفوليوم هادم
        setup.tested_support_without_high_volume_break = True

        if last.close <= prior_high:
            setup.rejections.append(
                f"لا اختراق: الإغلاق {last.close:.4f} تحت قمة {lookback} شمعة ({prior_high:.4f})"
            )
            return setup

        score = 30.0
        setup.reasons.append(f"إغلاق فوق قمة {lookback} شمعة عند {prior_high:.4f}")

        avg_volume = average_volume(candles_4h[:-1], min(lookback, len(candles_4h) - 1)) or 0.0
        if avg_volume <= 0 or last.volume < avg_volume * cfg.breakout_volume_multiple:
            setup.score = min(score, 45.0)
            setup.rejections.append(
                f"فوليوم الاختراق {last.volume:,.0f} دون {cfg.breakout_volume_multiple:g}× "
                f"المتوسط — اختراق كاذب على الأرجح"
            )
            return setup
        score += 25
        setup.reasons.append(f"فوليوم الاختراق {last.volume / avg_volume:.1f}× المتوسط")

        closes = [c.close for c in candles_4h]
        trend = sma(closes, cfg.breakout_trend_period)
        if trend is None or last.close <= trend:
            setup.score = min(score, 55.0)
            setup.rejections.append("السعر تحت متوسطه المتحرك — اختراق داخل هبوط")
            return setup
        score += 20
        setup.reasons.append(f"السعر فوق متوسط {cfg.breakout_trend_period} شمعة")

        # جسم قوي يغلق قرب القمة = تحكّم المشترين حتى النهاية
        if last.is_bullish and last.body_ratio >= 0.5 and last.upper_wick <= last.body:
            score += 15
            setup.reasons.append("شمعة اختراق بجسم قوي وإغلاق قرب القمة")

        atr_value = setup.atr_4h or (last.range)
        stop = last.close - atr_value * cfg.breakout_atr_stop_multiple
        if stop <= 0 or stop >= last.close:
            setup.rejections.append("تعذر بناء وقف منطقي من ATR")
            return setup

        setup.found = True
        setup.score = min(score, 100.0)
        setup.entry_reference = last.close
        setup.stop_reference = stop
        # منطقة «الدعم» هنا هي مستوى الاختراق نفسه — صار دعماً بعد كسره
        setup.zone = SupportZone(
            low=prior_high * 0.99, high=prior_high * 1.01, touches=2,
            last_touch_index=len(candles_4h) - 1,
        )
        return setup
