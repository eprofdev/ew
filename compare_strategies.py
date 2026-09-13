"""
compare_strategies.py
يشغّل كل الاستراتيجيات المتاحة على نفس الرمز والفترة، ويطبع مقارنة جنب بعض.

الاستخدام:
    python compare_strategies.py AAPL --capital 1000 --risk 0.01 --period 3y
"""
import argparse
import pandas as pd

from indicators import add_all_indicators
from strategy import TrendFollowStrategy
from faisal_strategies import BaseTrackStrategy, LiquiditySweepStrategy
from backtest import Backtester
from run_backtest import fetch_data


def build_strategies():
    return {
        "TrendFollow (الأصلية)": TrendFollowStrategy(rr_ratio=2.0, atr_mult=1.5),
        "BaseTrack-strict (20/30/50)": BaseTrackStrategy(ma_mode="strict"),
        "BaseTrack-loose (EMA30/50)": BaseTrackStrategy(ma_mode="loose"),
        "LiquiditySweep": LiquiditySweepStrategy(),
    }


def main():
    parser = argparse.ArgumentParser(description="مقارنة كل الاستراتيجيات على نفس البيانات")
    parser.add_argument("symbol")
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--risk", type=float, default=0.01)
    parser.add_argument("--period", default="3y")
    args = parser.parse_args()

    print(f"جاري تحميل بيانات {args.symbol}...")
    df = fetch_data(args.symbol, period=args.period)
    if df.empty:
        print("ما قدرت أجيب بيانات لهذا الرمز.")
        return
    df = add_all_indicators(df)

    rows = []
    for name, strat in build_strategies().items():
        try:
            signals = strat.generate_signals(df)
            results = Backtester(args.capital, args.risk).run(signals)
            rows.append({
                "الاستراتيجية": name,
                "عدد الصفقات": results["num_trades"],
                "نسبة الربح %": results["win_rate_pct"],
                "العائد الإجمالي %": results["total_return_pct"],
                "أقصى تراجع %": results["max_drawdown_pct"],
                "رأس المال النهائي $": results["final_capital"],
            })
        except Exception as e:
            rows.append({
                "الاستراتيجية": name, "عدد الصفقات": "خطأ",
                "نسبة الربح %": "-", "العائد الإجمالي %": "-",
                "أقصى تراجع %": "-", "رأس المال النهائي $": str(e)[:40],
            })

    result_df = pd.DataFrame(rows)
    print(f"\n{'=' * 70}")
    print(f"  مقارنة الاستراتيجيات — {args.symbol} (رأس مال ${args.capital}, مخاطرة {args.risk*100}%)")
    print(f"{'=' * 70}")
    print(result_df.to_string(index=False))
    print(f"{'=' * 70}\n")
    print("تذكير: عدد صفقات قليل (أقل من ~20-30) يعني النتيجة مو موثوقة إحصائيًا بعد —")
    print("جرّب عدة أسهم وفترات مختلفة قبل ما تعتمد على أي رقم هنا.")


if __name__ == "__main__":
    main()
