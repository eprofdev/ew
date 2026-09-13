"""
tests/test_strategies.py
يثبّت بالكود كل السيناريوهات اللي تحققنا منها يدويًا خلال البناء —
عشان أي تعديل مستقبلي ما يكسر شيء اشتغل صح بصمت.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import pytest

from indicators import add_all_indicators
from strategy import TrendFollowStrategy, position_size
from faisal_strategies import BaseTrackStrategy, LiquiditySweepStrategy
from backtest import Backtester


def _dates(n, start="2023-01-01"):
    return pd.date_range(start, periods=n, freq="B")


class TestPositionSize:
    def test_basic_calculation(self):
        # رأس مال 1000، مخاطرة 1% = 10$، فرق سعر الدخول عن الوقف = 2$ => 5 أسهم
        shares = position_size(capital=1000, risk_pct=0.01, entry=20, stop_loss=18)
        assert shares == 5

    def test_zero_when_stop_at_or_above_entry(self):
        assert position_size(capital=1000, risk_pct=0.01, entry=20, stop_loss=20) == 0
        assert position_size(capital=1000, risk_pct=0.01, entry=20, stop_loss=21) == 0

    def test_capped_by_available_capital(self):
        # مخاطرة كبيرة جدًا نظريًا تعطي أسهم أكثر مما يقدر رأس المال يشتريه فعليًا
        shares = position_size(capital=100, risk_pct=0.5, entry=10, stop_loss=9.99)
        assert shares <= 10  # ما يتجاوز 100/10


class TestTrendFollowStrategy:
    def _trending_df(self):
        n = 500
        t = np.arange(n)
        price = 100 + t * 0.15 + 8 * np.sin(t / 15)
        df = pd.DataFrame({
            "Open": price, "High": price + 1, "Low": price - 1, "Close": price,
            "Volume": np.random.RandomState(0).randint(1_000_000, 5_000_000, n),
        }, index=_dates(n))
        return add_all_indicators(df)

    def test_generates_signals_on_trend_with_pullbacks(self):
        df = self._trending_df()
        strat = TrendFollowStrategy(confirm_window=8)
        signals = strat.generate_signals(df)
        assert len(signals) > 0
        assert signals[0].side == "BUY"

    def test_backtest_produces_positive_result_on_designed_uptrend(self):
        df = self._trending_df()
        strat = TrendFollowStrategy(confirm_window=8)
        signals = strat.generate_signals(df)
        results = Backtester(1000, 0.01).run(signals)
        assert results["num_trades"] > 0
        assert results["final_capital"] >= results["initial_capital"]


class TestBaseTrackStrategy:
    def _decline_reclaim_df(self):
        np.random.seed(11)
        n = 280
        price = np.zeros(n)
        price[0] = 100.0
        for i in range(1, 201):
            price[i] = price[i - 1] * (1 + np.random.normal(0.0012, 0.006))
        for i in range(201, 226):
            price[i] = price[i - 1] * (1 - 0.014)
        for i in range(226, 240):
            price[i] = price[i - 1] * (1 + np.random.normal(0.001, 0.004))
        for i in range(240, 244):
            price[i] = price[i - 1] * (1 + np.random.normal(0.001, 0.003))
        price[244] = price[243] * 1.05
        for i in range(245, n):
            price[i] = price[i - 1] * (1 + np.random.normal(0.003, 0.005))

        open_, high, low = price.copy(), price * 1.01, price * 0.99
        volume = np.full(n, 700_000)
        open_[244] = price[243] * 0.99
        low[244] = price[243] * 0.975
        high[244] = price[244] * 1.005
        volume[244] = 2_800_000
        df = pd.DataFrame({"Open": open_, "High": high, "Low": low,
                            "Close": price, "Volume": volume}, index=_dates(n))
        return add_all_indicators(df)

    def test_strict_mode_fires_on_full_reclaim(self):
        df = self._decline_reclaim_df()
        strat = BaseTrackStrategy(confirm_window=7, ma_mode="strict")
        signals = strat.generate_signals(df)
        assert len(signals) >= 1
        assert signals[0].side == "BUY"

    def test_loose_mode_does_not_fire_before_ema30_50_reclaimed(self):
        """سيناريو ارتدادي محدد: يوم الإشارة بوضع strict، السعر لسا تحت
        EMA30/EMA50 (بس فوق MA20 السريع) — فوضع loose ما لازم يعطي إشارة
        بنفس اليوم، لأنه يطلب استرداد الاثنين معًا وهو شرط أصعب هنا."""
        df = self._decline_reclaim_df()
        strat = BaseTrackStrategy(confirm_window=7, ma_mode="loose")
        signals = strat.generate_signals(df)
        reclaim_day = pd.Timestamp("2023-12-08")
        assert not any(s.date == reclaim_day for s in signals)

    def test_invalid_ma_mode_raises(self):
        with pytest.raises(ValueError):
            BaseTrackStrategy(ma_mode="typo")


class TestLiquiditySweepStrategy:
    def _sweep_df(self):
        n = 80
        price = np.full(n, 11.0)
        for i in range(15, 46):
            price[i] = 10.0 + 0.3 * abs(np.sin((i - 30) / 6))
        price[46:60] = np.linspace(10.3, 11.5, 14)
        price[60:65] = np.linspace(11.5, 11.0, 5)
        price[65] = 9.7
        price[66] = 10.3
        price[67:] = np.linspace(10.5, 12.0, n - 67)

        open_, high, low = price.copy(), price + 0.15, price - 0.15
        low[65], open_[65], high[65] = 9.55, 9.85, 9.9  # كسر واضح تحت الدعم
        volume = np.full(n, 700_000)
        return pd.DataFrame({"Open": open_, "High": high, "Low": low,
                              "Close": price, "Volume": volume}, index=_dates(n))

    def test_fires_on_break_then_reclaim(self):
        df = add_all_indicators(self._sweep_df())
        strat = LiquiditySweepStrategy(reclaim_window=2)
        signals = strat.generate_signals(df)
        buys = [s for s in signals if s.side == "BUY"]
        assert len(buys) == 1
        assert buys[0].date == pd.Timestamp("2023-04-04")

    def test_stop_loss_is_below_not_at_sweep_low(self):
        """ارتدادي مباشر لخطأ سابق: الوقف كان =قاع السحب بالضبط بدل تحته."""
        df = add_all_indicators(self._sweep_df())
        strat = LiquiditySweepStrategy(reclaim_window=2)
        signals = strat.generate_signals(df)
        buy = next(s for s in signals if s.side == "BUY")
        sweep_low = 9.55
        assert buy.stop_loss < sweep_low

    def test_no_reclaim_within_window_cancels_pending_sweep(self):
        """لو الاسترداد ما صار خلال reclaim_window، ما لازم تظهر إشارة إطلاقًا
        حتى لو السعر رجع فوق الدعم لاحقًا بعد وقت طويل."""
        n = 80
        price = np.full(n, 11.0)
        for i in range(15, 46):
            price[i] = 10.0 + 0.3 * abs(np.sin((i - 30) / 6))
        price[46:60] = np.linspace(10.3, 11.5, 14)
        price[60:65] = np.linspace(11.5, 11.0, 5)
        # يكسر الدعم ويبقى تحته لأكثر بكثير من reclaim_window=2 قبل أي ارتداد
        price[65], price[66], price[67], price[68] = 9.7, 9.55, 9.5, 9.4
        price[69:] = np.linspace(10.5, 12.0, n - 69)
        open_, high, low = price.copy(), price + 0.15, price - 0.15
        low[65], open_[65], high[65] = 9.55, 9.85, 9.9
        volume = np.full(n, 700_000)
        df = pd.DataFrame({"Open": open_, "High": high, "Low": low,
                            "Close": price, "Volume": volume}, index=_dates(n))
        df = add_all_indicators(df)

        strat = LiquiditySweepStrategy(reclaim_window=2)
        signals = strat.generate_signals(df)
        buys = [s for s in signals if s.side == "BUY"]
        assert len(buys) == 0, (
            f"ما كان لازم تصير أي صفقة — الاسترداد تأخر عن reclaim_window. صار: {buys}"
        )
