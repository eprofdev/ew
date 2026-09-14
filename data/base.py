"""
data/base.py
العقد الموحّد لأي مزوّد بيانات.

كل مزوّد لازم يرجع نفس الشكل بالضبط، وإلا انكسرت المؤشرات والاستراتيجيات
بصمت: DataFrame بأعمدة Open/High/Low/Close/Volume، مفهرس بالتاريخ تصاعديًا،
وأسعار معدّلة للتجزئة والتوزيعات.

ليش التعديل (adjustment) شرط لا خيار؟
سهم انقسم 4:1 يظهر بسعر خام كأنه هبط 75% بيوم واحد. بدون تعديل، الاستراتيجية
تشوف انهيارًا وهميًا فتولّد إشارة كاذبة، والنموذج يتعلّم من نتيجتها درسًا
خاطئًا يثبّته بوزنه للأبد.
"""
from abc import ABC, abstractmethod

import pandas as pd

REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


class DataProvider(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self, symbol: str, period: str = "2y",
              interval: str = "1day") -> pd.DataFrame:
        """
        period: "1y" / "2y" / "5y" / "10y" / "max"
        interval: "1day" (المدعوم فعليًا بالاستراتيجيات الحالية)
        """

    @staticmethod
    def normalize(df: pd.DataFrame) -> pd.DataFrame:
        """
        يفرض الشكل الموحّد: الأعمدة الخمسة، أرقام عشرية، فهرس تاريخي تصاعدي
        بلا تكرار ولا منطقة زمنية.

        المنطقة الزمنية تُنزَع عمدًا: المزوّدون يرجّعونها بصيغ مختلفة، ومقارنة
        فهرس بمنطقة زمنية مع آخر بدونها ترمي استثناءً بمنتصف تشغيلة ليلية.
        """
        if df is None or len(df) == 0:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)

        df = df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"بيانات ناقصة الأعمدة: {missing}")

        df = df[REQUIRED_COLUMNS].astype(float)
        df.index = pd.to_datetime(df.index)
        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_localize(None)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df.dropna(subset=["Close"])


def period_to_days(period: str) -> int:
    """يحوّل مدة نصية إلى عدد أيام تقويمية. 'max' = 30 سنة (أطول من أي سهم عمليًا)."""
    period = str(period).strip().lower()
    if period in ("max", "all"):
        return 365 * 30
    units = {"d": 1, "w": 7, "mo": 30, "m": 30, "y": 365}
    for suffix in ("mo", "d", "w", "m", "y"):   # "mo" أولاً وإلا التقطها "m"
        if period.endswith(suffix):
            try:
                return int(float(period[: -len(suffix)]) * units[suffix])
            except ValueError:
                break
    raise ValueError(f"مدة غير مفهومة: {period} — استخدم مثل 1y / 6mo / 90d / max")
