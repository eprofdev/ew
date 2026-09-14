"""
data/download.py
ينزّل الأسعار مرة واحدة ويحفظها CSV، عشان التدريب والباكتيست يشتغلان
بعدها بلا شبكة وبلا استهلاك رصيد.

    python -m data.download AAPL,MSFT,NVDA --period 10y --out ./csv_data
    python -m data.download AAPL --provider twelvedata --period 5y

بعدها:
    export DATA_PROVIDER=csv
    export DATA_CSV_DIR=./csv_data
    python train_model.py --symbols AAPL,MSFT,NVDA --period 10y --strategy all

مفيد تحديدًا مع الباقة المجانية لـTwelve Data: تنزيل واحد بـ10 طلبات
يكفيك لعشرات جلسات التدريب والتجريب بدل طلب جديد بكل تشغيلة.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data import fetch_data


def main():
    parser = argparse.ArgumentParser(description="تنزيل أسعار وحفظها CSV")
    parser.add_argument("symbols", help="رموز مفصولة بفاصلة: AAPL,MSFT,NVDA")
    parser.add_argument("--period", default="10y")
    parser.add_argument("--interval", default="1day")
    parser.add_argument("--provider", default=None,
                        help="yahoo | twelvedata (الافتراضي: DATA_PROVIDER)")
    parser.add_argument("--out", default="./csv_data")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ok = failed = 0
    for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        try:
            df = fetch_data(symbol, period=args.period, interval=args.interval,
                            provider=args.provider)
        except Exception as e:
            print(f"  ❌ {symbol}: {e}")
            failed += 1
            continue
        if df.empty:
            print(f"  ⚠️  {symbol}: ما رجعت أي بيانات")
            failed += 1
            continue

        path = out_dir / f"{symbol}.csv"
        df.to_csv(path)
        print(f"  ✅ {symbol}: {len(df)} شمعة "
              f"({df.index[0].date()} → {df.index[-1].date()}) → {path}")
        ok += 1

    print(f"\nتم: {ok} نجحت، {failed} فشلت. المجلد: {out_dir.resolve()}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
