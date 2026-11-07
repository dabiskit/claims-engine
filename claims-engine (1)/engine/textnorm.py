"""
engine/textnorm.py — Normalisation unique du texte.

Miroir de kb.norm (SQL) et de norm() (frontend/localChecker.ts) : minuscules, sans
diacritiques, ligatures dépliées, apostrophes et espaces unifiés. Un test CI compare
les trois implémentations sur un corpus commun.

norm_with_offsets() conserve la correspondance caractère normalisé -> caractère
d'origine, pour surligner dans l'interface les passages détectés sur texte normalisé.
"""
from __future__ import annotations

import unicodedata

LETTERS = "A-Za-zÀ-ÖØ-öø-ÿ"  # classe portable Python/JavaScript (\w n'est pas Unicode en JS)

_SUBST = {"œ": "oe", "Œ": "oe", "æ": "ae", "Æ": "ae", "ß": "ss",
          "’": "'", "‘": "'", "ʼ": "'", "`": "'"}
_SPACES = {"\u00a0", "\u202f", "\u2009"}


def norm_with_offsets(text: str) -> tuple[str, list[int]]:
    out: list[str] = []
    offsets: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        for c in _SUBST.get(ch, ch):
            for d in unicodedata.normalize("NFKD", c):
                if unicodedata.combining(d):
                    continue
                for lc in d.lower():
                    if lc.isspace() or lc in _SPACES:
                        if prev_space:
                            continue
                        out.append(" ")
                        prev_space = True
                    else:
                        out.append(lc)
                        prev_space = False
                    offsets.append(i)
    if out and out[-1] == " ":
        out.pop()
        offsets.pop()
    return "".join(out), offsets


def norm(text: str) -> str:
    return norm_with_offsets(text)[0]


def original_span(offsets: list[int], start: int, end: int) -> tuple[int, int]:
    """Convertit un intervalle [start, end) du texte normalisé en intervalle d'origine."""
    if start >= end or not offsets:
        return (0, 0)
    return offsets[start], offsets[end - 1] + 1
