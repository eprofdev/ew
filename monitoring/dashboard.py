"""
dashboard.py
لوحة تحكم ويب بسيطة تعرض: منحنى رأس المال، سجل الصفقات، وسجل الأخطاء.
تُشغَّل محليًا على سيرفرك/راوترك — ينصح تعرضها عبر WireGuard وليس بورت مفتوح للعالم.

التشغيل:
    python dashboard.py
    ثم افتح: http://localhost:5000  (أو عبر IP جهازك داخل شبكة WireGuard)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # للوصول لـ learning/

from flask import Flask, jsonify, render_template
import trade_logger as tl
from report_generator import build_report

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/learning")
def api_learning():
    """حالة التعلّم: كم درسًا جمع البوت، هل النموذج ناضج، وهل الفلترة تنفع."""
    import os
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    model_path = os.environ.get("LEARNING_MODEL_PATH", str(root / "models" / "model.json"))
    bandit_path = os.environ.get("LEARNING_BANDIT_PATH", str(root / "models" / "bandit.json"))
    try:
        from learning.report import learning_summary
        return jsonify(learning_summary(model_path, bandit_path=bandit_path))
    except Exception as e:
        # اللوحة ما تنهار لو طبقة التعلّم معطّلة أو ما بدأت بعد
        return jsonify({"error": str(e), "model_ready": False})


@app.route("/api/data")
def api_data():
    report = build_report(hours=24)
    return jsonify({
        "report": {k: v for k, v in report.items() if k not in ("trades", "errors")},
        "recent_trades": tl.get_trades(limit=100),
        "recent_errors": tl.get_recent_errors(limit=100),
        "equity_curve": tl.get_equity_curve(),
    })


if __name__ == "__main__":
    tl.init_db()
    # host="0.0.0.0" يخلي الوصول متاح داخل شبكتك المحلية/WireGuard فقط،
    # وليس مفتوح على الإنترنت العام — تأكد أن الجدار الناري ما يعرّضه للخارج.
    app.run(host="0.0.0.0", port=5000, debug=False)
