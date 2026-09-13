"""
telegram_notifier.py
يرسل التقرير اليومي (نص + صورة منحنى رأس المال) عبر بوت تيليجرام.
يشتغل مع بوتك الحالي — بس محتاج توكن البوت ومعرف الشات (chat_id) بتاعك.

الإعداد:
    export TELEGRAM_BOT_TOKEN="123456:ABC-..."   # من BotFather
    export TELEGRAM_CHAT_ID="123456789"          # معرفك أو معرف القروب

كيف تجيب TELEGRAM_CHAT_ID:
    أرسل أي رسالة للبوت، ثم افتح:
    https://api.telegram.org/bot<TOKEN>/getUpdates
    وشوف قيمة "chat":{"id": ...}
"""
import os
import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _get_credentials():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "لازم تحدد TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID كمتغيرات بيئة أولاً."
        )
    return token, chat_id


def send_message(text: str) -> bool:
    token, chat_id = _get_credentials()
    url = TELEGRAM_API.format(token=token, method="sendMessage")
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
    if not resp.ok:
        print(f"فشل إرسال الرسالة: {resp.status_code} {resp.text}")
    return resp.ok


def send_photo(photo_path: str, caption: str = "") -> bool:
    token, chat_id = _get_credentials()
    url = TELEGRAM_API.format(token=token, method="sendPhoto")
    with open(photo_path, "rb") as f:
        resp = requests.post(
            url, data={"chat_id": chat_id, "caption": caption},
            files={"photo": f}, timeout=30
        )
    if not resp.ok:
        print(f"فشل إرسال الصورة: {resp.status_code} {resp.text}")
    return resp.ok


if __name__ == "__main__":
    # اختبار سريع للاتصال
    ok = send_message("✅ اختبار الاتصال — بوت التداول متصل بتيليجرام بنجاح.")
    print("نجح" if ok else "فشل — تأكد من التوكن والـ chat_id")
