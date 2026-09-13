"""
faisal_strategies.py
استراتيجيتان مستخرجتان من "دليل طريقة فيصل" — الأجزاء القابلة للترميز فقط.
كل استراتيجية موثّقة بوضوح: أيش مأخوذ حرفيًا من المصدر، وأيش تقريب مبسّط
لجزء كان أصلاً بصري/سياقي (شموع، Level 2) ما ينترجم لرقم واحد بأمانة.

مهم: المصدر نفسه ما يذكر أي رقم نسبة نجاح أو باكتيست — هذا الملف هو أول
اختبار كمي فعلي للمنهج، مو تطبيق لنتيجة مثبتة.
"""
from dataclasses import dataclass
from typing import Optional
import pandas as pd
from indicators import pivot_support_resistance, nearest_support_resistance
from strategy import TradeSignal


class BaseTrackStrategy:
    """
    "سهم الارتكاز" — من قسم التحديث (صفحات 62-88 بالدليل).

    ملاحظة مهمة اكتُشفت بعد مراجعة إضافية للصور (مو النص فقط):
    الدليل نفسه فيه نسختان مختلفتان لشرط المتوسطات (رقم 1 من الشروط الستة):
    - 3 مواضع مستقلة (النص السردي + صفحة "الشامل" + صفحة "الدخول الصحيح") تقول:
      "السعر تحت المتوسطات 20/30/50" — ثلاثة متوسطات.
    - موضع واحد فقط (إنفوجرافيك "أمثلة عملية 1-3") يقول: "عودة السعر فوق EMA30 وEMA50"
      — متوسطان فقط، وEMA تحديدًا لا SMA.
    هذا تناقض داخلي بالمصدر نفسه، مو اختلاف "من سهم لآخر" كما افترضنا أول مرة —
    الأرجح أنها نسخ مختلفة من نفس الفكرة تجمّعت بالدليل بدون توحيد. اعتمدنا هنا
    النسخة الأكثر تكرارًا (20/30/50) كافتراضي، مع خيار التبديل للنسخة الأخرى
    عبر ma_mode لاختبار الاثنتين فعليًا بالباكتيست بدل افتراض واحدة.

    مأخوذ حرفيًا من المصدر (مؤكَّد من 3 مواضع متطابقة):
    - RSI بين 23 و27 كمنطقة "ضغط بيعي عميق" — إنذار وليس أمر شراء بنص الدليل
      (موضع واحد بصفحة 82 يذكر "20-30" كمثال أوسع، لكن 23-27 هو المتكرر بثلاث صور/نص)
    - MACD "يتحسن أو يعطي تقاطعًا إيجابيًا" — الدليل يقبل الاثنين صراحة، فالتحسن
      التدريجي (بدون تقاطع كامل) مقبول أيضًا، مو خطأ كما ظننت بمراجعة سابقة
    - الدخول يكون بعد فشل الهبوط وظهور ثبات، مو عند لحظة RSI نفسها

    إضافة من مراجعة شاملة لكل صور الدليل (94 صفحة، مو عيّنة فقط): "عدم مطاردة
    السهم بعد انفجاره" و"قفزة خبر بدون قاعدة = ليس ارتكاز" يتكرران 9 مرات مستقلة
    عبر الدليل كامل (صفحات 16، 77، 78، 79، 80، 81، 87، 88) — أكثر تكرارًا من أي
    قاعدة رقمية أخرى بالملف، ولم يكن مُطبَّقًا هنا إطلاقًا قبل هذي المراجعة.
    صار الآن شرطين إضافيين:
    - was_stable_before_entry: ما فيه يوم بقفزة سعرية متطرفة (>30%) بالنافذة قبل
      يوم الدخول — يرفض "قفزة خبر بدون قاعدة" (صفحة 80)
    - not_chasing_entry_candle: حركة يوم الدخول نفسه لا تتجاوز ~2.5×ATR — يرفض
      "الدخول بعد شمعة انفجارية بعيدة عن المتوسطات" (صفحات 77، 78، 88)

    تقريب مبسّط (الدليل يصفه بصريًا، هنا رقم تقريبي فقط):
    - "شمعة ارتداد/Hammer": قربناها لشمعة خضراء بذيل سفلي أطول من العلوي.

    استُبعد عمدًا لأنه مو قابل للباكتيست بأمانة:
    - قراءة Level 2 (الدليل نفسه يقول: أداة مراقبة، مو إشارة دخول مستقلة)
    - الخبر/المحفز الإيجابي وفجوة الصعود (شرطان 5 و6) — سياقيان وليسا رقمين
    - "شمعة مضارب مقابل شمعة وهم" كفرق سياقي كامل (اكتفينا بفوليوم البيع المتراجع كتقريب جزئي)
    """

    def __init__(self, rsi_low: float = 23, rsi_high: float = 27,
                 rr_ratio: float = 2.0, atr_mult: float = 1.5,
                 sr_window: int = 10, sr_lookback: int = 150,
                 confirm_window: int = 7, ma_mode: str = "strict"):
        """
        ma_mode:
          "strict" (افتراضي) — تحت MA20 وMA30 وMA50، والاستعادة تبدأ من MA20.
                                النسخة الأكثر تكرارًا بالدليل (3 مواضع).
          "loose"  — تحت EMA30 وEMA50 فقط (بدون MA20)، والاستعادة فوق الاثنين معًا.
                     نسخة إنفوجرافيك واحد بالدليل (صفحة 82).
        """
        if ma_mode not in ("strict", "loose"):
            raise ValueError('ma_mode must be "strict" or "loose"')
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high
        self.rr_ratio = rr_ratio
        self.atr_mult = atr_mult
        self.sr_window = sr_window
        self.sr_lookback = sr_lookback
        self.confirm_window = confirm_window
        self.ma_mode = ma_mode

    def generate_signals(self, df: pd.DataFrame) -> list[TradeSignal]:
        pivot_high, pivot_low = pivot_support_resistance(df, window=self.sr_window)
        signals = []
        in_position = False
        stop_loss = None
        take_profit = None

        # الإحماء الفعلي المطلوب: MA50 (أطول متوسط نستخدمه هنا) + هامش نافذة التأكيد.
        # لا نستخدم SMA200 إطلاقًا بهذي الاستراتيجية، فلا داعي ننتظر 210 شمعة
        # زي TrendFollowStrategy — هذا كان يقصّي عمليًا أغلب الأسهم الصغيرة
        # حديثة الإدراج (تاريخ تداول قصير)، وهي بالضبط الفئة المستهدفة هنا.
        warmup = 50 + self.confirm_window
        for i in range(warmup, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]
            window = df.iloc[max(0, i - self.confirm_window):i + 1]

            # ضغط RSI 23-27 حصل مؤخرًا (مو بالضرورة اليوم بالضبط)
            rsi_compressed_recent = ((window["RSI"] >= self.rsi_low) &
                                      (window["RSI"] <= self.rsi_high)).any()

            w0 = window.iloc[0]
            if self.ma_mode == "strict":
                # النسخة الأكثر تكرارًا بالدليل: تحت الثلاث متوسطات، والاستعادة تبدأ من MA20
                was_below_mas = (w0["Close"] < w0["SMA20"] and w0["Close"] < w0["SMA30"]
                                  and w0["Close"] < w0["SMA50"])
                currently_reclaimed = row["Close"] > row["SMA20"]
                reclaimed_recent = ((window["Close"].shift(1) <= window["SMA20"].shift(1)) &
                                     (window["Close"] > window["SMA20"])).any()
            else:  # loose — نسخة صفحة 82 بالدليل: EMA30/EMA50 فقط
                was_below_mas = (w0["Close"] < w0["EMA30"] and w0["Close"] < w0["EMA50"])
                currently_reclaimed = row["Close"] > row["EMA30"] and row["Close"] > row["EMA50"]
                reclaimed_recent = ((window["Close"].shift(1) <= window["EMA50"].shift(1)) &
                                     (window["Close"] > window["EMA50"])).any()

            macd_improving = row["MACD_hist"] > window["MACD_hist"].iloc[0]

            # تقريب مبسّط لشمعة ارتداد (Hammer-like) — راجع التوثيق أعلاه
            body_low = min(row["Open"], row["Close"])
            lower_wick = body_low - row["Low"]
            upper_wick = row["High"] - max(row["Open"], row["Close"])
            reversal_candle = (row["Close"] > row["Open"]) and (lower_wick > upper_wick)

            volume_ok = row["Volume"] > row["VolAvg20"]

            # جديد: رفض "قفزة خبر بدون قاعدة" — ما فيه يوم بحركة متطرفة قبل يوم
            # الدخول نفسه (نستثني يوم الدخول لأنه المفترض يكون شمعة ارتداد حقيقية)
            prior_days = window.iloc[:-1]
            was_stable_before_entry = (
                prior_days["Close"].pct_change().abs().dropna() < 0.30
            ).all() if len(prior_days) > 1 else True

            # جديد: رفض "مطاردة شمعة انفجارية" — حركة يوم الدخول نفسه محدودة
            # نسبةً لتذبذب السهم الطبيعي (ATR)، لا قفزة ضخمة بعيدة عن القاعدة
            atr_for_chase_check = row["ATR"] if not pd.isna(row["ATR"]) else row["Close"] * 0.05
            entry_day_move = abs(row["Close"] - prev["Close"])
            not_chasing_entry_candle = entry_day_move <= 2.5 * atr_for_chase_check

            if (not in_position and rsi_compressed_recent and was_below_mas
                    and currently_reclaimed and reclaimed_recent
                    and macd_improving and reversal_candle and volume_ok
                    and was_stable_before_entry and not_chasing_entry_candle):
                support, resistance = nearest_support_resistance(
                    df, i, pivot_high, pivot_low, lookback=self.sr_lookback,
                    confirm_window=self.sr_window
                )
                entry_price = row["Close"]
                atr_val = row["ATR"] if not pd.isna(row["ATR"]) else entry_price * 0.03

                # المصدر: "الوقف تحت القاع أو تحت الدعم الحقيقي" — نفضّل الدعم المرصود
                if not pd.isna(support) and support < entry_price:
                    stop_loss = support - 0.01 * entry_price  # هامش صغير تحت الدعم
                else:
                    stop_loss = entry_price - self.atr_mult * atr_val

                if not pd.isna(resistance) and resistance > entry_price:
                    take_profit = resistance
                else:
                    risk = entry_price - stop_loss
                    take_profit = entry_price + risk * self.rr_ratio

                signals.append(TradeSignal(
                    date=row.name, side="BUY", price=entry_price,
                    stop_loss=stop_loss, take_profit=take_profit,
                    reason=f"ارتكاز ({self.ma_mode}): RSI 23-27 سابق + استعادة المتوسطات + تحسن MACD + شمعة ارتداد"
                ))
                in_position = True

            elif in_position:
                hit_stop = row["Low"] <= stop_loss
                hit_target = row["High"] >= take_profit
                exit_ma = row["SMA20"] if self.ma_mode == "strict" else row["EMA50"]
                exit_ma_prev = prev["SMA20"] if self.ma_mode == "strict" else prev["EMA50"]
                broke_support_again = row["Close"] < exit_ma < exit_ma_prev

                if hit_stop:
                    signals.append(TradeSignal(row.name, "SELL", stop_loss, reason="وقف خسارة"))
                    in_position = False
                elif hit_target:
                    signals.append(TradeSignal(row.name, "SELL", take_profit, reason="تحقيق الهدف"))
                    in_position = False
                elif broke_support_again:
                    signals.append(TradeSignal(row.name, "SELL", row["Close"],
                                                reason="بطل سبب الصفقة - كسر المتوسط مجددًا"))
                    in_position = False

        return signals


