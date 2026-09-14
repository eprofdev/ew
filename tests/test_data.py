"""
tests/test_data.py
اختبارات طبقة مزوّدي البيانات — بلا أي اتصال شبكة (الردود مُحاكاة).

التركيز على الشيء اللي لو انكسر بصمت أفسد كل شيء بعده: اختلاف شكل البيانات
بين مزوّد وآخر. عمود ناقص أو فهرس تنازلي أو منطقة زمنية ملتصقة تنتج مؤشرات
خاطئة بلا أي رسالة خطأ — والاستراتيجية تتداول على أرقام غلط وهي "شغّالة".
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json

import numpy as np
import pandas as pd
import pytest

from data import fetch_data, get_provider, reset_provider_cache, period_to_days
from data.base import DataProvider, REQUIRED_COLUMNS
from data.csv_provider import CsvProvider
from data.twelvedata import TwelveDataProvider, RateLimiter


@pytest.fixture(autouse=True)
def _clean_provider_cache():
    reset_provider_cache()
    yield
    reset_provider_cache()


# ═══════════════════════════════════════════════════ توحيد شكل البيانات
class TestNormalize:
    def test_sorts_ascending_and_drops_timezone(self):
        idx = pd.to_datetime(["2024-01-03", "2024-01-01", "2024-01-02"], utc=True)
        df = pd.DataFrame({c: [3.0, 1.0, 2.0] for c in REQUIRED_COLUMNS}, index=idx)
        out = DataProvider.normalize(df)
        assert out.index.is_monotonic_increasing
        assert out.index.tz is None
        assert list(out["Close"]) == [1.0, 2.0, 3.0]

    def test_removes_duplicate_dates_keeping_latest(self):
        idx = pd.to_datetime(["2024-01-01", "2024-01-01"])
        df = pd.DataFrame({c: [1.0, 9.0] for c in REQUIRED_COLUMNS}, index=idx)
        out = DataProvider.normalize(df)
        assert len(out) == 1 and out["Close"].iloc[0] == 9.0

    def test_missing_column_raises_instead_of_silently_continuing(self):
        df = pd.DataFrame({"Open": [1.0], "Close": [1.0]},
                          index=pd.to_datetime(["2024-01-01"]))
        with pytest.raises(ValueError, match="بيانات ناقصة الأعمدة"):
            DataProvider.normalize(df)

    def test_flattens_multiindex_columns(self):
        """yfinance ترجّع أعمدة متعددة المستويات لما تطلب أكثر من رمز."""
        idx = pd.to_datetime(["2024-01-01"])
        df = pd.DataFrame([[1.0, 2.0, 0.5, 1.5, 100.0]], index=idx,
                          columns=pd.MultiIndex.from_tuples(
                              [(c, "AAPL") for c in REQUIRED_COLUMNS]))
        out = DataProvider.normalize(df)
        assert list(out.columns) == REQUIRED_COLUMNS

    def test_string_numbers_become_floats(self):
        """Twelve Data ترجّع الأرقام كنصوص — لو بقيت نصًا انهارت المؤشرات."""
        df = pd.DataFrame({c: ["1.5"] for c in REQUIRED_COLUMNS},
                          index=pd.to_datetime(["2024-01-01"]))
        out = DataProvider.normalize(df)
        assert out["Close"].dtype == np.float64

    def test_empty_input_returns_empty_frame(self):
        out = DataProvider.normalize(pd.DataFrame())
        assert out.empty and list(out.columns) == REQUIRED_COLUMNS


class TestPeriodParsing:
    @pytest.mark.parametrize("period,days", [
        ("1y", 365), ("5y", 1825), ("6mo", 180), ("90d", 90), ("2w", 14),
    ])
    def test_known_periods(self, period, days):
        assert period_to_days(period) == days

    def test_months_not_confused_with_minutes(self):
        """'6mo' شهور لا دقائق — الالتباس هنا يقصّ بياناتك 99%."""
        assert period_to_days("6mo") == 180

    def test_max_is_long_enough_for_any_stock(self):
        assert period_to_days("max") >= 365 * 25

    def test_garbage_raises(self):
        with pytest.raises(ValueError, match="مدة غير مفهومة"):
            period_to_days("سنتين")


# ═══════════════════════════════════════════════════════ اختيار المزوّد
class TestProviderSelection:
    def test_defaults_to_yahoo(self, monkeypatch):
        monkeypatch.delenv("DATA_PROVIDER", raising=False)
        assert get_provider().name == "yahoo"

    def test_reads_env_variable(self, monkeypatch):
        monkeypatch.setenv("DATA_PROVIDER", "csv")
        assert get_provider().name == "csv"

    def test_twelvedata_aliases(self, monkeypatch):
        monkeypatch.setenv("TWELVEDATA_API_KEY", "k")
        for alias in ("twelvedata", "twelve_data", "td", "TwelveData"):
            reset_provider_cache()
            assert get_provider(alias).name == "twelvedata"

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValueError, match="DATA_PROVIDER غير معروف"):
            get_provider("bloomberg")

    def test_provider_instance_is_reused(self, monkeypatch):
        """الكاش ومحدّد المعدّل يعيشان داخل المزوّد — إنشاؤه بكل طلب يضيّعهما."""
        monkeypatch.setenv("DATA_PROVIDER", "csv")
        assert get_provider() is get_provider()

    def test_switching_provider_rebuilds(self, monkeypatch):
        monkeypatch.setenv("DATA_PROVIDER", "csv")
        first = get_provider()
        monkeypatch.setenv("DATA_PROVIDER", "yahoo")
        reset_provider_cache()
        assert get_provider() is not first

    def test_twelvedata_without_key_fails_clearly(self, monkeypatch):
        monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="TWELVEDATA_API_KEY"):
            get_provider("twelvedata")


# ═════════════════════════════════════════════════════ مزوّد ملفات CSV
class TestCsvProvider:
    @pytest.fixture
    def csv_dir(self, tmp_path):
        idx = pd.date_range("2020-01-01", periods=400, freq="B")
        df = pd.DataFrame({
            "Open": np.linspace(10, 50, 400), "High": np.linspace(11, 51, 400),
            "Low": np.linspace(9, 49, 400), "Close": np.linspace(10, 50, 400),
            "Volume": np.full(400, 1_000_000.0),
        }, index=idx)
        df.index.name = "Date"
        df.to_csv(tmp_path / "AAPL.csv")
        return tmp_path

    def test_reads_and_normalizes(self, csv_dir):
        df = CsvProvider(csv_dir).fetch("AAPL", period="10y")
        assert len(df) == 400 and list(df.columns) == REQUIRED_COLUMNS

    def test_period_trims_from_last_bar_not_today(self, csv_dir):
        """
        القص من آخر شمعة بالملف لا من تاريخ اليوم: ملف محفوظ من شهور
        يعطي نفس النتيجة مهما تأخّر تشغيله — شرط لإعادة إنتاج النتائج.
        """
        full = CsvProvider(csv_dir).fetch("AAPL", period="10y")
        one_year = CsvProvider(csv_dir).fetch("AAPL", period="1y")
        assert 200 < len(one_year) < 400
        # النهاية ثابتة عند آخر شمعة بالملف، والبداية تبعد عنها سنة بالضبط
        assert one_year.index[-1] == full.index[-1]
        assert (one_year.index[-1] - one_year.index[0]).days <= 365

    def test_symbol_is_case_insensitive(self, csv_dir):
        assert not CsvProvider(csv_dir).fetch("aapl", period="10y").empty

    def test_missing_file_names_the_fix(self, csv_dir):
        with pytest.raises(FileNotFoundError, match="data.download"):
            CsvProvider(csv_dir).fetch("TSLA")


# ═══════════════════════════════════════════════════ مزوّد Twelve Data
class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


def _td_payload(n=5):
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return {
        "meta": {"symbol": "AAPL", "interval": "1day"},
        "values": [{
            "datetime": d.strftime("%Y-%m-%d"),
            "open": f"{100 + i}", "high": f"{101 + i}",
            "low": f"{99 + i}", "close": f"{100.5 + i}",
            "volume": f"{1000000 + i}",
        } for i, d in enumerate(dates)],
        "status": "ok",
    }


class TestTwelveDataProvider:
    @pytest.fixture
    def provider(self, tmp_path, monkeypatch):
        monkeypatch.setattr("data.twelvedata.CACHE_DIR", tmp_path / "cache")
        return TwelveDataProvider(api_key="test-key", cache_hours=0)

    def test_parses_string_values_into_float_frame(self, provider, monkeypatch):
        monkeypatch.setattr("data.twelvedata.requests.get",
                            lambda *a, **k: _FakeResponse(_td_payload(5)))
        df = provider.fetch("AAPL", period="1y")
        assert len(df) == 5
        assert list(df.columns) == REQUIRED_COLUMNS
        assert df["Close"].dtype == np.float64
        assert df.index.is_monotonic_increasing

    def test_requests_dividend_and_split_adjusted_prices(self, provider, monkeypatch):
        """
        adjust=all إلزامي: الافتراضي بالواجهة يعدّل التجزئة فقط، فتختلف
        النتائج عن yfinance ويصير الفرق بين المزوّدين غير مفسَّر.
        """
        captured = {}

        def fake_get(url, **kwargs):
            captured.update(kwargs.get("params", {}))
            return _FakeResponse(_td_payload(3))

        monkeypatch.setattr("data.twelvedata.requests.get", fake_get)
        provider.fetch("AAPL", period="2y")
        assert captured["adjust"] == "all"
        assert captured["order"] == "asc"
        assert captured["outputsize"] == 5000

    def test_interval_alias_translated(self, provider, monkeypatch):
        captured = {}

        def fake_get(url, **kwargs):
            captured.update(kwargs.get("params", {}))
            return _FakeResponse(_td_payload(3))

        monkeypatch.setattr("data.twelvedata.requests.get", fake_get)
        provider.fetch("AAPL", interval="1d")
        assert captured["interval"] == "1day"

    def test_rate_limit_error_explains_the_fix(self, provider, monkeypatch):
        monkeypatch.setattr("data.twelvedata.requests.get",
                            lambda *a, **k: _FakeResponse({}, status_code=429))
        with pytest.raises(RuntimeError, match="TWELVEDATA_CACHE_HOURS"):
            provider.fetch("AAPL")

    def test_rate_limit_inside_body_also_caught(self, provider, monkeypatch):
        """الواجهة ترجّع أحيانًا 429 داخل الجسم مع HTTP 200."""
        monkeypatch.setattr("data.twelvedata.requests.get", lambda *a, **k: _FakeResponse(
            {"status": "error", "code": 429, "message": "limit"}))
        with pytest.raises(RuntimeError, match="حد الطلبات"):
            provider.fetch("AAPL")

    def test_invalid_key_message(self, provider, monkeypatch):
        monkeypatch.setattr("data.twelvedata.requests.get", lambda *a, **k: _FakeResponse(
            {"status": "error", "code": 401, "message": "bad key"}))
        with pytest.raises(RuntimeError, match="مفتاح API غير صالح"):
            provider.fetch("AAPL")

    def test_unknown_symbol_returns_empty_not_exception(self, provider, monkeypatch):
        """رمز واحد غلط ما يوقف تدريبًا على عشرين رمزًا."""
        monkeypatch.setattr("data.twelvedata.requests.get", lambda *a, **k: _FakeResponse(
            {"status": "error", "code": 404, "message": "not found"}))
        assert provider.fetch("NOTREAL").empty

    def test_empty_values_returns_empty_frame(self, provider, monkeypatch):
        monkeypatch.setattr("data.twelvedata.requests.get",
                            lambda *a, **k: _FakeResponse({"values": [], "status": "ok"}))
        assert provider.fetch("AAPL").empty

    def test_unreadable_response_raises_with_context(self, provider, monkeypatch):
        monkeypatch.setattr("data.twelvedata.requests.get",
                            lambda *a, **k: _FakeResponse("<html>error</html>"))
        with pytest.raises(RuntimeError, match="رد غير قابل للقراءة"):
            provider.fetch("AAPL")

    def test_network_failure_is_wrapped(self, provider, monkeypatch):
        import requests as rq

        def boom(*a, **k):
            raise rq.ConnectionError("down")

        monkeypatch.setattr("data.twelvedata.requests.get", boom)
        with pytest.raises(RuntimeError, match="فشل الاتصال"):
            provider.fetch("AAPL")

    def test_cache_prevents_a_second_request(self, tmp_path, monkeypatch):
        monkeypatch.setattr("data.twelvedata.CACHE_DIR", tmp_path / "cache")
        provider = TwelveDataProvider(api_key="k", cache_hours=12)
        calls = []

        def fake_get(*a, **k):
            calls.append(1)
            return _FakeResponse(_td_payload(5))

        monkeypatch.setattr("data.twelvedata.requests.get", fake_get)
        first = provider.fetch("AAPL", period="1y")
        second = provider.fetch("AAPL", period="1y")
        assert len(calls) == 1, "الطلب الثاني لازم يجي من الكاش"
        assert first.equals(second)

    def test_corrupt_cache_falls_back_to_network(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache"
        cache.mkdir()
        monkeypatch.setattr("data.twelvedata.CACHE_DIR", cache)
        provider = TwelveDataProvider(api_key="k", cache_hours=12)
        provider._cache_path("AAPL", "1day", "1y").write_text("ملف تالف تمامًا")

        monkeypatch.setattr("data.twelvedata.requests.get",
                            lambda *a, **k: _FakeResponse(_td_payload(4)))
        assert len(provider.fetch("AAPL", period="1y")) == 4

    def test_start_date_reflects_requested_period(self, provider, monkeypatch):
        from datetime import datetime, timezone
        captured = {}

        def fake_get(url, **kwargs):
            captured.update(kwargs.get("params", {}))
            return _FakeResponse(_td_payload(3))

        monkeypatch.setattr("data.twelvedata.requests.get", fake_get)
        provider.fetch("AAPL", period="5y")
        years_back = (datetime.now(timezone.utc)
                      - datetime.strptime(captured["start_date"], "%Y-%m-%d")
                      .replace(tzinfo=timezone.utc)).days / 365.0
        assert 4.9 < years_back < 5.1


class TestRateLimiter:
    def test_allows_calls_below_the_limit_without_sleeping(self):
        import time
        limiter = RateLimiter(max_calls=5, period_seconds=60)
        start = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        assert time.monotonic() - start < 0.5

    def test_sleeps_once_the_window_is_full(self):
        import time
        limiter = RateLimiter(max_calls=2, period_seconds=0.6)
        start = time.monotonic()
        for _ in range(4):
            limiter.acquire()
        assert time.monotonic() - start >= 0.6


# ═══════════════════════════════════════ تكامل: fetch_data لكل المشروع
class TestFetchDataEntryPoint:
    def test_run_backtest_fetch_data_uses_provider_layer(self, tmp_path, monkeypatch):
        """
        كل ملفات المشروع تستورد fetch_data من run_backtest — نتأكد أنها
        فعلاً تمر بطبقة المزوّدين، وإلا بقي تبديل المزوّد بلا أثر.
        """
        idx = pd.date_range("2023-01-02", periods=50, freq="B")
        df = pd.DataFrame({c: np.linspace(10, 20, 50) for c in REQUIRED_COLUMNS}, index=idx)
        df.index.name = "Date"
        df.to_csv(tmp_path / "TEST.csv")

        monkeypatch.setenv("DATA_PROVIDER", "csv")
        monkeypatch.setenv("DATA_CSV_DIR", str(tmp_path))
        reset_provider_cache()

        from run_backtest import fetch_data as rb_fetch
        assert len(rb_fetch("TEST", period="5y")) == 50

    def test_fetch_data_accepts_explicit_provider_override(self, tmp_path, monkeypatch):
        idx = pd.date_range("2023-01-02", periods=10, freq="B")
        df = pd.DataFrame({c: np.linspace(1, 2, 10) for c in REQUIRED_COLUMNS}, index=idx)
        df.index.name = "Date"
        df.to_csv(tmp_path / "X.csv")

        monkeypatch.setenv("DATA_PROVIDER", "yahoo")
        monkeypatch.setenv("DATA_CSV_DIR", str(tmp_path))
        assert len(fetch_data("X", period="5y", provider="csv")) == 10
