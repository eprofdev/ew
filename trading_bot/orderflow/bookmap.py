"""جسر Bookmap: يحوّل ما تراه عيناك في الخريطة الحرارية إلى قرارات قابلة للبرمجة.

لماذا Bookmap تحديداً في هذا البوت؟ لأن صمام نايف الأهم — «ممنوع الشراء من
العرض» — يحتاج دفتر أسعار حقيقياً، و Twelve Data لا تعطي Bid/Ask في REST
إطلاقاً. Bookmap تعطي L1/L2 لحظياً، فتسدّ الفجوة الوحيدة المتبقية.

ثلاث ظواهر نقرأها من الدفتر وتقابل ما يُرى بالعين في الخريطة الحرارية:

  • الامتصاص (Absorption): بيع ضخم يضرب مستوى الطلب والسعر لا ينكسر.
    في الخريطة: شريط أحمر/أصفر ثابت يبتلع التنفيذات. هذا **تأكيد الدعم**
    الذي تبحث عنه استراتيجية فيصل، لكن بدليل من الدفتر لا من الشمعة.

  • الجليد (Iceberg): مستوى يُستهلك ثم يعاد ملؤه مراراً — مشترٍ كبير يخفي حجمه.

  • الجدار (Wall): سيولة ضخمة على العرض فوق السعر. الشراء تحته مباشرة شراء
    في وجه بائع أكبر منك.

التشغيل داخل Bookmap: واجهة Python الرسمية تدعم L1 API (بايثون 3.7)، وتمرّر
تحديثات العمق والتنفيذات عبر callbacks. اربطها بـ `BookmapFeed.on_depth`
و`on_trade` — انظر `bookmap_addon.py`. ولا تعتمد هذه الوحدة على حزمة
`bookmap` إطلاقاً، فتعمل مع أي مصدر L2 (وسيط، WebSocket، أو إعادة تشغيل).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .book import OrderBook, OrderFlowSnapshot


@dataclass
class OrderFlowConfig:
    depth_levels: int = 10
    absorption_volume_multiple: float = 3.0   # حجم منفَّذ عند مستوى ÷ حجمه المعروض
    absorption_max_move_pct: float = 0.004    # ومع ذلك لم يتحرك السعر أكثر من هذا
    iceberg_refills: int = 3                  # عدد مرات إعادة الملء لاعتباره جليداً
    wall_multiple: float = 4.0                # الجدار = مستوى أكبر من متوسط المستويات
    min_bid_size: float = 500.0               # طلب أرق من هذا = سيولة وهمية
    stale_seconds: float = 5.0                # بيانات أقدم من هذا تُعدّ منقطعة
    tape_window: int = 500                    # عدد التنفيذات المحتفظ بها


class BookmapFeed:
    """يستقبل تحديثات العمق والتنفيذات ويستخرج منها إشارات تدفق الأوامر."""

    def __init__(self, ticker: str, config: Optional[OrderFlowConfig] = None) -> None:
        self.ticker = ticker
        self.config = config or OrderFlowConfig()
        self.book = OrderBook()
        self.tape: Deque[Tuple[float, float, float, bool]] = deque(maxlen=self.config.tape_window)
        self._traded_at_level: Dict[float, float] = defaultdict(float)
        self._level_refills: Dict[float, int] = defaultdict(int)
        self._last_size: Dict[float, float] = {}
        self._last_update: float = 0.0
        self._price_at_level_start: Dict[float, float] = {}

    # ── مداخل البيانات (تُستدعى من Bookmap أو أي مصدر L2) ────────────
    def on_depth(self, is_bid: bool, price: float, size: float, ts: Optional[float] = None) -> None:
        now = ts if ts is not None else time.time()
        self._last_update = now

        if is_bid:
            previous = self._last_size.get(price)
            # المستوى استُهلك ثم عاد بحجم مماثل ⇒ إعادة ملء (جليد محتمل)
            if previous is not None and previous <= 0 < size:
                self._level_refills[price] += 1
            self._last_size[price] = size

        self.book.update(is_bid, price, size)

    def on_trade(self, price: float, size: float, is_buy_aggressor: bool,
                 ts: Optional[float] = None) -> None:
        now = ts if ts is not None else time.time()
        self._last_update = now
        self.tape.append((now, price, size, is_buy_aggressor))
        self._traded_at_level[price] += size
        self._price_at_level_start.setdefault(price, price)

    def reset_level_stats(self) -> None:
        self._traded_at_level.clear()
        self._level_refills.clear()

    # ── القراءة ──────────────────────────────────────────────────────
    @property
    def is_stale(self) -> bool:
        return (time.time() - self._last_update) > self.config.stale_seconds if self._last_update else True

    def best_bid_ask(self) -> Tuple[float, float]:
        return self.book.best_bid[0], self.book.best_ask[0]

    def as_book_source(self) -> Callable[[str], Tuple[float, float]]:
        """دالة جاهزة للتمرير إلى `TwelveDataProvider(book_source=...)`."""

        def source(ticker: str) -> Tuple[float, float]:
            if ticker.upper() != self.ticker.upper():
                raise ValueError(f"هذا التدفق يخص {self.ticker} وليس {ticker}")
            if self.is_stale:
                raise RuntimeError("تدفق الدفتر منقطع — لا تسعّر على بيانات قديمة")
            return self.best_bid_ask()

        return source

    def snapshot(self) -> OrderFlowSnapshot:
        cfg = self.config
        bid, bid_size = self.book.best_bid
        ask, ask_size = self.book.best_ask
        snap = OrderFlowSnapshot(
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            depth_bid=self.book.depth(True, cfg.depth_levels),
            depth_ask=self.book.depth(False, cfg.depth_levels),
            imbalance=self.book.imbalance(cfg.depth_levels),
        )

        if self.tape:
            buys = sum(size for _, _, size, aggressor in self.tape if aggressor)
            total = sum(size for _, _, size, _ in self.tape)
            snap.aggressor_ratio = buys / total if total else 0.5

        snap.absorption_price = self._detect_absorption()
        snap.iceberg_prices = [
            price for price, count in self._level_refills.items() if count >= cfg.iceberg_refills
        ]
        snap.ask_wall = self._detect_ask_wall()

        if self.is_stale:
            snap.notes.append("التدفق منقطع أو قديم")
        return snap

    def _detect_absorption(self) -> Optional[float]:
        """مستوى طلب ابتلع بيعاً أضعاف حجمه المعروض دون أن ينكسر السعر."""
        cfg = self.config
        bid, bid_size = self.book.best_bid
        if bid <= 0 or bid_size <= 0:
            return None
        traded = self._traded_at_level.get(bid, 0.0)
        if traded < bid_size * cfg.absorption_volume_multiple:
            return None
        recent = [p for _, p, _, _ in list(self.tape)[-50:]]
        if not recent:
            return None
        move = (max(recent) - min(recent)) / bid if bid else 0.0
        return bid if move <= cfg.absorption_max_move_pct else None

    def _detect_ask_wall(self) -> Optional[float]:
        cfg = self.config
        largest = self.book.largest_level(False, cfg.depth_levels)
        if largest is None:
            return None
        price, size = largest
        levels = sorted(self.book.asks)[: cfg.depth_levels]
        # المقارنة ببقية المستويات لا بمتوسط يتضمن الجدار نفسه (وإلا رفع عتبته)
        others = [self.book.asks[p] for p in levels if p != price]
        if not others:
            return None
        average = sum(others) / len(others)
        return price if average > 0 and size >= average * cfg.wall_multiple else None


class OrderFlowGuard:
    """صمامات أمان إضافية مصدرها الدفتر — تكملة لفلاتر نايف على الشموع."""

    def __init__(self, config: Optional[OrderFlowConfig] = None) -> None:
        self.config = config or OrderFlowConfig()

    def evaluate(self, snap: OrderFlowSnapshot) -> Tuple[List[str], List[str]]:
        """يرجع (أسباب الرفض، إشارات التأكيد)."""
        cfg = self.config
        vetoes: List[str] = []
        confirmations: List[str] = []

        if snap.bid <= 0 or snap.ask <= 0:
            vetoes.append("BOOK_EMPTY: لا يوجد دفتر أسعار صالح")
            return vetoes, confirmations
        if "التدفق منقطع أو قديم" in snap.notes:
            vetoes.append("BOOK_STALE: بيانات الدفتر قديمة")
        if snap.bid_size < cfg.min_bid_size:
            vetoes.append(
                f"THIN_BID: حجم الطلب {snap.bid_size:,.0f} رقيق — سيولة وهمية عند الخروج"
            )
        if snap.ask_wall is not None and snap.ask_wall > snap.ask:
            vetoes.append(
                f"ASK_WALL: جدار عرض عند {snap.ask_wall:.4f} — شراء في وجه بائع أكبر"
            )
        if snap.imbalance < -0.5:
            vetoes.append(f"BOOK_PRESSURE: اختلال الدفتر {snap.imbalance:+.0%} لصالح البائعين")

        if snap.absorption_price is not None:
            confirmations.append(
                f"امتصاص عند {snap.absorption_price:.4f} — الدعم يبتلع البيع دون أن ينكسر"
            )
        if snap.iceberg_prices:
            confirmations.append(
                "مشترٍ مخفي (Iceberg) عند: "
                + ", ".join(f"{p:.4f}" for p in sorted(snap.iceberg_prices))
            )
        if snap.imbalance > 0.35:
            confirmations.append(f"اختلال الدفتر {snap.imbalance:+.0%} لصالح المشترين")
        if snap.aggressor_ratio > 0.6:
            confirmations.append(f"{snap.aggressor_ratio:.0%} من التنفيذات ضربت العرض")
        return vetoes, confirmations