class LiquiditySweepStrategy:
    """
    "سحب السيولة" — من الدليل (القسم 9، صفحة 16-17).

    مأخوذ حرفيًا من المصدر:
    - كسر بسيط لدعم معروف (لضرب وقف الخسائر)، ثم استرداد سريع فوق الدعم
    - "الكسر الذي لا يسترد الدعم = فشل" — لازم إغلاق فوق الدعم لاعتبارها سحب سيولة ناجح
    - الوقف تحت قاع السحب بالضبط (نص صريح بالدليل، مو تقدير)

    ملاحظة منهجية: المصدر يصف هذا كنمط تنفيذ لحظي داخل اليوم (Level 2 + شموع دقيقة).
    هنا مطبّق على بيانات يومية (Daily OHLC) كتقريب — "الكسر" هو Low اليوم تحت الدعم،
    و"الاسترداد" هو إغلاق نفس اليوم أو اليوم التالي فوقه. هذا أخشن من التنفيذ
    الحقيقي للمنهج، فالنتائج هنا تقيس الفكرة العامة لا الدقة اللحظية.
    """

    def __init__(self, rr_ratio: float = 2.0, sr_window: int = 10,
                 sr_lookback: int = 150, reclaim_window: int = 2):
        self.rr_ratio = rr_ratio
        self.sr_window = sr_window
        self.sr_lookback = sr_lookback
        self.reclaim_window = reclaim_window  # كم يوم مسموح للاسترداد بعد الكسر

    def generate_signals(self, df: pd.DataFrame) -> list[TradeSignal]:
        pivot_high, pivot_low = pivot_support_resistance(df, window=self.sr_window)
        signals = []
        in_position = False
        stop_loss = None
        take_profit = None
        pending_sweep_low = None
        pending_sweep_idx = None
        pending_support_level = None

        for i in range(60, len(df)):
            row = df.iloc[i]

            if not in_position and pending_sweep_low is None:
                # الدعم يُحسب من سياق الأمس (قبل أي كسر اليوم)، لا من إغلاق اليوم نفسه —
                # لأن إغلاق اليوم أثناء الكسر يكون أصلاً تحت الدعم، فلن يجد الدالة أي
                # مستوى "دعم أسفل السعر الحالي" ويرجع NaN. هذا كان خطأ فعليًا بالنسخة الأولى.
                support, _ = nearest_support_resistance(
                    df, i - 1, pivot_high, pivot_low, lookback=self.sr_lookback,
                    confirm_window=self.sr_window
                )
                if not pd.isna(support):
                    broke_support = row["Low"] < support
                    closed_below = row["Close"] < support
                    if broke_support and closed_below:
                        pending_sweep_low = row["Low"]
                        pending_sweep_idx = i
                        pending_support_level = support  # نثبّته؛ لا نعيد حسابه لاحقًا

            elif not in_position and pending_sweep_low is not None:
                days_since = i - pending_sweep_idx
                reclaimed = row["Close"] > pending_support_level

                # نفس مبدأ "عدم المطاردة" من صفحة 16 بالدليل (وهي الصفحة الأصلية
                # اللي بُنيت عليها استراتيجية سحب السيولة تحديدًا) — لا ندخل لو
                # يوم الاسترداد نفسه كان قفزة ضخمة بعيدة عن منطقة الطلب
                prev_row = df.iloc[i - 1]
                atr_for_chase_check = row["ATR"] if not pd.isna(row["ATR"]) else row["Close"] * 0.05
                not_chasing = abs(row["Close"] - prev_row["Close"]) <= 2.5 * atr_for_chase_check

                if reclaimed and days_since <= self.reclaim_window and not_chasing:
                    entry_price = row["Close"]
                    # نص الدليل: "الوقف تحت قاع السحب" — تحت وليس عنده بالضبط
                    stop_loss = pending_sweep_low * (1 - 0.005)
                    _, resistance = nearest_support_resistance(
                        df, i, pivot_high, pivot_low, lookback=self.sr_lookback,
                        confirm_window=self.sr_window
                    )
                    if not pd.isna(resistance) and resistance > entry_price:
                        take_profit = resistance
                    else:
                        risk = entry_price - stop_loss
                        take_profit = entry_price + risk * self.rr_ratio

                    signals.append(TradeSignal(
                        date=row.name, side="BUY", price=entry_price,
                        stop_loss=stop_loss, take_profit=take_profit,
                        reason="سحب سيولة: كسر دعم ثم استرداد خلال يومين"
                    ))
                    in_position = True
                    pending_sweep_low = pending_sweep_idx = pending_support_level = None

                elif days_since > self.reclaim_window:
                    # فشل الاسترداد بالوقت المحدد = "ضعف" حسب نص الدليل، نلغي المحاولة
                    pending_sweep_low = pending_sweep_idx = pending_support_level = None

            elif in_position:
                hit_stop = row["Low"] <= stop_loss
                hit_target = row["High"] >= take_profit

                if hit_stop:
                    signals.append(TradeSignal(row.name, "SELL", stop_loss, reason="وقف خسارة"))
                    in_position = False
                elif hit_target:
                    signals.append(TradeSignal(row.name, "SELL", take_profit, reason="تحقيق الهدف"))
                    in_position = False

        return signals
