"""
learning/walk_forward.py
التقييم الأمين زمنيًا — أهم ملف بطبقة التعلّم كلها.

المشكلة اللي يحلها:
لو درّبت نموذجًا على 200 صفقة ثم قِست دقته على نفس الـ200، راح يعطيك 75%
ويخسر فعليًا بالسوق. النموذج "يحفظ" إجابات ما درسه، ما "يتعلّم" نمطًا.

الحل هنا (prequential / test-then-train):
    نرتب الصفقات زمنيًا، ثم لكل صفقة بالترتيب:
        1. نتنبأ باحتمال نجاحها بنموذج ما شاف إلا الصفقات اللي قبلها
        2. نسجّل التنبؤ
        3. بعدها فقط ندرّب النموذج عليها
كل رقم بالتقرير الناتج هو أداء خارج العيّنة فعليًا — نفس ما راح يصير حيًا
بالضبط، لأن هذي حرفيًا آلية التشغيل اليومي: تتنبأ اليوم، تتعلّم بكرة.
"""
import numpy as np

from .dataset import sort_by_date
from .model import OnlineLogisticModel


def roc_auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    """
    مساحة تحت منحنى ROC بطريقة الرتب (Mann-Whitney) — بدون sklearn.
    المعنى العملي: احتمال أن النموذج يعطي صفقة رابحة عشوائية درجة أعلى
    من صفقة خاسرة عشوائية. 0.5 = عشوائي تمامًا (بلا فائدة)، 1.0 = مثالي.
    """
    y_true = np.asarray(y_true)
    scores = np.asarray(scores, dtype=float)
    n_pos = int((y_true == 1).sum())
    n_neg = int((y_true == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")  # فئة واحدة فقط — الـAUC غير معرّف

    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=float)
    sorted_scores = scores[order]
    i = 0
    while i < len(sorted_scores):
        j = i
        while j + 1 < len(sorted_scores) and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # رتب متوسطة للقيم المتساوية
        ranks[order[i:j + 1]] = avg_rank
        i = j + 1

    sum_pos_ranks = ranks[y_true == 1].sum()
    return float((sum_pos_ranks - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def brier_score(y_true: np.ndarray, probs: np.ndarray) -> float:
    """متوسط مربع الخطأ بالاحتمال. أقل = أفضل. 0.25 = تخمين ثابت عند 0.5."""
    return float(np.mean((np.asarray(probs, dtype=float) - np.asarray(y_true, dtype=float)) ** 2))


def log_loss(y_true: np.ndarray, probs: np.ndarray) -> float:
    p = np.clip(np.asarray(probs, dtype=float), 1e-9, 1 - 1e-9)
    y = np.asarray(y_true, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def calibration_bins(y_true: np.ndarray, probs: np.ndarray, n_bins: int = 5) -> list:
    """
    هل الاحتمالات صادقة؟ نقسم التنبؤات لشرائح ونقارن المتوقع بالفعلي.
    نموذج معاير: شريحة "توقّع 70%" تربح فعلاً قريبًا من 70% من الوقت.
    بدون معايرة، حساب Kelly يبني على رقم كاذب ويضخّم الأحجام خطأً.
    """
    y_true = np.asarray(y_true)
    probs = np.asarray(probs, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (probs >= lo) & (probs < hi if hi < 1.0 else probs <= hi)
        if not mask.any():
            continue
        out.append({
            "range": f"{lo:.2f}-{hi:.2f}",
            "count": int(mask.sum()),
            "predicted": round(float(probs[mask].mean()), 3),
            "actual": round(float(y_true[mask].mean()), 3),
        })
    return out


def threshold_sweep(y_true: np.ndarray, probs: np.ndarray, r_multiples: np.ndarray,
                    thresholds=None) -> list:
    """
    "لو رفضت كل إشارة احتمالها أقل من X، وش كان صار؟"

    هذا الجدول هو اللي تختار منه عتبة التشغيل — لا تختارها من راسك.
    انتبه للعمود kept: عتبة تترك 6 صفقات من 200 قد تبدو ممتازة وهي صدفة.
    """
    if thresholds is None:
        thresholds = [0.0, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
    y_true = np.asarray(y_true)
    probs = np.asarray(probs, dtype=float)
    r_multiples = np.asarray(r_multiples, dtype=float)
    total = len(y_true)

    rows = []
    for t in thresholds:
        mask = probs >= t
        kept = int(mask.sum())
        rows.append({
            "threshold": round(float(t), 2),
            "kept": kept,
            "kept_pct": round(kept / total * 100, 1) if total else 0.0,
            "win_rate": round(float(y_true[mask].mean() * 100), 1) if kept else None,
            "avg_r": round(float(r_multiples[mask].mean()), 3) if kept else None,
            "total_r": round(float(r_multiples[mask].sum()), 2) if kept else 0.0,
        })
    return rows


def walk_forward_eval(samples: list, min_train: int = 30,
                      model_kwargs: dict = None) -> dict:
    """
    التقييم الأساسي: تنبّأ ثم تعلّم، صفقة بصفقة، بترتيب زمني صارم.

    min_train: عدد الصفقات الأولى اللي نستخدمها للتدريب فقط بدون تقييم —
    تنبؤات نموذج شاف 3 صفقات ما لها معنى، فما نحاسبه عليها.

    يرجع dict فيه المقاييس + التنبؤات نفسها (probs) عشان تقدر تحسب
    أي شيء إضافي فوقها بدون إعادة التشغيل.
    """
    samples = sort_by_date(samples)
    n = len(samples)
    result = {
        "n_samples": n,
        "n_evaluated": 0,
        "min_train": min_train,
        "ready": False,
    }
    if n <= min_train:
        result["note"] = (f"عدد العيّنات ({n}) لا يتجاوز حد بدء التقييم ({min_train}) — "
                          "زِد عدد الأسهم أو طوّل الفترة التاريخية.")
        return result

    model = OnlineLogisticModel(**(model_kwargs or {}))
    probs, labels, rs = [], [], []

    for i, s in enumerate(samples):
        x = s.vector()
        if i >= min_train:
            # التنبؤ قبل التدريب — النموذج هنا شاف الصفقات [0, i) فقط
            probs.append(model.predict_proba(x))
            labels.append(s.label)
            rs.append(s.r_multiple)
        model.partial_fit(x, s.label)

    probs = np.array(probs, dtype=float)
    labels = np.array(labels, dtype=int)
    rs = np.array(rs, dtype=float)

    base_rate = float(labels.mean())
    result.update({
        "ready": True,
        "n_evaluated": len(labels),
        "base_win_rate": round(base_rate * 100, 1),
        "auc": round(roc_auc(labels, probs), 3),
        "brier": round(brier_score(labels, probs), 4),
        # خط الأساس: تخمين ثابت بنسبة الفوز العامة. لو Brier نموذجنا ما تفوّق
        # عليه، فالنموذج ما أضاف شيئًا مهما بدت أرقامه الأخرى جميلة.
        "brier_baseline": round(brier_score(labels, np.full_like(probs, base_rate)), 4),
        "log_loss": round(log_loss(labels, probs), 4),
        "avg_r_all": round(float(rs.mean()), 3),
        "total_r_all": round(float(rs.sum()), 2),
        "calibration": calibration_bins(labels, probs),
        "threshold_sweep": threshold_sweep(labels, probs, rs),
        "probs": probs.tolist(),
        "labels": labels.tolist(),
        "r_multiples": rs.tolist(),
        "dates": [str(s.date) for s in samples[min_train:]],
    })
    result["beats_baseline"] = result["brier"] < result["brier_baseline"]
    return result


def recommend_threshold(eval_result: dict, min_kept_pct: float = 40.0,
                        min_kept_trades: int = 25) -> dict:
    """
    يقترح عتبة تشغيل من نتيجة التقييم، بشرطين يمنعان الخداع الإحصائي:
      1. تبقي على نسبة معقولة من الصفقات (min_kept_pct)
      2. تبقي على عدد مطلق كافٍ منها (min_kept_trades)

    بدون الشرطين، "أفضل" عتبة دائمًا هي الأعلى — تترك 3 صفقات رابحة بالصدفة
    وتعطي أرقامًا خيالية ما تتكرر أبدًا.

    ولو ما فيه عتبة تتفوق على "خذ كل شيء"، يرجّح 0.0 صراحةً بدل ما يجبر
    اختيارًا: بوت بلا فلترة أفضل من بوت يفلتر بلا سبب.
    """
    if not eval_result.get("ready"):
        return {"threshold": 0.0, "reason": "بيانات غير كافية للتقييم — لا فلترة."}

    # بوابة أولى قبل أي نظر بجدول العتبات: لو النموذج ما تفوّق حتى على تخمين
    # ثابت (Brier)، فأي عتبة "تحسّن" النتيجة هنا محض صدفة بالبيانات. اقتراح
    # عتبة في هذي الحالة يحوّل ضجيجًا عشوائيًا إلى قاعدة تداول دائمة.
    if not eval_result.get("beats_baseline"):
        return {
            "threshold": 0.0,
            "reason": (f"النموذج لا يتفوق على التخمين الثابت "
                       f"(Brier {eval_result['brier']} مقابل "
                       f"{eval_result['brier_baseline']}) — أي فلترة الآن عشوائية."),
            "baseline_avg_r": eval_result.get("avg_r_all"),
        }

    sweep = eval_result["threshold_sweep"]
    baseline = next((r for r in sweep if r["threshold"] == 0.0), None)
    baseline_r = baseline["avg_r"] if baseline and baseline["avg_r"] is not None else 0.0

    candidates = [
        r for r in sweep
        if r["threshold"] > 0
        and r["kept"] >= min_kept_trades
        and r["kept_pct"] >= min_kept_pct
        and r["avg_r"] is not None
        and r["avg_r"] > baseline_r
    ]
    if not candidates:
        return {
            "threshold": 0.0,
            "reason": ("ما فيه عتبة تتفوق على عدم الفلترة مع إبقاء عدد صفقات كافٍ — "
                       "شغّل بدون فلترة واجمع صفقات أكثر."),
            "baseline_avg_r": baseline_r,
        }

    best = max(candidates, key=lambda r: r["avg_r"])
    return {
        "threshold": best["threshold"],
        "reason": (f"عند العتبة {best['threshold']}: تبقى {best['kept']} صفقة "
                   f"({best['kept_pct']}%)، متوسط R = {best['avg_r']} مقابل "
                   f"{baseline_r} بدون فلترة."),
        "baseline_avg_r": baseline_r,
        "expected_avg_r": best["avg_r"],
        "expected_win_rate": best["win_rate"],
    }


def format_eval_report(eval_result: dict, title: str = "تقييم نموذج التعلّم") -> str:
    """تقرير نصي جاهز للطباعة أو لإرساله بتيليجرام."""
    if not eval_result.get("ready"):
        return (f"\n{'=' * 62}\n  {title}\n{'=' * 62}\n"
                f"{eval_result.get('note', 'بيانات غير كافية.')}\n")

    lines = [
        "",
        "=" * 62,
        f"  {title}",
        "=" * 62,
        f"عدد العيّنات الكلي:        {eval_result['n_samples']}",
        f"مُقيَّمة خارج العيّنة:       {eval_result['n_evaluated']}  "
        f"(أول {eval_result['min_train']} للتدريب فقط)",
        f"نسبة الفوز العامة:         {eval_result['base_win_rate']}%",
        "",
        f"AUC:                       {eval_result['auc']}   "
        "(0.50 = عشوائي، فوق 0.55 = فيه إشارة حقيقية)",
        f"Brier (النموذج):           {eval_result['brier']}",
        f"Brier (تخمين ثابت):        {eval_result['brier_baseline']}",
        f"Log loss:                  {eval_result['log_loss']}",
        "",
        ("✅ النموذج يتفوق على التخمين الثابت."
         if eval_result["beats_baseline"] else
         "❌ النموذج لا يتفوق على التخمين الثابت — لا تفعّل الفلترة بعد."),
        "",
        "معايرة الاحتمالات (المتوقع مقابل الفعلي):",
    ]
    for b in eval_result["calibration"]:
        lines.append(f"   {b['range']}   n={b['count']:<4}  متوقع {b['predicted']:.2f}  "
                     f"فعلي {b['actual']:.2f}")

    lines += ["", "أثر العتبة على الأداء:", "",
              f"   {'عتبة':<8}{'صفقات':<10}{'٪ باقية':<10}{'فوز٪':<10}{'متوسط R':<10}{'مجموع R':<10}"]
    for r in eval_result["threshold_sweep"]:
        win = f"{r['win_rate']}" if r["win_rate"] is not None else "-"
        avg = f"{r['avg_r']}" if r["avg_r"] is not None else "-"
        lines.append(f"   {r['threshold']:<8}{r['kept']:<10}{r['kept_pct']:<10}"
                     f"{win:<10}{avg:<10}{r['total_r']:<10}")

    rec = recommend_threshold(eval_result)
    lines += ["", f"العتبة المقترحة: {rec['threshold']}", f"السبب: {rec['reason']}",
              "=" * 62, ""]
    return "\n".join(lines)
