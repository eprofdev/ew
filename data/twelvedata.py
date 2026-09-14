"""
data/twelvedata.py
مزوّد بيانات Twelve Data عبر واجهته الرسمية REST.

التوثيق: https://twelvedata.com/docs#time-series
المفتاح: احصل عليه مجانًا من twelvedata.com ثم:
    export TWELVEDATA_API_KEY="..."
    export DATA_PROVIDER=twelvedata

ثلاثة أشياء ضبطناها هنا وتستحق الانتباه:

1. adjust="all"
   الافتراضي بالواجهة هو تعديل التجزئة فقط (splits). نطلب "all" ليشمل
   التوزيعات أيضًا، فتطابق الأرقام أسلوب yfinance بـauto_adjust=True.
   بدونها تختلف نتائج الباكتيست بين المزوّدين على نفس السهم ونفس الفترة،
   ويصعب تفسير الفرق.

2. حدّ المعدّل (Rate limit)
   الباقة المجانية محدودة بعدد طلبات بالدقيقة (8 افتراضيًا هنا) وسقف يومي.
   بدون تنظيم، تدريب على 10 رموز يرتطم بالحد ويرجّع 429 بنص الشغل. المحدِّد
   هنا ينام تلقائيًا بين الطلبات بدل ما يفشل.

3. التخزين المؤقت (Cache)
   البوت اليومي يطلب نفس بيانات نفس الرموز كل تشغيلة، والباكتيست يعيد الطلب
   مرارًا. الكاش يحفظ كل سلسلة على القرص ويعيد استخدامها خلال TTL، فينزل
   استهلاك رصيدك اليومي بشكل حاد. احذفه بـclear_cache() لو بغيت بيانات طازجة.
"""
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import pandas as pd
import requests

from data.base import DataProvider, period_to_days

API_URL = "https://api.twelvedata.com/time_series"
CACHE_DIR = Path(__file__).resolve().parent / ".cache"

# تحويل مفردات المدد بين المزوّدين — الاستراتيجيات تتكلم بلغة "1d"
INTERVAL_ALIASES = {
    "1d": "1day", "1day": "1day", "d": "1day",
    "1wk": "1week", "1week": "1week",
    "1mo": "1month", "1month": "1month",
    "1h": "1h", "60m": "1h",
    "15m": "15min", "15min": "15min",
    "5m": "5min", "5min": "5min",
    "1m": "1min", "1min": "1min",
}


class RateLimiter:
    """
    محدِّد معدّل بسيط بنافذة منزلقة: لا يتجاوز max_calls خلال period_seconds.
    آمن مع الخيوط (Lock) لأن البوت قد يجلب رموزًا بالتوازي مستقبلاً.
    """

    def __init__(self, max_calls: int = 8, period_seconds: float = 60.0):
        self.max_calls = max(1, int(max_calls))
        self.period = float(period_seconds)
        self._calls = []
        self._lock = Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < self.period]
            if len(self._calls) >= self.max_calls:
                sleep_for = self.period - (now - self._calls[0]) + 0.05
                if sleep_for > 0:
                    print(f"   (حد معدّل Twelve Data: انتظار {sleep_for:.0f} ثانية)")
                    time.sleep(sleep_for)
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < self.period]
            self._calls.append(time.monotonic())


