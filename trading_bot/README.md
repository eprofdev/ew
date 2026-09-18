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
python -m unittest discover -s tests  # 170 اختبار وحدة (بلا شبكة)

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

## قياس الأداء فعلياً (Backtest)

```bash
python -m trading_bot.backtest_cli AAPL --bars 2000 --market-cap 1500000 --free-float 900000
python -m trading_bot.backtest_cli AEMD --csv-4h data/AEMD_4h.csv --csv-1d data/AEMD_1d.csv \
    --market-cap 30000000 --free-float 9000000 --trades
```

خمسة مبادئ يلتزم بها المحرك، وبدونها تكون الأرقام كذباً مريحاً:

1. **لا استشراف للمستقبل**: عند الشمعة i يرى البوت `candles_4h[:i+1]` والشموع
   اليومية **المكتملة قبل يوم الشمعة** — لأن شمعة اليوم تحوي إغلاقاً لم يحدث بعد.
2. **الدخول في الشمعة التالية** وفقط إذا لمس السعر حد الأمر فعلاً.
3. **الوقف قبل الهدف** عند التعارض داخل الشمعة (الافتراض الأسوأ هو الصادق).
4. **الفجوة تُنفَّذ على الافتتاح** لا على الوقف — واقع أسهم السنتات.
5. **العمولة والانزلاق** يُخصمان من كل صفقة.

### المحرك يرفض أن يتظاهر

| ميزة | ماذا تمنع |
|---|---|
| مجال ثقة Bootstrap للتوقع | متوسط R جذاب مبني على 3 صفقات |
| `is_statistically_usable` (حد 30 صفقة) | استنتاج من عينة صغيرة |
| `trades_needed_for_inference()` | يقول كم صفقة تلزم لدقة ±0.25R |
| قمع الفرص (funnel) | يكشف أي فلتر يقتل الإشارات فعلاً |

مثال حقيقي من تشغيل على AAPL (1160 شمعة 4 ساعات):

```
  صفقات مغلقة : 5     التوقع: -0.07R     مجال الثقة: [-1.31R , +1.19R]
  ⛔ المجال يشمل الصفر — لم تثبت أي أفضلية إحصائية
  ⛔ العينة 5 صفقة فقط — المطلوب لدقة ±0.25R نحو 139 صفقة
  قمع الفرص: no_setup 1011 | setup_no_reversal 40 | rsi_rejected 6 | confirmed 16
```

## خط الإنتاج — أمر واحد يستأنف نفسه

```bash
export TWELVE_DATA_API_KEY=xxxx
python -m trading_bot.pipeline_cli --data-dir data/run1 --research-mode
```

شغّله مرة. إن نفد رصيد اليوم توقف وحفظ موضعه — أعد نفس الأمر غداً فيكمل.
وللتشغيل التلقائي:

```bash
0 2 * * *  cd /path/to/repo && python -m trading_bot.pipeline_cli --data-dir data/run1 --quiet
```

| المرحلة | التكلفة | تُحفظ في |
|---|---|---|
| 1. `universe` | رصيد واحد | `universe.json` |
| 2. `prescreen0` | **صفر** | `stage0.json` |
| 3. `prescreen1` | رصيد/رمز | `quotes.jsonl` → `candidates.json` |
| 4. `backtest` | رصيدان/رمز | `backtest.jsonl` |
| 5. `report` | صفر | `report.json` |

```bash
python -m trading_bot.pipeline_cli --data-dir data/run1 --status   # أين توقف
python -m trading_bot.pipeline_cli --data-dir data/run1 --retry-failed
python -m trading_bot.pipeline_cli --data-dir data/run1 --reset     # ابدأ من الصفر
```

### ضبط الفلاتر بعد الجلب مجاني تماماً

الخط يخزّن **الاقتباس الخام** لا نتيجة الحكم عليه. الفرق جوهري: تشغيله ثانيةً
بفلاتر أضيق أو أوسع يُعيد التقييم من الملف **بصفر رصيد**. لولا ذلك لبقيت
الرموز محكوماً عليها بفلتر قديم — خطأ صامت يفسد كل ضبط لاحق.

### ثلاث حالات فشل عولجت صراحةً

| الحالة | السلوك |
|---|---|
| نفاد رصيد اليوم | توقف نظيف + حفظ الموضع (لا انهيار) |
| حد الدقيقة | إعادة محاولة بتراجع أسّي — أما الحد اليومي فلا انتظار فيه |
| فشل دفعة اقتباس | **لا تُخزَّن الأخطاء إطلاقاً** — تُعاد المحاولة بـ `--retry-failed` |

الأخيرة كانت عيباً حقيقياً: دفعة فاشلة واحدة كانت ستحكم على 50 رمزاً بالإعدام
الدائم في الذاكرة، فلا يُعاد جلبها أبداً.

## الترشيح المسبق — قبل إنفاق أي رصيد

