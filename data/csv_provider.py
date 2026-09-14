"""
data/csv_provider.py
مزوّد يقرأ من ملفات CSV محلية — بلا شبكة ولا مفاتيح ولا استهلاك رصيد.

فايدته العملية:
  • باكتيست وتدريب متكرر على نفس البيانات بلا حرق رصيد Twelve Data اليومي
  • نتائج قابلة لإعادة الإنتاج بالضبط — نفس الملفات = نفس الأرقام دائمًا
  • تشغيل على سيرفر بلا وصول للإنترنت

الاستخدام:
    export DATA_PROVIDER=csv
    export DATA_CSV_DIR=/path/to/csvs

كل رمز بملف باسمه: AAPL.csv ويحوي عمود تاريخ + Open/High/Low/Close/Volume.
تنزّل الملفات مرة واحدة بأي مزوّد:
    python -m data.download AAPL,MSFT,NVDA --period 10y --out ./csv_data
"""
import os
from pathlib import Path

import pandas as pd

from data.base import DataProvider, period_to_days

# أسماء أعمدة التاريخ الشائعة بين المصادر المختلفة
DATE_COLUMNS = ("Date", "date", "datetime", "Datetime", "timestamp", "time")


class CsvProvider(DataProvider):
    name = "csv"

    def __init__(self, directory: str = None):
        self.directory = Path(directory or os.environ.get("DATA_CSV_DIR", "./csv_data"))

    def fetch(self, symbol: str, period: str = "2y",
              interval: str = "1day") -> pd.DataFrame:
        path = self.directory / f"{symbol.strip().upper()}.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"ما لقيت ملف بيانات {symbol}: {path}\n"
                f"نزّله أولاً: python -m data.download {symbol} --out {self.directory}"
            )

        df = pd.read_csv(path)
        date_col = next((c for c in DATE_COLUMNS if c in df.columns), None)
        if date_col is None:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
        else:
            df[date_col] = pd.to_datetime(df[date_col])
            df = df.set_index(date_col)

        df = self.normalize(df)
        if df.empty:
            return df

        # نقصّ حسب المدة المطلوبة انطلاقًا من آخر تاريخ بالملف لا من تاريخ
        # اليوم — عشان ملف محفوظ من شهرين يعطي نفس النتيجة مهما تأخّر تشغيله
        cutoff = df.index[-1] - pd.Timedelta(days=period_to_days(period))
        return df[df.index >= cutoff]
