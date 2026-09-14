"""
run_backtest.py
نقطة التشغيل: يجيب بيانات تاريخية، يحسب المؤشرات، يولّد الإشارات، ويشغّل الباكتيست.

الاستخدام:
    python run_backtest.py AAPL
    python run_backtest.py TSLA --capital 500 --risk 0.02 --period 2y
"""
import argparse

from indicators import add_all_indicators
from strategy import TrendFollowStrategy
from backtest import Backtester, print_report
from data import fetch_data as _fetch_from_provider


def fetch_data(symbol: str, period: str = "2y", interval: str = "1day"):
    """
    جلب الأسعار عبر المزوّد المختار بـDATA_PROVIDER (yahoo | twelvedata | csv).

    الاسم والتوقيع محفوظان كما كانا: كل ملفات المشروع تستورد fetch_data من
    هنا، فبقيت نقطة الدخول واحدة وتبديل المزوّد صار متغيّر بيئة لا تعديل كود.
    """
    return _fetch_from_provider(symbol, period=period, interval=interval)


def main():
    parser = argparse.ArgumentParser(description="باكتيست استراتيجية تداول")
    parser.add_argument("symbol", help="رمز السهم، مثال: AAPL")
    parser.add_argument("--capital", type=float, default=1000.0, help="رأس المال الابتدائي")
    parser.add_argument("--risk", type=float, default=0.01, help="نسبة المخاطرة لكل صفقة (0.01 = 1%%)")
    parser.add_argument("--period", default="2y", help="مدة البيانات التاريخية (1y, 2y, 5y...)")
    args = parser.parse_args()

    print(f"جاري تحميل بيانات {args.symbol}...")
    df = fetch_data(args.symbol, period=args.period)
    if df.empty:
        print("ما قدرت أجيب بيانات لهذا الرمز. تأكد من الرمز وحاول مرة ثانية.")
        return

    df = add_all_indicators(df)

    strategy = TrendFollowStrategy(rr_ratio=2.0, atr_mult=1.5)
    signals = strategy.generate_signals(df)

    bt = Backtester(initial_capital=args.capital, risk_pct=args.risk)
    results = bt.run(signals)

    print_report(results, args.symbol)


if __name__ == "__main__":
    main()
