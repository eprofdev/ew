"""
data/
طبقة مزوّدي البيانات: مصدر واحد للأسعار يستخدمه الباكتيست والتدريب
والتشغيل اليومي وحلّ نتائج الصفقات — فتبديل المزوّد متغيّر بيئة واحد،
لا تعديلًا بخمسة ملفات.

    DATA_PROVIDER=twelvedata   واجهة رسمية بمفتاح، موثوقة ومعدّلة للتجزئة
    DATA_PROVIDER=yahoo        مجاني بلا مفتاح، لكن بلا ضمان (افتراضي)
    DATA_PROVIDER=csv          ملفات محلية — بلا شبكة ولا استهلاك رصيد

تحذير مهم عند التبديل: أعد تدريب النموذج بعد تغيير المزوّد.
نموذج تعلّم على أسعار Yahoo غير المعدّلة للتوزيعات ثم شُغِّل على أسعار
Twelve Data المعدّلة يقرأ خصائص بمقياس مختلف قليلاً عمّا تدرّب عليه —
الأرقام قريبة بما يكفي لتبدو سليمة، وبعيدة بما يكفي لتفسد القرارات.
"""
import os

from data.base import DataProvider, REQUIRED_COLUMNS, period_to_days

_CACHED_PROVIDER = None
_CACHED_NAME = None


def get_provider(name: str = None) -> DataProvider:
    """
    يبني المزوّد المطلوب. يُخزَّن بالذاكرة لأن المزوّدين يحملون حالة مفيدة
    (محدّد المعدّل والكاش بـTwelve Data) تضيع لو أنشأناه بكل طلب.
    """
    global _CACHED_PROVIDER, _CACHED_NAME

    name = (name or os.environ.get("DATA_PROVIDER", "yahoo")).lower().strip()
    if _CACHED_PROVIDER is not None and _CACHED_NAME == name:
        return _CACHED_PROVIDER

    if name in ("yahoo", "yfinance", "yf"):
        from data.yahoo import YahooProvider
        provider = YahooProvider()
    elif name in ("twelvedata", "twelve_data", "td"):
        from data.twelvedata import TwelveDataProvider
        provider = TwelveDataProvider()
    elif name == "csv":
        from data.csv_provider import CsvProvider
        provider = CsvProvider()
    else:
        raise ValueError(f"DATA_PROVIDER غير معروف: {name} — "
                         "الخيارات: yahoo | twelvedata | csv")

    _CACHED_PROVIDER, _CACHED_NAME = provider, name
    return provider


def fetch_data(symbol: str, period: str = "2y", interval: str = "1day",
               provider: str = None):
    """
    نقطة الدخول الموحّدة لجلب الأسعار. ترجع DataFrame بأعمدة
    Open/High/Low/Close/Volume مفهرسًا بالتاريخ تصاعديًا.
    """
    return get_provider(provider).fetch(symbol, period=period, interval=interval)


def reset_provider_cache():
    """يُستخدم بالاختبارات وبعد تغيير متغيرات البيئة داخل نفس العملية."""
    global _CACHED_PROVIDER, _CACHED_NAME
    _CACHED_PROVIDER = _CACHED_NAME = None


__all__ = ["DataProvider", "REQUIRED_COLUMNS", "period_to_days",
           "get_provider", "fetch_data", "reset_provider_cache"]
