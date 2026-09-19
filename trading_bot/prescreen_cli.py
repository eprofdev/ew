"""ترشيح الكون قبل إنفاق الرصيد على التاريخ.

    # 1) المرحلة 0 وحدها — مجانية تماماً، بلا أي طلب شبكة
    python -m trading_bot.prescreen_cli --universe data/universe_nasdaq.json \\
        --stage0-only --out data/universe_small.json

    # 2) المرحلتان معاً (رصيد واحد لكل رمز) ثم حفظ الناجين
    export TWELVE_DATA_API_KEY=xxxx
    python -m trading_bot.prescreen_cli --universe data/universe_nasdaq.json \\
        --out data/candidates.json --max-candidates 300

    # 3) شغّل الـ backtest على الناجين فقط
    python -m trading_bot.portfolio_cli --universe data/candidates.json \\
        --checkpoint data/run1.jsonl --research-mode
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from .prescreen import (
    MIC_CAPITAL_MARKET,
    MIC_GLOBAL_MARKET,
    MIC_GLOBAL_SELECT,
    PreScreenConfig,
    PreScreener,
    estimate_prescreen,
)
from .universe import SURVIVORSHIP_WARNING, Universe, fetch_universe


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m trading_bot.prescreen_cli",
        description="ترشيح مسبق للكون: قلّصه قبل أن تنفق رصيداً على تاريخه",
    )
    p.add_argument("--universe", help="ملف كون محفوظ")
    p.add_argument("--exchange", default="NASDAQ")
    p.add_argument("--country", default="United States")
    p.add_argument("--api-key", default=os.environ.get("TWELVE_DATA_API_KEY"))
    p.add_argument("--rate-limit", type=int, default=8)
    p.add_argument("--out", help="احفظ الناجين ككون جاهز لـ portfolio_cli")
    p.add_argument("--report", help="احفظ تفاصيل كل رمز JSON")

    p.add_argument("--stage0-only", action="store_true",
                   help="المرحلة المجانية فقط — بلا أي طلب")
    p.add_argument("--tier", choices=["capital", "global-market", "select", "all"],
                   default="capital", help="طبقة السوق (capital = موطن السنتات)")
    p.add_argument("--min-price", type=float, default=0.50)
    p.add_argument("--max-price", type=float, default=5.00)
    p.add_argument("--min-avg-volume", type=float, default=300_000)
    p.add_argument("--max-range-position", type=float, default=0.60,
                   help="0 = عند قاع 52 أسبوعاً، 1 = عند القمة")
    p.add_argument("--max-today-change", type=float, default=0.25)
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--max-candidates", type=int)
    p.add_argument("--quiet", action="store_true")
    return p


TIERS = {
    "capital": (MIC_CAPITAL_MARKET,),
    "global-market": (MIC_GLOBAL_MARKET,),
    "select": (MIC_GLOBAL_SELECT,),
    "all": (),
}


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    provider = None
    if not args.stage0_only:
        if not args.api_key:
            print("المرحلة 1 تحتاج مفتاح API — أو استخدم --stage0-only", file=sys.stderr)
            return 2
        from .providers.twelve_data import TwelveDataProvider

        provider = TwelveDataProvider(api_key=args.api_key,
                                      max_requests_per_minute=args.rate_limit)

    if args.universe:
        universe = Universe.load(args.universe)
    elif provider is not None:
        universe = fetch_universe(provider, exchange=args.exchange, country=args.country)
    else:
        print("مرّر --universe أو مفتاح API لجلبه", file=sys.stderr)
        return 2

    config = PreScreenConfig(
        allowed_mics=TIERS[args.tier],
        country=args.country,
        min_price=args.min_price,
        max_price=args.max_price,
        min_avg_volume=args.min_avg_volume,
        max_range_position=args.max_range_position,
        max_today_change=args.max_today_change,
        batch_size=args.batch_size,
        max_candidates=args.max_candidates,
    )
    screener = PreScreener(config)

    stage0 = screener.stage0(universe)
    estimate = estimate_prescreen(len(universe), len(stage0))
    print(f"الكون: {len(universe):,} → المرحلة 0 (مجانية): {len(stage0):,}")
    print(f"بلا ترشيح {estimate['naive_credits']:,} رصيداً "
          f"({estimate['naive_days_free_plan']:.1f} يوم على الخطة المجانية) | "
          f"مع الترشيح ≈ {estimate['prescreen_credits']:,} رصيداً "
          f"({estimate['prescreen_days_free_plan']:.1f} يوم) — توفير {estimate['saved_pct']:.0%}")
    if not args.stage0_only:
        print(f"المرحلة 1 ستستهلك {len(stage0):,} رصيداً (رصيد لكل رمز — التجميع لا يوفّر)")

    def progress(done: int, total: int, passed: int) -> None:
        if not args.quiet:
            print(f"  [{done}/{total}] ناجحون حتى الآن: {passed}", flush=True)

    result = screener.run(universe, provider, progress if provider else None)

    print()
    print(result.summary())
    print(f"⚠ {SURVIVORSHIP_WARNING}")

    if result.passed and not args.quiet:
        print("\n  أول 15 مرشحاً:")
        for candidate in result.passed[:15]:
            if candidate.stage == 0:
                # لم تُجلب أسعار بعد — لا تعرض أصفاراً توهم أنها بيانات
                print(f"      {candidate.symbol:<7} {candidate.mic_code:<6} {candidate.name[:44]}")
                continue
            position = f"{candidate.range_position:.0%}" if candidate.range_position is not None else "—"
            print(f"      {candidate.symbol:<7} {candidate.price:>7.2f}  "
                  f"فوليوم {candidate.avg_volume:>12,.0f}  موقع {position:>4}  {candidate.name[:28]}")

    if args.out:
        path = result.to_universe().save(args.out)
        print(f"\nالكون المُرشَّح ({len(result.passed)} رمزاً) → {path}")
        print(f"التالي: python -m trading_bot.portfolio_cli --universe {args.out} "
              f"--checkpoint data/run1.jsonl --research-mode")

    if args.report:
        Path(args.report).write_text(
            json.dumps(
                {
                    "universe_size": result.universe_size,
                    "stage0_survivors": len(result.stage0_survivors),
                    "passed": len(result.passed),
                    "credits_used": result.credits_used,
                    "credits_saved": result.credits_saved,
                    "rejection_reasons": result.rejection_reasons(),
                    "candidates": [c.to_json() for c in result.candidates],
                    "survivorship_warning": SURVIVORSHIP_WARNING,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"التقرير التفصيلي: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
