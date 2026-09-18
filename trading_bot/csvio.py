"""حفظ وتحميل الشموع بصيغة CSV — لاختبارات قابلة للتكرار دون شبكة."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence

from .models import Candle

HEADER = ["datetime", "open", "high", "low", "close", "volume"]


def save_candles_csv(path: str | Path, candles: Sequence[Candle]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for c in sorted(candles, key=lambda c: c.ts):
            stamp = datetime.fromtimestamp(c.ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow([stamp, c.open, c.high, c.low, c.close, c.volume])
    return path


def load_candles_csv(path: str | Path) -> List[Candle]:
    out: List[Candle] = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            stamp = row["datetime"].strip()
            fmt = "%Y-%m-%d %H:%M:%S" if " " in stamp else "%Y-%m-%d"
            ts = int(datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc).timestamp())
            out.append(
                Candle(
                    ts=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
    out.sort(key=lambda c: c.ts)
    return out
