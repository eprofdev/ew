"""
tests/test_brokers.py
اختبارات طبقة الوسطاء — بدون أي اتصال شبكة أو مفاتيح حقيقية.

الهدف الأساسي: التركيبات الخطرة تُرفض بخطأ واضح، لا تُقارَب بصمت.
أخطرها على الإطلاق: EXECUTION_MODE=PAPER مع BROKER=webull — المستخدم
يظن أنه يجرّب وهميًا وهو ينفّذ بفلوس حقيقية.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from brokers import get_executor, SUPPORTED_BROKERS
from brokers.base import BrokerExecutor


class TestExecutorFactory:
    def test_signal_only_returns_no_executor(self):
        assert get_executor("SIGNAL_ONLY", "alpaca") is None
        assert get_executor("SIGNAL_ONLY", "webull") is None

    def test_webull_paper_is_refused_loudly(self):
        """Webull ما عنده حساب تجريبي — PAPER معه إحساس كاذب بالأمان."""
        with pytest.raises(ValueError, match="ما يوفّر حسابًا تجريبيًا"):
            get_executor("PAPER", "webull")

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError, match="EXECUTION_MODE"):
            get_executor("DEMO", "alpaca")

    def test_unknown_broker_rejected(self):
        with pytest.raises(ValueError, match="BROKER"):
            get_executor("PAPER", "interactive_brokers")

    def test_mode_and_broker_are_case_insensitive(self):
        assert get_executor("signal_only", "ALPACA") is None

    def test_reads_environment_when_args_omitted(self, monkeypatch):
        monkeypatch.setenv("EXECUTION_MODE", "SIGNAL_ONLY")
        monkeypatch.setenv("BROKER", "webull")
        assert get_executor() is None

    def test_defaults_to_signal_only_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("EXECUTION_MODE", raising=False)
        monkeypatch.delenv("BROKER", raising=False)
        assert get_executor() is None


class TestBrokerCapabilities:
    """
    قدرات الوسيط معلنة كخصائص صنف، فـdaily_runner يقرأها بدون إنشاء
    اتصال — يعني نقدر نختبرها هنا بلا مفاتيح ولا شبكة.
    """

    def test_alpaca_declares_paper_and_bracket(self):
        pytest.importorskip("alpaca", reason="alpaca-py غير مثبّتة")
        from brokers.alpaca import AlpacaExecutor
        assert issubclass(AlpacaExecutor, BrokerExecutor)
        assert AlpacaExecutor.supports_paper is True
        assert AlpacaExecutor.supports_bracket is True

    def test_webull_declares_no_paper_no_bracket(self):
        pytest.importorskip("webullsdkcore", reason="Webull SDK غير مثبّتة")
        from brokers.webull import WebullExecutor
        assert issubclass(WebullExecutor, BrokerExecutor)
        assert WebullExecutor.supports_paper is False
        assert WebullExecutor.supports_bracket is False

    def test_risk_description_warns_about_missing_protections(self):
        class FakeBroker(BrokerExecutor):
            supports_paper = False
            supports_bracket = False

            def get_account_info(self): return {}
            def has_open_position(self, symbol): return False
            def open_position_count(self): return 0
            def execute_signal(self, symbol, signal, risk_pct=0.01): return None

        text = FakeBroker().describe_risk()
        assert "تجريبي" in text and "Bracket" in text

    def test_safe_broker_description_is_reassuring(self):
        class SafeBroker(BrokerExecutor):
            supports_paper = True
            supports_bracket = True

            def get_account_info(self): return {}
            def has_open_position(self, symbol): return False
            def open_position_count(self): return 0
            def execute_signal(self, symbol, signal, risk_pct=0.01): return None

        assert "يدعم" in SafeBroker().describe_risk()

    def test_interface_cannot_be_instantiated_incomplete(self):
        """وسيط جديد ينسى دالة إلزامية يفشل عند الإنشاء لا وقت أول أمر."""
        class Incomplete(BrokerExecutor):
            def get_account_info(self): return {}

        with pytest.raises(TypeError):
            Incomplete()

    def test_supported_brokers_list_matches_factory(self):
        assert set(SUPPORTED_BROKERS) == {"alpaca", "webull"}


class TestWebullGuards:
    def test_requires_explicit_live_confirmation(self, monkeypatch):
        """
        بدون WEBULL_CONFIRM_LIVE=yes ما ينبني المنفّذ إطلاقًا — حماية من
        إعداد ناقص يرسل أول أمر بفلوس حقيقية.
        """
        pytest.importorskip("webullsdkcore", reason="Webull SDK غير مثبّتة")
        from brokers.webull import WebullExecutor

        monkeypatch.setenv("WEBULL_APP_KEY", "fake")
        monkeypatch.setenv("WEBULL_APP_SECRET", "fake")
        monkeypatch.delenv("WEBULL_CONFIRM_LIVE", raising=False)
        with pytest.raises(RuntimeError, match="WEBULL_CONFIRM_LIVE"):
            WebullExecutor()

    def test_missing_keys_error_is_explicit(self, monkeypatch):
        pytest.importorskip("webullsdkcore", reason="Webull SDK غير مثبّتة")
        from brokers.webull import WebullExecutor

        monkeypatch.delenv("WEBULL_APP_KEY", raising=False)
        monkeypatch.delenv("WEBULL_APP_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="WEBULL_APP_KEY"):
            WebullExecutor()
