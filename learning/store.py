"""
learning/store.py
الذاكرة الدائمة للتعلّم: كل إشارة تُسجَّل لحظة صدورها، ونتيجتها تُكتب لاحقًا.

ليش هذي الطبقة ضرورية؟
النموذج يتعلّم من صفقات منتهية، والصفقة تنتهي بعد أيام من صدور إشارتها.
فنحتاج مكانًا نحفظ فيه "حالة السوق لحظة الدخول" حتى تظهر النتيجة — بدونه
البوت ينسى الخصائص وما يقدر يربط النتيجة بسببها.

نقطة مهمة: نسجّل حتى الإشارات المرفوضة (taken = 0) ونحلّ نتائجها كذلك.
هذا يعطيك جوابًا مباشرًا على "هل الفلترة تنفع أو تضر؟" — لو الصفقات
المرفوضة كانت رابحة أكثر من المقبولة، عتبتك غلط وتشوفها بالأرقام.
وأهم من ذلك: يمنع حلقة تغذية راجعة مغلقة يتعلّم فيها النموذج من قراراته
هو فقط فيثبّت أخطاءه ولا يصحّحها أبدًا.

قاعدة البيانات هي نفسها اللي يستخدمها monitoring/trade_logger.py، عشان
اللوحة والتقرير يقرأون كل شيء من مصدر واحد.
"""
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "monitoring" / "trading_log.db"


def get_connection(db_path=None):
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path=None):
    conn = get_connection(db_path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS learning_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        strategy TEXT NOT NULL,
        entry_date TEXT NOT NULL,        -- تاريخ شمعة الإشارة (لا وقت التسجيل)
        entry_price REAL NOT NULL,
        stop_loss REAL,
        take_profit REAL,
        features TEXT NOT NULL,          -- JSON لخصائص لحظة الدخول
        probability REAL,                -- ما توقّعه النموذج وقتها (للمراجعة)
        taken INTEGER NOT NULL DEFAULT 1,-- 1 نُفِّذت، 0 رفضها الفلتر
        decision_reason TEXT,
        mode TEXT,                       -- SIGNAL_ONLY / PAPER / LIVE / BACKTEST
        exit_price REAL,
        exit_date TEXT,
        exit_reason TEXT,
        label INTEGER,                   -- NULL = ما زالت مفتوحة
        r_multiple REAL,
        resolved INTEGER NOT NULL DEFAULT 0,
        trained INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE(symbol, strategy, entry_date)
    );

    CREATE INDEX IF NOT EXISTS idx_ls_unresolved ON learning_samples(resolved);
    CREATE INDEX IF NOT EXISTS idx_ls_untrained ON learning_samples(resolved, trained);
    """)
    conn.commit()
    conn.close()


def record_signal(symbol: str, strategy: str, entry_date, entry_price: float,
                  features: dict, stop_loss: float = None, take_profit: float = None,
                  probability: float = None, taken: bool = True,
                  decision_reason: str = "", mode: str = "SIGNAL_ONLY",
                  db_path=None) -> Optional[int]:
    """
    يسجّل إشارة لحظة صدورها. يرجع id العيّنة، أو None لو كانت مسجّلة أصلاً.

    قيد UNIQUE(symbol, strategy, entry_date) متعمّد: daily_runner قد يشتغل
    مرتين بنفس اليوم (تشغيل يدوي + cron) وما نبي نفس الصفقة تتكرر كعيّنتين
    فيتعلّم النموذج منها مرتين ويعطيها وزنًا مضاعفًا بلا سبب.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO learning_samples
               (symbol, strategy, entry_date, entry_price, stop_loss, take_profit,
                features, probability, taken, decision_reason, mode, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, strategy, str(entry_date)[:10], float(entry_price),
             stop_loss, take_profit, json.dumps(features), probability,
             1 if taken else 0, decision_reason, mode,
             datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount else None
    finally:
        conn.close()


def resolve_sample(sample_id: int, exit_price: float, exit_date,
                   exit_reason: str = "", db_path=None):
    """يكتب نتيجة صفقة انتهت: السعر، التاريخ، ومنها تُحسب التسمية وR."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT entry_price, stop_loss FROM learning_samples WHERE id = ?",
            (sample_id,)
        ).fetchone()
        if row is None:
            return
        entry, stop = row["entry_price"], row["stop_loss"]
        risk = (entry - stop) if (stop is not None and entry > stop) else None
        pnl = exit_price - entry
        conn.execute(
            """UPDATE learning_samples
               SET exit_price=?, exit_date=?, exit_reason=?, label=?, r_multiple=?,
                   resolved=1
               WHERE id = ?""",
            (float(exit_price), str(exit_date)[:10], exit_reason,
             1 if pnl > 0 else 0, (pnl / risk) if risk else None, sample_id)
        )
        conn.commit()
    finally:
        conn.close()


def _abandon_sample(sample_id: int, reason: str, db_path=None):
    """
    يغلق عيّنة بلا تسمية (label يبقى NULL).

    تُحسب "محلولة" فتتوقف عن التراكم بقائمة المعلّقات، لكنها مستبعدة من
    التدريب ومن كل الإحصاءات — لأن نتيجتها مجهولة فعلاً، وتخمينها أسوأ
    من إهمالها.
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE learning_samples SET resolved=1, trained=1, exit_reason=? WHERE id=?",
            (reason, sample_id)
        )
        conn.commit()
    finally:
        conn.close()


