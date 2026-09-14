"""
brokers/
طبقة الوسطاء: نفس البوت يشتغل على وسطاء مختلفين بدون تغيير منطقه.

    BROKER=alpaca  (افتراضي) — يدعم حسابًا تجريبيًا وأوامر Bracket
    BROKER=webull            — تنفيذ حقيقي فقط، بلا Bracket (راجع webull.py)

EXECUTION_MODE يبقى هو المفتاح الرئيسي:
    SIGNAL_ONLY — تحليل وتسجيل وتعلّم بدون أي أمر (الافتراضي، وأأمنها)
    PAPER       — تنفيذ تجريبي (Alpaca فقط)
    LIVE        — تنفيذ حقيقي
"""
import os

from brokers.base import BrokerExecutor

SUPPORTED_BROKERS = ("alpaca", "webull")


def get_executor(execution_mode: str = None, broker: str = None):
    """
    يبني المنفّذ المناسب، أو None بوضع SIGNAL_ONLY.

    يرفض بوضوح أي تركيبة غير آمنة بدل ما "يقاربها":
    طلب PAPER على Webull يعني عمليًا تنفيذًا حقيقيًا بينما أنت تظن أنه تجريبي —
    فنوقف بخطأ بدل ما نكمل.
    """
    mode = (execution_mode or os.environ.get("EXECUTION_MODE", "SIGNAL_ONLY")).upper()
    name = (broker or os.environ.get("BROKER", "alpaca")).lower()

    if mode == "SIGNAL_ONLY":
        return None
    if mode not in ("PAPER", "LIVE"):
        raise ValueError(f"EXECUTION_MODE غير معروف: {mode} — "
                         "الخيارات: SIGNAL_ONLY | PAPER | LIVE")
    if name not in SUPPORTED_BROKERS:
        raise ValueError(f"BROKER غير مدعوم: {name} — الخيارات: {SUPPORTED_BROKERS}")

    if name == "alpaca":
        from brokers.alpaca import AlpacaExecutor
        return AlpacaExecutor(paper=(mode == "PAPER"))

    if name == "webull":
        if mode == "PAPER":
            raise ValueError(
                "Webull OpenAPI ما يوفّر حسابًا تجريبيًا، و EXECUTION_MODE=PAPER معه "
                "يعطيك إحساسًا كاذبًا بالأمان.\n"
                "للتجربة: BROKER=alpaca مع EXECUTION_MODE=PAPER.\n"
                "للتنفيذ الحقيقي عبر Webull: EXECUTION_MODE=LIVE مع "
                "WEBULL_CONFIRM_LIVE=yes."
            )
        from brokers.webull import WebullExecutor
        return WebullExecutor()


__all__ = ["BrokerExecutor", "get_executor", "SUPPORTED_BROKERS"]
