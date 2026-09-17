"""مزود بيانات Twelve Data فعلي (REST) — بدون أي تبعيات خارجية.

يغطي بروتوكول `MarketDataProvider`:
    get_candles  →  /time_series   (متاح في كل الخطط)
    get_realtime →  /quote         (متاح في كل الخطط)
    get_metrics  →  /statistics    (يتطلب خطة Pro فأعلى)

ثلاث حقائق يجب معرفتها قبل الاعتماد عليه في التنفيذ:

1) **لا يوجد Bid/Ask في REST.** نقطة /quote تعطي آخر صفقة فقط. ولأن صمام
   نايف يمنع الشراء من العرض، لا يستطيع البوت تسعير أمر دون دفتر أسعار.
   الحل: مرّر `book_source` (دالة تعيد (bid, ask) من وسيطك أو من WebSocket).
   بدونه يبقى القرار WATCH ولا يصدر أمر — وهذا هو السلوك الآمن المقصود.

2) **الهيكل المالي (كاب/فلوت/شورت) يتطلب خطة Pro.** على الخطط الأدنى ترجع
   /statistics رسالة ترقية. عندها تُعلَّم البيانات كـ«غير معروفة» ويرفض صمام
   الأمان الصفقة (fail-closed) بدل التخمين. البديل دون ترقية: ملف
   `fundamentals_override` تضع فيه الكاب والفلوت يدوياً.

3) **/time_series يرجع الأحدث أولاً**، وهذا المزود يعكسه تصاعدياً لأن كل
   المؤشرات تفترض الترتيب الزمني الصاعد.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..models import Candle, RealTimeData, StockMetrics

BASE_URL = "https://api.twelvedata.com"

# دالة اختيارية تُرجع (bid, ask) لرمز معين من مصدر دفتر أسعار حقيقي
BookSource = Callable[[str], Tuple[float, float]]


class TwelveDataError(RuntimeError):
    """خطأ عام من الواجهة."""


class RateLimitError(TwelveDataError):
    """تجاوز حد الطلبات (429) أو نفاد الرصيد."""


class PlanLimitError(TwelveDataError):
    """النقطة غير متاحة في خطة الاشتراك الحالية."""


class SymbolNotFoundError(TwelveDataError):
    """الرمز غير موجود لدى المزود."""


def _parse_datetime(value: str) -> int:
    """'2026-09-17 09:30:00' أو '2026-09-17' → epoch seconds (UTC)."""
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise TwelveDataError(f"صيغة تاريخ غير معروفة: {value!r}")


def _to_float(value, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class _RateLimiter:
    """حد طلبات بسيط (الخطة المجانية = 8 طلبات/دقيقة)."""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max(int(max_per_minute), 0)
        self._calls: deque = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if not self.max_per_minute:
            return
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= 60:
                self._calls.popleft()
            if len(self._calls) >= self.max_per_minute:
                sleep_for = 60 - (now - self._calls[0]) + 0.05
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


class ShortInterestStore:
    """سجل محلي لنسب الشورت — يمنحنا المرجع التاريخي لرصد التقفيل.

    Twelve Data تعطي نسبة الشورت الحالية فقط، بلا قيمة الشهر السابق. نخزّن كل
    قراءة في ملف JSON، فتصبح «النسبة التاريخية» هي أعلى قراءة خلال النافذة.
    """

    def __init__(self, path: Optional[Path] = None, window_days: int = 90) -> None:
        self.path = Path(path or Path.home() / ".trading_bot" / "short_interest.json")
        self.window_days = window_days
        self._data: Dict[str, List[dict]] = {}
        self._load()

    def _load(self) -> None:
        try:
            self._data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False), "utf-8")
        except OSError:
            pass  # سجل الشورت مساعد — لا يوقف التحليل عند فشل الكتابة

    def record(self, ticker: str, short_ratio_of_float: float, ts: Optional[float] = None) -> None:
        if short_ratio_of_float <= 0:
            return
        now = ts if ts is not None else time.time()
        rows = self._data.setdefault(ticker.upper(), [])
        rows.append({"ts": now, "ratio": short_ratio_of_float})
        cutoff = now - self.window_days * 86_400
        self._data[ticker.upper()] = [r for r in rows if r.get("ts", 0) >= cutoff][-500:]
        self._save()

    def historical(self, ticker: str) -> float:
        """أعلى نسبة شورت مسجلة خلال النافذة (0 إذا لا سجل)."""
        rows = self._data.get(ticker.upper(), [])
        return max((float(r.get("ratio", 0)) for r in rows), default=0.0)


class TwelveDataProvider:
    """مزود بيانات حقيقي يحقق بروتوكول `MarketDataProvider`."""

    INTERVALS = {"4h": "4h", "1day": "1day", "1h": "1h", "1week": "1week"}

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
        max_requests_per_minute: int = 8,
        max_retries: int = 4,
        quote_ttl: float = 30.0,
        candles_ttl: float = 60.0,
        statistics_ttl: float = 12 * 3600.0,
        book_source: Optional[BookSource] = None,
        short_store: Optional[ShortInterestStore] = None,
        fundamentals_override: Optional[Dict[str, dict]] = None,
        prepost: bool = False,
    ) -> None:
        self.api_key = api_key or os.environ.get("TWELVE_DATA_API_KEY", "")
        if not self.api_key:
            raise TwelveDataError(
                "مفتاح Twelve Data مفقود — مرّر api_key أو اضبط TWELVE_DATA_API_KEY"
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.quote_ttl = quote_ttl
        self.candles_ttl = candles_ttl
        self.statistics_ttl = statistics_ttl
        self.book_source = book_source
        self.short_store = short_store if short_store is not None else ShortInterestStore()
        self.fundamentals_override = {
            k.upper(): v for k, v in (fundamentals_override or {}).items()
        }
        self.prepost = prepost

        self._limiter = _RateLimiter(max_requests_per_minute)
        self._cache: Dict[str, Tuple[float, dict]] = {}
        self.warnings: List[str] = []

    # ── طبقة الشبكة ──────────────────────────────────────────────────
    def _request(self, path: str, params: Dict[str, object]) -> dict:
        query = dict(params)
        query["apikey"] = self.api_key
        url = f"{self.base_url}/{path}?" + urllib.parse.urlencode(query)

        delay = 2.0
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._limiter.acquire()
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "trading-bot/1.0"})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                return self._check_payload(payload, path)
            except (RateLimitError, urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                    raise TwelveDataError(f"{path}: HTTP {exc.code}") from exc
                if attempt >= self.max_retries:
                    break
                time.sleep(delay)
                delay *= 2
            except ValueError as exc:  # JSON غير صالح
                raise TwelveDataError(f"{path}: استجابة غير صالحة") from exc

        raise TwelveDataError(f"فشل الاتصال بـ {path}: {last_error}")

    @staticmethod
    def _check_payload(payload: dict, path: str) -> dict:
        """Twelve Data ترجع أخطاءها داخل HTTP 200 أحياناً."""
        if not isinstance(payload, dict):
            return payload
        code = int(_to_float(payload.get("code"), 0.0) or 0)
        if payload.get("status") == "error" or code >= 400:
            message = str(payload.get("message", "خطأ غير معروف"))
            if code == 429 or "run out of API credits" in message:
                raise RateLimitError(message)
            if code == 403 or "available exclusively with" in message:
                raise PlanLimitError(message)
            if code == 404 or "not found" in message.lower():
                raise SymbolNotFoundError(message)
            raise TwelveDataError(f"{path}: {message}")
        return payload

    def _cached(self, key: str, ttl: float, loader: Callable[[], dict]) -> dict:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = loader()
        self._cache[key] = (now, value)
        return value

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # ── بروتوكول MarketDataProvider ──────────────────────────────────
    def get_candles(self, ticker: str, interval: str, limit: int) -> List[Candle]:
        """شموع OHLCV مرتبة تصاعدياً (الأقدم أولاً)."""
        td_interval = self.INTERVALS.get(interval, interval)
        payload = self._cached(
            f"ts:{ticker}:{td_interval}:{limit}",
            self.candles_ttl,
            lambda: self._request(
                "time_series",
                {
                    "symbol": ticker,
                    "interval": td_interval,
                    "outputsize": max(int(limit), 1),
                    "prepost": "true" if self.prepost else "false",
                    "order": "ASC",  # نطلبها تصاعدياً، ونتحقق منها بعد ذلك
                },
            ),
        )

        rows = payload.get("values") or []
        candles: List[Candle] = []
        for row in rows:
            close = _to_float(row.get("close"))
            open_ = _to_float(row.get("open"))
            high = _to_float(row.get("high"))
            low = _to_float(row.get("low"))
            if None in (open_, high, low, close):
                continue  # شمعة ناقصة (عطلة/توقف) — تجاهلها
            candles.append(
                Candle(
                    ts=_parse_datetime(str(row.get("datetime", ""))),
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=_to_float(row.get("volume"), 0.0) or 0.0,
                )
            )

        # ضمان الترتيب التصاعدي مهما كان ترتيب المزود (الافتراضي عنده تنازلي)
        candles.sort(key=lambda c: c.ts)
        return candles

    def get_quote(self, ticker: str) -> dict:
        return self._cached(
            f"q:{ticker}",
            self.quote_ttl,
            lambda: self._request("quote", {"symbol": ticker, "prepost": "true" if self.prepost else "false"}),
        )

    def get_statistics(self, ticker: str) -> Optional[dict]:
        """/statistics — يرجع None إذا كانت الخطة لا تدعمها."""
        try:
            payload = self._cached(
                f"st:{ticker}",
                self.statistics_ttl,
                lambda: self._request("statistics", {"symbol": ticker}),
            )
        except PlanLimitError:
            self._warn(
                "الهيكل المالي (كاب/فلوت/شورت) يتطلب خطة Twelve Data Pro فأعلى — "
                "استخدم fundamentals_override أو رقِّ الخطة"
            )
            return None
        return payload.get("statistics") or {}

    def get_metrics(self, ticker: str) -> StockMetrics:
        quote = self.get_quote(ticker)
        stats = self.get_statistics(ticker)
        override = self.fundamentals_override.get(ticker.upper(), {})

        market_cap: Optional[float] = None
        free_float: Optional[float] = None
        shares_outstanding = 0.0
        avg_volume = _to_float(quote.get("average_volume"), 0.0) or 0.0

        if stats:
            valuations = stats.get("valuations_metrics") or {}
            stock = stats.get("stock_statistics") or {}
            market_cap = _to_float(valuations.get("market_capitalization"))
            free_float = _to_float(stock.get("float_shares"))
            shares_outstanding = _to_float(stock.get("shares_outstanding"), 0.0) or 0.0
            avg_volume = avg_volume or (_to_float(stock.get("avg_10_volume"), 0.0) or 0.0)

        # القيم اليدوية تتقدم دائماً (بيانات الفلوت الرسمية أدق من أي تقدير)
        market_cap = _to_float(override.get("market_cap"), market_cap)
        free_float = _to_float(override.get("free_float"), free_float)
        shares_outstanding = _to_float(override.get("shares_outstanding"), shares_outstanding) or 0.0

        if market_cap is None or free_float is None:
            self._warn(f"{ticker}: الهيكل المالي غير معروف — سيرفضه صمام الأمان")

        return StockMetrics(
            ticker=ticker,
            market_cap=market_cap,
            free_float=free_float,
            shares_outstanding=shares_outstanding,
            avg_daily_volume=avg_volume,
            is_otc=str(quote.get("exchange", "")).upper() in {"OTC", "OTCMKTS", "PINK"},
            has_options=bool(override.get("has_options", False)),
            has_active_shelf_offering=bool(override.get("has_active_shelf_offering", False)),
            recent_reverse_split_days=self._reverse_split_age(ticker, stats, override),
        )

    def get_realtime(self, ticker: str) -> RealTimeData:
        quote = self.get_quote(ticker)
        stats = self.get_statistics(ticker)

        price = _to_float(quote.get("close"), 0.0) or 0.0
        previous_close = _to_float(quote.get("previous_close"), 0.0) or 0.0
        session_volume = _to_float(quote.get("volume"), 0.0) or 0.0

        bid = ask = 0.0
        if self.book_source is not None:
            try:
                bid, ask = self.book_source(ticker)
            except Exception as exc:  # مصدر خارجي — لا يُسقط التحليل
                self._warn(f"{ticker}: تعذر جلب دفتر الأسعار ({exc})")
        else:
            self._warn(
                "لا يوجد Bid/Ask في REST — مرّر book_source لتفعيل التسعير، "
                "وإلا يبقى القرار WATCH (ممنوع الشراء من العرض)"
            )

        current_short = 0.0
        historical_short = 0.0
        if stats:
            stock = stats.get("stock_statistics") or {}
            shares_short = _to_float(stock.get("shares_short"), 0.0) or 0.0
            float_shares = _to_float(stock.get("float_shares"), 0.0) or 0.0
            if shares_short > 0 and float_shares > 0:
                current_short = shares_short / float_shares
                self.short_store.record(ticker, current_short)
                historical_short = max(self.short_store.historical(ticker), current_short)

        # تغيّر ما قبل الافتتاح: يُحتسب فقط والسوق مغلق (وإلا فهو تغير الجلسة)
        premarket = 0.0
        if not quote.get("is_market_open") and previous_close > 0 and price > 0:
            premarket = (price - previous_close) / previous_close

        return RealTimeData(
            ticker=ticker,
            price=price,
            bid=bid,
            ask=ask,
            historical_short_interest=historical_short,
            current_short_interest=current_short,
            borrow_fee=0.0,  # غير متاح لدى Twelve Data — يُملأ من وسيطك عند توفره
            session_volume=session_volume,
            premarket_change_pct=premarket,
            halted=False,     # لا تعطي الواجهة حالة الإيقاف
        )

    # ── مساعدات ──────────────────────────────────────────────────────
    def _reverse_split_age(
        self, ticker: str, stats: Optional[dict], override: dict
    ) -> Optional[int]:
        """عدد الأيام منذ آخر تجزئة عكسية (Reverse Split)، أو None."""
        if "recent_reverse_split_days" in override:
            value = override["recent_reverse_split_days"]
            return int(value) if value is not None else None
        if not stats:
            return None

        splits = stats.get("dividends_and_splits") or {}
        factor = str(splits.get("last_split_factor") or "").strip()
        date_str = str(splits.get("last_split_date") or "").strip()
        if not factor or not date_str:
            return None
        try:
            from_factor, to_factor = (float(p) for p in factor.replace("-", ":").split(":")[:2])
        except ValueError:
            return None
        # تجزئة عكسية: تحصل على أسهم أقل مما تملك (1:10)
        if from_factor >= to_factor:
            return None
        try:
            age = (time.time() - _parse_datetime(date_str)) / 86_400
        except TwelveDataError:
            return None
        return max(int(age), 0)
