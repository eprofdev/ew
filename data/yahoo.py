"""
data/yahoo.py
مزوّد Yahoo Finance عبر مكتبة yfinance — المصدر الأصلي للمشروع.

مجاني وبلا مفتاح، لكن بلا ضمان خدمة: Yahoo تغيّر واجهتها بلا إشعار، وبعض
الشبكات (شركات، بروكسيات، مزوّدو VPS) تحجب نطاقاتها فتفشل كل الطلبات بـSSL
أو "possibly delisted" وهي أسهم قائمة. لو صار هذا معك، بدّل إلى:
    export DATA_PROVIDER=twelvedata
"""
import pandas as pd

from data.base import DataProvider

# yfinance تتكلم بمفردات مختلفة عن Twelve Data — نترجم لها
INTERVAL_ALIASES = {
    "1day": "1d", "1d": "1d", "d": "1d",
    "1week": "1wk", "1wk": "1wk",
    "1month": "1mo", "1mo": "1mo",
    "1h": "1h", "60min": "1h",
    "15min": "15m", "5min": "5m", "1min": "1m",
}


class YahooProvider(DataProvider):
    name = "yahoo"

    def fetch(self, symbol: str, period: str = "2y",
              interval: str = "1day") -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError as e:
            raise RuntimeError("مكتبة yfinance غير مثبّتة: pip install yfinance") from e

        yf_interval = INTERVAL_ALIASES.get(str(interval).lower(), str(interval))
        df = yf.download(symbol, period=period, interval=yf_interval,
                         auto_adjust=True, progress=False)
        return self.normalize(df)
