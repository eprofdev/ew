# البوت المدمج: سلوك سعري (فيصل) + صمامات أمان (نايف)

دمج استراتيجيتين في محرك واحد:

| الطبقة | المصدر | الدور |
|---|---|---|
| **السلوك السعري** | استراتيجية فيصل (@kisar_) | تحليل الشموع الساقطة والدعوم على فريم **4 ساعات** — هي وحدها من تولّد إشارة الدخول |
| **صمامات الأمان** | فلاتر نايف (@Naifo9x) | لا تولّد إشارات — تُسقِط الخطرة منها: حظر العقود، إلغاء القنوات السعرية، منع الشراء من العرض، رصد الشورت، وتفادي فخاخ التصريف |

> ⚠️ للأغراض البحثية والتعليمية فقط، وليست توصية أو نصيحة مالية. أسهم السنتات
> (Micro/Nano Cap) عالية المخاطر وقد تخسر رأس المال بالكامل. اختبر أي إعداد
> على حساب تجريبي أولاً.

## التشغيل

```bash
python -m trading_bot.demo            # مثال كامل ببيانات تركيبية
python -m unittest discover -s tests  # 57 اختبار وحدة (بلا شبكة)

export TWELVE_DATA_API_KEY=xxxxxxxx   # فحص حي ببيانات Twelve Data
python -m trading_bot.scan VSME --equity 10000
```

لا توجد أي تبعيات خارجية — بايثون 3.9+ فقط.

## ترتيب اتخاذ القرار

```
لقطة السوق (MarketSnapshot)
        │
        ▼
1) صمامات نايف ──► أي رفض = VETO وتوقف فوري (لا تحليل أصلاً)
        │
        ▼
2) سلوك فيصل على 4H: دعم بلمستين+ → شموع ساقطة بفوليوم متناقص →
   اختبار الدعم بلا كسر بفوليوم → شمعة انعكاس تغلق فوق الدعم
        │
        ▼
3) فلاتر RSI المزدوجة (نموذج الارتكاز): يومي 40–48 و 4 ساعات 48–55
        │
        ▼
4) تقفيل الشورت: مضاعِف قوة (+15 نقطة) وليس سبب دخول وحيداً
        │
        ▼
5) إدارة المخاطر: أمر محدد على الطلب + وقف تحت الدعم + 3 أهداف
```

## صمامات نايف المطبّقة

| الكود | الحماية |
|---|---|
| `OPTIONS_BLOCKED` | حظر العقود تماماً — أسهم فقط |
| `STRUCTURE` | كاب ≤ 2M وفلوت ≤ 1.5M (سهم حاد قابل للانفجار) |
| `SPREAD` / `LIQUIDITY` / `HALTED` | حماية التنفيذ من التعليق والفروقات الواسعة |
| `DILUTION` / `REVERSE_SPLIT` | تسجيل عرض أسهم فعّال أو تجزئة عكسية حديثة |
| `PUMP_GROUP` / `PAID_INDICATOR` / `SPIKE_TRAP` | فخاخ جروبات التصريف والمؤشرات التجارية والقفزات قبل الافتتاح |
| `BORROW_FEE` | تكلفة اقتراض خانقة = ضغط بيعي حاد |
| — | **منع الشراء من العرض**: الأمر يوضع على الطلب (Bid) دائماً |
| — | **إلغاء القنوات السعرية** (Linear Regression) بسبب كثرة الفجوات |

## الربط ببيانات Twelve Data الحقيقية

```bash
export TWELVE_DATA_API_KEY=xxxxxxxx
python -m trading_bot.scan VSME ABCD --equity 10000
python -m trading_bot.scan --watchlist watchlist.txt --fundamentals fund.json --json
```

أو برمجياً:

```python
from trading_bot import EnhancedTradingBot, BotConfig
from trading_bot.data_feed import build_snapshots
from trading_bot.providers import TwelveDataProvider

provider = TwelveDataProvider(              # المفتاح من TWELVE_DATA_API_KEY
    max_requests_per_minute=8,              # حد الخطة المجانية
    book_source=my_broker_level1,           # دالة تعيد (bid, ask) — انظر أدناه
)
bot = EnhancedTradingBot(BotConfig(account_equity=10_000))
for signal in bot.scan(build_snapshots(provider, ["VSME", "ABCD"])):
    if signal.is_actionable:
        print(signal.to_alert())
```

المحرك يُخرج تنبيهات فقط — لا يرسل أوامر لأي وسيط. ربط التنفيذ قرارك أنت.

### ثلاثة قيود حقيقية في Twelve Data يجب معرفتها

| القيد | الأثر | الحل |
|---|---|---|
| **لا يوجد Bid/Ask في REST** — `/quote` تعطي آخر صفقة فقط | صمام «ممنوع الشراء من العرض» لا يجد سعر طلب ⇒ القرار يبقى `WATCH` ولا يصدر أمر | مرّر `book_source=` دالة تعيد `(bid, ask)` من وسيطك أو من WebSocket |
| **`/statistics` (كاب/فلوت/شورت) تتطلب خطة Pro فأعلى** | الهيكل المالي «غير معروف» ⇒ رفض `DATA_UNAVAILABLE` (لا تخمين) | ملف `--fundamentals fund.json` يدوياً، أو ترقية الخطة |
| **لا تعطي الواجهة نسبة شورت الشهر السابق** | لا مرجع تاريخي لرصد التقفيل | `ShortInterestStore` يخزّن كل قراءة محلياً، والمرجع التاريخي = أعلى قراءة خلال 90 يوماً |

كذلك: `borrow_fee` وحالة الإيقاف (`halted`) غير متاحتين لدى المزود، و`/time_series`
ترجع الأحدث أولاً — المزود يعكسها تصاعدياً لأن كل المؤشرات تفترض الترتيب الصاعد.

### ملف الهيكل المالي اليدوي

```json
{
  "VSME": {
    "market_cap": 1450000,
    "free_float": 980000,
    "has_active_shelf_offering": false,
    "has_options": false
  }
}
```

القيم اليدوية تتقدم دائماً على قيم الواجهة (بيانات الفلوت الرسمية أدق من أي تقدير).

### مزوّد آخر؟

نفّذ بروتوكول `MarketDataProvider` في `data_feed.py` (ثلاث دوال فقط:
`get_candles` / `get_metrics` / `get_realtime`) ومرّره لنفس المحرك دون تعديل
أي منطق.

## الضبط

كل الأرقام في `config.py` (`BotConfig`): نطاقات RSI، عرض منطقة الدعم، طول
سلسلة السقوط، حدود الكاب والفلوت، نسبة المخاطرة لكل صفقة، ومضاعفات الأهداف.

## الملفات

```
trading_bot/
  config.py                          إعدادات قابلة للضبط
  models.py                          Candle / StockMetrics / RealTimeData / Signal
  indicators.py                      RSI, ATR, SMA/EMA, القيعان المحورية، نسبة الفجوات
  risk.py                            حجم المركز والوقف والأهداف
  engine.py                          المحرك المدمج (EnhancedTradingBot)
  data_feed.py                       واجهة مزوّد البيانات (بروتوكول)
  providers/twelve_data.py           مزوّد Twelve Data فعلي (REST)
  scan.py                            أداة سطر الأوامر للفحص الحي
  demo.py                            مثال تشغيلي بأربع حالات
  strategies/faisal_price_action.py  السلوك السعري على 4 ساعات
  strategies/naif_safeguards.py      صمامات الأمان
tests/test_bot.py                    31 اختبار للمحرك والاستراتيجيتين
tests/test_twelve_data.py            26 اختبار للمزود (بلا شبكة)
```
