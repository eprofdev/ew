"""هيكل إضافة Bookmap (Python L1 API) — يربط الدفتر الحي بالبوت.

التشغيل داخل Bookmap:
  1) ثبّت إضافة Python API من داخل Bookmap (تتطلب بايثون 3.7).
  2) ضع هذا الملف كإضافة وشغّلها على الرمز المطلوب.
  3) كل تحديث عمق أو تنفيذ يمر إلى `BookmapFeed`، والبوت يقرأ منه.

هذا الملف وحده هو ما يعتمد على حزمة `bookmap`؛ بقية الحزمة لا تعتمد عليها،
فتبقى الاختبارات والتشغيل ممكنين بدون تثبيتها.
"""

from __future__ import annotations

from typing import Optional

from ..config import BotConfig
from ..engine import EnhancedTradingBot
from .bookmap import BookmapFeed, OrderFlowConfig, OrderFlowGuard

TICKER = "VSME"

feed = BookmapFeed(TICKER, OrderFlowConfig())
guard = OrderFlowGuard()
bot = EnhancedTradingBot(BotConfig())


def handle_depth(addr, is_bid: bool, price_level: int, size: int, pips: float = 0.01) -> None:
    """Bookmap تمرر المستوى كعدد صحيح من الـ pips — نحوّله إلى سعر."""
    feed.on_depth(is_bid=is_bid, price=price_level * pips, size=float(size))


def handle_trade(addr, price: float, size: int, is_buy_aggressor: bool) -> None:
    feed.on_trade(price=price, size=float(size), is_buy_aggressor=is_buy_aggressor)


def current_order_flow() -> dict:
    """ملخص جاهز للعرض أو للدمج مع قرار البوت."""
    snap = feed.snapshot()
    vetoes, confirmations = guard.evaluate(snap)
    return {
        "bid": snap.bid,
        "ask": snap.ask,
        "imbalance": round(snap.imbalance, 3),
        "absorption": snap.absorption_price,
        "icebergs": snap.iceberg_prices,
        "ask_wall": snap.ask_wall,
        "vetoes": vetoes,
        "confirmations": confirmations,
    }


def book_source_for_provider():
    """مرّرها إلى TwelveDataProvider(book_source=...) لتفعيل التسعير الحقيقي."""
    return feed.as_book_source()


def main(subscribe=None) -> Optional[BookmapFeed]:  # pragma: no cover - يُشغَّل داخل Bookmap
    """نقطة الدخول داخل Bookmap. `subscribe` تأتي من واجهة الإضافة."""
    try:
        import bookmap as bm  # type: ignore
    except ImportError:
        print("حزمة bookmap غير مثبتة — هذا الملف يُشغَّل داخل Bookmap فقط")
        return None

    addon = bm.create_addon()
    bm.add_depth_handler(addon, handle_depth)
    bm.add_trades_handler(addon, handle_trade)
    bm.start_addon(addon)
    bm.wait_until_addon_is_turned_off(addon)
    return feed
