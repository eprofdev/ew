#!/bin/bash
# run_daily.sh
# كرون ما يقرأ ملفات .env تلقائيًا، فهذا السكربت يحمّل المتغيرات يدويًا قبل التشغيل.
# عدّل المسار SCRIPT_DIR إذا نقلت المجلد لمكان ثاني.

set -a  # يصدّر كل متغير يتحمّل من .env تلقائيًا للبيئة
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$SCRIPT_DIR/deploy/.env"
set +a

cd "$SCRIPT_DIR" || exit 1
/usr/bin/python3 monitoring/daily_runner.py
