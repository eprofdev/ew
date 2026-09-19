"""التشغيل الذاتي — دورة واحدة أو عمل مستمر بلا تدخل.

    export TWELVE_DATA_API_KEY=xxxx

    # دورة واحدة (هذا ما يستدعيه cron)
    python -m trading_bot.auto_cli --data-dir data/run1 --research-mode

    # عمل مستمر بلا توقف (ينام عند نفاد ميزانية اليوم)
    python -m trading_bot.auto_cli --data-dir data/run1 --research-mode --forever

    # الحالة والقرارات
    python -m trading_bot.auto_cli --data-dir data/run1 --status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from .auto import DEFAULT_ARMS, AutoRunner, AutoState
from .pipeline import PipelineConfig
from .prescreen import MIC_CAPITAL_MARKET, MIC_GLOBAL_MARKET, MIC_GLOBAL_SELECT

TIERS = {
    "capital": (MIC_CAPITAL_MARKET,),
    "global-market": (MIC_GLOBAL_MARKET,),
    "select": (MIC_GLOBAL_SELECT,),
    "all": (),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m trading_bot.auto_cli",
        description="تشغيل ذاتي كامل: خط الإنتاج + الضبط + بوابة الترقية",
    )
    p.add_argument("--data-dir", default="data/run1")
    p.add_argument("--api-key", default=os.environ.get("TWELVE_DATA_API_KEY"))
    p.add_argument("--tier", choices=list(TIERS), default="capital")
    p.add_argument("--rate-limit", type=int, default=8)
    p.add_argument("--daily-budget", type=int, default=760)
    p.add_argument("--max-candidates", type=int, default=300)
    p.add_argument("--bars-4h", type=int, default=5000)
    p.add_argument("--bars-1d", type=int, default=5000)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--research-mode", action="store_true")
    p.add_argument("--min-trades-to-promote", type=int, default=60)
    p.add_argument("--forever", action="store_true", help="عمل مستمر بلا توقف")
    p.add_argument("--cycles", type=int, help="عدد الدورات في وضع --forever")
    p.add_argument("--sleep", type=int, default=3600, help="ثواني بين الدورات")
    p.add_argument("--status", action="store_true")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = Path(args.data_dir)

    if args.status:
        state = AutoState.load(data_dir / "auto_state.json")
        print(f"دورات منفّذة      : {state.runs}")
        print(f"آخر تشغيل        : {state.last_run or '—'}")
        print(f"آخر بحث          : {state.last_research or '—'}")
        print(f"الإعداد المعتمد   : {state.adopted or 'الافتراضي (لم تُعتمد ترقية)'}")
        print(f"ترقيات مرفوضة     : {state.rejected_promotions}")
        last = data_dir / "auto_last_run.txt"
        if last.exists():
            print()
            print(last.read_text("utf-8"))
        return 0

    provider = None
    if not args.offline:
        if not args.api_key:
            print("لا مفتاح API — اضبط TWELVE_DATA_API_KEY أو استخدم --offline",
                  file=sys.stderr)
            return 2
        from .providers.twelve_data import TwelveDataProvider

        provider = TwelveDataProvider(
            api_key=args.api_key, max_requests_per_minute=args.rate_limit
        )

    config = PipelineConfig(
        data_dir=data_dir,
        tier=TIERS[args.tier],
        max_candidates=args.max_candidates,
        bars_4h=args.bars_4h,
        bars_1d=args.bars_1d,
        equity=args.equity,
        risk_pct=args.risk_pct / 100,
        research_mode=args.research_mode,
        daily_credit_budget=args.daily_budget,
    )

    runner = AutoRunner(
        provider=provider,
        config=config,
        log=(lambda m: None) if args.quiet else (lambda m: print(m, flush=True)),
        min_trades_to_promote=args.min_trades_to_promote,
    )

    if args.forever:
        runner.run_forever(sleep_seconds=args.sleep, max_cycles=args.cycles)
    else:
        print(runner.run_once().text())

    print("\nالمخرجات: auto_last_run.txt | auto_history.jsonl | report.json")
    print("تذكير: تنبيهات بحثية — لا تنفيذ آلي للأوامر، ولا وعد بأداء مستقبلي.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
