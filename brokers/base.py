"""
brokers/base.py
الواجهة الموحّدة لأي وسيط. daily_runner ما يعرف أنه Alpaca أو Webull —
يعرف فقط هذي الدوال الخمس، فإضافة وسيط جديد ما تحتاج تعديل منطق البوت.
"""
from abc import ABC, abstractmethod

from strategy import TradeSignal


class BrokerExecutor(ABC):
    """
    عقد أي وسيط. الخاصيتان الوصفيتان مهمتان عمليًا:

    supports_paper: هل يوفر الوسيط حسابًا تجريبيًا؟ Alpaca نعم، Webull لا.
    supports_bracket: هل يقدر يسجّل وقف الخسارة والهدف مع أمر الدخول نفسه؟
        لو لا، فوقف الخسارة يحتاج أمرًا منفصلاً بعد التنفيذ، والهدف يبقى
        تحت مراقبة daily_runner — وهذا فرق جوهري بالمخاطرة لازم يكون
        ظاهرًا للبوت لا مدفونًا داخل كود الوسيط.
    """

    name: str = "base"
    supports_paper: bool = False
    supports_bracket: bool = False

    @abstractmethod
    def get_account_info(self) -> dict:
        """يرجع على الأقل: equity, cash, mode."""

    @abstractmethod
    def has_open_position(self, symbol: str) -> bool:
        ...

    @abstractmethod
    def open_position_count(self) -> int:
        """عدد الصفقات المفتوحة بالحساب كله — لسقف المخاطرة الإجمالي."""

    @abstractmethod
    def execute_signal(self, symbol: str, signal: TradeSignal,
                       risk_pct: float = 0.01):
        """ينفّذ إشارة. يترك الاستثناءات تصعد ليسجّلها المستدعي مركزيًا."""

    def describe_risk(self) -> str:
        """تحذير قابل للطباعة بالتقرير عن حدود حماية هذا الوسيط."""
        parts = []
        if not self.supports_paper:
            parts.append("لا يوفر حسابًا تجريبيًا — كل أمر بفلوس حقيقية")
        if not self.supports_bracket:
            parts.append("لا يدعم Bracket — الهدف يُراقَب من البوت لا من الوسيط")
        return "؛ ".join(parts) if parts else "يدعم الحساب التجريبي وأوامر Bracket"
