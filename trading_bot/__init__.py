"""حزمة بوت التداول المدمج: سلوك سعري (فيصل) + صمامات أمان (نايف).

تحذير: هذا الكود أداة بحثية/تعليمية لبناء التنبيهات فقط، وليس نصيحة مالية.
أسهم السنتات (Micro/Nano Cap) عالية المخاطر جداً وقد تخسر رأس المال بالكامل.
"""

from .models import (
    Candle,
    Decision,
    MarketSnapshot,
    RealTimeData,
    Signal,
    StockMetrics,
)
from .engine import EnhancedTradingBot
from .config import BotConfig

__all__ = [
    "BotConfig",
    "Candle",
    "Decision",
    "EnhancedTradingBot",
    "MarketSnapshot",
    "RealTimeData",
    "Signal",
    "StockMetrics",
]

__version__ = "1.0.0"
