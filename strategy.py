"""
strategy.py
يحوّل المؤشرات إلى إشارات دخول/خروج، ويحسب حجم الصفقة ووقف الخسارة
بناءً على إدارة مخاطر ثابتة النسبة (Fixed Fractional Risk).
"""
from dataclasses import dataclass
from typing import Optional
import pandas as pd
from indicators import pivot_support_resistance, nearest_support_resistance


@dataclass
class TradeSignal:
    date: pd.Timestamp
    side: str            # "BUY" أو "SELL" (خروج)
    price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    # حالة السوق لحظة الإشارة، تملؤها learning.dataset.attach_features لاحقًا.
    # تُحفظ هنا لا داخل الاستراتيجيات: الاستراتيجيات تبقى بلا أي معرفة
    # بطبقة التعلّم، وأي استراتيجية جديدة تستفيد منها تلقائيًا.
    features: Optional[dict] = None


class TrendFollowStrategy:
    """
    استراتيجية دخول:
    - الاتجاه العام صاعد (السعر فوق SMA50 وSMA50 فوق SMA200) => يسمح بالشراء فقط
    - RSI خارج من منطقة تشبع بيعي (يعبر فوق 30 قادمًا من تحته) = زخم عائد
    - MACD histogram يتحول من سالب لموجب = تأكيد زخم
    - الحجم أعلى من متوسطه (تأكيد اهتمام السوق)
    - يفضّل الدخول قرب مستوى دعم معروف (منطقة دخول أرخص وأأمن)

    خروج:
    - وقف خسارة: ATR × multiplier تحت سعر الدخول
    - هدف ربح: عند أقرب مقاومة، أو نسبة مخاطرة/عائد ثابتة إذا ما فيه مقاومة واضحة
    - خروج مبكر إذا انعكس MACD أو كسر السعر SMA20 بقوة
    """

    def __init__(self, rr_ratio: float = 2.0, atr_mult: float = 1.5,
                 sr_window: int = 10, sr_lookback: int = 150,
                 confirm_window: int = 8):
        self.rr_ratio = rr_ratio
        self.atr_mult = atr_mult
        self.sr_window = sr_window
        self.sr_lookback = sr_lookback
        self.confirm_window = confirm_window  # أيام السماح لتوافق الإشارات مع بعضها

    def generate_signals(self, df: pd.DataFrame) -> list[TradeSignal]:
        pivot_high, pivot_low = pivot_support_resistance(df, window=self.sr_window)
        signals = []
        in_position = False
        entry_price = None
        stop_loss = None
        take_profit = None

        for i in range(210, len(df)):  # يبدأ بعد كفاية بيانات لـ SMA200
            row = df.iloc[i]
            prev = df.iloc[i - 1]

            # الاتجاه العام صاعد على المدى الطويل (SMA50 > SMA200)
            # بدون اشتراط السعر فوق SMA50 لحظيًا، لأن الدخول المستهدف
            # أصلًا هو أثناء تصحيح/ارتداد داخل الاتجاه الصاعد
            uptrend = row["SMA50"] > row["SMA200"]
            volume_ok = row["Volume"] > row["VolAvg20"]

            window = df.iloc[max(0, i - self.confirm_window):i + 1]
            rsi_cross_recent = ((window["RSI"].shift(1) < 30) & (window["RSI"] >= 30)).any()
            macd_turn_recent = ((window["MACD_hist"].shift(1) < 0) & (window["MACD_hist"] >= 0)).any()

            if not in_position and uptrend and rsi_cross_recent and macd_turn_recent and volume_ok:
                support, resistance = nearest_support_resistance(
                    df, i, pivot_high, pivot_low, lookback=self.sr_lookback,
                    confirm_window=self.sr_window
                )
                entry_price = row["Close"]
                atr_val = row["ATR"] if not pd.isna(row["ATR"]) else entry_price * 0.02
                stop_loss = entry_price - self.atr_mult * atr_val

                if not pd.isna(resistance) and resistance > entry_price:
                    take_profit = resistance
                else:
                    risk = entry_price - stop_loss
                    take_profit = entry_price + risk * self.rr_ratio

                signals.append(TradeSignal(
                    date=row.name, side="BUY", price=entry_price,
                    stop_loss=stop_loss, take_profit=take_profit,
                    reason="اتجاه صاعد + RSI خارج من تشبع بيعي + تأكيد MACD وحجم"
                ))
                in_position = True

            elif in_position:
                hit_stop = row["Low"] <= stop_loss
                hit_target = row["High"] >= take_profit
                macd_reversal = prev["MACD_hist"] > 0 >= row["MACD_hist"]

                if hit_stop:
                    signals.append(TradeSignal(row.name, "SELL", stop_loss,
                                                reason="وقف خسارة"))
                    in_position = False
                elif hit_target:
                    signals.append(TradeSignal(row.name, "SELL", take_profit,
                                                reason="تحقيق الهدف"))
                    in_position = False
                elif macd_reversal:
                    signals.append(TradeSignal(row.name, "SELL", row["Close"],
                                                reason="انعكاس زخم MACD - خروج مبكر"))
                    in_position = False

        return signals


def position_size(capital: float, risk_pct: float, entry: float, stop_loss: float) -> int:
    """
    يحسب عدد الأسهم بناءً على نسبة مخاطرة ثابتة من رأس المال.
    مثال: رأس مال 1000$, مخاطرة 1% = 10$ أقصى خسارة مسموحة للصفقة.
    """
    risk_amount = capital * risk_pct
    per_share_risk = entry - stop_loss
    if per_share_risk <= 0:
        return 0
    shares = int(risk_amount / per_share_risk)
    max_affordable = int(capital / entry)
    return max(0, min(shares, max_affordable))
