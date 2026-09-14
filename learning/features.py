"""
learning/features.py
يحوّل لحظة الإشارة (شمعة الدخول) إلى متجه أرقام يفهمه النموذج.

قاعدة مقدّسة هنا: كل رقم يُحسب من بيانات الشمعة idx وما قبلها فقط.
ولا رقم واحد يلمس أي شمعة بعدها. لو انكسرت هذي القاعدة، النموذج يصير
"يعرف المستقبل" فيعطي نتائج باكتيست ممتازة ويخسر فعليًا بالسوق الحي.
كل دالة هنا تستخدم .iloc[:idx+1] أو نوافذ منتهية عند idx — لا شيء بعده.

الخصائص مُطبَّعة (normalized) عمدًا: نقسم المسافات على ATR والأسعار على
السعر نفسه، عشان سهم بسعر 300$ وسهم بسعر 5$ ينتجان نفس مقياس الأرقام،
فيقدر النموذج يتعلّم من الاثنين معًا بدل ما يتعلّم "الأسهم الغالية أفضل".
"""
from typing import Optional
import numpy as np
import pandas as pd

# ترتيب ثابت وإلزامي — النموذج المحفوظ يعتمد على هذا الترتيب بالضبط.
# أي إضافة جديدة تنضاف في الآخر فقط، وأي حذف يُبطل النماذج المدرّبة سابقًا.
FEATURE_NAMES = [
    "rsi",                # RSI الحالي (÷100)
    "rsi_min_5",          # أدنى RSI بآخر 5 شمعات — عمق التشبع البيعي قبل الارتداد
    "rsi_slope_3",        # تغيّر RSI خلال 3 شمعات — سرعة تعافي الزخم
    "macd_hist_atr",      # MACD histogram منسوبًا لتذبذب السهم
    "macd_slope_atr",     # ميل الـhistogram (تسارع الزخم)
    "dist_sma20_atr",     # بُعد السعر عن SMA20 بوحدات ATR
    "dist_sma50_atr",
    "dist_sma200_atr",
    "trend_50_200",       # (SMA50 - SMA200) ÷ السعر — قوة الاتجاه طويل المدى
    "vol_ratio",          # الحجم ÷ متوسط 20 (لوغاريتمي لتهدئة القيم المتطرفة)
    "atr_pct",            # ATR ÷ السعر — نظام التذبذب الحالي
    "ret_5",              # عائد آخر 5 شمعات
    "ret_20",
    "ret_60",
    "range_pos_20",       # موقع السعر داخل نطاق آخر 20 شمعة (0 = القاع، 1 = القمة)
    "body_ratio",         # جسم الشمعة ÷ مداها — قوة الإغلاق
    "lower_wick_ratio",   # الذيل السفلي ÷ المدى — رفض البائعين
    "gap_pct",            # فجوة الافتتاح عن إغلاق أمس
    "dist_high_252",      # بُعد السعر عن أعلى قمة بالسنة (سالب دائمًا أو صفر)
    "up_streak",          # عدد الشمعات الصاعدة/الهابطة المتتالية (موقّع، مقسوم على 5)
    "stop_dist_atr",      # مسافة وقف الخسارة بوحدات ATR — من الإشارة نفسها
    "rr_ratio",           # نسبة الهدف إلى المخاطرة — من الإشارة نفسها
]

N_FEATURES = len(FEATURE_NAMES)


def _safe_div(a: float, b: float) -> float:
    """قسمة ترجع NaN بدل ما تنفجر أو ترجع لانهاية عند مقام صفري."""
    if b is None or not np.isfinite(b) or abs(b) < 1e-12:
        return np.nan
    if a is None or not np.isfinite(a):
        return np.nan
    return a / b


def _val(row, key: str) -> float:
    v = row.get(key, np.nan)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return np.nan
    return v if np.isfinite(v) else np.nan


def _atr_of(df: pd.DataFrame, idx: int) -> float:
    """ATR الشمعة، وإن كان غير متوفر بعد (بداية البيانات) نقدّره بـ2% من السعر."""
    atr = _val(df.iloc[idx], "ATR")
    if np.isnan(atr) or atr <= 0:
        close = _val(df.iloc[idx], "Close")
        return close * 0.02 if np.isfinite(close) and close > 0 else np.nan
    return atr


def _streak(closes: pd.Series) -> float:
    """عدد الشمعات المتتالية بنفس الاتجاه، موجب للصعود وسالب للهبوط."""
    diffs = closes.diff().dropna()
    if diffs.empty:
        return 0.0
    direction = 1 if diffs.iloc[-1] > 0 else (-1 if diffs.iloc[-1] < 0 else 0)
    if direction == 0:
        return 0.0
    count = 0
    for d in reversed(diffs.tolist()):
        if (d > 0 and direction > 0) or (d < 0 and direction < 0):
            count += 1
        else:
            break
    return float(direction * count)


