"""
tests/test_indicators.py
اختبارات المؤشرات الأساسية + اختبار ارتدادي (regression) لمشكلة تسرّب
البيانات المستقبلية (look-ahead bias) اللي انصلحت بمراجعة سابقة.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import pytest

from indicators import (
    rsi, macd, sma, ema, atr, pivot_support_resistance, nearest_support_resistance
)


def make_ohlcv(closes, volumes=None):
    n = len(closes)
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    closes = np.array(closes, dtype=float)
    if volumes is None:
        volumes = np.full(n, 1_000_000)
    return pd.DataFrame({
        "Open": closes, "High": closes + 0.5, "Low": closes - 0.5,
        "Close": closes, "Volume": volumes,
    }, index=dates)


def test_rsi_bounds():
    closes = 100 + np.cumsum(np.random.normal(0, 1, 300))
    r = rsi(pd.Series(closes))
    valid = r.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


def test_rsi_pure_uptrend_approaches_100():
    closes = np.arange(100, 130, dtype=float)  # صعود يومي ثابت بدون أي هبوط
    r = rsi(pd.Series(closes), period=14)
    assert r.iloc[-1] > 95  # لا خسائر إطلاقًا => RSI قريب من الحد الأعلى


def test_macd_histogram_is_macd_minus_signal():
    closes = pd.Series(100 + np.cumsum(np.random.normal(0, 1, 200)))
    macd_line, signal_line, hist = macd(closes)
    # علاقة رياضية ثابتة يجب أن تتحقق دائمًا بغض النظر عن البيانات
    pd.testing.assert_series_equal(hist, macd_line - signal_line, check_names=False)


def test_sma_matches_manual_mean():
    closes = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
    s = sma(closes, 3)
    assert s.iloc[2] == pytest.approx((1 + 2 + 3) / 3)
    assert s.iloc[-1] == pytest.approx((8 + 9 + 10) / 3)


def test_atr_never_negative():
    n = 100
    closes = 50 + np.cumsum(np.random.normal(0, 2, n))
    df = make_ohlcv(closes)
    a = atr(df["High"], df["Low"], df["Close"])
    assert (a.dropna() >= 0).all()


def test_pivot_detection_finds_known_low():
    # قاع واضح مصمم يدويًا عند المنتصف بالضبط (بدون تكرار القيمة عند نقطة الالتقاء)
    n = 61
    closes = np.concatenate([
        np.linspace(20, 10, 31),        # هبوط إلى القاع عند index 30 (قيمته 10.0)
        np.linspace(10.5, 20, 29),      # صعود يبدأ من 10.5 لتفادي تكرار قيمة القاع بالضبط
    ])
    df = make_ohlcv(closes)
    pivot_high, pivot_low = pivot_support_resistance(df, window=10)
    assert pivot_low.iloc[30] == pytest.approx(9.5)  # Low = Close - 0.5
    assert pivot_low.dropna().shape[0] == 1  # قاع واحد فقط بهذا النموذج البسيط


class TestNoLookaheadBias:
    """
    اختبار ارتدادي مباشر للثغرة المكتشفة سابقًا: pivot عند الموضع p لا يصير
    'معروفًا' فعليًا إلا بعد p + window شمعة، لأن اكتشافه أصلاً يحتاج يشوف
    window شمعة بعده. استخدام pivot أحدث من ذلك = معلومة من المستقبل.
    """

    def _make_df_with_late_pivot(self):
        # قاع عند index 90 (قريب من نهاية البيانات)، ثم 15 شمعة فقط بعده
        n = 106
        closes = np.concatenate([
            np.linspace(20, 10, 91),
            np.linspace(10, 12, n - 91),
        ])
        return make_ohlcv(closes)

    def test_recent_unconfirmed_pivot_is_excluded(self):
        df = self._make_df_with_late_pivot()
        window = 10
        pivot_high, pivot_low = pivot_support_resistance(df, window=window)

        # القاع عند index 90 يصير "مؤكَّدًا" فقط عند index >= 100 (90+10)
        # فلو سألنا عند index 95 (قبل التأكيد)، ما لازم يظهر كدعم متاح إطلاقًا
        support, _ = nearest_support_resistance(
            df, idx=95, pivot_high=pivot_high, pivot_low=pivot_low,
            lookback=100, confirm_window=window
        )
        assert pd.isna(support), (
            "القاع القريب استُخدم قبل أن يصير مؤكَّدًا فعليًا — تسرّب بيانات مستقبلية"
        )

    def test_pivot_becomes_available_once_confirmed(self):
        df = self._make_df_with_late_pivot()
        window = 10
        pivot_high, pivot_low = pivot_support_resistance(df, window=window)

        # عند index 101 (بعد نافذة التأكيد كاملة)، لازم يصير الدعم متاحًا
        support, _ = nearest_support_resistance(
            df, idx=101, pivot_high=pivot_high, pivot_low=pivot_low,
            lookback=100, confirm_window=window
        )
        assert not pd.isna(support)
        assert support == pytest.approx(9.5)  # Low = Close(10.0) - 0.5

    def test_without_confirm_window_param_bug_reproduces(self):
        """يوثّق سلوك النسخة القديمة (بدون confirm_window) لإظهار أنها كانت
        تسمح بنفس هذا التسرّب — للتأكد أن الإصلاح فعليًا يغيّر النتيجة."""
        df = self._make_df_with_late_pivot()
        window = 10
        pivot_high, pivot_low = pivot_support_resistance(df, window=window)

        # confirm_window=0 يحاكي السلوك القديم (يقبل أي pivot قبل idx مباشرة)
        support_old_behavior, _ = nearest_support_resistance(
            df, idx=95, pivot_high=pivot_high, pivot_low=pivot_low,
            lookback=100, confirm_window=0
        )
        assert not pd.isna(support_old_behavior), (
            "هذا يوثّق أن السلوك القديم (confirm_window=0) كان يسمح بتسرّب البيانات"
        )