```bash
# المرحلة 0 وحدها: مجانية تماماً، بلا أي طلب شبكة
python -m trading_bot.prescreen_cli --universe data/universe_nasdaq.json \
    --stage0-only --out data/universe_small.json

# المرحلتان: رصيد واحد لكل رمز ثم حفظ الناجين
python -m trading_bot.prescreen_cli --universe data/universe_nasdaq.json \
    --out data/candidates.json --max-candidates 300

# ثم الـ backtest على الناجين فقط
python -m trading_bot.portfolio_cli --universe data/candidates.json --research-mode
```

### الحقيقة الاقتصادية التي تحكم التصميم (مقيسة لا مفترضة)

Twelve Data تحسب الرصيد **لكل رمز لا لكل طلب**: طلب مجمّع لخمسة رموز رفع
الاستهلاك من 1 إلى 7، **وحتى الطلب الفاشل يُخصم**. إذن التجميع يوفّر
اتصالات لا أرصدة، والتوفير الحقيقي الوحيد هو **ألا تسأل عن الرمز أصلاً**.

### ثلاث مراحل مرتّبة بالتكلفة

| المرحلة | التكلفة | ما تفحصه | الأثر على ناسداك |
|---|---|---|---|
| **0** | **صفر رصيد** | النوع، الدولة، **طبقة السوق**، اسم الشركة | 4,509 → 1,415 |
| **1** | رصيد/رمز | السعر، متوسط الفوليوم، الموقع من مدى 52 أسبوعاً، حركة اليوم | 1,415 → عشرات |
| **2** | رصيدان/رمز | التاريخ الكامل (في `portfolio.py`) | الناجون فقط |

**أقوى فلتر مجاني هو طبقة السوق**: NASDAQ Capital Market (`XNCM`) موطن
الشركات الصغيرة — AEMD وVSME كلاهما هناك، بينما AAPL وMSFT وNVDA في
Global Select (`XNGS`). التوزيع الفعلي: XNCM 1,480 | XNGS 1,259 |
XNMS 919. فلتر واحد بلا رصيد يقصّ 60% من الكون ويُبقي بالضبط ما تستهدفه
استراتيجية نايف.

### التوفير المقاس

```
بلا ترشيح : 9,018 رصيداً — 11.3 يوم على الخطة المجانية
مع الترشيح: 1,697 رصيداً —  2.1 يوم        ← توفير 81%
```

## اختبار الكون الكامل (كل الأسهم)

```bash
# 1) اعرف التكلفة أولاً — لا تشغّل ما يستغرق أياماً وأنت تجهل ذلك
python -m trading_bot.portfolio_cli --exchange NASDAQ --dry-run

# 2) ابنِ قائمة الرموز (4,509 رمزاً في ناسداك، منها 3,683 سهماً عادياً)
python -m trading_bot.portfolio_cli --exchange NASDAQ \
    --build-universe data/universe_nasdaq.json --extra-symbols AMED VSME

# 3) شغّل مع استئناف تلقائي — آمن للإيقاف والمتابعة في أي لحظة
python -m trading_bot.portfolio_cli --universe data/universe_nasdaq.json \
    --checkpoint data/run1.jsonl --rate-limit 8 --research-mode \
    --fundamentals data/fundamentals.json --report data/report.json

# 4) أعد التحليل من الذاكرة المؤقتة دون أي طلب شبكة
python -m trading_bot.portfolio_cli --universe data/universe_nasdaq.json --offline
```

### التكلفة الحقيقية (أرقام مقاسة لا مقدَّرة)

| الخطة | طلبات/دقيقة | زمن جلب 3,683 رمزاً |
|---|---|---|
| مجانية | 8 | **15.3 ساعة** (أو 9.2 يوم بحد 800 طلب يومياً) |
| Grow | 55 | 2.2 ساعة |
| Pro+ | 610 | 12 دقيقة |

الحساب نفسه 0.3 ساعة فقط للكون كله — **العنق هو الـ API لا المعالج**،
ولذلك الذاكرة المؤقتة ونقطة الاستئناف أهم من التوازي.

### مستويان للحكم

الخطأ الشائع جمع منحنيات رأس المال: لا يمكنك فتح 40 صفقة متزامنة بـ 1%
مخاطرة لكل واحدة. لذلك يفصل المحرك:

- **تجميع إحصائي**: كل الصفقات في سلة واحدة → التوقع بوحدات R ومجال ثقته.
  هذا حكم على الاستراتيجية نفسها.
- **محاكاة محفظة**: ترتيب زمني بسقف للصفقات المتزامنة → منحنى رأس مال
  واقعي مع الصفقات التي فاتتك لامتلاء الفتحات. هذا حكم على قابلية التطبيق.

### انحياز البقاء — أخطر ما في الأمر

`/stocks` تعطي **المدرجين اليوم فقط**. الدليل من هذا المستودع: `AMED`
(Amedisys) شُطبت بعد استحواذ UnitedHealth، فهي غائبة تماماً عن الكون.
كل شركة أفلست أو شُطبت غائبة كذلك — وفي أسهم السنتات الشطب مصير شائع.

