"""
dashboard.py
لوحة تحكم ويب بسيطة تعرض: منحنى رأس المال، سجل الصفقات، وسجل الأخطاء.
تُشغَّل محليًا على سيرفرك/راوترك — ينصح تعرضها عبر WireGuard وليس بورت مفتوح للعالم.

التشغيل:
    python dashboard.py
    ثم افتح: http://localhost:5000  (أو عبر IP جهازك داخل شبكة WireGuard)
"""
from flask import Flask, jsonify, render_template
import trade_logger as tl
from report_generator import build_report

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("dashboard.html")


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