class TwelveDataProvider(DataProvider):
    name = "twelvedata"

    def __init__(self, api_key: str = None, rate_limit: int = None,
                 cache_hours: float = None, timeout: int = 30):
        self.api_key = api_key or os.environ.get("TWELVEDATA_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "لازم تحدد TWELVEDATA_API_KEY كمتغير بيئة.\n"
                "احصل على مفتاح مجاني من https://twelvedata.com ثم:\n"
                '    export TWELVEDATA_API_KEY="..."'
            )
        self.timeout = timeout
        self.limiter = RateLimiter(
            int(rate_limit if rate_limit is not None
                else os.environ.get("TWELVEDATA_RATE_LIMIT", "8"))
        )
        self.cache_hours = float(cache_hours if cache_hours is not None
                                 else os.environ.get("TWELVEDATA_CACHE_HOURS", "12"))

    # ------------------------------------------------------------- الكاش
    def _cache_path(self, symbol: str, interval: str, period: str) -> Path:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        safe = symbol.replace("/", "_").replace(":", "_").upper()
        return CACHE_DIR / f"{safe}__{interval}__{period}.csv"

    def _read_cache(self, path: Path):
        if self.cache_hours <= 0 or not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600.0
        if age_hours > self.cache_hours:
            return None
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            return self.normalize(df)
        except Exception:
            return None       # كاش تالف: نتجاهله ونطلب من جديد

    @staticmethod
    def clear_cache():
        """يمسح كل الملفات المخزّنة — استخدمها لو شككت ببيانات قديمة."""
        if CACHE_DIR.exists():
            for f in CACHE_DIR.glob("*.csv"):
                f.unlink()

    # ------------------------------------------------------------- الجلب
    def fetch(self, symbol: str, period: str = "2y",
              interval: str = "1day") -> pd.DataFrame:
        symbol = symbol.strip().upper()
        interval = INTERVAL_ALIASES.get(str(interval).lower(), str(interval))

        cache_file = self._cache_path(symbol, interval, period)
        cached = self._read_cache(cache_file)
        if cached is not None and not cached.empty:
            return cached

        start_date = (datetime.now(timezone.utc)
                      - timedelta(days=period_to_days(period))).strftime("%Y-%m-%d")

        self.limiter.acquire()
        try:
            resp = requests.get(API_URL, timeout=self.timeout, params={
                "symbol": symbol,
                "interval": interval,
                "start_date": start_date,
                "outputsize": 5000,          # السقف الأعلى للواجهة
                "order": "asc",
                "adjust": "all",             # تجزئة + توزيعات — راجع رأس الملف
                "apikey": self.api_key,
                "format": "JSON",
            })
        except requests.RequestException as e:
            raise RuntimeError(f"Twelve Data: فشل الاتصال عند جلب {symbol}: {e}") from e

        payload = self._parse(resp, symbol)
        values = payload.get("values") or []
        if not values:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        df = pd.DataFrame(values)
        df = df.rename(columns={
            "datetime": "Date", "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        })
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
        # بعض الأصول (فوركس مثلاً) ترجع بلا حجم — نصفّرها بدل ما ننهار،
        # ومؤشرات الحجم تتعامل معها كـ"لا تأكيد" لا كخطأ
        if "Volume" not in df.columns:
            df["Volume"] = 0.0

        df = self.normalize(df)
        if self.cache_hours > 0 and not df.empty:
            try:
                df.to_csv(cache_file)
            except OSError:
                pass          # قرص ممتلئ أو صلاحيات: الكاش ترف لا ضرورة
        return df

    def _parse(self, resp, symbol: str) -> dict:
        """يحوّل الرد إلى dict ويترجم أخطاء الواجهة إلى رسائل مفهومة."""
        if resp.status_code == 429:
            raise RuntimeError(
                f"Twelve Data: تجاوزت حد الطلبات عند {symbol}. "
                "قلّل TWELVEDATA_RATE_LIMIT أو ارفع TWELVEDATA_CACHE_HOURS، "
                "أو وزّع التدريب على أكثر من يوم."
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise RuntimeError(
                f"Twelve Data: رد غير قابل للقراءة عند {symbol} "
                f"(HTTP {resp.status_code}): {resp.text[:200]}"
            ) from e

        if isinstance(payload, dict) and payload.get("status") == "error":
            code = payload.get("code")
            message = payload.get("message", "")
            if code == 429:
                raise RuntimeError(f"Twelve Data: تجاوزت حد الطلبات عند {symbol}. {message}")
            if code == 401:
                raise RuntimeError(f"Twelve Data: مفتاح API غير صالح. {message}")
            if code == 404:
                # رمز غير موجود ما يوقف تدريبًا على 20 رمزًا — نرجّع فارغًا
                print(f"   ⚠️  Twelve Data: الرمز {symbol} غير موجود ({message})")
                return {"values": []}
            raise RuntimeError(f"Twelve Data ({code}) عند {symbol}: {message}")

        if resp.status_code >= 400:
            raise RuntimeError(f"Twelve Data: فشل الطلب عند {symbol} "
                               f"(HTTP {resp.status_code})")
        return payload