النتيجة: أي رقم من هذا الاختبار هو **حد أعلى متفائل** لا توقع واقعي. خفّف
الأثر بـ `--extra-symbols` لإضافة رموز مشطوبة تعرفها، واقرأ الباقي بحذر.

### وضع البحث

`--research-mode` يعطّل فلتر الهيكل المالي (الكاب والفلوت) لقياس السلوك
السعري وحده على كون كبير دون خطة Pro. **يُعلَن في كل تقرير** لأنه يعطّل
أهم صمامات نايف — النتيجة حينها لا تمثل البوت الحقيقي.

## اختيار الأسهم (Screener)

قمع بثلاث طبقات في `screener.py`: استبعاد صلب (سعر، سيولة، فلوت، RVOL) ثم
ترتيب بالنقاط (الفوليوم النسبي 35، ضيق الفلوت 25، الموقع من المدى 15، وقود
الشورت 15، النطاق السعري 10) ثم عقوبات المخاطر (تجزئة عكسية، عرض أسهم،
ترويج، وامتداد سعري بالفعل).

**الأوزان فرضية وليست نتيجة مثبتة** — الحكم عليها من الـ backtest وحده.

## تدفق الأوامر و Bookmap

`orderflow/` يسدّ الفجوة الوحيدة المتبقية: Twelve Data لا تعطي Bid/Ask، و
Bookmap تعطي L1/L2 لحظياً.

| ما تراه في الخريطة الحرارية | ما يقرؤه الكود |
|---|---|
| شريط ثابت يبتلع التنفيذات | `absorption_price` — تأكيد الدعم من الدفتر لا من الشمعة |
| مستوى يُستهلك ويُعاد ملؤه | `iceberg_prices` — مشترٍ كبير يخفي حجمه |
| كتلة ضخمة فوق السعر | `ask_wall` — رفض الشراء في وجه بائع أكبر |
| طلب رقيق | `THIN_BID` — سيولة وهمية تظهر عند الخروج |

```python
from trading_bot.orderflow import BookmapFeed
feed = BookmapFeed("VSME")
provider = TwelveDataProvider(book_source=feed.as_book_source())
```

التشغيل داخل Bookmap عبر `orderflow/bookmap_addon.py` (واجهة Python L1،
بايثون 3.7). بقية الحزمة **لا تعتمد** على حزمة `bookmap`، فتعمل مع أي مصدر L2.

## جودة البيانات

`dataquality.py` يميّز التجزئة غير المعدّلة عن الخبر الحقيقي **بالفوليوم**:
تجزئة AEMD 1:5 رافقها فوليوم 0.89× المعدل (تُصحَّح)، وقفزة اندماجها +493%
رافقها 567× (تُترك كما هي). السعر وحده لا يكفي للتمييز.

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
  backtest.py                        محرك walk-forward لقياس الأداء
  backtest_cli.py                    تشغيل الاختبار من سطر الأوامر
  screener.py                        اختيار الأسهم وترتيبها
  universe.py                        بناء كون الرموز + تحذير انحياز البقاء
  prescreen.py                       الترشيح المسبق (يحمي رصيد الـ API)
  prescreen_cli.py                   سطر أوامر الترشيح
  pipeline.py                        خط الإنتاج الخمسي بحالة محفوظة
  pipeline_cli.py                    الأمر الواحد الذي يشغّل كل شيء
  portfolio.py                       تشغيل الكون كله وتجميع النتائج
  portfolio_cli.py                   سطر أوامر الكون الكامل
  dataquality.py                     حارس التجزئة وجودة السلاسل
  csvio.py                           حفظ/تحميل الشموع
  orderflow/book.py                  دفتر أسعار L2
  orderflow/bookmap.py               جسر Bookmap وصمامات تدفق الأوامر
  orderflow/bookmap_addon.py         إضافة تعمل داخل Bookmap
  scan.py                            أداة سطر الأوامر للفحص الحي
  demo.py                            مثال تشغيلي بأربع حالات
  strategies/faisal_price_action.py  السلوك السعري على 4 ساعات
  strategies/naif_safeguards.py      صمامات الأمان
data/AEMD_*.csv                      بيانات AEMD حقيقية للاختبار المتكرر
tests/test_bot.py                    31 اختبار للمحرك والاستراتيجيتين
tests/test_twelve_data.py            26 اختبار للمزود (بلا شبكة)
tests/test_backtest.py               18 اختبار للـ backtest وجودة البيانات
tests/test_screener_orderflow.py     30 اختبار للمرشّح وتدفق الأوامر
tests/test_portfolio.py              23 اختبار للكون والمحفظة
tests/test_prescreen.py              20 اختبار للترشيح المسبق
tests/test_pipeline.py               22 اختبار لخط الإنتاج
```
