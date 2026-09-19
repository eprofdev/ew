"""تشغيل الـ backtest على كون كامل من الأسهم.

    # 1) تقدير التكلفة أولاً — لا تشغّل شيئاً يستغرق أياماً دون أن تعرف
    python -m trading_bot.portfolio_cli --exchange NASDAQ --dry-run

    # 2) بناء قائمة الرموز وحفظها
    python -m trading_bot.portfolio_cli --exchange NASDAQ --build-universe data/universe.json

    # 3) التشغيل مع استئناف تلقائي (آمن للإيقاف والمتابعة)
    python -m trading_bot.portfolio_cli --universe data/universe.json \\
        --checkpoint data/run1.jsonl --rate-limit 8 --research-mode

    # 4) إعادة التحليل من الذاكرة المؤقتة دون أي طلب شبكة
    python -m trading_bot.portfolio_cli --universe data/universe.json --offline
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from .backtest import BacktestConfig
from .config import BotConfig
from .portfolio import PortfolioBacktester, estimate_cost
from .universe import SURVIVORSHIP_WARNING, Universe, fetch_universe


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m trading_bot.portfolio_cli",
        description="اختبار الاستراتيجية على كون كامل من الأسهم",
    )
    src = p.add_argument_group("مصدر الرموز")
    src.add_argument("--exchange", default="NASDAQ", help="البورصة (NASDAQ, NYSE, ...)")
    src.add_argument("--country", default="United States")
    src.add_argument("--universe", help="ملف كون محفوظ مسبقاً")
    src.add_argument("--build-universe", help="اجلب الكون واحفظه في هذا المسار ثم توقف")
    src.add_argument("--symbols", nargs="*", default=[], help="رموز محددة بدل الكون")
    src.add_argument("--extra-symbols", nargs="*", default=[],
                     help="رموز مشطوبة تُضاف يدوياً لتقليل انحياز البقاء")
    src.add_argument("--limit", type=int, help="اكتفِ بأول N رمزاً")
    src.add_argument("--offset", type=int, default=0, help="ابدأ من الرمز رقم N")

    run = p.add_argument_group("التشغيل")
    run.add_argument("--api-key", default=os.environ.get("TWELVE_DATA_API_KEY"))
    run.add_argument("--rate-limit", type=int, default=8, help="طلبات/دقيقة")
    run.add_argument("--cache-dir", default="data/cache")
    run.add_argument("--checkpoint", help="ملف استئناف (JSONL)")
    run.add_argument("--no-resume", action="store_true")
    run.add_argument("--offline", action="store_true", help="من الذاكرة المؤقتة فقط")
    run.add_argument("--dry-run", action="store_true", help="اعرض التكلفة ولا تشغّل")
    run.add_argument("--stop-after-failures", type=int, default=25,
                     help="أوقف بعد N إخفاقاً متتالياً (0 = لا توقف)")
    run.add_argument("--bars-4h", type=int, default=1000)
    run.add_argument("--bars-1d", type=int, default=400)

    strat = p.add_argument_group("الاستراتيجية")
    strat.add_argument("--equity", type=float, default=10_000.0)
    strat.add_argument("--risk-pct", type=float, default=1.0)
    strat.add_argument("--fundamentals", help="ملف JSON بالكاب والفلوت لكل رمز")
    strat.add_argument("--research-mode", action="store_true",
                       help="عطّل فلتر الهيكل المالي (يُعلن في التقرير)")
    strat.add_argument("--max-concurrent", type=int, default=5,
                       help="سقف الصفقات المتزامنة في محاكاة المحفظة")

    out = p.add_argument_group("المخرجات")
    out.add_argument("--report", help="احفظ التقرير الكامل JSON هنا")
    out.add_argument("--top", type=int, default=10)
    out.add_argument("--quiet", action="store_true")
    return p


def _resolve_symbols(args, provider) -> List[str]:
    if args.symbols:
        return [s.upper() for s in args.symbols]
    if args.universe:
        universe = Universe.load(args.universe)
    else:
        if provider is None:
            raise SystemExit("لجلب الكون تحتاج مفتاح API، أو مرّر --universe / --symbols")
        universe = fetch_universe(
            provider, exchange=args.exchange, country=args.country,
            extra_symbols=args.extra_symbols,
        )
    symbols = universe.filter(country=args.country).symbols
    symbols = symbols[args.offset:]
    return symbols[: args.limit] if args.limit else symbols


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    provider = None
    if not args.offline and args.api_key:
        from .providers.twelve_data import TwelveDataProvider

        provider = TwelveDataProvider(
            api_key=args.api_key, max_requests_per_minute=args.rate_limit
        )

    if args.build_universe:
        if provider is None:
            print("بناء الكون يحتاج مفتاح API", file=sys.stderr)
            return 2
        universe = fetch_universe(
            provider, exchange=args.exchange, country=args.country,
            extra_symbols=args.extra_symbols,
        )
        path = universe.save(args.build_universe)
        common = universe.filter(country=args.country)
        print(f"الكون: {len(universe)} رمزاً ({len(common)} سهماً عادياً) → {path}")
        print(f"⚠ {SURVIVORSHIP_WARNING}")
        return 0

    symbols = _resolve_symbols(args, provider)
    if not symbols:
        print("لا توجد رموز للاختبار", file=sys.stderr)
        return 2

    cost = estimate_cost(
        len(symbols), requests_per_minute=args.rate_limit, bars_per_symbol=args.bars_4h
    )
    print(f"الرموز: {len(symbols):,} | الطلبات: {cost['requests']:,} | "
          f"جلب البيانات: {cost['hours']:.1f} ساعة @ {args.rate_limit}/دقيقة | "
          f"الحساب: {cost['compute_hours']:.1f} ساعة | العنق: {cost['bottleneck']}")
    if cost["days_by_daily_limit"] > 1 and not args.offline:
        print(f"⚠ بحد يومي 800 طلب يستغرق هذا {cost['days_by_daily_limit']:.1f} يوماً — "
              "استخدم --limit و --checkpoint، أو ارقِ الخطة")
    print(f"⚠ {SURVIVORSHIP_WARNING}")
    if args.dry_run:
        print("(dry-run — لم يُنفَّذ شيء)")
        return 0

    fundamentals = json.loads(Path(args.fundamentals).read_text("utf-8")) if args.fundamentals else {}
    runner = PortfolioBacktester(
        provider=provider,
        bot_config=BotConfig(
            account_equity=args.equity,
            risk_per_trade_pct=args.risk_pct / 100,
            require_fundamentals=not args.research_mode,
        ),
        backtest_config=BacktestConfig(),
        cache_dir=args.cache_dir,
        bars_4h=args.bars_4h,
        bars_1d=args.bars_1d,
        fundamentals=fundamentals,
    )

    def progress(index: int, total: int, outcome) -> None:
        if args.quiet:
            return
        if index % 25 == 0 or outcome.trades:
            status = outcome.error or f"{outcome.trades} صفقة"
            print(f"  [{index}/{total}] {outcome.symbol}: {status}", flush=True)

    result = runner.run(
        symbols,
        checkpoint=args.checkpoint,
        resume=not args.no_resume,
        progress=progress,
        stop_after_failures=args.stop_after_failures,
    )

    print()
    print(result.summary(top=args.top))

    sim = result.simulate_portfolio(
        starting_equity=args.equity,
        risk_per_trade_pct=args.risk_pct / 100,
        max_concurrent=args.max_concurrent,
    )
    print()
    print("─" * 70)
    print(f"محاكاة محفظة بسقف {sim['max_concurrent']} صفقات متزامنة:")
    print(f"  نُفِّذت {sim['trades_taken']} صفقة، وتُخطّيت {sim['trades_skipped_full']} لامتلاء الفتحات")
    print(f"  رأس المال: {sim['starting_equity']:,.0f} ← {sim['ending_equity']:,.0f} "
          f"({sim['return_pct']:+.1%})")
    print(f"  أقصى تراجع: {sim['max_drawdown_pct']:.1%}")

    if args.report:
        payload = {
            "symbols_tested": result.symbols_tested,
            "symbols_failed": len(result.symbols_failed),
            "symbols_with_trades": result.symbols_with_trades,
            "total_trades": result.total_trades,
            "win_rate": round(result.win_rate, 4),
            "expectancy_r": round(result.expectancy_r, 4),
            "expectancy_ci": result.expectancy_ci(),
            "profit_factor": None if result.profit_factor == float("inf") else round(result.profit_factor, 4),
            "statistically_usable": result.is_statistically_usable,
            "research_mode": result.research_mode,
            "funnel": result.aggregate_funnel(),
            "vetoes": result.aggregate_vetoes(),
            "portfolio": {k: v for k, v in sim.items() if k != "curve"},
            "survivorship_warning": SURVIVORSHIP_WARNING,
            "per_symbol": [o.to_json() for o in result.outcomes if o.trades > 0],
        }
        Path(args.report).write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        print(f"\nالتقرير الكامل: {args.report}")

    print("\nتذكير: نتائج تاريخية على فرضيات محددة — ليست وعداً بأداء مستقبلي.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
