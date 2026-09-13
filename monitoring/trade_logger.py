"""
trade_logger.py
طبقة تسجيل: كل صفقة، إشارة، وخطأ يُسجَّل في قاعدة بيانات SQLite محلية.
هذي القاعدة هي مصدر الحقيقة الواحد لكل من: التقرير اليومي، لوحة الويب،
وأي تحليل يُرسل لاحقًا لـ Claude.
"""
import sqlite3
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "trading_log.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,              -- BUY / SELL
        price REAL NOT NULL,
        shares REAL,
        stop_loss REAL,
        take_profit REAL,
        reason TEXT,
        mode TEXT,                       -- PAPER / LIVE / BACKTEST
        pnl REAL,                        -- يُملأ فقط عند إغلاق الصفقة (SELL)
        timestamp TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        context TEXT NOT NULL,           -- أي جزء من الكود صار فيه الخطأ
        error_message TEXT NOT NULL,
        traceback TEXT,
        symbol TEXT,
        resolved INTEGER DEFAULT 0,
        timestamp TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS daily_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL UNIQUE,
        equity REAL,
        cash REAL,
        open_positions INTEGER,
        mode TEXT,
        timestamp TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()


def log_trade(symbol: str, side: str, price: float, shares: float = None,
              stop_loss: float = None, take_profit: float = None,
              reason: str = "", mode: str = "PAPER", pnl: float = None):
    conn = get_connection()
    conn.execute(
        """INSERT INTO trades (symbol, side, price, shares, stop_loss, take_profit,
                                reason, mode, pnl, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, side, price, shares, stop_loss, take_profit, reason, mode, pnl,
         datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def log_error(context: str, exc: Exception, symbol: str = None):
    """يسجل أي استثناء يصير أثناء التشغيل، مع الـ traceback كامل للمراجعة لاحقًا."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO errors (context, error_message, traceback, symbol, timestamp)
           VALUES (?, ?, ?, ?, ?)""",
        (context, str(exc), traceback.format_exc(), symbol,
         datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def log_snapshot(equity: float, cash: float, open_positions: int, mode: str = "PAPER"):
    """لقطة يومية لرأس المال — تُستخدم لرسم منحنى الأداء بمرور الوقت."""
    today = datetime.now(timezone.utc).date().isoformat()
    conn = get_connection()
    conn.execute(
        """INSERT INTO daily_snapshots (date, equity, cash, open_positions, mode, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
               equity=excluded.equity, cash=excluded.cash,
               open_positions=excluded.open_positions, timestamp=excluded.timestamp""",
        (today, equity, cash, open_positions, mode, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def get_trades(limit: int = 200):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_errors(limit: int = 50, unresolved_only: bool = False):
    conn = get_connection()
    query = "SELECT * FROM errors"
    if unresolved_only:
        query += " WHERE resolved = 0"
    query += " ORDER BY timestamp DESC LIMIT ?"
    rows = conn.execute(query, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_equity_curve():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM daily_snapshots ORDER BY date ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_error_resolved(error_id: int):
    conn = get_connection()
    conn.execute("UPDATE errors SET resolved = 1 WHERE id = ?", (error_id,))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"قاعدة البيانات جاهزة: {DB_PATH}")
