"""مثال تشغيلي كامل ببيانات تركيبية — شغّله بـ:  python -m trading_bot.demo

يعرض أربع حالات:
  1) إعداد مثالي يمر من كل الفلاتر         → BUY_CONFIRMED
  2) شركة ضخمة الهيكل                      → VETO (STRUCTURE)
  3) سهم مروَّج في جروبات + قفزة ما قبل الافتتاح → VETO (فخ تصريف)
  4) كسر الدعم بفوليوم مضاعف               → WAIT / WATCH
"""

from __future__ import annotations

from dataclasses import replace
from typing import List, Sequence, Tuple

from .config import BotConfig
from .engine import EnhancedTradingBot
from .models import Candle, MarketSnapshot, RealTimeData, StockMetrics

FOUR_HOURS = 14_400
ONE_DAY = 86_400

# سلسلة 4 ساعات: تذبذب فوق دعم 1.00، ثم شموع ساقطة بفوليوم متناقص، ثم انعكاس
_OHLCV_4H: List[Tuple[float, float, float, float, float]] = [
    *[
        row
        for _ in range(3)
        for row in (
            (1.000, 1.080, 0.990, 1.060, 900_000),
            (1.060, 1.180, 1.050, 1.160, 800_000),
            (1.160, 1.200, 1.100, 1.120, 700_000),
            (1.120, 1.130, 1.010, 1.030, 650_000),
            (1.030, 1.050, 0.995, 1.010, 600_000),
        )
    ],
    (1.010, 1.120, 1.000, 1.100, 900_000),
    (1.100, 1.220, 1.090, 1.200, 950_000),
    (1.200, 1.210, 1.140, 1.150, 700_000),   # سقوط 1
    (1.150, 1.160, 1.080, 1.090, 520_000),   # سقوط 2
    (1.090, 1.100, 1.030, 1.040, 380_000),   # سقوط 3
    (1.040, 1.050, 1.005, 1.015, 300_000),   # سقوط 4 — استنزاف
    (1.015, 1.075, 0.985, 1.065, 800_000),   # انعكاس عند الدعم
]

# إغلاقات يومية: صعود ثم تبريد طويل — RSI يومي ≈ 45 (نطاق الارتكاز)
_DAILY_CLOSES: List[float] = [
    0.62, 0.68, 0.66, 0.74, 0.80, 0.78, 0.88, 0.96, 0.93, 1.05, 1.15, 1.12,
    1.26, 1.38, 1.34, 1.45, 1.426, 1.403, 1.379, 1.391, 1.368, 1.344, 1.321,
    1.332, 1.309, 1.285, 1.262, 1.274, 1.250, 1.226, 1.203, 1.215, 1.191, 1.065,
]


def candles_4h() -> List[Candle]:
    return [
        Candle(i * FOUR_HOURS, o, h, l, c, v)
        for i, (o, h, l, c, v) in enumerate(_OHLCV_4H)
    ]


def candles_1d(closes_: Sequence[float] = _DAILY_CLOSES) -> List[Candle]:
    out: List[Candle] = []
    prev = closes_[0]
    for i, close in enumerate(closes_):
        out.append(
            Candle(
                ts=i * ONE_DAY,
                open=prev,
                high=max(prev, close) * 1.02,
                low=min(prev, close) * 0.98,
                close=close,
                volume=2_000_000,
            )
        )
        prev = close
    return out


def base_snapshot(ticker: str = "VSME") -> MarketSnapshot:
    return MarketSnapshot(
        ticker=ticker,
        metrics=StockMetrics(
            ticker=ticker,
            market_cap=1_450_000,
            free_float=980_000,
            shares_outstanding=1_900_000,
            avg_daily_volume=1_200_000,
            is_otc=False,
            has_options=False,
        ),
        realtime=RealTimeData(
            ticker=ticker,
            price=1.065,
            bid=1.060,
            ask=1.075,
            bid_size=5_000,
            ask_size=4_200,
            historical_short_interest=0.92,
            current_short_interest=0.41,   # تقفيل شورت واضح
            borrow_fee=0.55,
            session_volume=1_500_000,
            premarket_change_pct=0.04,
        ),
        candles_4h=candles_4h(),
        candles_1d=candles_1d(),
    )


def build_cases() -> List[MarketSnapshot]:
    good = base_snapshot("VSME")

    # 2) هيكل ضخم — يُرفض بفلتر نايف للهيكل المالي
    big = base_snapshot("BIGC")
    big.metrics = replace(good.metrics, ticker="BIGC", market_cap=95_000_000, free_float=40_000_000)

    # 3) فخ تصريف: ترويج جروبات + قفزة ما قبل الافتتاح
    pumped = base_snapshot("PUMP")
    pumped.promoted_by_groups = True
    pumped.realtime = replace(good.realtime, ticker="PUMP", premarket_change_pct=0.85)

    # 4) كسر الدعم بفوليوم مضاعف
    broken = base_snapshot("BRKN")
    tail = list(candles_4h())
    tail[-1] = Candle(tail[-1].ts, 1.015, 1.020, 0.880, 0.890, 6_000_000)
    broken.candles_4h = tail

    return [good, big, pumped, broken]


def main() -> None:
    bot = EnhancedTradingBot(BotConfig(account_equity=10_000))
    print("=" * 64)
    print("البوت المدمج: سلوك فيصل السعري + صمامات نايف")
    print("=" * 64)
    for signal in bot.scan(build_cases()):
        print(signal.to_alert())
        print("-" * 64)
    print("تنبيه: للأغراض البحثية فقط — ليست توصية مالية.")


if __name__ == "__main__":
    main()
