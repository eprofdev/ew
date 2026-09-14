"""
brokers/webull.py
تنفيذ عبر Webull OpenAPI الرسمي (webull-inc/webull-openapi-python-sdk).

اقرأ هذا قبل أي شيء — ثلاثة فروق جوهرية عن Alpaca:

1. لا يوجد حساب تجريبي (Paper).
   Alpaca يعطيك حسابًا وهميًا كاملاً تجرّب عليه شهورًا مجانًا. Webull OpenAPI
   يتصل بحسابك الحقيقي فقط. يعني: أول أمر ترسله عبر هذا الملف فلوس حقيقية.
   لذلك تجربة البوت بوضع PAPER لازم تبقى على Alpaca حتى لو نيّتك التنفيذ
   النهائي عبر Webull. ولهذا السبب بالذات يفرض هذا الملف متغيّر تأكيد صريح
   (WEBULL_CONFIRM_LIVE=yes) قبل أول أمر — حماية من خطأ إعداد يكلّفك مالاً.

2. لا يدعم أوامر Bracket (دخول + وقف + هدف بأمر واحد).
   بـAlpaca، وقف الخسارة يُسجَّل عند الوسيط لحظة الدخول ويشتغل حتى لو جهازك
   مطفي. هنا لازم أمر وقف منفصل بعد تنفيذ الشراء — وبين اللحظتين مركزك
   مكشوف. البوت يغطي الفجوة بـensure_protective_stop() اللي تُستدعى كل
   تشغيلة، لكن تبقى فجوة حقيقية بين التشغيلات. الهدف (take profit) يبقى
   مراقَبًا من البوت لا من الوسيط.

3. يحتاج موافقة على التطبيق قبل الاستخدام.
   لازم تطلب App Key/Secret من بوابة مطوّري Webull، والتوفّر يختلف حسب
   السوق (US / HK / JP). بعض الواجهات — تحديدًا مجموعة v2 — غير متاحة
   لعملاء Webull US حتى تاريخ كتابة هذا الملف حسب توثيق الـSDK نفسه،
   فنستخدم واجهات v1 للأوامر ونرجع لـv2 فقط عند الحاجة.

التوثيق الرسمي: https://developer.webull.com/apis/docs  (US)
                https://developer.webull.hk/apis/docs   (HK)

التثبيت:
    pip install webull-python-sdk-core webull-python-sdk-trade webull-python-sdk-mdata

الإعداد:
    export WEBULL_APP_KEY="..."
    export WEBULL_APP_SECRET="..."
    export WEBULL_ACCOUNT_ID="..."        # من get_account_list()
    export WEBULL_REGION="us"             # us | hk | jp
    export WEBULL_CONFIRM_LIVE="yes"      # تأكيد صريح أنك فاهم أنها فلوس حقيقية
"""
import os
import time
import uuid

from strategy import TradeSignal, position_size
from brokers.base import BrokerExecutor


