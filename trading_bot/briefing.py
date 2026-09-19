"""الموجز اليومي: النظام مُرشِّح لا قرار.

هذا هو المسار الذي اختير بعد أن أثبت الاختبار أن الاستراتيجية الآلية بلا
حافة: لا يُصدر النظام أوامر، بل يضع أمامك في كل صباح قائمة قصيرة مرتّبة،
ومعها السياق الذي يلزم لاتخاذ القرار — والقرار قرارك.

ثلاثة مبادئ في التصميم:

1) **لا يتظاهر بما لا يعرف.** خطة البيانات تعطي السعر والفوليوم فقط: لا
   أخبار، ولا إفصاحات، ولا فلوت. فبدل تجاهل ذلك، يضع الموجز قائمة صريحة
   بما يجب أن تتحقق منه بنفسك — وهي غالباً ما يحرّك السهم فعلاً (حالة
   AEMD: اندماج وتجزئة عكسية وطرح تمويلي، ولا شيء منها في بيانات السعر).

2) **الفلاتر تصبح تحذيرات لا أحكاماً.** ما دام القرار بشرياً، فحجب المعلومة
   عنك خطأ. الرفض يُعرَض مع سببه وتقرر أنت.

3) **الانضباط الذي نجا من الاختبار يبقى ملزماً**: فلتر السيولة أثبت أنه
   حماية لا قيد (تخفيفه أنزل التوقع)، وملاحقة القفزات أسوأ ما في النتائج.
   هذان يُعرضان كتحذير أحمر لا كملاحظة.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from .config import BotConfig
from .indicators import atr, average_volume, closes, rsi, sma
from .models import Decision, MarketSnapshot, Signal
from .risk import RiskManager
from .screener import Screener, ScreenerConfig
from .strategies.faisal_price_action import FaisalPriceAction
from .strategies.momentum_breakout import MomentumBreakout

# ما لا تراه بيانات السعر — وهو غالباً سبب الحركة الحقيقي
BLIND_SPOTS = (
    "سبب الحركة: خبر؟ اندماج؟ نتائج؟ (ابحث في أخبار الشركة اليوم)",
    "إفصاحات SEC: طرح أسهم فعّال (S-1/S-3/ATM) أو تجزئة عكسية قادمة",
    "الفلوت الحقيقي ونسبة الشورت (خطة البيانات الحالية لا تعطيهما)",
    "دفتر الأسعار: حجم الطلب الحقيقي عند سعر دخولك",
)


@dataclass
class BriefingItem:
    ticker: str
    price: float = 0.0
    change_pct: float = 0.0
    rvol: float = 0.0
    range_position: Optional[float] = None
    rsi_daily: Optional[float] = None
    rsi_4h: Optional[float] = None
    support: Optional[str] = None
    breakout_level: Optional[float] = None
    suggested_stop: Optional[float] = None
    risk_per_share: Optional[float] = None
    shares_at_1pct: int = 0
    decision: str = ""
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    red_flags: List[str] = field(default_factory=list)
    score: float = 0.0


@dataclass
class DailyBriefing:
    date: str = ""
    scanned: int = 0
    items: List[BriefingItem] = field(default_factory=list)
    rejected: List[tuple] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_markdown(self, top: int = 15) -> str:
        lines = [
            f"# الموجز اليومي — {self.date}",
            "",
            "> **هذا موجز قرار لا توصية.** الاختبار على 656 صفقة أثبت أن التنفيذ",
            "> الآلي لهذه الشروط بلا حافة. القيمة هنا في تقليص السوق إلى قائمة",
            "> قصيرة تفحصها بنفسك — والقرار قرارك وحدك.",
            "",
            f"فُحص **{self.scanned}** رمزاً · مرشحون: **{len(self.items)}**",
            "",
        ]

        if not self.items:
            lines += ["## لا مرشحين اليوم", "",
                      "لا شيء يستوفي الشروط. يوم بلا صفقة قرار صحيح أيضاً.", ""]
        else:
            lines.append("## المرشحون")
            lines.append("")
            for index, item in enumerate(self.items[:top], start=1):
                lines.extend(self._item_block(index, item))

        if self.rejected:
            lines += ["## استُبعدوا (للاطلاع لا للتجاهل)", ""]
            for ticker, reason in self.rejected[:8]:
                lines.append(f"- **{ticker}** — {reason}")
            lines.append("")

        lines += [
            "## قبل أي دخول — تحقق بنفسك",
            "",
            *[f"- [ ] {item}" for item in BLIND_SPOTS],
            "",
            "## انضباط نجا من الاختبار",
            "",
            "- **لا تلاحق قفزة**: أسوأ نتائج الاختبار جاءت من الدخول بعد ارتفاع كبير.",
            "- **لا تخفّف فلتر السيولة**: تخفيفه أنزل التوقع من +0.100R إلى −0.017R.",
            "- **لا تشترِ من العرض**: ضع أمراً محدداً على الطلب ولو فاتتك الصفقة.",
            "- **حجم واحد لكل صفقة**: 1% من رأس المال مخاطرةً، بلا استثناء.",
            "",
        ]
        if self.notes:
            lines += ["---", ""] + [f"_{note}_" for note in self.notes]
        return "\n".join(lines)

    @staticmethod
    def _item_block(index: int, item: BriefingItem) -> List[str]:
        out = [f"### {index}. {item.ticker} — {item.price:.4f}$ ({item.change_pct:+.1%})", ""]

        context = [f"فوليوم نسبي **{item.rvol:.1f}×**"]
        if item.range_position is not None:
            context.append(f"الموقع من مدى 52 أسبوعاً **{item.range_position:.0%}**")
        if item.rsi_daily is not None:
            context.append(f"RSI يومي {item.rsi_daily:.0f}")
        if item.rsi_4h is not None:
            context.append(f"4 ساعات {item.rsi_4h:.0f}")
        out.append("**السياق**: " + " · ".join(context))

        structure = []
        if item.support:
            structure.append(f"دعم {item.support}")
        if item.breakout_level:
            structure.append(f"قمة 20 شمعة {item.breakout_level:.4f}")
        if structure:
            out.append("**البنية**: " + " · ".join(structure))

        if item.suggested_stop and item.risk_per_share:
            distance = item.risk_per_share / item.price if item.price else 0
            out.append(
                f"**لو دخلت**: وقف مقترح {item.suggested_stop:.4f} (−{distance:.1%}) · "
                f"1R = {item.risk_per_share:.4f}$ · بمخاطرة 1% ⇒ **{item.shares_at_1pct:,} سهم**"
            )

        if item.decision:
            out.append(f"**قراءة النظام**: {item.decision}")
        for reason in item.reasons[:4]:
            out.append(f"  - ✔ {reason}")
        for flag in item.red_flags:
            out.append(f"  - 🛑 {flag}")
        for warning in item.warnings[:4]:
            out.append(f"  - ⚠ {warning}")
        out.append("")
        return out


class BriefingBuilder:
    """يبني الموجز من لقطات السوق — بلا أي قرار تنفيذي."""

    def __init__(self, config: Optional[BotConfig] = None) -> None:
        self.config = config or BotConfig()
        # المسار البشري: الفلوت المجهول تحذير لا حكم — حجب المعلومة عن
        # من سيقرر بنفسه خطأ، وإظهارها مع علامة الجهل هو الصواب.
        self.screener = Screener(ScreenerConfig(require_known_float=False))
        self.bounce = FaisalPriceAction(self.config)
        self.breakout = MomentumBreakout(self.config)
        self.risk = RiskManager(self.config)

    def build(self, snapshots: Sequence[MarketSnapshot], signals: Optional[dict] = None) -> DailyBriefing:
        briefing = DailyBriefing(
            date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            scanned=len(snapshots),
        )
        signals = signals or {}
        items: List[BriefingItem] = []

        for snapshot in snapshots:
            candidate = self.screener.score(snapshot)
            if not candidate.passed:
                briefing.rejected.append((snapshot.ticker, candidate.rejected))
                continue
            items.append(self._item(snapshot, candidate, signals.get(snapshot.ticker)))

        items.sort(key=lambda i: i.score, reverse=True)
        briefing.items = items
        return briefing

    def _item(self, snapshot: MarketSnapshot, candidate, signal: Optional[Signal]) -> BriefingItem:
        rt = snapshot.realtime
        item = BriefingItem(
            ticker=snapshot.ticker,
            price=rt.price,
            change_pct=rt.premarket_change_pct,
            rvol=candidate.rvol,
            range_position=self.screener._range_position(snapshot),
            score=candidate.score,
        )

        c4, c1 = list(snapshot.candles_4h), list(snapshot.candles_1d)
        if len(c1) > self.config.rsi_period:
            item.rsi_daily = rsi(closes(c1), self.config.rsi_period)
        if len(c4) > self.config.rsi_period:
            item.rsi_4h = rsi(closes(c4), self.config.rsi_period)

        setup = self.bounce.analyze(c4)
        if setup.zone:
            item.support = f"{setup.zone.low:.4f}–{setup.zone.high:.4f} ({setup.zone.touches} لمسات)"
        if len(c4) > 21:
            item.breakout_level = max(c.high for c in c4[-21:-1])

        atr_value = atr(c4, min(14, max(len(c4) - 1, 2)))
        reference = setup.stop_reference or (rt.price - (atr_value or rt.price * 0.05) * 2)
        plan = self.risk.build_plan(rt.price, reference, snapshot.metrics) if reference else None
        if plan:
            item.suggested_stop = plan.stop
            item.risk_per_share = round(plan.entry - plan.stop, 4)
            item.shares_at_1pct = plan.size

        item.reasons = list(candidate.reasons[:3]) + list(setup.reasons[:2])
        item.warnings = list(candidate.penalties)
        if signal is not None:
            item.decision = signal.decision.value
            item.red_flags = list(signal.vetoes)
            if signal.decision is Decision.BUY_CONFIRMED:
                item.reasons.insert(0, "الشروط السعرية مكتملة")
        if rt.spread_pct > 0.03:
            item.warnings.append(f"فرق طلب/عرض {rt.spread_pct:.1%} — الدخول والخروج مكلفان")
        if rt.premarket_change_pct > 0.25:
            item.red_flags.append(
                f"ارتفع {rt.premarket_change_pct:.0%} بالفعل — الملاحقة أسوأ ما في نتائج الاختبار"
            )
        return item
