"""دفتر أسعار حي (L2) — البنية التي تبني عليها Bookmap خريطتها الحرارية."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class OrderFlowSnapshot:
    """صورة لحظية لما يقوله الدفتر."""

    bid: float = 0.0
    ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    depth_bid: float = 0.0          # مجموع السيولة على الطلب ضمن N مستويات
    depth_ask: float = 0.0
    imbalance: float = 0.0          # (طلب−عرض)/(طلب+عرض)، +1 = ضغط شرائي
    aggressor_ratio: float = 0.5    # نسبة الصفقات التي ضربت العرض
    absorption_price: Optional[float] = None   # سعر امتص بيعاً ضخماً دون أن ينكسر
    iceberg_prices: List[float] = field(default_factory=list)
    ask_wall: Optional[float] = None           # جدار عرض فوق السعر
    notes: List[str] = field(default_factory=list)

    @property
    def spread(self) -> float:
        return self.ask - self.bid if (self.bid > 0 and self.ask > 0) else 0.0

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2
        return self.spread / mid if mid > 0 else 0.0


class OrderBook:
    """دفتر أوامر بسيط يُبنى من تحديثات العمق."""

    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}

    def update(self, is_bid: bool, price: float, size: float) -> None:
        """تحديث مستوى واحد. size = 0 يعني إلغاء المستوى."""
        side = self.bids if is_bid else self.asks
        if size <= 0:
            side.pop(price, None)
        else:
            side[price] = size

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()

    @property
    def best_bid(self) -> Tuple[float, float]:
        if not self.bids:
            return 0.0, 0.0
        price = max(self.bids)
        return price, self.bids[price]

    @property
    def best_ask(self) -> Tuple[float, float]:
        if not self.asks:
            return 0.0, 0.0
        price = min(self.asks)
        return price, self.asks[price]

    def depth(self, is_bid: bool, levels: int = 10) -> float:
        side = self.bids if is_bid else self.asks
        prices = sorted(side, reverse=is_bid)[:levels]
        return sum(side[p] for p in prices)

    def imbalance(self, levels: int = 10) -> float:
        bid_depth = self.depth(True, levels)
        ask_depth = self.depth(False, levels)
        total = bid_depth + ask_depth
        return (bid_depth - ask_depth) / total if total > 0 else 0.0

    def largest_level(self, is_bid: bool, levels: int = 10) -> Optional[Tuple[float, float]]:
        """أضخم مستوى سيولة — «الجدار» الذي يظهر أحمر/أصفر في الخريطة الحرارية."""
        side = self.bids if is_bid else self.asks
        prices = sorted(side, reverse=is_bid)[:levels]
        if not prices:
            return None
        price = max(prices, key=lambda p: side[p])
        return price, side[price]
