"""
daily_runner.py
نقطة التشغيل اليومية (تُجدوَل عبر cron). لكل رمز سهم بمتابعته:
  1. يجيب البيانات ويحسب المؤشرات
  2. يولّد إشارة من الاستراتيجية
  3. ينفذها عبر Alpaca (Paper أو Live حسب الإعداد)
  4. يسجل كل شيء بقاعدة البيانات (صفقة/خطأ)
كل خطوة ملفوفة بـ try/except خاص فيها — خطأ في سهم واحد ما يوقف البقية،
وكل خطأ يُسجَّل بدل ما يختفي بصمت.
في النهاية: يبني التقرير، يحفظ نسخة للوحة الويب، ويرسله عبر تيليجرام.

جدولته (مثال - كل يوم الساعة 8 مساءً بعد إغلاق السوق):
    crontab -e
    0 20 * * 1-5  cd /path/to/trading_bot && python monitoring/daily_runner.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # للوصول لـ indicators/strategy
sys.path.insert(0, str(Path(__file__).parent))          # للوصول لملفات المراقبة

import trade_logger as tl
from report_generator import build_report, format_report_text, export_dashboard_json, generate_equity_chart
import telegram_notifier as notifier

from indicators import add_all_indicators
from strategy import TrendFollowStrategy
from faisal_strategies import BaseTrackStrategy, LiquiditySweepStrategy
from run_backtest import fetch_data

# عدّل هذي القائمة بالأسهم اللي تبي تتابعها
WATCHLIST = os.environ.get("WATCHLIST", "AAPL,MSFT,NVDA").split(",")
RISK_PCT = float(os.environ.get("RISK_PCT", "0.01"))
EXECUTION_MODE = os.environ.get("EXECUTION_MODE", "SIGNAL_ONLY")  # SIGNAL_ONLY | PAPER | LIVE

# سقف مخاطرة على مستوى المحفظة كاملة — لا يعتمد بس على RISK_PCT لكل صفقة منفردة.
# بدونه: لو عدة أسهم بقائمة المتابعة أعطت إشارة بنفس اليوم، كل صفقة تُحسب حجمها
# من نفس رأس المال الكلي بشكل مستقل، فالمخاطرة الفعلية المجمّعة ممكن تتضاعف
# بدون أي حد أعلى. هذا سقف بسيط: عدد صفقات مفتوحة أقصى بنفس الوقت.
MAX_CONCURRENT_POSITIONS = int(os.environ.get("MAX_CONCURRENT_POSITIONS", "3"))

STRATEGY_MAP = {
    "trend_follow": lambda: TrendFollowStrategy(rr_ratio=2.0, atr_mult=1.5),
    "base_track": lambda: BaseTrackStrategy(),
    "liquidity_sweep": lambda: LiquiditySweepStrategy(),
}
# لا تفعّل استراتيجيات فيصل هنا تلقائيًا إلا بعد ما تثبت نتائجها بالباكتيست —
# الافتراضي يبقى الاستراتيجية المُختبرة أصلًا، والتبديل صريح عبر متغير بيئة.
STRATEGY_NAME = os.environ.get("STRATEGY_NAME", "trend_follow")


def get_strategy():
    if STRATEGY_NAME not in STRATEGY_MAP:
        raise ValueError(f"STRATEGY_NAME غير معروفة: {STRATEGY_NAME} — "
                          f"الخيارات: {list(STRATEGY_MAP.keys())}")
    return STRATEGY_MAP[STRATEGY_NAME]()


def get_executor():
    """يرجع AlpacaExecutor فقط إذا وضع التنفيذ مفعّل، وإلا None (وضع الإشارات فقط)."""
    if EXECUTION_MODE == "SIGNAL_ONLY":
        return None
    from alpaca_executor import AlpacaExecutor
    return AlpacaExecutor(paper=(EXECUTION_MODE == "PAPER"))


def process_symbol(symbol: str, strategy, executor):
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

    if not signals:
        return

    latest_signal = signals[-1]
    # نتصرف فقط إذا الإشارة الأخيرة صدرت اليوم أو آخر يوم تداول بالبيانات
    if latest_signal.date != df.index[-1]:
        return

    mode = EXECUTION_MODE
    try:
        if executor is not None:
            if latest_signal.side == "BUY":
                open_count = executor.open_position_count()
                if open_count >= MAX_CONCURRENT_POSITIONS:
                    print(f"[{symbol}] تجاهل: وصلنا سقف الصفقات المتزامنة "
                          f"({open_count}/{MAX_CONCURRENT_POSITIONS}).")
                    return
            executor.execute_signal(symbol, latest_signal, risk_pct=RISK_PCT)
        tl.log_trade(
            symbol=symbol, side=latest_signal.side, price=float(latest_signal.price),
            stop_loss=float(latest_signal.stop_loss) if latest_signal.stop_loss else None,
            take_profit=float(latest_signal.take_profit) if latest_signal.take_profit else None,
            reason=latest_signal.reason, mode=mode,
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


def send_daily_report():
    try:
        report = build_report(hours=24)
        text = format_report_text(report)
        export_dashboard_json(report)
        chart_path = generate_equity_chart()

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
    strategy = get_strategy()

    executor = None
    try:
        executor = get_executor()
    except Exception as e:
        tl.log_error("daily_runner.get_executor", e)

    for symbol in WATCHLIST:
        process_symbol(symbol.strip(), strategy, executor)

    update_equity_snapshot(executor)
    send_daily_report()


if __name__ == "__main__":
    main()
