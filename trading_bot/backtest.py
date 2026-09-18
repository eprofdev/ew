"""محرك backtest سيري (walk-forward) يقيس أداء الاستراتيجية فعلياً.

مبادئ ملتزم بها حرفياً — وبدونها تكون الأرقام كذباً مريحاً:

1) **لا استشراف للمستقبل (No lookahead).** عند الشمعة i يرى البوت
   `candles_4h[:i+1]` فقط، والشموع اليومية **المكتملة قبل يوم الشمعة** —
   لأن شمعة اليوم اليومية تحتوي إغلاق اليوم الذي لم يحدث بعد.
2) **الدخول في الشمعة التالية.** الإشارة تولد عند إغلاق الشمعة i، والأمر
   المحدد يُنفَّذ في الشمعة i+1 وفقط إذا لمس السعر حد الأمر فعلاً.
3) **الوقف قبل الهدف.** إذا لمست الشمعة الوقف والهدف معاً نفترض الوقف —
   لأن ترتيب الحركة داخل الشمعة مجهول. الافتراض الأسوأ هو الافتراض الصادق.
4) **الفجوات تُنفَّذ على الافتتاح.** لو فتحت الشمعة تحت الوقف نخرج على
   الافتتاح لا على الوقف — وهذا ما يحدث فعلاً في أسهم السنتات.
5) **العمولة والانزلاق يُخصمان من كل صفقة.**

القيود المعروفة (مذكورة في التقرير، لا تُخفى):
  • لا يوجد تاريخ Bid/Ask ⇒ يُفترض فرق ثابت (`assumed_spread_pct`).
  • لا يوجد تاريخ لنسب الشورت ⇒ مضاعِف تقفيل الشورت معطّل في الاختبار.
  • لا يُحاكى التعليق (Halt) ولا قاعدة SSR ولا حدود السيولة اللحظية.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Dict, List, Optional, Sequence, Tuple

from .config import BotConfig
from .dataquality import check_series
from .engine import EnhancedTradingBot
from .indicators import average_volume
from .models import Candle, Decision, MarketSnapshot, RealTimeData, StockMetrics


@dataclass
class BacktestConfig:
    assumed_spread_pct: float = 0.02      # فرضية الفرق بين الطلب والعرض
    slippage_pct: float = 0.005           # انزلاق على الخروج
    commission_per_share: float = 0.0
    commission_min: float = 0.0
    order_ttl_bars: int = 1               # عمر الأمر المحدد إن لم يُنفَّذ
    max_holding_bars: int = 30            # خروج زمني إجباري
    scale_out: Tuple[float, ...] = (0.5, 0.25, 0.25)   # نسب البيع عند الأهداف
    breakeven_after_first_target: bool = True
    warmup_bars: int = 40                 # شموع 4H لازمة قبل أول قرار


@dataclass
class Fill:
    ts: int
    price: float
    shares: int
    reason: str


@dataclass
class Trade:
    ticker: str
    entry_ts: int
    entry: float
    stop: float
    targets: List[float]
    shares: int
    exits: List[Fill] = field(default_factory=list)
    bars_held: int = 0
    commission: float = 0.0
    score: float = 0.0

    @property
    def risk_per_share(self) -> float:
        return max(self.entry - self.stop, 1e-9)

    @property
    def exited_shares(self) -> int:
        return sum(f.shares for f in self.exits)

    @property
    def is_open(self) -> bool:
        return self.exited_shares < self.shares

    @property
    def pnl(self) -> float:
        gross = sum((f.price - self.entry) * f.shares for f in self.exits)
        return gross - self.commission

    @property
    def r_multiple(self) -> float:
        """الربح مقاساً بوحدات المخاطرة (R) — المقياس الوحيد القابل للمقارنة."""
        return self.pnl / (self.risk_per_share * self.shares) if self.shares else 0.0

    @property
    def exit_reason(self) -> str:
        return self.exits[-1].reason if self.exits else "open"


@dataclass
class BacktestResult:
    ticker: str
    trades: List[Trade] = field(default_factory=list)
    bars_tested: int = 0
    signals_seen: int = 0
    orders_placed: int = 0
    orders_unfilled: int = 0
    veto_counts: Dict[str, int] = field(default_factory=dict)
    funnel: Dict[str, int] = field(default_factory=dict)   # أين تموت الإشارات
    warnings: List[str] = field(default_factory=list)
    starting_equity: float = 0.0

    # ── مقاييس الأداء ────────────────────────────────────────────────
    @property
    def closed(self) -> List[Trade]:
        return [t for t in self.trades if not t.is_open]

    @property
    def wins(self) -> List[Trade]:
        return [t for t in self.closed if t.pnl > 0]

    @property
    def losses(self) -> List[Trade]:
        return [t for t in self.closed if t.pnl <= 0]

    @property
    def win_rate(self) -> float:
        return len(self.wins) / len(self.closed) if self.closed else 0.0

    @property
    def expectancy_r(self) -> float:
        """متوسط R لكل صفقة — الرقم الذي يقرر إن كانت الاستراتيجية تستحق."""
        return mean(t.r_multiple for t in self.closed) if self.closed else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.wins)
        pains = abs(sum(t.pnl for t in self.losses))
        if pains == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / pains

    @property
    def net_pnl(self) -> float:
        return sum(t.pnl for t in self.closed)

    @property
    def return_pct(self) -> float:
        return self.net_pnl / self.starting_equity if self.starting_equity else 0.0

    @property
    def equity_curve(self) -> List[float]:
        equity, curve = self.starting_equity, [self.starting_equity]
        for trade in sorted(self.closed, key=lambda t: t.exits[-1].ts):
            equity += trade.pnl
            curve.append(equity)
        return curve

    @property
    def max_drawdown_pct(self) -> float:
        """أقصى تراجع على منحنى الصفقات المغلقة."""
        peak, worst = self.starting_equity, 0.0
        for value in self.equity_curve:
            peak = max(peak, value)
            if peak > 0:
                worst = max(worst, (peak - value) / peak)
        return worst

    @property
    def avg_bars_held(self) -> float:
        return mean(t.bars_held for t in self.closed) if self.closed else 0.0

    # ── كفاية العينة: الفرق بين «نتيجة» و«صدفة» ──────────────────────
    MIN_TRADES_FOR_INFERENCE = 30

    @property
    def is_statistically_usable(self) -> bool:
        return len(self.closed) >= self.MIN_TRADES_FOR_INFERENCE

    def expectancy_ci(self, confidence: float = 0.95, resamples: int = 2000,
                      seed: int = 7) -> Optional[Tuple[float, float]]:
        """مجال ثقة للتوقع بإعادة العينة (Bootstrap) — بلا مكتبات خارجية.

        إن كان المجال يشمل الصفر فالاستراتيجية **لم تثبت** أي أفضلية، مهما
        بدا متوسط الـ R جذاباً.
        """
        values = [t.r_multiple for t in self.closed]
        if len(values) < 2:
            return None
        rng = random.Random(seed)
        means = sorted(
            mean(rng.choice(values) for _ in values) for _ in range(resamples)
        )
        lower = means[int((1 - confidence) / 2 * resamples)]
        upper = means[int((1 + confidence) / 2 * resamples) - 1]
        return lower, upper

    @property
    def r_stdev(self) -> float:
        values = [t.r_multiple for t in self.closed]
        return pstdev(values) if len(values) > 1 else 0.0

    def trades_needed_for_inference(self, target_precision: float = 0.25) -> Optional[int]:
        """كم صفقة تلزم لقياس التوقع بدقة ±target_precision من R."""
        if self.r_stdev <= 0:
            return None
        return max(int((1.96 * self.r_stdev / target_precision) ** 2), self.MIN_TRADES_FOR_INFERENCE)

    def summary(self) -> str:
        lines = [
            "═" * 60,
            f"نتيجة الاختبار: {self.ticker}",
            "═" * 60,
            f"  شموع مختبرة        : {self.bars_tested}",
            f"  إشارات شراء        : {self.signals_seen}",
            f"  أوامر منفّذة        : {len(self.trades)} (لم تُنفَّذ: {self.orders_unfilled})",
            f"  صفقات مغلقة        : {len(self.closed)}",
        ]
        if self.closed:
            lines += [
                f"  نسبة الربح         : {self.win_rate:.0%} ({len(self.wins)}W / {len(self.losses)}L)",
                f"  التوقع لكل صفقة    : {self.expectancy_r:+.2f}R",
                f"  عامل الربح         : {self.profit_factor:.2f}",
                f"  صافي الربح         : {self.net_pnl:+,.2f} ({self.return_pct:+.1%})",
                f"  أقصى تراجع         : {self.max_drawdown_pct:.1%}",
                f"  متوسط مدة الصفقة   : {self.avg_bars_held:.1f} شمعة",
            ]
            ci = self.expectancy_ci()
            if ci:
                lines.append(f"  مجال ثقة 95% للتوقع: [{ci[0]:+.2f}R , {ci[1]:+.2f}R]")
                if ci[0] <= 0 <= ci[1]:
                    lines.append("  ⛔ المجال يشمل الصفر — لم تثبت أي أفضلية إحصائية")
            if not self.is_statistically_usable:
                needed = self.trades_needed_for_inference()
                lines.append(
                    f"  ⛔ العينة {len(self.closed)} صفقة فقط — الحد الأدنى للاستنتاج "
                    f"{self.MIN_TRADES_FOR_INFERENCE}"
                    + (f"، والمطلوب لدقة ±0.25R نحو {needed} صفقة" if needed else "")
                )
        else:
            lines.append("  لا توجد صفقات مغلقة — لا يمكن استخراج أي حكم إحصائي")
        if self.funnel:
            lines.append("  قمع الفرص (أين تموت الإشارة):")
            order = ["vetoed", "no_setup", "setup_no_reversal", "rsi_rejected",
                     "low_score", "no_bid", "no_risk_plan", "confirmed"]
            for key in order:
                if self.funnel.get(key):
                    lines.append(f"      {key:<18} {self.funnel[key]:>6}")
        if self.veto_counts:
            top = sorted(self.veto_counts.items(), key=lambda kv: -kv[1])[:5]
            lines.append("  أكثر أسباب الرفض   : " + ", ".join(f"{k}×{v}" for k, v in top))
        for warning in self.warnings:
            lines.append(f"  ⚠ {warning}")
        return "\n".join(lines)


def _day_start(ts: int) -> int:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


class Backtester:
    """يشغّل `EnhancedTradingBot` شمعةً بشمعة على تاريخ حقيقي."""

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        backtest_config: Optional[BacktestConfig] = None,
    ) -> None:
        self.bot_config = bot_config or BotConfig()
        self.cfg = backtest_config or BacktestConfig()
        self.bot = EnhancedTradingBot(self.bot_config)

    # ── أدوات داخلية ─────────────────────────────────────────────────
    def _daily_visible(self, daily: Sequence[Candle], bar: Candle) -> List[Candle]:
        """الشموع اليومية المكتملة فقط — لا شمعة اليوم الجاري."""
        cutoff = _day_start(bar.ts)
        return [c for c in daily if c.ts < cutoff]

    def _commission(self, shares: int) -> float:
        return max(shares * self.cfg.commission_per_share, self.cfg.commission_min)

    def _snapshot(
        self,
        ticker: str,
        metrics: StockMetrics,
        history_4h: Sequence[Candle],
        daily: Sequence[Candle],
        prev_bar: Optional[Candle],
    ) -> MarketSnapshot:
        bar = history_4h[-1]
        half_spread = self.cfg.assumed_spread_pct / 2
        gap = 0.0
        if prev_bar is not None and prev_bar.close > 0 and _day_start(bar.ts) != _day_start(prev_bar.ts):
            gap = (bar.open - prev_bar.close) / prev_bar.close

        adv = average_volume(daily, min(20, len(daily))) if daily else None
        live_metrics = StockMetrics(
            ticker=metrics.ticker,
            market_cap=metrics.market_cap,
            free_float=metrics.free_float,
            shares_outstanding=metrics.shares_outstanding,
            avg_daily_volume=adv or metrics.avg_daily_volume,
            is_otc=metrics.is_otc,
            has_options=metrics.has_options,
            has_active_shelf_offering=metrics.has_active_shelf_offering,
            recent_reverse_split_days=metrics.recent_reverse_split_days,
        )
        return MarketSnapshot(
            ticker=ticker,
            metrics=live_metrics,
            realtime=RealTimeData(
                ticker=ticker,
                price=bar.close,
                bid=round(bar.close * (1 - half_spread), 4),
                ask=round(bar.close * (1 + half_spread), 4),
                session_volume=bar.volume,
                premarket_change_pct=gap,
                # لا تاريخ لنسب الشورت ⇒ مضاعِف التقفيل معطّل عمداً
            ),
            candles_4h=list(history_4h),
            candles_1d=list(daily),
        )

    @staticmethod
    def _classify(signal) -> str:
        """تحديد المرحلة التي توقفت عندها الإشارة — لضبط الفلاتر لاحقاً."""
        if signal.decision is Decision.VETO:
            return "vetoed"
        if signal.decision is Decision.BUY_CONFIRMED:
            return "confirmed"
        notes = " ".join(signal.notes)
        if "RSI" in notes:
            return "rsi_rejected"
        if "قوة الإعداد" in notes:
            return "low_score"
        if "ممنوع الشراء من العرض" in notes:
            return "no_bid"
        if "خطة مخاطرة" in notes:
            return "no_risk_plan"
        if "انعكاسية" in notes:
            return "setup_no_reversal"
        return "no_setup"

    # ── الحلقة الرئيسية ──────────────────────────────────────────────
    def run(
        self,
        ticker: str,
        candles_4h: Sequence[Candle],
        candles_1d: Sequence[Candle],
        metrics: StockMetrics,
    ) -> BacktestResult:
        result = BacktestResult(ticker=ticker, starting_equity=self.bot_config.account_equity)

        for name, series in (("4h", candles_4h), ("1d", candles_1d)):
            report = check_series(series, name)
            if not report.ok:
                result.warnings.extend(report.issues)

        if len(candles_4h) <= self.cfg.warmup_bars + 2:
            result.warnings.append("بيانات غير كافية للاختبار")
            return result

        open_trade: Optional[Trade] = None
        pending: Optional[dict] = None   # أمر محدد ينتظر التنفيذ
        stop_now: float = 0.0
        next_target = 0

        for i in range(self.cfg.warmup_bars, len(candles_4h)):
            bar = candles_4h[i]
            result.bars_tested += 1

            # ①  إدارة صفقة مفتوحة على شمعة اليوم
            if open_trade is not None:
                open_trade.bars_held += 1
                remaining = open_trade.shares - open_trade.exited_shares

                # فجوة تحت الوقف ⇒ خروج على الافتتاح (الواقع المر)
                if bar.open <= stop_now:
                    price = bar.open * (1 - self.cfg.slippage_pct)
                    open_trade.exits.append(Fill(bar.ts, price, remaining, "gap_stop"))
                    open_trade.commission += self._commission(remaining)
                    open_trade = None
                    continue

                # الوقف يسبق الهدف دائماً عند التعارض
                if bar.low <= stop_now:
                    price = stop_now * (1 - self.cfg.slippage_pct)
                    open_trade.exits.append(Fill(bar.ts, price, remaining, "stop"))
                    open_trade.commission += self._commission(remaining)
                    open_trade = None
                    continue

                # الأهداف بالتدريج
                while (
                    next_target < len(open_trade.targets)
                    and bar.high >= open_trade.targets[next_target]
                    and remaining > 0
                ):
                    portion = (
                        self.cfg.scale_out[next_target]
                        if next_target < len(self.cfg.scale_out)
                        else 1.0
                    )
                    shares = remaining if next_target == len(open_trade.targets) - 1 else int(open_trade.shares * portion)
                    shares = max(min(shares, remaining), 1)
                    open_trade.exits.append(
                        Fill(bar.ts, open_trade.targets[next_target], shares, f"target{next_target + 1}")
                    )
                    open_trade.commission += self._commission(shares)
                    remaining -= shares
                    if next_target == 0 and self.cfg.breakeven_after_first_target:
                        stop_now = max(stop_now, open_trade.entry)
                    next_target += 1

                if remaining <= 0:
                    open_trade = None
                    continue

                if open_trade.bars_held >= self.cfg.max_holding_bars:
                    price = bar.close * (1 - self.cfg.slippage_pct)
                    open_trade.exits.append(Fill(bar.ts, price, remaining, "time_exit"))
                    open_trade.commission += self._commission(remaining)
                    open_trade = None
                continue  # صفقة واحدة في كل مرة

            # ②  تنفيذ أمر محدد معلّق من الشمعة السابقة
            if pending is not None:
                limit = pending["limit"]
                if bar.low <= limit:                      # لمس السعر حد الأمر
                    trade = Trade(
                        ticker=ticker,
                        entry_ts=bar.ts,
                        entry=limit,
                        stop=pending["stop"],
                        targets=pending["targets"],
                        shares=pending["shares"],
                        score=pending["score"],
                    )
                    trade.commission += self._commission(trade.shares)
                    result.trades.append(trade)
                    open_trade, stop_now, next_target = trade, pending["stop"], 0
                    pending = None
                    continue
                pending["ttl"] -= 1
                if pending["ttl"] <= 0:
                    result.orders_unfilled += 1
                    pending = None

            # ③  توليد إشارة على إغلاق هذه الشمعة (للتنفيذ في التالية)
            history = candles_4h[: i + 1]
            daily = self._daily_visible(candles_1d, bar)
            snapshot = self._snapshot(ticker, metrics, history, daily, candles_4h[i - 1])
            signal = self.bot.evaluate(snapshot)

            for veto in signal.vetoes:
                code = veto.split(":", 1)[0]
                result.veto_counts[code] = result.veto_counts.get(code, 0) + 1

            stage = self._classify(signal)
            result.funnel[stage] = result.funnel.get(stage, 0) + 1

            if signal.decision is Decision.BUY_CONFIRMED and signal.position_size > 0:
                result.signals_seen += 1
                result.orders_placed += 1
                pending = {
                    "limit": signal.entry_limit,
                    "stop": signal.stop_loss,
                    "targets": list(signal.targets),
                    "shares": signal.position_size,
                    "score": signal.score,
                    "ttl": self.cfg.order_ttl_bars,
                }

        # إغلاق أي صفقة باقية على آخر سعر معلوم
        if open_trade is not None and open_trade.is_open:
            last = candles_4h[-1]
            remaining = open_trade.shares - open_trade.exited_shares
            open_trade.exits.append(Fill(last.ts, last.close, remaining, "end_of_data"))
            open_trade.commission += self._commission(remaining)

        return result