class WebullExecutor(BrokerExecutor):
    name = "webull"
    supports_paper = False     # لا حساب تجريبي بـOpenAPI — راجع رأس الملف
    supports_bracket = False   # لا أوامر مركّبة — الوقف أمر منفصل

    def __init__(self, confirm_live: bool = None, region: str = None,
                 account_id: str = None):
        try:
            from webullsdkcore.client import ApiClient
            from webullsdktrade.api import API
        except ImportError as e:
            raise RuntimeError(
                "مكتبات Webull غير مثبّتة. ثبّتها بـ:\n"
                "  pip install webull-python-sdk-core webull-python-sdk-trade "
                "webull-python-sdk-mdata"
            ) from e

        app_key = os.environ.get("WEBULL_APP_KEY")
        app_secret = os.environ.get("WEBULL_APP_SECRET")
        if not app_key or not app_secret:
            raise RuntimeError(
                "لازم تحدد WEBULL_APP_KEY و WEBULL_APP_SECRET كمتغيرات بيئة. "
                "تحصل عليهما بعد اعتماد تطبيقك من بوابة مطوّري Webull."
            )

        if confirm_live is None:
            confirm_live = os.environ.get("WEBULL_CONFIRM_LIVE", "").lower() == "yes"
        if not confirm_live:
            raise RuntimeError(
                "Webull OpenAPI ما فيه حساب تجريبي — أي أمر هنا بفلوس حقيقية.\n"
                "لو فاهم هذا وتبي تكمل، حدّد: export WEBULL_CONFIRM_LIVE=yes\n"
                "ونصيحة: جرّب البوت أسابيع على Alpaca Paper أولاً."
            )

        self.region = (region or os.environ.get("WEBULL_REGION", "us")).lower()
        self.account_id = account_id or os.environ.get("WEBULL_ACCOUNT_ID")
        self.client = ApiClient(app_key, app_secret, self.region)
        self.api = API(self.client)
        self._instrument_cache = {}

        if not self.account_id:
            self.account_id = self._discover_account_id()

    # ------------------------------------------------------------ مساعدات
    @staticmethod
    def _json(response):
        """
        يفكّ رد الـSDK (كائن requests.Response) إلى dict/list.
        نرمي خطأً واضحًا عند فشل الطلب بدل ما نكمل على رد فاضي — صمت هنا
        يعني أمرًا ظنّيته نُفِّذ وهو ما وصل.
        """
        if response is None:
            raise RuntimeError("Webull: لا يوجد رد من الخادم")
        status = getattr(response, "status_code", 200)
        try:
            data = response.json()
        except Exception as e:
            raise RuntimeError(
                f"Webull: رد غير قابل للقراءة (HTTP {status}): "
                f"{getattr(response, 'text', '')[:200]}"
            ) from e
        if status >= 400:
            raise RuntimeError(f"Webull: فشل الطلب (HTTP {status}): {data}")
        return data

    @staticmethod
    def _pick(d: dict, *keys, default=None):
        """
        يلتقط أول مفتاح موجود من عدة تسميات محتملة.

        أسماء حقول Webull تختلف بين أسواق الـSDK (مثلاً totalMarketValue مقابل
        totalAssetValue). بدل ما نكسر عند أول اختلاف، نجرّب البدائل المعروفة
        ثم نرجع default — والقيم المهمة (كرأس المال) تُفحص عند المستدعي.
        """
        for k in keys:
            if isinstance(d, dict) and d.get(k) not in (None, ""):
                return d[k]
        return default

    def _discover_account_id(self) -> str:
        data = self._json(self.api.account_v2.get_account_list())
        items = data if isinstance(data, list) else self._pick(data, "data", "accounts",
                                                               default=[])
        if not items:
            raise RuntimeError(
                "ما لقيت أي حساب Webull مرتبط بهذا التطبيق. تأكد من اعتماد "
                "التطبيق وربطه بحسابك، أو حدّد WEBULL_ACCOUNT_ID يدويًا."
            )
        acc_id = self._pick(items[0], "account_id", "accountId", "id")
        if not acc_id:
            raise RuntimeError(f"رد قائمة الحسابات بصيغة غير متوقعة: {items[0]}")
        return str(acc_id)

    def _instrument_id(self, symbol: str) -> str:
        """
        يحوّل رمز السهم إلى instrument_id اللي تطلبه واجهة الأوامر.
        نخزّنه بالذاكرة: الرمز ثابت ولا داعي لطلب شبكة بكل أمر.
        """
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]

        category = {"us": "US_STOCK", "hk": "HK_STOCK", "jp": "JP_STOCK"}.get(
            self.region, "US_STOCK")
        data = self._json(self.api.instrument.get_instrument(symbol, category))
        items = data if isinstance(data, list) else self._pick(data, "data", default=[])
        if not items:
            raise RuntimeError(f"Webull: ما لقيت الرمز {symbol} بسوق {category}")
        inst_id = self._pick(items[0], "instrument_id", "instrumentId")
        if not inst_id:
            raise RuntimeError(f"Webull: رد الرمز {symbol} بصيغة غير متوقعة: {items[0]}")
        self._instrument_cache[symbol] = str(inst_id)
        return str(inst_id)

    @staticmethod
    def _client_order_id() -> str:
        """معرّف أمر فريد من طرفنا — Webull يشترطه ويحدّه بـ40 حرفًا."""
        return uuid.uuid4().hex[:32]

    def _positions(self) -> list:
        data = self._json(self.api.account_v2.get_account_position(self.account_id))
        items = self._pick(data, "positions", "holdings", "data", default=None)
        if items is None:
            items = data if isinstance(data, list) else []
        return items

    # -------------------------------------------------------- واجهة الوسيط
    def get_account_info(self) -> dict:
        data = self._json(self.api.account_v2.get_account_balance(self.account_id))
        body = self._pick(data, "data", default=data)
        equity = self._pick(body, "totalAssetValue", "total_asset_value",
                            "totalMarketValue", "netLiquidationValue")
        cash = self._pick(body, "cashBalance", "cash_balance", "settledFunds",
                          "totalCashValue")
        if equity is None:
            raise RuntimeError(
                f"ما قدرت أقرأ رأس المال من رد Webull: {body}. "
                "راجع توثيق سوقك — أسماء الحقول تختلف بين US/HK/JP."
            )
        return {
            "equity": float(equity),
            "cash": float(cash) if cash is not None else 0.0,
            "buying_power": float(cash) if cash is not None else 0.0,
            "mode": "LIVE (حقيقي — Webull ما عنده تجريبي)",
            "broker": "webull",
            "account_id": self.account_id,
        }

    def has_open_position(self, symbol: str) -> bool:
        symbol = symbol.upper()
        for p in self._positions():
            sym = str(self._pick(p, "symbol", "ticker", "tickerSymbol", default="")).upper()
            qty = float(self._pick(p, "quantity", "qty", "position", default=0) or 0)
            if sym == symbol and qty != 0:
                return True
        return False

    def open_position_count(self) -> int:
        return sum(1 for p in self._positions()
                   if float(self._pick(p, "quantity", "qty", "position", default=0) or 0) != 0)

    def position_qty(self, symbol: str) -> float:
        symbol = symbol.upper()
        for p in self._positions():
            sym = str(self._pick(p, "symbol", "ticker", "tickerSymbol", default="")).upper()
            if sym == symbol:
                return float(self._pick(p, "quantity", "qty", "position", default=0) or 0)
        return 0.0

    def execute_signal(self, symbol: str, signal: TradeSignal, risk_pct: float = 0.01):
        from webullsdktrade.common.order_side import OrderSide
        from webullsdktrade.common.order_type import OrderType
        from webullsdktrade.common.order_tif import OrderTIF

        capital = self.get_account_info()["equity"]

        if signal.side == "BUY":
            if self.has_open_position(symbol):
                print(f"[{symbol}] فيه صفقة مفتوحة أصلًا، تم تجاهل إشارة الشراء.")
                return None
            if signal.stop_loss is None:
                raise ValueError(f"[{symbol}] إشارة شراء بدون وقف خسارة — مرفوضة.")
            shares = position_size(capital, risk_pct, signal.price, signal.stop_loss)
            if shares == 0:
                print(f"[{symbol}] حجم الصفقة المحسوب = 0، تم تجاهل الإشارة.")
                return None

            # نفس قاعدة Alpaca: الدخول Limit دائمًا، ممنوع الشراء من العرض
            resp = self._json(self.api.order.place_order(
                account_id=self.account_id,
                qty=str(shares),
                instrument_id=self._instrument_id(symbol),
                side=OrderSide.BUY.name,
                client_order_id=self._client_order_id(),
                order_type=OrderType.LIMIT.name,
                extended_hours_trading=False,
                tif=OrderTIF.DAY.name,
                limit_price=str(round(signal.price, 2)),
            ))
            print(f"[{symbol}] أمر شراء Limit عند ${signal.price:.2f} × {shares} سهم.")
            print("تنبيه Webull: وقف الخسارة ما يُسجَّل مع الأمر — يُرسل بأمر منفصل "
                  "بعد التنفيذ عبر ensure_protective_stop().")
            return resp

        elif signal.side == "SELL":
            qty = self.position_qty(symbol)
            if qty <= 0:
                print(f"[{symbol}] ما فيه صفقة مفتوحة، تم تجاهل إشارة البيع.")
                return None
            resp = self._json(self.api.order.place_order(
                account_id=self.account_id,
                qty=str(int(abs(qty))),
                instrument_id=self._instrument_id(symbol),
                side=OrderSide.SELL.name,
                client_order_id=self._client_order_id(),
                order_type=OrderType.MARKET.name,
                extended_hours_trading=False,
                tif=OrderTIF.DAY.name,
            ))
            print(f"[{symbol}] خروج (Market): {signal.reason}")
            return resp

    # ------------------------------------------------ حماية خاصة بـWebull
    def open_orders(self) -> list:
        data = self._json(self.api.order.list_open_orders(self.account_id, page_size=100))
        items = self._pick(data, "orders", "data", default=None)
        return items if items is not None else (data if isinstance(data, list) else [])

    def has_open_stop_order(self, symbol: str) -> bool:
        symbol = symbol.upper()
        for o in self.open_orders():
            sym = str(self._pick(o, "symbol", "ticker", "tickerSymbol", default="")).upper()
            otype = str(self._pick(o, "orderType", "order_type", default="")).upper()
            if sym == symbol and "STOP" in otype:
                return True
        return False

    def ensure_protective_stop(self, symbol: str, stop_price: float,
                               retries: int = 3, delay: float = 2.0):
        """
        يضمن وجود أمر وقف خسارة على مركز مفتوح — يُستدعى كل تشغيلة.

        هذي الدالة هي تعويض غياب الـBracket بـWebull. تبقى فجوة حقيقية
        بين لحظة تنفيذ الشراء وأول تشغيلة بعدها؛ قلّلها بتشغيل daily_runner
        أكثر من مرة يوميًا لو تنفّذ عبر Webull.
        """
        from webullsdktrade.common.order_side import OrderSide
        from webullsdktrade.common.order_type import OrderType
        from webullsdktrade.common.order_tif import OrderTIF

        qty = self.position_qty(symbol)
        if qty <= 0:
            return None                      # الأمر ما نُفِّذ بعد، ما فيه ما نحميه
        if self.has_open_stop_order(symbol):
            return None                      # الحماية موجودة أصلاً

        last_error = None
        for attempt in range(retries):
            try:
                resp = self._json(self.api.order.place_order(
                    account_id=self.account_id,
                    qty=str(int(abs(qty))),
                    instrument_id=self._instrument_id(symbol),
                    side=OrderSide.SELL.name,
                    client_order_id=self._client_order_id(),
                    order_type=OrderType.STOP_LOSS.name,
                    extended_hours_trading=False,
                    tif=OrderTIF.GTC.name,     # يبقى فعّالاً حتى ينفّذ أو تلغيه
                    stop_price=str(round(stop_price, 2)),
                ))
                print(f"[{symbol}] تم تسجيل وقف خسارة عند ${stop_price:.2f} × {int(qty)} سهم.")
                return resp
            except Exception as e:
                # مركز مكشوف بلا وقف حالة خطيرة — نعيد المحاولة قبل الاستسلام
                last_error = e
                if attempt < retries - 1:
                    time.sleep(delay * (attempt + 1))

        raise RuntimeError(
            f"[{symbol}] فشل تسجيل وقف الخسارة بعد {retries} محاولات — "
            f"المركز مكشوف الآن، تدخّل يدويًا فورًا. آخر خطأ: {last_error}"
        )


if __name__ == "__main__":
    ex = WebullExecutor()
    print("معلومات الحساب:")
    for k, v in ex.get_account_info().items():
        print(f"  {k}: {v}")
    print(f"\nحدود الحماية: {ex.describe_risk()}")
