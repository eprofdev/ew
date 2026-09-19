#!/usr/bin/env bash
# تثبيت التشغيل الذاتي مرة واحدة — بعدها لا تدخّل.
#
#   ./autorun.sh install   # يضيف مهمة cron يومية
#   ./autorun.sh once      # دورة واحدة الآن
#   ./autorun.sh forever   # عمل مستمر في هذه الجلسة
#   ./autorun.sh status    # ماذا جرى وماذا اعتُمد
#   ./autorun.sh backup    # احفظ بيانات التشغيل (رصيد API مدفوع)
#   ./autorun.sh restore   # استعِدها على جهاز آخر أو بعد فقدان الحاوية
#
# المفتاح يُقرأ من البيئة أو من ملف .env المحلي (غير مرفوع للمستودع).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${DATA_DIR:-$REPO/data/run1}"
ARGS="--data-dir $DATA_DIR --research-mode"

[ -f "$REPO/.env" ] && set -a && . "$REPO/.env" && set +a

# الأوامر التي لا تلمس الشبكة لا تحتاج مفتاحاً
case "${1:-once}" in status|backup|restore|uninstall) NEEDS_KEY=0 ;; *) NEEDS_KEY=1 ;; esac
if [ -z "${TWELVE_DATA_API_KEY:-}" ] && [ "$NEEDS_KEY" = 1 ]; then
  echo "TWELVE_DATA_API_KEY غير مضبوط (ضعه في $REPO/.env)" >&2
  exit 2
fi

case "${1:-once}" in
  install)
    LINE="5 2 * * * cd $REPO && ./autorun.sh once >> $DATA_DIR/cron.log 2>&1"
    ( crontab -l 2>/dev/null | grep -v "autorun.sh once" ; echo "$LINE" ) | crontab -
    echo "ثُبّتت مهمة يومية 02:05 UTC:"; echo "  $LINE"
    ;;
  uninstall)
    crontab -l 2>/dev/null | grep -v "autorun.sh once" | crontab - || true
    echo "أُزيلت المهمة"
    ;;
  backup)
    # بيانات التشغيل تمثّل رصيد API مدفوعاً ولا تُستعاد إلا بإنفاقه ثانيةً،
    # والحاويات المؤقتة تمحوها. لذا نحفظها كأرشيف واحد داخل المستودع.
    mkdir -p "$REPO/data/snapshots"
    tar czf "$REPO/data/snapshots/run1.tgz" -C "$REPO" data/run1
    echo "حُفظت: data/snapshots/run1.tgz ($(du -h "$REPO/data/snapshots/run1.tgz" | cut -f1))"
    echo "اربطها بالمستودع: git add -f data/snapshots/run1.tgz && git commit && git push"
    ;;
  restore)
    [ -f "$REPO/data/snapshots/run1.tgz" ] || { echo "لا أرشيف محفوظ" >&2; exit 1; }
    tar xzf "$REPO/data/snapshots/run1.tgz" -C "$REPO"
    echo "استُعيدت بيانات التشغيل — أكمل بـ ./autorun.sh once"
    ;;
  once)     cd "$REPO" && python3 -m trading_bot.auto_cli $ARGS ;;
  forever)  cd "$REPO" && python3 -m trading_bot.auto_cli $ARGS --forever ;;
  status)   cd "$REPO" && python3 -m trading_bot.auto_cli --data-dir "$DATA_DIR" --status ;;
  *) echo "استخدام: $0 {install|uninstall|once|forever|status|backup|restore}" >&2; exit 1 ;;
esac
