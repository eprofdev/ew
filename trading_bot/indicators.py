"""مؤشرات فنية بدون أي مكتبات خارجية (stdlib فقط)."""

from __future__ import annotations

from statistics import mean
from typing import List, Optional, Sequence

from .models import Candle


def closes(candles: Sequence[Candle]) -> List[float]:
    return [c.close for c in candles]


def rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    """RSI بطريقة Wilder. يرجع None إذا كانت البيانات غير كافية."""
    if len(values) < period + 1:
        return None

    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)

    avg_gain = gains / period
    avg_loss = losses / period

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return mean(values[-period:])


def ema(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    k = 2 / (period + 1)
    out = mean(values[:period])
    for v in values[period:]:
        out = v * k + out * (1 - k)
    return out


def true_range(prev: Candle, cur: Candle) -> float:
    return max(
        cur.high - cur.low,
        abs(cur.high - prev.close),
        abs(cur.low - prev.close),
    )


def atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = [true_range(candles[i - 1], candles[i]) for i in range(1, len(candles))]
    out = mean(trs[:period])
    for tr in trs[period:]:
        out = (out * (period - 1) + tr) / period
    return out


def average_volume(candles: Sequence[Candle], period: int = 20) -> Optional[float]:
    if len(candles) < period or period <= 0:
        return None
    return mean(c.volume for c in candles[-period:])


def pivot_lows(candles: Sequence[Candle], left: int = 2, right: int = 2) -> List[int]:
    """مواقع القيعان المحورية (Swing Lows) داخل السلسلة."""
    idx: List[int] = []
    for i in range(left, len(candles) - right):
        low = candles[i].low
        window = candles[i - left : i + right + 1]
        if all(low <= c.low for c in window) and any(low < c.low for c in window):
            idx.append(i)
    return idx


def gap_ratio(candles: Sequence[Candle], lookback: int = 30) -> float:
    """نسبة الشموع التي فتحت بفجوة > 3% — أساس إلغاء القنوات السعرية عند نايف."""
    window = candles[-(lookback + 1):]
    if len(window) < 2:
        return 0.0
    gaps = 0
    for prev, cur in zip(window, window[1:]):
        if prev.close <= 0:
            continue
        if abs(cur.open - prev.close) / prev.close > 0.03:
            gaps += 1
    return gaps / max(len(window) - 1, 1)
