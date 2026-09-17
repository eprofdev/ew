"""واجهة مزوّد البيانات: افصل مصدر البيانات عن منطق الاستراتيجية.

نفّذ `MarketDataProvider` بمزوّدك (Twelve Data, Polygon, IBKR ...) ثم مرّره
إلى `build_snapshot`. يبقى منطق الاستراتيجية والفلاتر كما هو دون تعديل.
"""

from __future__ import annotations

from typing import List, Protocol, Sequence

from .models import Candle, MarketSnapshot, RealTimeData, StockMetrics


class MarketDataProvider(Protocol):
    """العقد المطلوب من أي مزوّد بيانات."""

    def get_candles(self, ticker: str, interval: str, limit: int) -> Sequence[Candle]:
        """interval: '4h' أو '1day'."""

    def get_metrics(self, ticker: str) -> StockMetrics: ...

    def get_realtime(self, ticker: str) -> RealTimeData: ...


def build_snapshot(
    provider: MarketDataProvider,
    ticker: str,
    candles_4h: int = 120,
    candles_1d: int = 120,
    promoted_by_groups: bool = False,
    paid_indicator_signal: bool = False,
) -> MarketSnapshot:
    """يجمع كل ما يحتاجه المحرك لسهم واحد في لقطة واحدة."""
    return MarketSnapshot(
        ticker=ticker,
        metrics=provider.get_metrics(ticker),
        realtime=provider.get_realtime(ticker),
        candles_4h=list(provider.get_candles(ticker, "4h", candles_4h)),
        candles_1d=list(provider.get_candles(ticker, "1day", candles_1d)),
        promoted_by_groups=promoted_by_groups,
        paid_indicator_signal=paid_indicator_signal,
    )


def build_snapshots(
    provider: MarketDataProvider, tickers: Sequence[str], **kwargs
) -> List[MarketSnapshot]:
    """لقطات لقائمة مراقبة كاملة — تجاهل الرموز التي يفشل جلبها."""
    out: List[MarketSnapshot] = []
    for ticker in tickers:
        try:
            out.append(build_snapshot(provider, ticker, **kwargs))
        except Exception as exc:  # مزوّد البيانات قد يفشل لرمز واحد
            print(f"[data] تخطي {ticker}: {exc}")
    return out
