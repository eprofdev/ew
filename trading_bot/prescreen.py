"""فلتر ترشيح مسبق: قلّص الكون **قبل** أن تنفق رصيداً على تاريخه.

الحقيقة الاقتصادية التي تحكم التصميم (مقيسة لا مفترضة): Twelve Data تحسب
الرصيد **لكل رمز لا لكل طلب** — طلب مجمّع لخمسة رموز استهلك خمسة أرصدة،
وحتى الطلب الفاشل يُخصم. إذن التجميع لا يوفّر شيئاً، والتوفير الوحيد
الحقيقي هو **ألا تسأل عن الرمز أصلاً**.

ثلاث مراحل مرتّبة بالتكلفة تصاعدياً — لا تنتقل للأغلى إلا بعد الأرخص:

    المرحلة 0 — صفر رصيد (طلب واحد للكون كله، تم مسبقاً)
        النوع، البورصة، **طبقة السوق**، واسم الشركة.
        أقوى فلتر مجاني: NASDAQ Capital Market (XNCM) هو موطن الشركات
        الصغيرة — 1,480 من 3,683 رمزاً، بينما XNGS موطن العمالقة.
        يقصّ 60% من الكون بلا رصيد واحد.

    المرحلة 1 — رصيد واحد لكل رمز (اقتباس)
        السعر، متوسط الفوليوم، الموقع من مدى 52 أسبوعاً، وحركة اليوم.
        يقصّ الباقي إلى عشرات.

    المرحلة 2 — رصيدان لكل رمز (تاريخ 4 ساعات + يومي)
        للناجين فقط، في `portfolio.py`.

بلا ترشيح: 2 × 3,683 = 7,366 رصيداً (9.2 يوم على الخطة المجانية).
مع الترشيح: 0 + 1,480 + 2 × الناجين ≈ 1,800 رصيداً (2.2 يوم).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .universe import Universe, UniverseEntry

# طبقات ناسداك: Capital Market موطن الشركات الصغيرة،
# Global Select موطن الكبرى، Global Market بينهما.
MIC_CAPITAL_MARKET = "XNCM"
MIC_GLOBAL_MARKET = "XNMS"
MIC_GLOBAL_SELECT = "XNGS"

# أنماط أسماء تدل على كيانات لا تصلح للسلوك السعري (شركات استحواذ فارغة،
# وحدات، وارنتس، صناديق) — استبعادها مجاني تماماً.
DEFAULT_NAME_EXCLUSIONS: Tuple[str, ...] = (
    "acquisition corp",
    "acquisition co",
    "acquisition holdings",
    "spac",
    " units",
    " unit)",
    "warrant",
    "right",
    " etf",
    "trust series",
    "index fund",
)


@dataclass
class PreScreenConfig:
    # ── المرحلة 0 (مجانية) ───────────────────────────────────────────
    allowed_mics: Tuple[str, ...] = (MIC_CAPITAL_MARKET,)
    allowed_types: Tuple[str, ...] = ("Common Stock",)
    country: Optional[str] = "United States"
    name_exclusions: Tuple[str, ...] = DEFAULT_NAME_EXCLUSIONS

    # ── المرحلة 1 (رصيد لكل رمز) ─────────────────────────────────────
    min_price: float = 0.50
    max_price: float = 5.00
    min_avg_volume: float = 300_000.0
    max_range_position: float = 0.60     # 0 = عند قاع 52 أسبوعاً
    max_today_change: float = 0.25       # لا تلاحق ما ارتفع 25% اليوم
    min_today_change: float = -0.50      # ولا ما انهار 50% (خبر كارثي)
    batch_size: int = 50                 # رموز لكل اتصال (لا يوفّر رصيداً)

    max_candidates: Optional[int] = None  # سقف أعلى للناجين


@dataclass
class PreScreenCandidate:
    symbol: str
    name: str = ""
    mic_code: str = ""
    price: float = 0.0
    avg_volume: float = 0.0
    today_change: float = 0.0
    range_position: Optional[float] = None
    rejected: Optional[str] = None
    stage: int = 0

    @property
    def passed(self) -> bool:
        return self.rejected is None

    def to_json(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "mic_code": self.mic_code,
            "price": self.price,
            "avg_volume": self.avg_volume,
            "today_change": round(self.today_change, 4),
            "range_position": round(self.range_position, 4) if self.range_position is not None else None,
            "rejected": self.rejected,
            "stage": self.stage,
        }


@dataclass
class PreScreenResult:
    universe_size: int = 0
    stage0_survivors: List[UniverseEntry] = field(default_factory=list)
    candidates: List[PreScreenCandidate] = field(default_factory=list)
    credits_used: int = 0
    credits_saved: int = 0
    elapsed: float = 0.0
    warnings: List[str] = field(default_factory=list)

    @property
    def passed(self) -> List[PreScreenCandidate]:
        return [c for c in self.candidates if c.passed]

    @property
    def symbols(self) -> List[str]:
        return [c.symbol for c in self.passed]

    def rejection_reasons(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for candidate in self.candidates:
            if candidate.rejected:
                key = candidate.rejected.split(":", 1)[0]
                counts[key] = counts.get(key, 0) + 1
        return counts

    def to_universe(self) -> Universe:
        entries = [
            UniverseEntry(symbol=c.symbol, name=c.name, exchange="NASDAQ",
                          mic_code=c.mic_code, type="Common Stock", country="United States")
            for c in self.passed
        ]
        return Universe(entries=entries, source="prescreen", fetched_at=time.time(),
                        notes=["كون مُرشَّح مسبقاً — انحياز البقاء ما زال قائماً"])

    def summary(self) -> str:
        stage0 = len(self.stage0_survivors)
        lines = [
            "═" * 66,
            "نتيجة الترشيح المسبق",
            "═" * 66,
            f"  الكون الأصلي            : {self.universe_size:,}",
            f"  بعد المرحلة 0 (مجانية)  : {stage0:,}"
            + (f"  ({stage0/self.universe_size:.0%})" if self.universe_size else ""),
            f"  بعد المرحلة 1 (اقتباس)  : {len(self.passed):,}",
            f"  الرصيد المستهلك         : {self.credits_used:,}",
            f"  الرصيد الموفَّر          : {self.credits_saved:,}",
            f"  الزمن                   : {self.elapsed/60:.1f} دقيقة",
        ]
        reasons = self.rejection_reasons()
        if reasons:
            lines.append("  أسباب الاستبعاد:")
            for key, value in sorted(reasons.items(), key=lambda kv: -kv[1]):
                lines.append(f"      {key:<22} {value:>7,}")
        return "\n".join(lines)


class PreScreener:
    """يقلّص الكون على مرحلتين قبل أن يلمس تاريخ السعر."""

    def __init__(self, config: Optional[PreScreenConfig] = None) -> None:
        self.config = config or PreScreenConfig()

    # ── المرحلة 0: مجانية تماماً ─────────────────────────────────────
    def stage0(self, universe: Universe) -> List[UniverseEntry]:
        cfg = self.config
        mics = {m.upper() for m in cfg.allowed_mics} if cfg.allowed_mics else None
        types = {t.lower() for t in cfg.allowed_types} if cfg.allowed_types else None

        kept: List[UniverseEntry] = []
        for entry in universe.entries:
            if types and entry.type.lower() not in types:
                continue
            if cfg.country and entry.country and entry.country != cfg.country:
                continue
            if mics and entry.mic_code.upper() not in mics:
                continue
            name = entry.name.lower()
            if any(pattern in name for pattern in cfg.name_exclusions):
                continue
            if not entry.symbol:
                continue
            kept.append(entry)
        return kept

    # ── المرحلة 1: رصيد واحد لكل رمز ─────────────────────────────────
    @staticmethod
    def _range_position(quote: dict, price: float) -> Optional[float]:
        """موقع السعر بين قاع وقمة 52 أسبوعاً (0 = عند القاع)."""
        window = quote.get("fifty_two_week") or {}
        low = quote.get("fifty_two_week_low", window.get("low"))
        high = quote.get("fifty_two_week_high", window.get("high"))
        try:
            low_f, high_f = float(low), float(high)
        except (TypeError, ValueError):
            return None
        if high_f <= low_f:
            return None
        return max(0.0, min((price - low_f) / (high_f - low_f), 1.0))

    def judge_quote(self, entry: UniverseEntry, quote: dict) -> PreScreenCandidate:
        cfg = self.config
        candidate = PreScreenCandidate(
            symbol=entry.symbol, name=entry.name, mic_code=entry.mic_code, stage=1
        )
        if not quote or "error" in quote:
            candidate.rejected = f"NO_QUOTE: {quote.get('error', 'لا اقتباس')}" if quote else "NO_QUOTE"
            return candidate

        def num(key, default=0.0):
            try:
                return float(quote.get(key))
            except (TypeError, ValueError):
                return default

        price = num("close")
        previous = num("previous_close")
        candidate.price = price
        candidate.avg_volume = num("average_volume")
        candidate.today_change = ((price - previous) / previous) if previous > 0 else 0.0
        candidate.range_position = self._range_position(quote, price)

        if price <= 0:
            candidate.rejected = "NO_PRICE: لا سعر صالح"
        elif not (cfg.min_price <= price <= cfg.max_price):
            candidate.rejected = f"PRICE: {price:.2f} خارج {cfg.min_price}–{cfg.max_price}"
        elif candidate.avg_volume < cfg.min_avg_volume:
            candidate.rejected = f"VOLUME: متوسط {candidate.avg_volume:,.0f} أقل من الحد"
        elif candidate.today_change > cfg.max_today_change:
            candidate.rejected = f"EXTENDED: ارتفع {candidate.today_change:.0%} اليوم"
        elif candidate.today_change < cfg.min_today_change:
            candidate.rejected = f"COLLAPSED: انهار {candidate.today_change:.0%} اليوم"
        elif (
            candidate.range_position is not None
            and candidate.range_position > cfg.max_range_position
        ):
            candidate.rejected = (
                f"RANGE: الموقع {candidate.range_position:.0%} من مدى 52 أسبوعاً (قريب من القمة)"
            )
        return candidate

    def stage1(
        self,
        provider,
        entries: Sequence[UniverseEntry],
        progress: Optional[Callable[[int, int, int], None]] = None,
    ) -> List[PreScreenCandidate]:
        cfg = self.config
        by_symbol = {e.symbol: e for e in entries}
        symbols = list(by_symbol)
        candidates: List[PreScreenCandidate] = []

        for start in range(0, len(symbols), max(cfg.batch_size, 1)):
            chunk = symbols[start : start + max(cfg.batch_size, 1)]
            quotes = provider.get_quotes(chunk)
            for symbol in chunk:
                candidates.append(self.judge_quote(by_symbol[symbol], quotes.get(symbol, {})))
            if progress:
                passed = sum(1 for c in candidates if c.passed)
                progress(min(start + len(chunk), len(symbols)), len(symbols), passed)
            if cfg.max_candidates and sum(1 for c in candidates if c.passed) >= cfg.max_candidates:
                break
        return candidates

    # ── التشغيل الكامل ───────────────────────────────────────────────
    def run(
        self,
        universe: Universe,
        provider=None,
        progress: Optional[Callable[[int, int, int], None]] = None,
    ) -> PreScreenResult:
        started = time.time()
        result = PreScreenResult(universe_size=len(universe))
        result.stage0_survivors = self.stage0(universe)

        if provider is None:
            result.candidates = [
                PreScreenCandidate(symbol=e.symbol, name=e.name, mic_code=e.mic_code, stage=0)
                for e in result.stage0_survivors
            ]
            result.warnings.append("بلا مزوّد: المرحلة 0 فقط — لم يُفحص أي سعر")
        else:
            before = getattr(provider, "credits_used", 0)
            result.candidates = self.stage1(provider, result.stage0_survivors, progress)
            result.credits_used = getattr(provider, "credits_used", 0) - before
            result.warnings.extend(getattr(provider, "warnings", [])[-3:])

        # لولا الترشيح لجلبنا تاريخ كل رمز في الكون (رصيدان لكل رمز)
        naive = result.universe_size * 2
        actual = result.credits_used + len(result.passed) * 2
        result.credits_saved = max(naive - actual, 0)
        result.elapsed = time.time() - started
        return result


def estimate_prescreen(universe_size: int, stage0_survivors: int, expected_pass_rate: float = 0.10) -> dict:
    """مقارنة صريحة بين الجلب الأعمى والترشيح المسبق."""
    naive = universe_size * 2
    survivors = int(stage0_survivors * expected_pass_rate)
    with_prescreen = stage0_survivors + survivors * 2
    return {
        "naive_credits": naive,
        "prescreen_credits": with_prescreen,
        "saved_credits": naive - with_prescreen,
        "saved_pct": (naive - with_prescreen) / naive if naive else 0.0,
        "expected_candidates": survivors,
        "naive_days_free_plan": naive / 800,
        "prescreen_days_free_plan": with_prescreen / 800,
    }
