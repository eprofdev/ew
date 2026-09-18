"""بناء كون التداول: قائمة الرموز التي سيُختبر عليها كل شيء.

تحذير منهجي لا يجوز تجاهله — **انحياز البقاء (Survivorship Bias)**:
نقطة /stocks تعطي الرموز **المدرجة اليوم فقط**. كل شركة أفلست أو شُطبت أو
استُحوذ عليها اختفت من القائمة. الدليل من هذه الجلسة نفسها: AMED
(Amedisys) شُطبت بعد استحواذ UnitedHealth فلم تعد موجودة في الكون إطلاقاً.

الأثر على النتائج: أي backtest على هذا الكون يختبر **الناجين فقط**، فيبالغ
في الأداء — وفي أسهم السنتات المبالغة ضخمة لأن الشطب مصير شائع. عالج ذلك
بإضافة رموز مشطوبة يدوياً عبر `extra_symbols`، أو اقرأ النتيجة كحد أعلى
متفائل لا كتوقع.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

DEFAULT_TYPES = ("Common Stock",)
EXCLUDED_TYPES = (
    "Warrant", "Unit", "Preferred Stock", "Exchange-Traded Note",
    "Depositary Receipt", "American Depositary Receipt", "Limited Partnership",
)


@dataclass
class UniverseEntry:
    symbol: str
    name: str = ""
    exchange: str = ""
    mic_code: str = ""
    type: str = ""
    country: str = ""

    @classmethod
    def from_api(cls, row: dict) -> "UniverseEntry":
        return cls(
            symbol=str(row.get("symbol", "")).upper(),
            name=str(row.get("name", "")),
            exchange=str(row.get("exchange", "")),
            mic_code=str(row.get("mic_code", "")),
            type=str(row.get("type", "")),
            country=str(row.get("country", "")),
        )


@dataclass
class Universe:
    entries: List[UniverseEntry] = field(default_factory=list)
    source: str = ""
    fetched_at: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def symbols(self) -> List[str]:
        return [e.symbol for e in self.entries]

    def __len__(self) -> int:
        return len(self.entries)

    def filter(
        self,
        types: Sequence[str] = DEFAULT_TYPES,
        exchanges: Optional[Sequence[str]] = None,
        country: Optional[str] = "United States",
        exclude: Optional[Set[str]] = None,
    ) -> "Universe":
        wanted_types = {t.lower() for t in types} if types else None
        wanted_ex = {e.upper() for e in exchanges} if exchanges else None
        skip = {s.upper() for s in (exclude or set())}

        kept = [
            e
            for e in self.entries
            if (wanted_types is None or e.type.lower() in wanted_types)
            and (wanted_ex is None or e.exchange.upper() in wanted_ex)
            and (country is None or e.country == country)
            and e.symbol not in skip
            and e.symbol
        ]
        return Universe(entries=kept, source=self.source, fetched_at=self.fetched_at,
                        notes=list(self.notes))

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "source": self.source,
                    "fetched_at": self.fetched_at,
                    "notes": self.notes,
                    "entries": [e.__dict__ for e in self.entries],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Universe":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            entries=[UniverseEntry(**row) for row in data.get("entries", [])],
            source=data.get("source", ""),
            fetched_at=data.get("fetched_at", 0.0),
            notes=data.get("notes", []),
        )


SURVIVORSHIP_WARNING = (
    "انحياز البقاء: القائمة تضم المدرجين اليوم فقط. المشطوبون والمفلسون غائبون، "
    "لذا اقرأ النتيجة كحد أعلى متفائل لا كتوقع واقعي."
)


def fetch_universe(
    provider,
    exchange: Optional[str] = None,
    country: Optional[str] = "United States",
    extra_symbols: Sequence[str] = (),
) -> Universe:
    """يجلب قائمة الرموز من /stocks عبر مزوّد Twelve Data."""
    params: Dict[str, object] = {}
    if exchange:
        params["exchange"] = exchange
    if country:
        params["country"] = country

    payload = provider._request("stocks", params)
    rows = payload.get("data") or []
    entries = [UniverseEntry.from_api(row) for row in rows]

    known = {e.symbol for e in entries}
    for symbol in extra_symbols:
        symbol = symbol.upper().strip()
        if symbol and symbol not in known:
            entries.append(UniverseEntry(symbol=symbol, name="(مضاف يدوياً)", type="Common Stock"))

    universe = Universe(
        entries=entries,
        source=f"twelvedata/stocks exchange={exchange or 'all'} country={country or 'all'}",
        fetched_at=time.time(),
        notes=[SURVIVORSHIP_WARNING],
    )
    return universe
