"""
indicators.py
حساب المؤشرات الفنية: RSI, MACD, المتوسطات المتحركة، الدعم والمقاومة، ATR
"""
import pandas as pd
import numpy as np


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    result = pd.Series(np.nan, index=close.index)
    has_data = avg_gain.notna() & avg_loss.notna()

    normal = has_data & (avg_loss > 0)
    rs = avg_gain[normal] / avg_loss[normal]
    result[normal] = 100 - (100 / (1 + rs))

    # لا خسائر إطلاقًا خلال الفترة: RS -> لانهاية رياضيًا => RSI = 100 (مو NaN)
    no_loss_with_gain = has_data & (avg_loss == 0) & (avg_gain > 0)
    result[no_loss_with_gain] = 100

    # سعر ثابت تمامًا (لا مكاسب ولا خسائر): تقليديًا RSI = 50
    perfectly_flat = has_data & (avg_loss == 0) & (avg_gain == 0)
    result[perfectly_flat] = 50

    return result


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean()


def ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def pivot_support_resistance(df: pd.DataFrame, window: int = 10):
    """
    يحدد قمم وقيعان محلية (swing highs/lows) كمناطق مقاومة/دعم مرشحة.
    window: عدد الشموع على كل جانب لاعتبار النقطة قمة/قاع محلي.
    يرجع عمودين: pivot_high, pivot_low (NaN حيث لا يوجد pivot)
    """
    high, low = df["High"], df["Low"]
    pivot_high = pd.Series(np.nan, index=df.index)
    pivot_low = pd.Series(np.nan, index=df.index)

    for i in range(window, len(df) - window):
        seg_h = high.iloc[i - window: i + window + 1]
        seg_l = low.iloc[i - window: i + window + 1]
        if high.iloc[i] == seg_h.max():
            pivot_high.iloc[i] = high.iloc[i]
        if low.iloc[i] == seg_l.min():
            pivot_low.iloc[i] = low.iloc[i]

    return pivot_high, pivot_low


def nearest_support_resistance(df: pd.DataFrame, idx: int, pivot_high: pd.Series,
                                pivot_low: pd.Series, lookback: int = 100,
                                confirm_window: int = 10):
    """
    يرجع أقرب دعم/مقاومة معروفين ومؤكَّدين حتى شمعة idx فقط، بدون أي تسرّب
    بيانات مستقبلية (look-ahead bias).

    مهم: نقطة الـ pivot عند الموضع p لا تصير "معروفة" فعليًا إلا بعد مرور
    confirm_window شمعة إضافية بعدها — لأن اكتشافها أصلاً (pivot_support_resistance)
    يحتاج يشوف confirm_window شمعة قبلها وبعدها ليتأكد إنها أعلى/أقل نقطة محليًا.
    فلو استخدمنا pivot قريب جدًا من idx، معناها استخدمنا معلومة ما كانت
    متوفرة فعليًا في ذلك التاريخ لو كنا نُشغّل هذا حيًا يوم بيوم.
    confirm_window هنا يجب أن يطابق الـ window المستخدم أصلاً بـ pivot_support_resistance.
    """
    start = max(0, idx - lookback)
    known_upto = idx - confirm_window  # آخر موضع يُسمح لأي pivot يكون "مؤكَّدًا" عنده
    if known_upto < start:
        return np.nan, np.nan

    price = df["Close"].iloc[idx]
    highs = pivot_high.iloc[start:known_upto + 1].dropna()
    lows = pivot_low.iloc[start:known_upto + 1].dropna()

    resistance = highs[highs > price].min() if not highs[highs > price].empty else np.nan
    support = lows[lows < price].max() if not lows[lows < price].empty else np.nan

    return support, resistance


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["RSI"] = rsi(df["Close"])
    macd_line, signal_line, hist = macd(df["Close"])
    df["MACD"] = macd_line
    df["MACD_signal"] = signal_line
    df["MACD_hist"] = hist
    df["SMA20"] = sma(df["Close"], 20)
    df["SMA30"] = sma(df["Close"], 30)
    df["EMA30"] = ema(df["Close"], 30)
    df["EMA50"] = ema(df["Close"], 50)
    df["SMA50"] = sma(df["Close"], 50)
    df["SMA200"] = sma(df["Close"], 200)
    df["ATR"] = atr(df["High"], df["Low"], df["Close"])
    df["VolAvg20"] = df["Volume"].rolling(20).mean()
    return df
