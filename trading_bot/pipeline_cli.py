"""أمر واحد يشغّل كل شيء ويستأنف نفسه.

    export TWELVE_DATA_API_KEY=xxxx
    python -m trading_bot.pipeline_cli --data-dir data/run1

شغّله مرة، وإن توقف لنفاد رصيد اليوم أعد نفس الأمر غداً — يكمل من موضعه.
للتشغيل التلقائي يومياً:

    0 2 * * *  cd /path/to/repo && python -m trading_bot.pipeline_cli --data-dir data/run1 --quiet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from .pipeline import STAGES, Pipeline, PipelineConfig
from .prescreen import MIC_CAPITAL_MARKET, MIC_GLOBAL_MARKET, MIC_GLOBAL_SELECT

TIERS = {
    "capital": (MIC_CAPITAL_MARKET,),
    "global-market": (MIC_GLOBAL_MARKET,),
    "select": (MIC_GLOBAL_SELECT,),
    "all": (),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m trading_bot.pipeline_cli",
        description="خط إنتاج كامل: كون ← ترشيح ← اختبار ← تقرير، يستأنف نفسه",
    )
    p.add_argument("--data-dir", default="data/run1", help="مجلد الحالة والمخرجات")
    p.add_argument("--api-key", default=os.environ.get("TWELVE_DATA_API_KEY"))
    p.add_argument("--exchange", default="NASDAQ")
    p.add_argument("--country", default="United States")
    p.add_argument("--tier", choices=list(TIERS), default="capital")
    p.add_argument("--rate-limit", type=int, default=8, help="طلبات/دقيقة")
    p.add_argument("--daily-budget", type=int, default=800, help="سقف الرصيد اليومي")

    p.add_argument("--min-price", type=float, default=0.50)
    p.add_argument("--max-price", type=float, default=5.00)
    p.add_argument("--min-avg-volume", type=float, default=300_000)
    p.add_argument("--max-range-position", type=float, default=0.60)
    p.add_argument("--max-candidates", type=int, default=300)
    p.add_argument("--quote-batch", type=int, default=50)

    p.add_argument("--bars-4h", type=int, default=1000)
    p.add_argument("--bars-1d", type=int, default=400)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--max-concurrent", type=int, default=5)
    p.add_argument("--fundamentals", help="ملف JSON بالكاب والفلوت")
    p.add_argument("--research-mode", action="store_true",
                   help="عطّل فلتر الهيكل المالي (يُعلن في التقرير)")

    p.add_argument("--offline", action="store_true", help="من الملفات والذاكرة فقط")
    p.add_argument("--reset", action="store_true", help="امسح الحالة وابدأ من الصفر")
    p.add_argument("--retry-failed", action="store_true",
                   help="أعد جلب الرموز التي فشل اقتباسها سابقاً")
    p.add_argument("--status", action="store_true", help="اعرض الحالة فقط ثم اخرج")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    config = PipelineConfig(
        data_dir=Path(args.data_dir),
        exchange=args.exchange,
        country=args.country,
        tier=TIERS[args.tier],
        min_price=args.min_price,
        max_price=args.max_price,
        min_avg_volume=args.min_avg_volume,
        max_range_position=args.max_range_position,
        max_candidates=args.max_candidates,
        quote_batch=args.quote_batch,
        bars_4h=args.bars_4h,
        bars_1d=args.bars_1d,
        equity=args.equity,
        risk_pct=args.risk_pct / 100,
        research_mode=args.research_mode,
        max_concurrent=args.max_concurrent,
        daily_credit_budget=args.daily_budget,
        retry_failed_quotes=args.retry_failed,
    )

    provider = None
    # عرض الحالة قراءة محضة — لا يحتاج مفتاحاً ولا اتصالاً
    if not args.offline and not args.status:
        if not args.api_key:
            print("لا يوجد مفتاح API — استخدم --offline أو اضبط TWELVE_DATA_API_KEY",
                  file=sys.stderr)
            return 2
        from .providers.twelve_data import TwelveDataProvider

        provider = TwelveDataProvider(
            api_key=args.api_key, max_requests_per_minute=args.rate_limit
        )

    def progress(stage: str, message: str) -> None:
        if not args.quiet:
            print(f"  [{stage}] {message}", flush=True)

    pipeline = Pipeline(provider=provider, config=config, progress=progress)

    if args.reset:
        pipeline.reset()
        print("أُعيدت الحالة إلى الصفر (الذاكرة المؤقتة للشموع محفوظة)")

    if args.status:
        state = pipeline.state
        done = STAGES.index(state.stage) if state.stage in STAGES else 0
        print(f"المرحلة   : {state.stage} ({done + 1}/{len(STAGES)})"
              + ("  ✔ مكتمل" if state.finished else ""))
        print(f"موضع الاقتباس : {state.quote_cursor:,}")
        print(f"رصيد اليوم    : {state.credits_today:,} / {config.daily_credit_budget:,}")
        print(f"رصيد إجمالي   : {state.credits_total:,}")
        return 0

    fundamentals = json.loads(Path(args.fundamentals).read_text("utf-8")) if args.fundamentals else {}
    outcome = pipeline.run(fundamentals)

    print()
    print(outcome.summary())

    if outcome.complete:
        print(f"\n✔ اكتمل — التقرير: {config.paths()['report']}")
    elif outcome.stopped_reason:
        print(f"\n⏸ توقف مؤقت. أعد نفس الأمر ليكمل من موضعه:")
        print(f"   python -m trading_bot.pipeline_cli --data-dir {args.data_dir}")
    print("\nتذكير: نتائج تاريخية على فرضيات محددة — ليست وعداً بأداء مستقبلي.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