def pending_samples(db_path=None) -> list:
    """العيّنات اللي ما ظهرت نتيجتها بعد — المرشحة للحل بكل تشغيلة."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM learning_samples WHERE resolved = 0 ORDER BY entry_date ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def untrained_samples(db_path=None) -> list:
    """عيّنات محلولة وما تعلّم منها النموذج بعد — وقود التدريب اليومي."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM learning_samples
               WHERE resolved = 1 AND trained = 0 AND label IS NOT NULL
               ORDER BY exit_date ASC, id ASC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_trained(sample_ids: list, db_path=None):
    if not sample_ids:
        return
    conn = get_connection(db_path)
    try:
        conn.executemany("UPDATE learning_samples SET trained = 1 WHERE id = ?",
                         [(int(i),) for i in sample_ids])
        conn.commit()
    finally:
        conn.close()


def all_resolved(db_path=None) -> list:
    """كل العيّنات المحلولة — تُستخدم لإعادة تدريب كاملة أو تقييم walk-forward."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM learning_samples
               WHERE resolved = 1 AND label IS NOT NULL
               ORDER BY entry_date ASC, id ASC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats(db_path=None) -> dict:
    """ملخص حالة التعلّم — يدخل بالتقرير اليومي وباللوحة."""
    conn = get_connection(db_path)
    try:
        def _one(sql, *args):
            r = conn.execute(sql, args).fetchone()
            return r[0] if r and r[0] is not None else 0

        taken_wins = _one("SELECT COUNT(*) FROM learning_samples "
                          "WHERE resolved=1 AND taken=1 AND label=1")
        taken_total = _one("SELECT COUNT(*) FROM learning_samples "
                           "WHERE resolved=1 AND taken=1")
        skipped_wins = _one("SELECT COUNT(*) FROM learning_samples "
                            "WHERE resolved=1 AND taken=0 AND label=1")
        skipped_total = _one("SELECT COUNT(*) FROM learning_samples "
                             "WHERE resolved=1 AND taken=0")
        return {
            "total": _one("SELECT COUNT(*) FROM learning_samples"),
            "pending": _one("SELECT COUNT(*) FROM learning_samples WHERE resolved=0"),
            # "محلولة" = ظهرت نتيجتها فعلاً. العيّنات المهجورة (بلا تسمية)
            # مستبعدة هنا عمدًا: عدّها ضمن الدروس يضخّم رصيد التعلّم كذبًا.
            "resolved": _one("SELECT COUNT(*) FROM learning_samples "
                             "WHERE resolved=1 AND label IS NOT NULL"),
            "abandoned": _one("SELECT COUNT(*) FROM learning_samples "
                              "WHERE resolved=1 AND label IS NULL"),
            "untrained": _one("SELECT COUNT(*) FROM learning_samples "
                              "WHERE resolved=1 AND trained=0 AND label IS NOT NULL"),
            "taken_total": taken_total,
            "taken_win_rate": round(taken_wins / taken_total * 100, 1) if taken_total else None,
            "taken_avg_r": round(_one("SELECT AVG(r_multiple) FROM learning_samples "
                                       "WHERE resolved=1 AND taken=1"), 3) if taken_total else None,
            "skipped_total": skipped_total,
            # هذا الرقم هو محاسبة الفلتر: لو الصفقات المرفوضة تربح أكثر من
            # المقبولة، فالفلتر يضرّك ولازم تنزّل العتبة أو تعيد التدريب.
            "skipped_win_rate": (round(skipped_wins / skipped_total * 100, 1)
                                 if skipped_total else None),
            "skipped_avg_r": (round(_one("SELECT AVG(r_multiple) FROM learning_samples "
                                          "WHERE resolved=1 AND taken=0"), 3)
                              if skipped_total else None),
        }
    finally:
        conn.close()


def resolve_pending_with_prices(fetch_fn, max_hold_days: int = 60,
                                db_path=None) -> int:
    """
    يحلّ نتائج العيّنات المعلّقة من بيانات الأسعار التاريخية.

    هذي الدالة هي اللي تخلي البوت يتعلّم حتى بوضع SIGNAL_ONLY بدون أي
    حساب وسيط: يحاكي ما كان بيصير لو دخلت الصفقة فعلاً — أي المستويين
    وصل السعر له أولاً، الوقف أو الهدف.

    fetch_fn(symbol) -> DataFrame فيه High/Low/Close ومفهرس بالتاريخ.

    قاعدة تحفّظية مقصودة: لو شمعة واحدة لمست الوقف والهدف معًا، نعتبرها
    وقف خسارة. البيانات اليومية ما تقول أيهما جاء أولاً، وافتراض الأفضل
    هنا يولّد بيانات تدريب متفائلة كذبًا فيتعلّم النموذج تفاؤلاً وهميًا.
    """
    import pandas as pd

    pending = pending_samples(db_path)
    if not pending:
        return 0

    by_symbol = {}
    for s in pending:
        by_symbol.setdefault(s["symbol"], []).append(s)

    resolved_count = 0
    for symbol, samples in by_symbol.items():
        try:
            df = fetch_fn(symbol)
        except Exception:
            continue  # سهم واحد فشل تحميله ما يوقف البقية
        if df is None or len(df) == 0:
            continue

        first_bar = df.index.min()

        for s in samples:
            entry_date = pd.Timestamp(s["entry_date"])

            # حارس أساسي: البيانات المجلوبة لازم تغطي تاريخ الإشارة نفسه.
            # لو بدأت بعده (نافذة جلب قصيرة، أو البوت كان متوقفًا فترة طويلة)
            # فنحن نجهل تمامًا ما صار بين الدخول وأول شمعة متاحة — وأول شمعة
            # قد تكون بعده بسنوات، فتُحتسب "تحقيق هدف" وهميًا ويتعلّم النموذج
            # درسًا مزيّفًا. لا نحلّها، ونتركها معلّقة لتشغيلة بنافذة أوسع.
            if first_bar > entry_date:
                age_days = (pd.Timestamp.now().normalize() - entry_date).days
                if age_days > max_hold_days * 3:
                    # قديمة جدًا ولا أمل بتغطيتها — نغلقها بلا تسمية بدل ما
                    # تبقى تتراكم بقائمة المعلّقات إلى الأبد
                    _abandon_sample(s["id"],
                                    f"بيانات غير متوفرة لتاريخ الإشارة ({age_days} يومًا)",
                                    db_path)
                continue

            after = df[df.index > entry_date]
            if after.empty:
                continue

            stop, target = s["stop_loss"], s["take_profit"]
            deadline = entry_date + timedelta(days=max_hold_days)
            exit_price = exit_date = reason = None

            for ts, bar in after.iterrows():
                hit_stop = stop is not None and float(bar["Low"]) <= stop
                hit_target = target is not None and float(bar["High"]) >= target
                if hit_stop:                       # الوقف له الأولوية عمدًا
                    exit_price, exit_date, reason = stop, ts, "وقف خسارة"
                    break
                if hit_target:
                    exit_price, exit_date, reason = target, ts, "تحقيق الهدف"
                    break
                if ts >= deadline:
                    exit_price, exit_date, reason = float(bar["Close"]), ts, \
                        f"انتهت مهلة {max_hold_days} يومًا"
                    break

            if exit_price is not None:
                resolve_sample(s["id"], exit_price, exit_date, reason, db_path)
                resolved_count += 1

    return resolved_count


def sample_row_to_training_pair(row: dict):
    """يحوّل صفًا من القاعدة إلى (متجه خصائص، تسمية) جاهز لـpartial_fit."""
    from .features import features_to_vector
    return features_to_vector(json.loads(row["features"])), int(row["label"])
