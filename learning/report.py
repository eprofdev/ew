"""
learning/report.py
ملخص حالة التعلّم — يدخل بالتقرير اليومي وبلوحة الويب.

يجاوب على أربعة أسئلة بنظرة واحدة:
  1. كم درسًا جمع البوت، وكم منها ما زال ينتظر نتيجته؟
  2. هل النموذج ناضج بما يكفي ليتدخّل بالقرارات؟
  3. وش تعلّمه فعليًا (أثقل الخصائص وزنًا)؟
  4. هل الفلترة تنفع أو تضر؟ (مقارنة المقبولة بالمرفوضة)
"""
from pathlib import Path

from . import store
from .model import OnlineLogisticModel


def learning_summary(model_path, db_path=None, bandit_path=None) -> dict:
    """يجمع حالة التعلّم كاملة في dict واحد جاهز للعرض أو الحفظ."""
    st = store.stats(db_path)
    model = None
    if Path(model_path).exists():
        try:
            model = OnlineLogisticModel.load(model_path)
        except Exception as e:
            st["model_error"] = str(e)

    summary = {
        "samples": st,
        "model_ready": bool(model and model.is_ready),
        "model_samples": model.n_samples if model else 0,
        "model_min_samples": model.min_samples if model else None,
        "model_base_rate": round(model.base_rate * 100, 1) if model else None,
        "top_weights": model.weights_report()[:6] if model else [],
    }

    if bandit_path and Path(bandit_path).exists():
        from .bandit import StrategySelector
        try:
            sel = StrategySelector.load_or_new(bandit_path, [])
            summary["strategy_ranking"] = sel.report()
        except Exception:
            summary["strategy_ranking"] = []

    # محاسبة الفلتر: هل الصفقات اللي رفضها كانت فعلاً أسوأ من اللي قبلها؟
    taken_r, skipped_r = st.get("taken_avg_r"), st.get("skipped_avg_r")
    if taken_r is not None and skipped_r is not None and st["skipped_total"] >= 10:
        summary["filter_verdict"] = (
            f"✅ الفلترة تنفع: متوسط R للمقبولة {taken_r} مقابل {skipped_r} للمرفوضة."
            if taken_r > skipped_r else
            f"⚠️ الفلترة تضر حاليًا: المرفوضة ({skipped_r}) أفضل من المقبولة "
            f"({taken_r}) — نزّل العتبة أو أعد التدريب."
        )
    else:
        summary["filter_verdict"] = "لسه ما فيه صفقات مرفوضة كافية للحكم على الفلترة."

    return summary


def format_learning_text(summary: dict) -> str:
    """نص عربي مختصر يُلحق بالتقرير اليومي بتيليجرام."""
    st = summary["samples"]
    lines = ["", "🧠 حالة التعلّم:",
             f"   دروس مسجّلة: {st['total']}  (محلولة {st['resolved']}، "
             f"منتظرة {st['pending']})"]

    if summary["model_ready"]:
        lines.append(f"   النموذج: جاهز ويؤثر على القرارات "
                     f"({summary['model_samples']} صفقة، "
                     f"نسبة فوز أساسية {summary['model_base_rate']}%)")
    else:
        need = summary.get("model_min_samples")
        lines.append(f"   النموذج: يجمع بيانات فقط "
                     f"({summary['model_samples']}"
                     f"{f'/{need}' if need else ''} صفقة) — لا فلترة بعد")

    if st["taken_total"]:
        lines.append(f"   المنفَّذة: {st['taken_total']} صفقة، "
                     f"فوز {st['taken_win_rate']}%، متوسط R {st['taken_avg_r']}")
    if st["skipped_total"]:
        lines.append(f"   المرفوضة: {st['skipped_total']} صفقة، "
                     f"فوز {st['skipped_win_rate']}%، متوسط R {st['skipped_avg_r']}")

    lines.append(f"   {summary['filter_verdict']}")

    if summary.get("top_weights"):
        top = "، ".join(f"{name} ({w:+.2f})" for name, w in summary["top_weights"][:4])
        lines.append(f"   أثقل الخصائص: {top}")

    if summary.get("strategy_ranking"):
        best = summary["strategy_ranking"][0]
        lines.append(f"   أفضل استراتيجية حاليًا: {best['strategy']} "
                     f"(R متوقع {best['expected_r']} من {best['trials']} صفقة)")

    return "\n".join(lines)
