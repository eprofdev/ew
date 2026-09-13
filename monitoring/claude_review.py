"""
claude_review.py
طبقة تحليل تلقائي اختيارية: ترسل التقرير اليومي + الأخطاء الأخيرة لـ Claude API
وترجع تحليل نصي مختصر بالعربي مع اقتراحات.

هذا السكربت يحلل ويقترح فقط — أبدًا ما يعدّل أي ملف كود تلقائيًا.
أي إصلاح مقترح لازم تراجعه وتطبّقه أنت بنفسك، خصوصًا إن كان يمس كود التنفيذ الحي.

الإعداد:
    pip install anthropic
    export ANTHROPIC_API_KEY="sk-ant-..."

الاستخدام المستقل:
    python claude_review.py

أو من داخل daily_runner.py تلقائيًا إذا فعّلت:
    export ENABLE_CLAUDE_REVIEW=true
"""
import os
from anthropic import Anthropic

import trade_logger as tl
from report_generator import build_report, format_report_text

# Haiku أرخص خيار ومناسب لتقرير يومي بسيط.
# لتحليل أعمق (مثلاً مراجعة أسبوعية شاملة)، غيّرها لـ "claude-sonnet-5"
MODEL = os.environ.get("CLAUDE_REVIEW_MODEL", "claude-haiku-4-5-20251001")

SYSTEM_PROMPT = """أنت تراجع سجلات تشغيل يومية لبوت تداول أسهم شخصي (بايثون + Alpaca API).
مهمتك فقط التحليل والاقتراح — لا تفترض أنك ستعدل أي كود مباشرة.
ركّز على:
1. هل فيه أخطاء متكررة تدل على مشكلة بنيوية (مو مجرد عطل عابر)؟
2. هل أداء الاستراتيجية (نسبة الربح، عدد الصفقات) يبدو طبيعي أو فيه شيء يستدعي القلق؟
3. أعطِ أولوية واضحة: إيش أهم شيء يحتاج مراجعة بشرية اليوم، إن وجد.
اكتب بالعربي، مختصر ومباشر (فقرة أو فقرتين كحد أقصى)، وبدون مجاملات زائدة."""


def build_context(report: dict) -> str:
    lines = [format_report_text(report), ""]

    unresolved = tl.get_recent_errors(limit=20, unresolved_only=True)
    if unresolved:
        lines.append("تفاصيل الأخطاء غير المحلولة (آخر 20):")
        for e in unresolved:
            lines.append(f"- [{e['timestamp']}] {e['context']}: {e['error_message']}")
            if e.get("traceback"):
                # آخر سطرين من الـ traceback كافية للسياق بدون إغراق الطلب
                tb_lines = e["traceback"].strip().splitlines()[-2:]
                lines.append("  " + " | ".join(tb_lines))

    return "\n".join(lines)


def analyze(report: dict = None) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "⚠️ ما فيه ANTHROPIC_API_KEY — تحليل Claude التلقائي متوقف. التقرير الخام متاح كما هو."

    if report is None:
        report = build_report(hours=24)

    context = build_context(report)
    client = Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": context}],
        )
        return response.content[0].text
    except Exception as e:
        # فشل استدعاء Claude نفسه يُسجَّل كخطأ عادي، ونرجع التقرير الخام بدل ما نوقف كل شيء
        tl.log_error("claude_review.analyze", e)
        return f"⚠️ فشل تحليل Claude التلقائي ({e}). التقرير الخام متاح كما هو."


if __name__ == "__main__":
    tl.init_db()
    result = analyze()
    print(result)
