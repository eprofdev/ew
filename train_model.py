"""
train_model.py
تدريب نموذج التعلّم وتقييمه تقييمًا زمنيًا أمينًا، ثم حفظه للتشغيل اليومي.

الاستخدام:
    # تدريب على استراتيجية واحدة عبر عدة أسهم
    python train_model.py --symbols AAPL,MSFT,NVDA,AMD,META --period 5y

    # تدريب مجمّع على كل الاستراتيجيات (بيانات أكثر، تخصيص أقل)
    python train_model.py --symbols AAPL,MSFT,NVDA --strategy all --period 5y

    # تحديث ترتيب الاستراتيجيات (أي واحدة تشتغل أحسن) من نفس البيانات
    python train_model.py --symbols AAPL,MSFT,NVDA --strategy all --update-bandit

اقرأ التقرير الناتج قبل ما تفعّل الفلترة. السطر الحاسم فيه:
"❌ النموذج لا يتفوق على التخمين الثابت" — لو ظهر، لا تفعّل ENABLE_LEARNING
للفلترة بعد؛ شغّل البوت بعتبة 0 حتى تتجمع صفقات أكثر.

لماذا عدة أسهم إلزامي عمليًا؟
استراتيجية على سهم واحد بخمس سنوات تعطي 10-30 صفقة. نموذج بـ22 خاصية على
20 صفقة يحفظ ولا يتعلّم. 5-15 سهمًا يعطيك مئات العيّنات، وهذا الحد الأدنى
المعقول لأي استنتاج.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from indicators import add_all_indicators
from strategy import TrendFollowStrategy
from faisal_strategies import BaseTrackStrategy, LiquiditySweepStrategy
from run_backtest import fetch_data

from learning.dataset import build_samples, sort_by_date, samples_to_arrays
from learning.model import OnlineLogisticModel
from learning.walk_forward import walk_forward_eval, format_eval_report, recommend_threshold
from learning.bandit import StrategySelector

STRATEGY_MAP = {
    "trend_follow": lambda: TrendFollowStrategy(rr_ratio=2.0, atr_mult=1.5),
    "base_track": lambda: BaseTrackStrategy(ma_mode="strict"),
    "base_track_loose": lambda: BaseTrackStrategy(ma_mode="loose"),
    "liquidity_sweep": lambda: LiquiditySweepStrategy(),
}


def collect_samples(symbols: list, strategies: list, period: str,
                    verbose: bool = True) -> list:
    """يجمع عيّنات التدريب من كل رمز × كل استراتيجية مطلوبة."""
    all_samples = []
    for symbol in symbols:
        symbol = symbol.strip().upper()
        if not symbol:
            continue
        try:
            df = fetch_data(symbol, period=period)
        except Exception as e:
            print(f"  ⚠️  {symbol}: فشل تحميل البيانات ({e})")
            continue
        if df is None or df.empty:
            print(f"  ⚠️  {symbol}: ما فيه بيانات")
            continue

        df = add_all_indicators(df)
        for strat_name in strategies:
            try:
                signals = STRATEGY_MAP[strat_name]().generate_signals(df)
                samples = build_samples(df, signals, symbol, strategy=strat_name)
            except Exception as e:
                print(f"  ⚠️  {symbol}/{strat_name}: خطأ بتوليد الإشارات ({e})")
                continue
            all_samples.extend(samples)
            if verbose:
                wins = sum(s.label for s in samples)
                print(f"  {symbol:<7} {strat_name:<18} {len(samples):>4} صفقة"
                      + (f"  (فوز {wins}/{len(samples)})" if samples else ""))
    return all_samples


def main():
    parser = argparse.ArgumentParser(
        description="تدريب وتقييم نموذج التعلّم على بيانات تاريخية")
    parser.add_argument("--symbols", required=True,
                        help="رموز مفصولة بفاصلة، مثال: AAPL,MSFT,NVDA")
    parser.add_argument("--period", default="5y", help="مدة البيانات (3y, 5y, 10y...)")
    parser.add_argument("--strategy", default="trend_follow",
                        help=f"{' | '.join(STRATEGY_MAP)} | all")
    parser.add_argument("--out", default="models/model.json", help="مسار حفظ النموذج")
    parser.add_argument("--min-train", type=int, default=30,
                        help="عدد الصفقات الأولى للتدريب فقط قبل بدء التقييم")
    parser.add_argument("--min-samples", type=int, default=40,
                        help="أقل عدد صفقات قبل ما يُسمح للنموذج بالتأثير على القرارات")
    parser.add_argument("--epochs", type=int, default=12,
                        help="عدد تمريرات التدريب النهائي")
    parser.add_argument("--update-bandit", action="store_true",
                        help="حدّث ترتيب الاستراتيجيات من نفس البيانات")
    parser.add_argument("--bandit-out", default="models/bandit.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="قيّم فقط بدون حفظ أي ملف")
    args = parser.parse_args()

    strategies = list(STRATEGY_MAP) if args.strategy == "all" else [args.strategy]
    for s in strategies:
        if s not in STRATEGY_MAP:
            parser.error(f"استراتيجية غير معروفة: {s} — الخيارات: {list(STRATEGY_MAP)}")

    symbols = [s for s in args.symbols.split(",") if s.strip()]
    print(f"\nجمع البيانات: {len(symbols)} رمز × {len(strategies)} استراتيجية "
          f"× {args.period}\n")

    samples = collect_samples(symbols, strategies, args.period)
    if not samples:
        print("\n❌ ما تجمّعت أي عيّنة. جرّب فترة أطول أو رموزًا أكثر.")
        return 1

    samples = sort_by_date(samples)
    wins = sum(s.label for s in samples)
    print(f"\nالمجموع: {len(samples)} صفقة مكتملة، منها {wins} رابحة "
          f"({wins / len(samples) * 100:.1f}%)")
    print(f"المدى الزمني: {samples[0].date.date()} → {samples[-1].date.date()}")

    if len(samples) < 60:
        print("\n⚠️  تحذير: أقل من 60 صفقة. أي استنتاج من هذا العدد ضعيف إحصائيًا —"
              "\n    زِد عدد الرموز أو طوّل الفترة قبل ما تعتمد على النتيجة.")

    # التقييم الأمين: تنبّأ ثم تعلّم، بترتيب زمني
    evaluation = walk_forward_eval(
        samples, min_train=args.min_train,
        model_kwargs={"min_samples": args.min_samples},
    )
    print(format_eval_report(evaluation,
                             title=f"تقييم walk-forward — {args.strategy}"))

    rec = recommend_threshold(evaluation)

    if args.update_bandit and not args.dry_run:
        selector = StrategySelector.load_or_new(args.bandit_out, strategies)
        for s in samples:
            selector.update(s.strategy, s.r_multiple)
        selector.save(args.bandit_out)
        print(selector.format_report())
        print(f"تم حفظ ترتيب الاستراتيجيات: {args.bandit_out}\n")

    if args.dry_run:
        print("(--dry-run: ما حُفظ أي ملف)\n")
        return 0

    # التدريب النهائي على كل البيانات — هذا النموذج للتشغيل المستقبلي فقط،
    # وأرقام تقييمه هي أرقام walk-forward أعلاه لا أرقامه على بيانات تدريبه
    X, y, _ = samples_to_arrays(samples)
    model = OnlineLogisticModel(min_samples=args.min_samples)
    model.fit(X, y, epochs=args.epochs)
    model.save(args.out)

    print(f"✅ حُفظ النموذج: {args.out}  ({model.n_samples} صفقة)")
    print("\nأثقل الخصائص وزنًا (ما تعلّمه النموذج فعليًا):")
    for name, w in model.weights_report()[:8]:
        direction = "يرفع احتمال النجاح" if w > 0 else "يخفضه"
        print(f"   {name:<20} {w:+.4f}   {direction}")

    print("\n" + "─" * 62)
    print("الخطوة التالية — فعّل التعلّم بالتشغيل اليومي:")
    print(f"   export ENABLE_LEARNING=true")
    print(f"   export LEARNING_THRESHOLD={rec['threshold']}")
    print(f"   export LEARNING_MODEL_PATH={args.out}")
    if rec["threshold"] == 0.0:
        print("\n   ملاحظة: العتبة 0 تعني لا فلترة بعد — حجم Kelly يبقى شغّالاً،")
        print("   والبوت يواصل جمع الدروس. أعد التدريب بعد ما تتجمع صفقات أكثر.")
    print("─" * 62 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
