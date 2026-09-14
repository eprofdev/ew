"""
tests/test_learning.py
اختبارات طبقة التعلّم. تركّز على الأشياء اللي لو انكسرت بصمت تكلّفك فلوسًا:

  1. تسرّب البيانات المستقبلية — الخصائص ما تلمس أي شمعة بعد لحظة الإشارة
  2. أمانة التقييم الزمني — walk-forward ما يتدرّب على المستقبل
  3. حدود الأمان — التعلّم ما يرفع مخاطرتك فوق السقف أبدًا، وما يفلتر
     قبل نضج النموذج، وما يعطّل البوت عند أي عطل
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json

import numpy as np
import pandas as pd
import pytest

from indicators import add_all_indicators
from strategy import TrendFollowStrategy, TradeSignal
from backtest import Backtester

from learning.features import (FEATURE_NAMES, extract_features, features_to_vector,
                               date_to_index)
from learning.model import OnlineLogisticModel, RunningScaler
from learning.dataset import build_samples, sort_by_date, samples_to_arrays
from learning.walk_forward import (roc_auc, brier_score, calibration_bins,
                                   threshold_sweep, walk_forward_eval,
                                   recommend_threshold)
from learning.filter import SignalFilter, kelly_fraction
from learning.bandit import StrategySelector, r_to_reward
from learning import store


def _dates(n, start="2022-01-03"):
    return pd.date_range(start, periods=n, freq="B")


def _price_df(n=500, seed=0):
    """
    اتجاه صاعد بتصحيحات دورية — نفس مولّد البيانات المستخدم بـtest_strategies.py
    عشان تُنتج TrendFollowStrategy إشارات فعلية نقدر نختبر التكامل عليها.
    """
    rng = np.random.RandomState(seed)
    t = np.arange(n)
    price = 100 + t * 0.15 + 8 * np.sin(t / 15)
    df = pd.DataFrame({
        "Open": price - 0.3, "High": price + 1.0, "Low": price - 1.0, "Close": price,
        "Volume": rng.randint(1_000_000, 5_000_000, n),
    }, index=_dates(n))
    return add_all_indicators(df)


# ══════════════════════════════════════════════ الخصائص وتسرّب البيانات
class TestFeatures:
    def test_returns_all_declared_features(self):
        df = _price_df()
        feats = extract_features(df, 300, stop_loss=df["Close"].iloc[300] * 0.95,
                                 take_profit=df["Close"].iloc[300] * 1.10)
        assert set(feats) == set(FEATURE_NAMES)
        assert len(features_to_vector(feats)) == len(FEATURE_NAMES)

    def test_no_lookahead_future_bars_cannot_change_features(self):
        """
        الاختبار الأهم بالملف: نحسب خصائص شمعة 300 مرتين — مرة على البيانات
        كاملة ومرة على بيانات مقصوصة عند 301. لو اختلف أي رقم، فالكود يقرأ
        المستقبل، وكل نتائج الباكتيست بعدها كذب.
        """
        df = _price_df()
        full = extract_features(df, 300, stop_loss=95.0, take_profit=110.0)
        truncated = extract_features(df.iloc[:301].copy(), 300,
                                     stop_loss=95.0, take_profit=110.0)
        for name in FEATURE_NAMES:
            a, b = full[name], truncated[name]
            if np.isnan(a) and np.isnan(b):
                continue
            assert a == pytest.approx(b, rel=1e-12), f"تسرّب مستقبلي بالخاصية: {name}"

    def test_missing_data_becomes_nan_not_crash(self):
        df = _price_df(n=60)   # قصير جدًا لـSMA200
        feats = extract_features(df, 30)
        assert np.isnan(feats["dist_sma200_atr"])
        assert np.isnan(feats["stop_dist_atr"])   # ما مرّرنا وقفًا
        assert not np.isnan(feats["rsi"])

    def test_features_are_scale_free_across_price_levels(self):
        """سهم بـ$5 وسهم بـ$500 بنفس الشكل لازم يعطيان خصائص متقاربة."""
        cheap = _price_df()
        expensive = cheap.copy()
        for col in ("Open", "High", "Low", "Close"):
            expensive[col] = cheap[col] * 100
        expensive = add_all_indicators(expensive)

        f_cheap = extract_features(cheap, 300)
        f_exp = extract_features(expensive, 300)
        for name in ("rsi", "dist_sma20_atr", "atr_pct", "ret_20", "range_pos_20"):
            assert f_cheap[name] == pytest.approx(f_exp[name], rel=0.02), name

    def test_date_to_index_roundtrip_and_missing(self):
        df = _price_df(n=50)
        assert date_to_index(df, df.index[17]) == 17
        assert date_to_index(df, pd.Timestamp("1999-01-01")) is None


# ══════════════════════════════════════════════════════════════ النموذج
class TestModel:
    def test_learns_a_separable_pattern(self):
        """خاصية واحدة تحدد النتيجة تمامًا — النموذج لازم يلتقطها."""
        rng = np.random.RandomState(1)
        n = 400
        X = rng.normal(0, 1, (n, len(FEATURE_NAMES)))
        y = (X[:, 0] > 0).astype(int)

        model = OnlineLogisticModel(min_samples=10).fit(X, y, epochs=15)
        assert model.predict_proba(X[y == 1][0]) > 0.6
        assert model.predict_proba(X[y == 0][0]) < 0.4
        # أثقل وزن لازم يكون للخاصية الحقيقية لا لواحدة عشوائية
        assert model.weights_report()[0][0] == FEATURE_NAMES[0]

    def test_not_ready_below_min_samples(self):
        model = OnlineLogisticModel(min_samples=40)
        assert not model.is_ready
        for _ in range(39):
            model.partial_fit(np.zeros(len(FEATURE_NAMES)), 1)
        assert not model.is_ready
        model.partial_fit(np.zeros(len(FEATURE_NAMES)), 1)
        assert model.is_ready

    def test_fit_reports_real_sample_count_not_epochs(self):
        """12 تمريرة على 50 صفقة = 50 صفقة، لا 600 — وإلا min_samples بلا معنى."""
        X = np.random.RandomState(2).normal(0, 1, (50, len(FEATURE_NAMES)))
        y = np.random.RandomState(3).randint(0, 2, 50)
        model = OnlineLogisticModel(min_samples=40).fit(X, y, epochs=12)
        assert model.n_samples == 50

    def test_nan_features_treated_as_average_not_crash(self):
        model = OnlineLogisticModel(min_samples=1)
        x = np.full(len(FEATURE_NAMES), np.nan)
        model.partial_fit(x, 1)
        p = model.predict_proba(x)
        assert 0.0 <= p <= 1.0 and np.isfinite(p)

    def test_predict_does_not_mutate_scaler(self):
        """التنبؤ ما يحدّث التطبيع — وإلا صار تسرّبًا خفيفًا من عيّنة لم يُتعلَّم منها."""
        model = OnlineLogisticModel(min_samples=1)
        for i in range(30):
            model.partial_fit(np.full(len(FEATURE_NAMES), float(i)), i % 2)
        before = model.scaler.mean.copy()
        model.predict_proba(np.full(len(FEATURE_NAMES), 999.0))
        assert np.array_equal(before, model.scaler.mean)

    def test_save_load_roundtrip_preserves_predictions(self, tmp_path):
        X = np.random.RandomState(4).normal(0, 1, (100, len(FEATURE_NAMES)))
        y = (X[:, 2] > 0).astype(int)
        model = OnlineLogisticModel(min_samples=10).fit(X, y, epochs=8)

        path = tmp_path / "m.json"
        model.save(path)
        loaded = OnlineLogisticModel.load(path)
        for row in X[:10]:
            assert loaded.predict_proba(row) == pytest.approx(model.predict_proba(row))
        assert loaded.n_samples == model.n_samples

    def test_load_rejects_mismatched_feature_list(self, tmp_path):
        model = OnlineLogisticModel()
        d = model.to_dict()
        d["feature_names"] = ["something_else"]
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(d))
        with pytest.raises(ValueError, match="خصائص النموذج"):
            OnlineLogisticModel.load(path)

    def test_load_or_new_survives_corrupt_file(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("{ليس JSON صالحًا")
        model = OnlineLogisticModel.load_or_new(path, min_samples=40)
        assert model.n_samples == 0 and not model.is_ready

    def test_balanced_fit_beats_lazy_majority_guess(self):
        """مع 80% خسائر، نموذج غير متوازن يتعلّم "توقّع الخسارة دائمًا"."""
        rng = np.random.RandomState(5)
        n = 500
        X = rng.normal(0, 1, (n, len(FEATURE_NAMES)))
        y = np.zeros(n, dtype=int)
        winners = X[:, 1] > 1.2               # ~11% رابحة
        y[winners] = 1

        model = OnlineLogisticModel(min_samples=10).fit(X, y, epochs=15, balance=True)
        probs = np.array([model.predict_proba(x) for x in X])
        assert roc_auc(y, probs) > 0.8
        assert probs[y == 1].mean() > probs[y == 0].mean()

    def test_scaler_handles_constant_feature(self):
        scaler = RunningScaler(3)
        for _ in range(10):
            scaler.update(np.array([5.0, 1.0, 2.0]))
        z = scaler.transform(np.array([5.0, 1.0, 2.0]))
        assert np.all(np.isfinite(z))


# ═══════════════════════════════════════════ بناء العيّنات من الإشارات
class TestDataset:
    def test_pairs_buy_with_following_sell(self):
        df = _price_df()
        signals = TrendFollowStrategy(confirm_window=8).generate_signals(df)
        samples = build_samples(df, signals, "TEST", strategy="trend_follow")
        n_buys = sum(1 for s in signals if s.side == "BUY")
        n_sells = sum(1 for s in signals if s.side == "SELL")
        assert len(samples) == min(n_buys, n_sells)
        for s in samples:
            assert s.label in (0, 1)
            assert set(s.features) == set(FEATURE_NAMES)

    def test_label_and_r_multiple_match_actual_pnl(self):
        df = _price_df(n=300)
        buy = TradeSignal(df.index[250], "BUY", 100.0, stop_loss=95.0,
                          take_profit=110.0, reason="اختبار")
        sell = TradeSignal(df.index[260], "SELL", 110.0, reason="تحقيق الهدف")
        samples = build_samples(df, [buy, sell], "TEST")
        assert len(samples) == 1
        assert samples[0].label == 1
        assert samples[0].r_multiple == pytest.approx(2.0)   # ربح 10 ÷ مخاطرة 5

    def test_losing_trade_gets_negative_r(self):
        df = _price_df(n=300)
        buy = TradeSignal(df.index[250], "BUY", 100.0, stop_loss=95.0, take_profit=110.0)
        sell = TradeSignal(df.index[255], "SELL", 95.0, reason="وقف خسارة")
        samples = build_samples(df, [buy, sell], "TEST")
        assert samples[0].label == 0
        assert samples[0].r_multiple == pytest.approx(-1.0)

    def test_unclosed_trade_is_dropped(self):
        """صفقة مفتوحة بنهاية البيانات ما تصير درسًا — نتيجتها مجهولة."""
        df = _price_df(n=300)
        buy = TradeSignal(df.index[250], "BUY", 100.0, stop_loss=95.0, take_profit=110.0)
        assert build_samples(df, [buy], "TEST") == []

    def test_sort_by_date_is_stable_and_chronological(self):
        df = _price_df(n=300)
        raw = []
        for i, price in ((280, 100.0), (250, 90.0), (265, 95.0)):
            raw += [TradeSignal(df.index[i], "BUY", price, stop_loss=price * 0.95,
                                take_profit=price * 1.1),
                    TradeSignal(df.index[i + 5], "SELL", price * 1.1)]
        samples = sort_by_date(build_samples(df, raw, "TEST"))
        dates = [s.date for s in samples]
        assert dates == sorted(dates)


# ═════════════════════════════════════════════════ التقييم الزمني الأمين
class TestWalkForward:
    def test_auc_of_perfect_and_random_scores(self):
        y = np.array([0, 0, 1, 1])
        assert roc_auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == pytest.approx(1.0)
        assert roc_auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == pytest.approx(0.0)
        assert roc_auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == pytest.approx(0.5)

    def test_auc_undefined_with_single_class(self):
        assert np.isnan(roc_auc(np.array([1, 1, 1]), np.array([0.2, 0.5, 0.9])))

    def test_brier_score_basic(self):
        assert brier_score(np.array([1, 0]), np.array([1.0, 0.0])) == pytest.approx(0.0)
        assert brier_score(np.array([1, 0]), np.array([0.5, 0.5])) == pytest.approx(0.25)

    def test_walk_forward_never_trains_on_the_future(self, monkeypatch):
        """
        نتأكد بالمراقبة الفعلية: وقت التنبؤ بالعيّنة رقم i، النموذج لازم يكون
        تدرّب على i عيّنة بالضبط — ولا واحدة أكثر.
        """
        samples = _synthetic_samples(n=120, seed=7)
        seen_at_predict = []
        original_predict = OnlineLogisticModel.predict_proba

        def spy(self, x):
            seen_at_predict.append(self.n_samples)
            return original_predict(self, x)

        monkeypatch.setattr(OnlineLogisticModel, "predict_proba", spy)
        walk_forward_eval(samples, min_train=30)

        assert seen_at_predict == list(range(30, len(samples)))

    def test_detects_real_signal_in_synthetic_data(self):
        result = walk_forward_eval(_synthetic_samples(n=300, seed=11), min_train=40)
        assert result["ready"]
        assert result["auc"] > 0.65
        assert result["beats_baseline"]

    def test_reports_no_signal_on_pure_noise(self):
        """بيانات عشوائية بحتة: النموذج ما يفترض إشارة وهمية."""
        result = walk_forward_eval(_synthetic_samples(n=300, seed=13, noise_only=True),
                                   min_train=40)
        assert result["ready"]
        assert 0.35 < result["auc"] < 0.65

    def test_too_few_samples_returns_not_ready(self):
        result = walk_forward_eval(_synthetic_samples(n=20), min_train=30)
        assert not result["ready"]
        assert "note" in result

    def test_threshold_sweep_monotonic_in_kept_count(self):
        y = np.array([1, 0, 1, 0, 1, 1, 0, 0])
        p = np.array([0.9, 0.1, 0.8, 0.2, 0.7, 0.6, 0.3, 0.4])
        r = np.array([2.0, -1.0, 1.5, -1.0, 1.0, 0.5, -1.0, -1.0])
        rows = threshold_sweep(y, p, r)
        kept = [row["kept"] for row in rows]
        assert kept == sorted(kept, reverse=True)
        assert rows[0]["threshold"] == 0.0 and rows[0]["kept"] == len(y)

    def test_recommend_threshold_refuses_tiny_samples(self):
        """عتبة تترك 3 صفقات ما تُقترح مهما بدت أرقامها جميلة."""
        result = walk_forward_eval(_synthetic_samples(n=300, seed=17), min_train=40)
        rec = recommend_threshold(result, min_kept_pct=40.0, min_kept_trades=25)
        if rec["threshold"] > 0:
            chosen = next(r for r in result["threshold_sweep"]
                          if r["threshold"] == rec["threshold"])
            assert chosen["kept"] >= 25 and chosen["kept_pct"] >= 40.0

    def test_recommend_falls_back_to_no_filter_on_noise(self):
        result = walk_forward_eval(_synthetic_samples(n=300, seed=19, noise_only=True),
                                   min_train=40)
        rec = recommend_threshold(result, min_kept_pct=60.0, min_kept_trades=80)
        assert rec["threshold"] == 0.0

    def test_calibration_bins_cover_only_populated_ranges(self):
        y = np.array([1, 1, 0, 0])
        p = np.array([0.9, 0.85, 0.1, 0.15])
        bins = calibration_bins(y, p, n_bins=5)
        assert sum(b["count"] for b in bins) == 4


# ═════════════════════════════════════════════ الفلترة وحجم الصفقة
class TestFilter:
    def test_kelly_formula_known_values(self):
        # p=0.5, b=2  →  (0.5*3 - 1)/2 = 0.25
        assert kelly_fraction(0.5, 2.0) == pytest.approx(0.25)
        # حافة التعادل عند b=2 هي p=1/3 بالضبط
        assert kelly_fraction(1 / 3, 2.0) == pytest.approx(0.0, abs=1e-9)
        assert kelly_fraction(0.2, 2.0) == 0.0        # توقّع سلبي
        assert kelly_fraction(0.9, 0.0) == 0.0        # عائد صفري

    def test_passthrough_without_model(self):
        d = SignalFilter(model=None, base_risk_pct=0.01).evaluate({})
        assert d.take and d.size_mult == 1.0 and d.risk_pct == 0.01

    def test_passthrough_when_model_immature(self):
        model = OnlineLogisticModel(min_samples=40)
        model.partial_fit(np.zeros(len(FEATURE_NAMES)), 1)
        d = SignalFilter(model, threshold=0.9, base_risk_pct=0.01).evaluate(
            {n: 0.0 for n in FEATURE_NAMES})
        assert d.take, "نموذج غير ناضج ما يحق له يرفض صفقات"
        assert d.size_mult == 1.0

    def test_passthrough_when_features_missing(self):
        model = _ready_model()
        d = SignalFilter(model, threshold=0.9, base_risk_pct=0.01).evaluate(None)
        assert d.take and d.size_mult == 1.0

    def test_never_exceeds_configured_risk(self):
        """
        حد الأمان المركزي: مهما كان احتمال النموذج مرتفعًا، المخاطرة الفعلية
        ما تتجاوز RISK_PCT اللي حدده المستخدم.
        """
        model = _ready_model(always_win=True)
        f = SignalFilter(model, threshold=0.0, kelly_frac=1.0, base_risk_pct=0.01)
        feats = {n: 1.0 for n in FEATURE_NAMES}
        feats["rr_ratio"] = 5.0
        d = f.evaluate(feats)
        assert d.risk_pct <= 0.01 + 1e-12
        assert d.size_mult <= 1.0 + 1e-12

    def test_rejects_below_threshold(self):
        model = _ready_model(always_lose=True)
        f = SignalFilter(model, threshold=0.6, base_risk_pct=0.01)
        d = f.evaluate({n: 0.0 for n in FEATURE_NAMES})
        assert not d.take and d.risk_pct == 0.0

    def test_rejects_negative_expectancy_even_at_zero_threshold(self):
        """عتبة 0 لا تعني "خذ كل شيء" — Kelly يرفض التوقّع السلبي رياضيًا."""
        model = _ready_model(always_lose=True)
        d = SignalFilter(model, threshold=0.0, base_risk_pct=0.01).evaluate(
            {**{n: 0.0 for n in FEATURE_NAMES}, "rr_ratio": 2.0})
        assert not d.take

    def test_from_path_missing_file_is_passthrough(self, tmp_path):
        f = SignalFilter.from_path(tmp_path / "nope.json", threshold=0.9,
                                   base_risk_pct=0.01)
        assert f.model is None
        assert f.evaluate({n: 0.0 for n in FEATURE_NAMES}).take

    def test_decision_serializes_for_logging(self):
        d = SignalFilter(model=None, base_risk_pct=0.01).evaluate({})
        assert set(d.to_dict()) >= {"take", "probability", "size_mult", "reason"}


# ════════════════════════════════════ تكامل الفلترة مع محرّك الباكتيست
class TestBacktestIntegration:
    def test_backtest_unchanged_without_filter(self):
        df = _price_df()
        signals = TrendFollowStrategy(confirm_window=8).generate_signals(df)
        results = Backtester(1000, 0.01).run(signals)
        assert results["skipped_by_filter"] == 0
        assert results["num_trades"] > 0

    def test_filter_can_skip_trades(self):
        df = _price_df()
        signals = TrendFollowStrategy(confirm_window=8).generate_signals(df)
        from learning.dataset import attach_features
        attach_features(df, signals)

        blocking = SignalFilter(_ready_model(always_lose=True), threshold=0.99,
                                base_risk_pct=0.01)
        results = Backtester(1000, 0.01).run(signals, signal_filter=blocking)
        assert results["num_trades"] == 0
        assert results["skipped_by_filter"] > 0

    def test_skipped_buy_does_not_leave_dangling_sell(self):
        """رفض شراء لازم يُلغي بيعه التالي أيضًا، وإلا انحسب ربح من لا شيء."""
        df = _price_df(n=300)
        signals = [
            TradeSignal(df.index[250], "BUY", 100.0, stop_loss=95.0, take_profit=110.0),
            TradeSignal(df.index[255], "SELL", 110.0, reason="هدف"),
        ]
        from learning.dataset import attach_features
        attach_features(df, signals)
        results = Backtester(1000, 0.01).run(
            signals, signal_filter=SignalFilter(_ready_model(always_lose=True),
                                                threshold=0.99, base_risk_pct=0.01))
        assert results["num_trades"] == 0
        assert results["final_capital"] == 1000.0


# ═══════════════════════════════════════════ اختيار الاستراتيجية (Bandit)
class TestBandit:
    def test_reward_mapping_bounds(self):
        assert r_to_reward(-1.0) == pytest.approx(0.0)
        assert r_to_reward(3.0) == pytest.approx(1.0)
        assert r_to_reward(-5.0) == 0.0        # مقصوص
        assert r_to_reward(99.0) == 1.0
        assert r_to_reward(None) == 0.5        # مجهول = محايد

    def test_tries_every_strategy_before_judging(self):
        sel = StrategySelector(["a", "b", "c"], seed=0)
        picked = {sel.select() for _ in range(40)}
        # ما جُرّبت أي واحدة بعد، فالاختيار لازم يشمل الجميع
        assert picked == {"a", "b", "c"}

    def test_converges_to_the_better_strategy(self):
        sel = StrategySelector(["good", "bad"], seed=3)
        for _ in range(60):
            sel.update("good", 2.0)
            sel.update("bad", -1.0)
        assert sel.best() == "good"
        picks = [sel.select() for _ in range(200)]
        assert picks.count("good") > picks.count("bad") * 3

    def test_decay_lets_a_revived_strategy_win_again(self):
        """
        سوق تغيّر: استراتيجية كانت ممتازة صارت سيئة والعكس. بدون النسيان
        يبقى الاختيار عالقًا على البطل القديم للأبد.
        """
        sel = StrategySelector(["old", "new"], decay=0.9, seed=5)
        for _ in range(40):
            sel.update("old", 2.5)
            sel.update("new", -1.0)
        assert sel.best() == "old"
        for _ in range(40):
            sel.update("old", -1.0)
            sel.update("new", 2.5)
        assert sel.best() == "new"

    def test_save_load_roundtrip(self, tmp_path):
        sel = StrategySelector(["a", "b"], seed=1)
        for _ in range(10):
            sel.update("a", 1.5)
        path = tmp_path / "bandit.json"
        sel.save(path)
        loaded = StrategySelector.load_or_new(path, ["a", "b"])
        assert loaded.trials["a"] == 10
        assert loaded.best() == sel.best()

    def test_new_strategy_added_later_starts_fresh(self):
        sel = StrategySelector(["a"], seed=1)
        sel.update("a", 2.0)
        sel.update("brand_new", 1.0)
        assert "brand_new" in sel.trials and sel.trials["brand_new"] == 1

    def test_load_or_new_adds_strategies_missing_from_file(self, tmp_path):
        """استراتيجية أُضيفت للكود بعد آخر حفظ تنضم بحالة بكر ولها أولوية تجربة."""
        path = tmp_path / "b.json"
        existing = StrategySelector(["a"], seed=1)
        existing.update("a", 1.0)       # "a" صارت مجرّبة، "c" لسه لا
        existing.save(path)

        loaded = StrategySelector.load_or_new(path, ["a", "c"])
        assert loaded.trials["a"] == 1 and loaded.trials["c"] == 0
        # الوحيدة غير المجرّبة => تُختار حتميًا، بأي بذرة عشوائية
        assert all(loaded.select() == "c" for _ in range(20))


# ═════════════════════════════════════════════════ الذاكرة الدائمة
class TestStore:
    @pytest.fixture
    def db(self, tmp_path):
        path = tmp_path / "test.db"
        store.init_db(path)
        return path

    def _feats(self):
        return {n: 0.5 for n in FEATURE_NAMES}

    def test_record_and_resolve_computes_label_and_r(self, db):
        sid = store.record_signal("AAPL", "trend_follow", "2024-01-10", 100.0,
                                  self._feats(), stop_loss=95.0, take_profit=110.0,
                                  db_path=db)
        assert sid is not None
        assert len(store.pending_samples(db)) == 1

        store.resolve_sample(sid, 110.0, "2024-01-20", "هدف", db_path=db)
        assert store.pending_samples(db) == []
        rows = store.all_resolved(db)
        assert rows[0]["label"] == 1
        assert rows[0]["r_multiple"] == pytest.approx(2.0)

    def test_duplicate_signal_recorded_once(self, db):
        """تشغيل يدوي + cron بنفس اليوم ما يخلي البوت يتعلّم الصفقة مرتين."""
        args = ("AAPL", "trend_follow", "2024-01-10", 100.0, self._feats())
        first = store.record_signal(*args, stop_loss=95.0, db_path=db)
        second = store.record_signal(*args, stop_loss=95.0, db_path=db)
        assert first is not None and second is None
        assert store.stats(db)["total"] == 1

    def test_untrained_then_marked_trained(self, db):
        sid = store.record_signal("MSFT", "base_track", "2024-02-01", 50.0,
                                  self._feats(), stop_loss=48.0, db_path=db)
        store.resolve_sample(sid, 55.0, "2024-02-10", db_path=db)
        assert len(store.untrained_samples(db)) == 1
        store.mark_trained([sid], db_path=db)
        assert store.untrained_samples(db) == []

    def test_stats_separates_taken_from_skipped(self, db):
        taken = store.record_signal("A", "s", "2024-01-01", 10.0, self._feats(),
                                    stop_loss=9.0, taken=True, db_path=db)
        skipped = store.record_signal("B", "s", "2024-01-02", 10.0, self._feats(),
                                      stop_loss=9.0, taken=False, db_path=db)
        store.resolve_sample(taken, 12.0, "2024-01-05", db_path=db)
        store.resolve_sample(skipped, 8.0, "2024-01-06", db_path=db)

        st = store.stats(db)
        assert st["taken_total"] == 1 and st["taken_win_rate"] == 100.0
        assert st["skipped_total"] == 1 and st["skipped_win_rate"] == 0.0

    def test_training_pair_roundtrips_features(self, db):
        sid = store.record_signal("A", "s", "2024-01-01", 10.0, self._feats(),
                                  stop_loss=9.0, db_path=db)
        store.resolve_sample(sid, 12.0, "2024-01-05", db_path=db)
        x, label = store.sample_row_to_training_pair(store.all_resolved(db)[0])
        assert len(x) == len(FEATURE_NAMES) and label == 1

    def test_resolve_from_prices_picks_stop_before_target(self, db):
        """
        شمعة لمست الوقف والهدف معًا: نعتبرها خسارة عمدًا. البيانات اليومية
        ما تقول أيهما أولاً، والتفاؤل هنا يولّد بيانات تدريب كاذبة.
        """
        sid = store.record_signal("AAPL", "s", "2024-01-10", 100.0, self._feats(),
                                  stop_loss=95.0, take_profit=110.0, db_path=db)
        df = pd.DataFrame(
            {"High": [101.0, 112.0], "Low": [99.0, 94.0], "Close": [100.0, 105.0]},
            index=pd.to_datetime(["2024-01-10", "2024-01-11"]),
        )
        assert store.resolve_pending_with_prices(lambda s: df, db_path=db) == 1
        row = store.all_resolved(db)[0]
        assert row["label"] == 0 and row["exit_price"] == pytest.approx(95.0)

    def test_resolve_from_prices_hits_target(self, db):
        sid = store.record_signal("AAPL", "s", "2024-01-10", 100.0, self._feats(),
                                  stop_loss=95.0, take_profit=110.0, db_path=db)
        df = pd.DataFrame(
            {"High": [101.0, 105.0, 111.0], "Low": [99.0, 99.0, 104.0],
             "Close": [100.0, 104.0, 110.0]},
            index=pd.to_datetime(["2024-01-10", "2024-01-11", "2024-01-12"]),
        )
        store.resolve_pending_with_prices(lambda s: df, db_path=db)
        assert store.all_resolved(db)[0]["label"] == 1

    def test_open_trade_stays_pending(self, db):
        store.record_signal("AAPL", "s", "2024-01-10", 100.0, self._feats(),
                            stop_loss=95.0, take_profit=110.0, db_path=db)
        df = pd.DataFrame(
            {"High": [101.0, 103.0], "Low": [99.0, 98.0], "Close": [100.0, 101.0]},
            index=pd.to_datetime(["2024-01-10", "2024-01-11"]),
        )
        assert store.resolve_pending_with_prices(lambda s: df, db_path=db) == 0
        assert len(store.pending_samples(db)) == 1

    def test_max_hold_days_forces_an_exit(self, db):
        store.record_signal("AAPL", "s", "2024-01-10", 100.0, self._feats(),
                            stop_loss=95.0, take_profit=200.0, db_path=db)
        idx = pd.date_range("2024-01-10", periods=40, freq="B")
        df = pd.DataFrame({"High": [103.0] * 40, "Low": [98.0] * 40,
                           "Close": [101.0] * 40}, index=idx)
        assert store.resolve_pending_with_prices(lambda s: df, max_hold_days=20,
                                                 db_path=db) == 1
        assert "مهلة" in store.all_resolved(db)[0]["exit_reason"]

    def test_refuses_to_resolve_when_data_starts_after_entry(self, db):
        """
        البيانات المجلوبة ما تغطي تاريخ الإشارة (نافذة قصيرة، أو البوت كان
        متوقفًا شهورًا). أول شمعة متاحة قد تكون بعدها بسنوات وسعرها أعلى
        بكثير — فتُحتسب "تحقيق هدف" وهميًا ويتعلّم النموذج درسًا مزيّفًا.
        الصح: ما نحلّها إطلاقًا، وتبقى معلّقة لتشغيلة بنافذة أوسع.
        """
        recent = (pd.Timestamp.now().normalize() - pd.Timedelta(days=20))
        store.record_signal("AAPL", "s", recent, 100.0, self._feats(),
                            stop_loss=95.0, take_profit=110.0, db_path=db)
        # بيانات تبدأ بعد تاريخ الإشارة بكثير، وبأسعار أعلى من الهدف
        late = pd.DataFrame(
            {"High": [900.0], "Low": [880.0], "Close": [890.0]},
            index=[pd.Timestamp.now().normalize() - pd.Timedelta(days=1)],
        )
        assert store.resolve_pending_with_prices(lambda s: late, db_path=db) == 0
        assert len(store.pending_samples(db)) == 1
        assert store.all_resolved(db) == []

    def test_hopelessly_old_sample_is_abandoned_not_mislabeled(self, db):
        """عيّنة قديمة جدًا بلا أمل بتغطيتها تُغلق بلا تسمية بدل ما تتراكم للأبد."""
        store.record_signal("AAPL", "s", "2015-01-10", 100.0, self._feats(),
                            stop_loss=95.0, take_profit=110.0, db_path=db)
        recent = pd.DataFrame(
            {"High": [900.0], "Low": [880.0], "Close": [890.0]},
            index=[pd.Timestamp.now().normalize() - pd.Timedelta(days=1)],
        )
        assert store.resolve_pending_with_prices(lambda s: recent, max_hold_days=60,
                                                 db_path=db) == 0
        assert store.pending_samples(db) == []      # ما عادت تُعاد المحاولة
        assert store.all_resolved(db) == []         # ومستبعدة من التدريب

        st = store.stats(db)
        assert st["abandoned"] == 1 and st["resolved"] == 0

    def test_one_symbol_failure_does_not_block_others(self, db):
        store.record_signal("BAD", "s", "2024-01-10", 100.0, self._feats(),
                            stop_loss=95.0, take_profit=110.0, db_path=db)
        store.record_signal("GOOD", "s", "2024-01-10", 100.0, self._feats(),
                            stop_loss=95.0, take_profit=110.0, db_path=db)
        good_df = pd.DataFrame(
            {"High": [101.0, 111.0], "Low": [99.0, 99.0], "Close": [100.0, 110.0]},
            index=pd.to_datetime(["2024-01-10", "2024-01-11"]),
        )

        def fetch(symbol):
            if symbol == "BAD":
                raise RuntimeError("فشل تحميل")
            return good_df

        assert store.resolve_pending_with_prices(fetch, db_path=db) == 1


# ══════════════════════════════════════════════════════════ أدوات مساعدة
def _synthetic_samples(n=200, seed=0, noise_only=False):
    """
    عيّنات اصطناعية بتاريخ متصاعد. بدون noise_only، أول خاصيتين تحملان
    إشارة حقيقية للنتيجة — فنقدر نتأكد أن النموذج يلتقط ما هو موجود فعلاً،
    ويصمت عندما لا يوجد شيء.
    """
    from learning.dataset import Sample
    rng = np.random.RandomState(seed)
    dates = pd.date_range("2020-01-01", periods=n, freq="B")
    samples = []
    for i in range(n):
        vals = rng.normal(0, 1, len(FEATURE_NAMES))
        if noise_only:
            p_win = 0.45
        else:
            score = 1.4 * vals[0] - 1.1 * vals[1]
            p_win = 1.0 / (1.0 + np.exp(-score))
        label = int(rng.random() < p_win)
        samples.append(Sample(
            date=dates[i], symbol="SYN", strategy="syn",
            features=dict(zip(FEATURE_NAMES, vals)),
            label=label,
            r_multiple=2.0 if label else -1.0,
            entry_price=100.0, exit_price=110.0 if label else 95.0,
        ))
    return samples


def _ready_model(always_win=False, always_lose=False):
    """نموذج ناضج بسلوك معروف — لاختبار منطق الفلترة بمعزل عن جودة التنبؤ."""
    model = OnlineLogisticModel(min_samples=10)
    rng = np.random.RandomState(0)
    X = rng.normal(0, 1, (60, len(FEATURE_NAMES)))
    if always_win:
        y = np.ones(60, dtype=int)
    elif always_lose:
        y = np.zeros(60, dtype=int)
    else:
        y = (X[:, 0] > 0).astype(int)
    model.fit(X, y, epochs=20, balance=False)
    return model
