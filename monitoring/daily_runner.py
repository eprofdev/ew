"""
daily_runner.py
نقطة التشغيل اليومية (تُجدوَل عبر cron). دورة كاملة كل يوم:

  0. حلقة التعلّم أولاً (قبل أي قرار جديد):
     أ. يحلّ نتائج الإشارات القديمة من أسعار السوق الفعلية
     ب. يدرّب النموذج على الدروس الجديدة ويحفظه
     ج. يحدّث ترتيب الاستراتيجيات (أيها يشتغل أحسن حاليًا)
  1. لكل رمز: يجيب البيانات، يحسب المؤشرات، يولّد إشارة
  2. يمرّر الإشارة على فلتر التعلّم (قبول/رفض + حجم Kelly)
  3. ينفذها عبر الوسيط (Alpaca أو Webull) حسب الإعداد
  4. يسجّل كل شيء: الصفقة، الخصائص، القرار، والأخطاء
  5. يبني التقرير ويرسله بتيليجرام

ترتيب الخطوة 0 مقصود: نتعلّم من الأمس قبل ما نقرر اليوم، لا بعده.

كل خطوة ملفوفة بـtry/except خاص فيها — خطأ في سهم واحد ما يوقف البقية،
وكل خطأ يُسجَّل بدل ما يختفي بصمت.

جدولته (مثال - كل يوم الساعة 8 مساءً بعد إغلاق السوق):
    crontab -e
    0 20 * * 1-5  cd /path/to/trading_bot && python monitoring/daily_runner.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))                    # للوصول لـ indicators/strategy/learning
sys.path.insert(0, str(Path(__file__).parent))   # للوصول لملفات المراقبة

import trade_logger as tl
from report_generator import (build_report, format_report_text,
                              export_dashboard_json, generate_equity_chart)
import telegram_notifier as notifier

from indicators import add_all_indicators
from strategy import TrendFollowStrategy
from faisal_strategies import BaseTrackStrategy, LiquiditySweepStrategy
from run_backtest import fetch_data
from brokers import get_executor

from learning import store as learn_store
from learning.dataset import attach_features
from learning.features import date_to_index, extract_features
from learning.filter import SignalFilter
from learning.model import OnlineLogisticModel
from learning.bandit import StrategySelector
from learning.report import learning_summary, format_learning_text

# ------------------------------------------------------------------ الإعداد
WATCHLIST = os.environ.get("WATCHLIST", "AAPL,MSFT,NVDA").split(",")
RISK_PCT = float(os.environ.get("RISK_PCT", "0.01"))
EXECUTION_MODE = os.environ.get("EXECUTION_MODE", "SIGNAL_ONLY")  # SIGNAL_ONLY | PAPER | LIVE
BROKER = os.environ.get("BROKER", "alpaca")                        # alpaca | webull

# سقف مخاطرة على مستوى المحفظة كاملة — لا يعتمد بس على RISK_PCT لكل صفقة منفردة.
# بدونه: لو عدة أسهم بقائمة المتابعة أعطت إشارة بنفس اليوم، كل صفقة تُحسب حجمها
# من نفس رأس المال الكلي بشكل مستقل، فالمخاطرة الفعلية المجمّعة ممكن تتضاعف
# بدون أي حد أعلى. هذا سقف بسيط: عدد صفقات مفتوحة أقصى بنفس الوقت.
MAX_CONCURRENT_POSITIONS = int(os.environ.get("MAX_CONCURRENT_POSITIONS", "3"))

# ------------------------------------------------------------ إعداد التعلّم
ENABLE_LEARNING = os.environ.get("ENABLE_LEARNING", "true").lower() == "true"
MODEL_PATH = os.environ.get("LEARNING_MODEL_PATH", str(ROOT / "models" / "model.json"))
BANDIT_PATH = os.environ.get("LEARNING_BANDIT_PATH", str(ROOT / "models" / "bandit.json"))
# 0.0 = لا فلترة (لكن حجم Kelly يبقى شغّالاً). ارفعها فقط بناءً على
# جدول العتبات اللي يطبعه train_model.py، لا بالحدس.
LEARNING_THRESHOLD = float(os.environ.get("LEARNING_THRESHOLD", "0.0"))
LEARNING_MIN_SAMPLES = int(os.environ.get("LEARNING_MIN_SAMPLES", "40"))
KELLY_FRACTION = float(os.environ.get("KELLY_FRACTION", "0.25"))
MAX_HOLD_DAYS = int(os.environ.get("MAX_HOLD_DAYS", "60"))

STRATEGY_MAP = {
    "trend_follow": lambda: TrendFollowStrategy(rr_ratio=2.0, atr_mult=1.5),
    "base_track": lambda: BaseTrackStrategy(),
    "liquidity_sweep": lambda: LiquiditySweepStrategy(),
}
# لا تفعّل استراتيجيات فيصل هنا تلقائيًا إلا بعد ما تثبت نتائجها بالباكتيست —
# الافتراضي يبقى الاستراتيجية المُختبرة أصلًا، والتبديل صريح عبر متغير بيئة.
# "auto" يسلّم الاختيار لـThompson Sampling حسب الأداء الفعلي المتراكم.
STRATEGY_NAME = os.environ.get("STRATEGY_NAME", "trend_follow")


def get_strategy() -> tuple:
    """يرجع (اسم الاستراتيجية، كائنها). يدعم الاختيار التلقائي بـ auto."""
    if STRATEGY_NAME == "auto":
        selector = StrategySelector.load_or_new(BANDIT_PATH, list(STRATEGY_MAP))
        name = selector.select()
        print(f"اختيار تلقائي للاستراتيجية: {name}")
        return name, STRATEGY_MAP[name]()
    if STRATEGY_NAME not in STRATEGY_MAP:
        raise ValueError(f"STRATEGY_NAME غير معروفة: {STRATEGY_NAME} — "
                         f"الخيارات: {list(STRATEGY_MAP.keys())} أو auto")
    return STRATEGY_NAME, STRATEGY_MAP[STRATEGY_NAME]()


# ---------------------------------------------------------- حلقة التعلّم
def run_learning_cycle() -> dict:
    """
    يحلّ نتائج الإشارات السابقة ويتعلّم منها. يُستدعى قبل قرارات اليوم.

    يشتغل حتى بوضع SIGNAL_ONLY بدون أي حساب وسيط: النتيجة تُستنتج من
    أسعار السوق الفعلية (أي المستويين وصل السعر له أولاً، الوقف أو الهدف).
    يعني البوت يتعلّم من اليوم الأول حتى قبل ما تودع ريالًا واحدًا.
    """
    outcome = {"resolved": 0, "trained": 0, "model_samples": 0}
    if not ENABLE_LEARNING:
        return outcome

    learn_store.init_db()

    try:
        outcome["resolved"] = learn_store.resolve_pending_with_prices(
            lambda sym: fetch_data(sym, period="1y"),   # الأسعار الخام تكفي هنا
            max_hold_days=MAX_HOLD_DAYS,
        )
    except Exception as e:
        tl.log_error("daily_runner.resolve_pending", e)

    try:
        new_rows = learn_store.untrained_samples()
        if new_rows:
            model = OnlineLogisticModel.load_or_new(
                MODEL_PATH, min_samples=LEARNING_MIN_SAMPLES)
            selector = StrategySelector.load_or_new(BANDIT_PATH, list(STRATEGY_MAP))
            for row in new_rows:
                x, label = learn_store.sample_row_to_training_pair(row)
                model.partial_fit(x, label)
                if row.get("r_multiple") is not None:
                    selector.update(row["strategy"], row["r_multiple"])
            model.save(MODEL_PATH)
            selector.save(BANDIT_PATH)
            learn_store.mark_trained([r["id"] for r in new_rows])
            outcome["trained"] = len(new_rows)
            outcome["model_samples"] = model.n_samples
            print(f"🧠 تعلّم من {len(new_rows)} صفقة جديدة "
                  f"(إجمالي {model.n_samples}).")
    except Exception as e:
        tl.log_error("daily_runner.train_from_resolved", e)

    return outcome


def build_filter() -> SignalFilter:
    """يبني فلتر التعلّم. عند أي عطل يرجع فلترًا ممرِّرًا لا مانعًا."""
    if not ENABLE_LEARNING:
        return SignalFilter(model=None, base_risk_pct=RISK_PCT)
    return SignalFilter.from_path(
        MODEL_PATH, threshold=LEARNING_THRESHOLD,
        kelly_frac=KELLY_FRACTION, base_risk_pct=RISK_PCT,
    )


# ------------------------------------------------------------ معالجة رمز
def process_symbol(symbol: str, strategy, strategy_name: str,
                   executor, signal_filter: SignalFilter):
    try:
        df = fetch_data(symbol, period="1y")
        if df.empty:
            raise ValueError(f"ما فيه بيانات لـ {symbol}")
    except Exception as e:
        tl.log_error(f"daily_runner.fetch_data[{symbol}]", e, symbol=symbol)
        return

    try:
        df = add_all_indicators(df)
        signals = strategy.generate_signals(df)
    except Exception as e:
        tl.log_error(f"daily_runner.generate_signals[{symbol}]", e, symbol=symbol)
        return

    # وسيط بلا Bracket (Webull): نتأكد كل تشغيلة أن كل مركز مفتوح محمي بوقف
    if executor is not None and not executor.supports_bracket:
        try:
            if executor.has_open_position(symbol):
                open_buy = next((s for s in reversed(signals)
                                 if s.side == "BUY" and s.stop_loss), None)
                if open_buy and hasattr(executor, "ensure_protective_stop"):
                    executor.ensure_protective_stop(symbol, float(open_buy.stop_loss))
        except Exception as e:
            tl.log_error(f"daily_runner.ensure_protective_stop[{symbol}]", e, symbol=symbol)

    if not signals:
        return

    latest_signal = signals[-1]
    # نتصرف فقط إذا الإشارة الأخيرة صدرت اليوم أو آخر يوم تداول بالبيانات
    if latest_signal.date != df.index[-1]:
        return

    # ---- قرار التعلّم (على إشارات الشراء فقط؛ الخروج ما يُفلتر أبدًا:
    # رفض خروج يعني إبقاء صفقة خاسرة مفتوحة، وهذا عكس الغرض تمامًا)
    decision = None
    if latest_signal.side == "BUY":
        try:
            attach_features(df, [latest_signal])
            if latest_signal.features is None:  # تاريخ ما انطابق مع الفهرس
                idx = date_to_index(df, latest_signal.date)
                if idx is not None:
                    latest_signal.features = extract_features(
                        df, idx, latest_signal.stop_loss, latest_signal.take_profit)
            decision = signal_filter.evaluate(latest_signal.features)
        except Exception as e:
            tl.log_error(f"daily_runner.learning_decision[{symbol}]", e, symbol=symbol)
            decision = None

        # نسجّل الإشارة كدرس — مقبولة كانت أو مرفوضة. تسجيل المرفوضة هو
        # اللي يسمح لنا لاحقًا نحكم على الفلتر نفسه بدل ما نثق فيه عمياني.
        try:
            if ENABLE_LEARNING and latest_signal.features:
                learn_store.record_signal(
                    symbol=symbol, strategy=strategy_name,
                    entry_date=latest_signal.date, entry_price=float(latest_signal.price),
                    features=latest_signal.features,
                    stop_loss=float(latest_signal.stop_loss) if latest_signal.stop_loss else None,
                    take_profit=float(latest_signal.take_profit) if latest_signal.take_profit else None,
                    probability=decision.probability if decision else None,
                    taken=bool(decision.take) if decision else True,
                    decision_reason=decision.reason if decision else "",
                    mode=EXECUTION_MODE,
                )
        except Exception as e:
            tl.log_error(f"daily_runner.record_signal[{symbol}]", e, symbol=symbol)

        if decision is not None and not decision.take:
            print(f"[{symbol}] 🧠 رفضها فلتر التعلّم: {decision.reason}")
            return

    # ---- التنفيذ
    effective_risk = decision.risk_pct if (decision and decision.take) else RISK_PCT
    try:
        if executor is not None:
            if latest_signal.side == "BUY":
                open_count = executor.open_position_count()
                if open_count >= MAX_CONCURRENT_POSITIONS:
                    print(f"[{symbol}] تجاهل: وصلنا سقف الصفقات المتزامنة "
                          f"({open_count}/{MAX_CONCURRENT_POSITIONS}).")
                    return
            executor.execute_signal(symbol, latest_signal, risk_pct=effective_risk)
        tl.log_trade(
            symbol=symbol, side=latest_signal.side, price=float(latest_signal.price),
            stop_loss=float(latest_signal.stop_loss) if latest_signal.stop_loss else None,
            take_profit=float(latest_signal.take_profit) if latest_signal.take_profit else None,
            reason=(latest_signal.reason +
                    (f" | 🧠 {decision.probability:.0%} × حجم {decision.size_mult:.0%}"
                     if decision and decision.probability is not None else "")),
            mode=EXECUTION_MODE,
        )
    except Exception as e:
        tl.log_error(f"daily_runner.execute_signal[{symbol}]", e, symbol=symbol)


def update_equity_snapshot(executor):
    try:
        if executor is not None:
            info = executor.get_account_info()
            tl.log_snapshot(info["equity"], info["cash"],
                            open_positions=executor.open_position_count(), mode=EXECUTION_MODE)
    except Exception as e:
        tl.log_error("daily_runner.update_equity_snapshot", e)


def send_daily_report(learning_outcome: dict):
    try:
        report = build_report(hours=24)
        text = format_report_text(report)
        export_dashboard_json(report)
        chart_path = generate_equity_chart()

        if ENABLE_LEARNING:
            try:
                summary = learning_summary(MODEL_PATH, bandit_path=BANDIT_PATH)
                summary["cycle"] = learning_outcome
                text += "\n" + format_learning_text(summary)
            except Exception as e:
                tl.log_error("daily_runner.learning_summary", e)

        print(text)  # يطبع بالـ log المحلي دائمًا حتى لو تيليجرام فشل

        if os.environ.get("ENABLE_CLAUDE_REVIEW", "").lower() == "true":
            from claude_review import analyze
            analysis = analyze(report)
            print("\n🤖 تحليل Claude:\n" + analysis)
            text = text + "\n\n🤖 تحليل Claude:\n" + analysis

        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            notifier.send_photo(str(chart_path), caption=text)
    except Exception as e:
        # حتى إرسال التقرير نفسه لو فشل، يُسجَّل بدل ما يوقف كل شيء بصمت
        tl.log_error("daily_runner.send_daily_report", e)
        print(f"تحذير: فشل إرسال/بناء التقرير: {e}")


def main():
    tl.init_db()

    # (0) نتعلّم من نتائج الأمس قبل ما نقرر شيئًا اليوم
    learning_outcome = run_learning_cycle()

    strategy_name, strategy = get_strategy()
    signal_filter = build_filter()
    if signal_filter.active:
        print(f"🧠 فلتر التعلّم فعّال — عتبة {LEARNING_THRESHOLD:.0%}، "
              f"ربع Kelly ({KELLY_FRACTION}).")

    executor = None
    try:
        executor = get_executor(EXECUTION_MODE, BROKER)
        if executor is not None:
            print(f"الوسيط: {executor.name} — {executor.describe_risk()}")
    except Exception as e:
        tl.log_error("daily_runner.get_executor", e)
        print(f"تحذير: فشل تهيئة الوسيط ({e}) — نكمل بوضع الإشارات فقط.")

    for symbol in WATCHLIST:
        symbol = symbol.strip()
        if symbol:
            process_symbol(symbol, strategy, strategy_name, executor, signal_filter)

    update_equity_snapshot(executor)
    send_daily_report(learning_outcome)


if __name__ == "__main__":
    main()
