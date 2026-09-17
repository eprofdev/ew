"""إدارة المخاطر: حجم المركز، الوقف، والأهداف."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .config import BotConfig
from .models import StockMetrics


@dataclass
class RiskPlan:
    entry: float
    stop: float
    size: int
    targets: List[float]
    risk_amount: float
    notes: List[str]


class RiskManager:
    def __init__(self, config: Optional[BotConfig] = None) -> None:
        self.config = config or BotConfig()

    def build_plan(
        self,
        entry: float,
        stop_reference: float,
        metrics: StockMetrics,
    ) -> Optional[RiskPlan]:
        cfg = self.config
        notes: List[str] = []

        stop = round(stop_reference * (1 - cfg.stop_buffer_pct), 4)
        if entry <= 0 or stop <= 0 or stop >= entry:
            return None

        risk_per_share = entry - stop
        risk_amount = cfg.account_equity * cfg.risk_per_trade_pct
        size = int(risk_amount // risk_per_share)

        # سقف قيمة المركز من رأس المال
        max_value = cfg.account_equity * cfg.max_position_pct_of_equity
        if size * entry > max_value:
            size = int(max_value // entry)
            notes.append("تم تقليص الكمية بسقف نسبة المركز من رأس المال")

        # سقف السيولة: لا تكن أنت السوق
        if metrics.avg_daily_volume:
            max_shares = int(metrics.avg_daily_volume * cfg.max_position_pct_of_adv)
            if max_shares and size > max_shares:
                size = max_shares
                notes.append("تم تقليص الكمية بسقف نسبة الفوليوم اليومي (خطر التعليق)")

        if size <= 0:
            return None

        targets = [
            round(entry + risk_per_share * r, 4) for r in cfg.targets_r_multiples
        ]
        return RiskPlan(
            entry=round(entry, 4),
            stop=stop,
            size=size,
            targets=targets,
            risk_amount=round(size * risk_per_share, 2),
            notes=notes,
        )
