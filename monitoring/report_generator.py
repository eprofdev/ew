"""
report_generator.py
يبني التقرير اليومي من قاعدة البيانات: أداء الصفقات + الأخطاء الجديدة.
يرجع نص جاهز لتيليجرام/للصق في شات Claude، وملف JSON للوحة الويب،
وصورة لمنحنى رأس المال.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import trade_logger as tl

EXPORT_DIR = Path(__file__).parent / "exports"
EXPORT_DIR.mkdir(exist_ok=True)


def _trades_since(hours: int = 24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [t for t in tl.get_trades(limit=500)
            if datetime.fromisoformat(t["timestamp"]) >= cutoff]


def _errors_since(hours: int = 24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [e for e in tl.get_recent_errors(limit=200)
            if datetime.fromisoformat(e["timestamp"]) >= cutoff]


def build_report(hours: int = 24) -> dict:
    trades = _trades_since(hours)
    errors = _errors_since(hours)
    closed = [t for t in trades if t["side"] == "SELL" and t["pnl"] is not None]
    opened = [t for t in trades if t["side"] == "BUY"]

    total_pnl = sum(t["pnl"] for t in closed)
    wins = [t for t in closed if t["pnl"] > 0]
    equity_curve = tl.get_equity_curve()
    current_equity = equity_curve[-1]["equity"] if equity_curve else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_hours": hours,
        "trades_opened": len(opened),
        "trades_closed": len(closed),
        "total_pnl": round(total_pnl, 2),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "current_equity": current_equity,
        "new_errors": len(errors),
        "unresolved_errors_total": len(tl.get_recent_errors(limit=500, unresolved_only=True)),
        "trades": trades,
        "errors": errors,
    }


def format_report_text(report: dict) -> str:
    lines = [
        "📊 التقرير اليومي — بوت التداول",
        f"الفترة: آخر {report['period_hours']} ساعة",
        "",
        f"صفقات جديدة: {report['trades_opened']} فتح / {report['trades_closed']} إغلاق",
    ]
    if report["trades_closed"]:
        lines.append(f"الربح/الخسارة الصافي: {report['total_pnl']:+.2f}$")
        lines.append(f"نسبة الربح: {report['win_rate_pct']}%")
    if report["current_equity"] is not None:
        lines.append(f"رأس المال الحالي: {report['current_equity']:.2f}$")

    lines.append("")
    if report["new_errors"] == 0:
        lines.append("✅ ما فيه أخطاء جديدة اليوم.")
    else:
        lines.append(f"⚠️ {report['new_errors']} خطأ جديد اليوم "
                      f"(إجمالي غير محلول: {report['unresolved_errors_total']}):")
        for e in report["errors"][:5]:
            lines.append(f"  • [{e['context']}] {e['error_message']}")
        if len(report["errors"]) > 5:
            lines.append(f"  ...و{len(report['errors']) - 5} خطأ آخر (التفاصيل بالتقرير الكامل)")

    return "\n".join(lines)


def export_dashboard_json(report: dict):
    """يحفظ لقطة كاملة (تقرير + منحنى رأس المال + آخر الصفقات) تقرأها لوحة الويب."""
    data = {
        "report": {k: v for k, v in report.items() if k not in ("trades", "errors")},
        "recent_trades": tl.get_trades(limit=100),
        "recent_errors": tl.get_recent_errors(limit=100),
        "equity_curve": tl.get_equity_curve(),
    }
    path = EXPORT_DIR / "dashboard_data.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    return path


def generate_equity_chart() -> Path:
    curve = tl.get_equity_curve()
    path = EXPORT_DIR / "equity_chart.png"
    if len(curve) < 2:
        # ما فيه بيانات كافية لرسم منحنى بعد
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, "بيانات غير كافية بعد لرسم المنحنى",
                ha="center", va="center", fontsize=12)
        ax.axis("off")
        fig.savefig(path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return path

    dates = [c["date"] for c in curve]
    equity = [c["equity"] for c in curve]

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(dates, equity, marker="o", linewidth=2, color="#2563eb")
    ax.set_title("منحنى رأس المال")
    ax.set_ylabel("USD")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


if __name__ == "__main__":
    report = build_report(hours=24)
    print(format_report_text(report))
    export_dashboard_json(report)
    generate_equity_chart()
