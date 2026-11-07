"""
engine/lexicon.py — Index lexical déterministe (couche 2 du pipeline).

Construit à partir de kb.compile_rules(). Miroir de frontend/localChecker.ts :
mêmes formes normalisées, mêmes frontières portables, mêmes neutralisants.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .textnorm import LETTERS, norm_with_offsets, original_span

SEV_RANK = {"NEUTRE": 0, "INFO": 1, "ORANGE": 2, "ROUGE": 3}
CONTROL_CATEGORIES = frozenset({"VERBE_EFFET", "THEME_PHYSIOLOGIQUE", "ATTRIBUT_FACTUEL"})
_BL = f"(?<![{LETTERS}0-9])"
_BR = f"(?![{LETTERS}0-9])"


def max_severity(*levels: str) -> str:
    return max(levels, key=lambda s: SEV_RANK[s])


@dataclass(frozen=True)
class LexEntry:
    code: str
    category: str
    legal_basis: str
    forms: tuple[str, ...]
    pattern: str | None
    severity: dict = field(hash=False, compare=False)
    topic: str | None = None
    neutralizers: tuple[str, ...] = ()

    @property
    def is_control(self) -> bool:
        return self.category in CONTROL_CATEGORIES


@dataclass(frozen=True)
class LexHit:
    hit_id: str
    entry: LexEntry
    surface: str          # forme normalisée trouvée
    start: int            # décalages dans le texte d'ORIGINE
    end: int


class LexiconIndex:
    def __init__(self, entries: list[LexEntry]):
        self.entries = entries
        self._compiled: list[tuple[LexEntry, re.Pattern]] = []
        for e in entries:
            alternatives = []
            if e.forms:
                forms = sorted({re.escape(f) for f in e.forms}, key=len, reverse=True)
                alternatives.append("(?:" + "|".join(forms) + ")")
            if e.pattern:
                alternatives.append("(?:" + e.pattern + ")")
            self._compiled.append((e, re.compile(_BL + "(?:" + "|".join(alternatives) + ")" + _BR)))

    @classmethod
    def from_compiled_rules(cls, rules: dict) -> "LexiconIndex":
        entries = [
            LexEntry(code=x["code"], category=x["category"], legal_basis=x["legal_basis"],
                     forms=tuple(x["forms"]), pattern=x.get("pattern"), severity=x["severity"],
                     topic=x.get("topic"), neutralizers=tuple(x.get("neutralizers") or ()))
            for x in rules["lexicon"]
        ]
        return cls(entries)

    def scan(self, text: str, *, categories: set[str] | None = None,
             exclude_control: bool = False) -> list[LexHit]:
        normalized, offsets = norm_with_offsets(text)
        found: list[tuple[int, int, LexEntry, str]] = []
        for entry, rx in self._compiled:
            if categories is not None and entry.category not in categories:
                continue
            if exclude_control and entry.is_control:
                continue
            for m in rx.finditer(normalized):
                left = normalized[max(0, m.start() - 40):m.start()].rstrip()
                if any(left.endswith(n) for n in entry.neutralizers):
                    continue
                found.append((m.start(), m.end(), entry, m.group(0)))
        found.sort(key=lambda f: (f[0], -f[1], f[2].code))
        hits = []
        for i, (s, e, entry, surface) in enumerate(found, start=1):
            os_, oe = original_span(offsets, s, e)
            hits.append(LexHit(f"H{i}", entry, surface, os_, oe))
        return hits
