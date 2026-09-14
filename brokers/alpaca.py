"""
brokers/alpaca.py
مرحلة التنفيذ: يربط إشارات الاستراتيجية بحساب Alpaca (Paper أو Live)
عبر الـ SDK الرسمية alpaca-py.

الإعداد:
    pip install alpaca-py
    احصل على API Key/Secret من: https://app.alpaca.markets (بعد فتح حساب Paper مجانًا)

    ضع المفاتيح كمتغيرات بيئة (لا تكتبها داخل الكود):
        export ALPACA_API_KEY="..."
        export ALPACA_SECRET_KEY="..."

هذا الملف يبدأ بحساب Paper (تجريبي بدون فلوس حقيقية) دائمًا كافتراضي.
للانتقال لحساب حقيقي غيّر paper=False فقط بعد ما تثق تمامًا بنتائج الباكتيست
وبعد فترة تشغيل تجريبي (Paper) موازية لا تقل عن عدة أسابيع.

مراجعة شاملة (تصحيح مهم): النسخة الأولى كانت تستخدم أوامر Market للشراء —
وهذا يخالف أهم قاعدة متكررة بكامل المصدر: "ممنوع الشراء من العرض/Market
نهائيًا... ضع أوامر Limit داخل منطقة الطلب". صار الشراء الآن Limit عند
سعر الإشارة بالضبط، وليس Market.

تصحيح ثانٍ: النسخة الأولى ما كانت تضع وقف الخسارة/الهدف على مستوى الوسيط،
وكان التعليق يحذّر إنك لازم تراقب يدويًا. المشكلة العملية: daily_runner.py
يشتغل مرة يوميًا عبر cron — لو السعر انهار بمنتصف اليوم، محد يلاحظ إلا
تشغيلة الغد. صار الأمر الآن Bracket Order: وقف الخسارة والهدف يتسجلان
على الوسيط نفسه لحظة الدخول، فينفذان تلقائيًا حتى لو كان daily_runner
نايم بينهم.
"""
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, MarketOrderRequest, TakeProfitRequest, StopLossRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

from strategy import TradeSignal, position_size
from brokers.base import BrokerExecutor


class AlpacaExecutor(BrokerExecutor):
    name = "alpaca"
    supports_paper = True
    supports_bracket = True

    def __init__(self, paper: bool = True):
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError(
                "لازم تحدد ALPACA_API_KEY و ALPACA_SECRET_KEY كمتغيرات بيئة أولاً."
            )
        self.client = TradingClient(api_key, secret_key, paper=paper)
        self.paper = paper

    def get_account_info(self) -> dict:
        acct = self.client.get_account()
        return {
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "mode": "PAPER (تجريبي)" if self.paper else "LIVE (حقيقي)",
        }

    def has_open_position(self, symbol: str) -> bool:
        try:
            self.client.get_open_position(symbol)
            return True
        except Exception:
            return False

    def open_position_count(self) -> int:
        """عدد الصفقات المفتوحة حاليًا بكل الحساب — يُستخدم لفرض حد أقصى
        على عدد الصفقات المتزامنة (مخاطرة على مستوى المحفظة، لا صفقة واحدة فقط)."""
        return len(self.client.get_all_positions())

    def execute_signal(self, symbol: str, signal: TradeSignal, risk_pct: float = 0.01):
        """
        ينفذ الإشارة فعليًا. يترك أي استثناء يصعد لمن يستدعيه (daily_runner.py)
        بدل ما يبتلعه هنا — عشان التسجيل والتنبيه يصير من مكان مركزي واحد.
        """
        account = self.client.get_account()
        capital = float(account.equity)

        if signal.side == "BUY":
            if self.has_open_position(symbol):
                print(f"[{symbol}] فيه صفقة مفتوحة أصلًا، تم تجاهل إشارة الشراء.")
                return None
            shares = position_size(capital, risk_pct, signal.price, signal.stop_loss)
            if shares == 0:
                print(f"[{symbol}] حجم الصفقة المحسوب = 0، تم تجاهل الإشارة.")
                return None
            if signal.stop_loss is None or signal.take_profit is None:
                raise ValueError(
                    f"[{symbol}] الإشارة بدون stop_loss/take_profit — "
                    "Bracket Order يحتاج الاثنين إلزاميًا."
                )

            order = LimitOrderRequest(
                symbol=symbol, qty=shares, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(signal.price, 2),
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=round(signal.take_profit, 2)),
                stop_loss=StopLossRequest(stop_price=round(signal.stop_loss, 2)),
            )
            result = self.client.submit_order(order)
            print(f"[{symbol}] أمر شراء Limit عند ${signal.price:.2f} × {shares} سهم "
                  f"(Bracket: وقف ${signal.stop_loss:.2f} / هدف ${signal.take_profit:.2f})")
            print("ملاحظة: أمر Limit قد لا يُنفَّذ إطلاقًا لو السعر ما نزل لمستوى الإشارة.")
            return result

        elif signal.side == "SELL":
            # خروج الطوارئ اليدوي فقط (مثلاً: بطل سبب الصفقة قبل ما يوصل السعر
            # للوقف أو الهدف المسجّلين أصلًا كـBracket). نستخدم Market هنا عمدًا
            # لأن أولوية الخروج السريع أهم من دقة السعر — والقاعدة الممنوعة
            # بالمصدر خاصة بالدخول (المطاردة)، مو الخروج.
            if not self.has_open_position(symbol):
                print(f"[{symbol}] ما فيه صفقة مفتوحة، تم تجاهل إشارة البيع.")
                return None
            position = self.client.get_open_position(symbol)
            order = MarketOrderRequest(
                symbol=symbol, qty=abs(float(position.qty)), side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            result = self.client.submit_order(order)
            print(f"[{symbol}] خروج طارئ يدوي (Market): {signal.reason}")
            print("تنبيه: هذا يغلق المركز بس ما يلغي أوامر الـBracket المعلّقة تلقائيًا —")
            print("لازم تلغي أوامر الوقف/الهدف القديمة يدويًا (أو عبر close_position) لتفادي تكرار الإغلاق.")
            return result


if __name__ == "__main__":
    executor = AlpacaExecutor(paper=True)
    info = executor.get_account_info()
    print("معلومات الحساب:")
    for k, v in info.items():
        print(f"  {k}: {v}")