def extract_features(df: pd.DataFrame, idx: int,
                     stop_loss: Optional[float] = None,
                     take_profit: Optional[float] = None) -> dict:
    """
    يستخرج خصائص لحظة الدخول عند الشمعة idx.

    stop_loss/take_profit اختياريان: لو مرّرتهما (وهو المعتاد، من TradeSignal)
    تنضاف خاصيتا هندسة الصفقة — مسافة الوقف ونسبة العائد/المخاطرة. وهما مهمتان
    فعليًا: نفس الإشارة بوقف ضيق جدًا تختلف احتمالية نجاحها عن وقف واسع.

    يرجع dict بأسماء FEATURE_NAMES. أي خاصية ما نقدر نحسبها (بيانات غير كافية)
    ترجع NaN — والنموذج يعاملها كقيمة متوسطة بدل ما يرفض العيّنة كاملة.
    """
    if idx < 0 or idx >= len(df):
        raise IndexError(f"idx={idx} خارج حدود البيانات (len={len(df)})")

    row = df.iloc[idx]
    prev = df.iloc[idx - 1] if idx >= 1 else row

    close = _val(row, "Close")
    atr = _atr_of(df, idx)

    # نوافذ تنتهي عند idx بالضبط — لا شمعة واحدة بعدها
    win5 = df.iloc[max(0, idx - 4): idx + 1]
    win20 = df.iloc[max(0, idx - 19): idx + 1]
    win252 = df.iloc[max(0, idx - 251): idx + 1]

    rsi_now = _val(row, "RSI")
    rsi_3_ago = _val(df.iloc[idx - 3], "RSI") if idx >= 3 else np.nan

    hist_now = _val(row, "MACD_hist")
    hist_3_ago = _val(df.iloc[idx - 3], "MACD_hist") if idx >= 3 else np.nan

    high20 = win20["High"].max()
    low20 = win20["Low"].min()
    high252 = win252["High"].max()

    candle_range = _val(row, "High") - _val(row, "Low")
    body = close - _val(row, "Open")
    lower_wick = min(_val(row, "Open"), close) - _val(row, "Low")

    raw_vol_ratio = _safe_div(_val(row, "Volume"), _val(row, "VolAvg20"))
    vol_ratio = np.log1p(raw_vol_ratio) if (np.isfinite(raw_vol_ratio)
                                             and raw_vol_ratio >= 0) else np.nan

    feats = {
        "rsi": _safe_div(rsi_now, 100.0),
        "rsi_min_5": _safe_div(win5["RSI"].min(), 100.0),
        "rsi_slope_3": _safe_div(rsi_now - rsi_3_ago, 100.0),
        "macd_hist_atr": _safe_div(hist_now, atr),
        "macd_slope_atr": _safe_div(hist_now - hist_3_ago, atr),
        "dist_sma20_atr": _safe_div(close - _val(row, "SMA20"), atr),
        "dist_sma50_atr": _safe_div(close - _val(row, "SMA50"), atr),
        "dist_sma200_atr": _safe_div(close - _val(row, "SMA200"), atr),
        "trend_50_200": _safe_div(_val(row, "SMA50") - _val(row, "SMA200"), close),
        "vol_ratio": vol_ratio,
        "atr_pct": _safe_div(atr, close),
        "ret_5": _safe_div(close - _val(df.iloc[idx - 5], "Close"), _val(df.iloc[idx - 5], "Close"))
                 if idx >= 5 else np.nan,
        "ret_20": _safe_div(close - _val(df.iloc[idx - 20], "Close"), _val(df.iloc[idx - 20], "Close"))
                  if idx >= 20 else np.nan,
        "ret_60": _safe_div(close - _val(df.iloc[idx - 60], "Close"), _val(df.iloc[idx - 60], "Close"))
                  if idx >= 60 else np.nan,
        "range_pos_20": _safe_div(close - low20, high20 - low20),
        "body_ratio": _safe_div(body, candle_range),
        "lower_wick_ratio": _safe_div(lower_wick, candle_range),
        "gap_pct": _safe_div(_val(row, "Open") - _val(prev, "Close"), _val(prev, "Close")),
        "dist_high_252": _safe_div(close - high252, high252),
        "up_streak": _streak(win20["Close"]) / 5.0,
        "stop_dist_atr": _safe_div(close - stop_loss, atr) if stop_loss is not None else np.nan,
        "rr_ratio": (_safe_div(take_profit - close, close - stop_loss)
                     if (stop_loss is not None and take_profit is not None) else np.nan),
    }

    # تنظيف نهائي: أي لانهاية تتحول NaN (يعاملها النموذج كقيمة متوسطة)
    return {k: (float(v) if v is not None and np.isfinite(v) else np.nan)
            for k, v in feats.items()}


def features_to_vector(feats: dict) -> np.ndarray:
    """يحوّل dict الخصائص إلى متجه بترتيب FEATURE_NAMES الثابت."""
    return np.array([feats.get(name, np.nan) for name in FEATURE_NAMES], dtype=float)


def date_to_index(df: pd.DataFrame, date) -> Optional[int]:
    """
    يحوّل تاريخ الإشارة إلى موضع رقمي بالإطار. يرجع None لو التاريخ مو موجود
    (يصير مثلاً لو الوقف/الهدف نُفّذ بسعر داخل شمعة ما تطابق فهرسها بالضبط).
    """
    try:
        loc = df.index.get_loc(date)
    except KeyError:
        return None
    if isinstance(loc, slice):  # تواريخ مكررة — نأخذ أولها
        loc = loc.start
    if not isinstance(loc, (int, np.integer)):
        return None
    return int(loc)
