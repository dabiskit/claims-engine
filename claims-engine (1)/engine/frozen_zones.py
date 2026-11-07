"""
engine/frozen_zones.py — Zones gelées.

Contrat vérifié par assert_frozen_preserved() sur toute sortie (LLM ou édition manuelle) :
  1. Chaque zone PROTÉGÉE de la source (dose, posologie, % VNR, prix, URL, e-mail,
     avertissement obligatoire, nom de produit, durée de cure) se retrouve dans la
     sortie avec la même valeur canonique, exactement autant de fois.
  2. Une zone SUPPRIMABLE (promesse chiffrée « en 7 jours », perte de poids chiffrée,
     nombre isolé) peut disparaître, jamais être modifiée ni dupliquée.
  3. Aucun chiffre n'apparaît hors d'une zone gelée ou d'un identifiant de substance
     (B6, Q10, KSM-66, oméga-3).

Valeur canonique : « deux gélules par jour » == « 2 gélules par jour », « 2,0 mg » ==
« 2 mg », « 500 mcg » == « 500 µg ». Changer une seule valeur numérique change la clé.

Les motifs n'utilisent que la syntaxe commune Python/JavaScript (pas de \\b, pas de
drapeaux en ligne) : export_patterns_for_frontend() les livre tels quels à l'éditeur.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from .textnorm import LETTERS, norm


class FrozenKind(str, Enum):
    AVERTISSEMENT_OBLIGATOIRE = "AVERTISSEMENT_OBLIGATOIRE"
    NOM_PRODUIT = "NOM_PRODUIT"
    URL = "URL"
    EMAIL = "EMAIL"
    PROMESSE_CHIFFREE = "PROMESSE_CHIFFREE"
    POURCENTAGE_VNR = "POURCENTAGE_VNR"
    POSOLOGIE = "POSOLOGIE"
    DUREE_CURE = "DUREE_CURE"
    PRIX = "PRIX"
    POIDS_PERTE = "POIDS_PERTE"
    DOSE = "DOSE"
    NOMBRE = "NOMBRE"


REMOVABLE_KINDS = frozenset({FrozenKind.PROMESSE_CHIFFREE, FrozenKind.POIDS_PERTE, FrozenKind.NOMBRE})

BL = f"(?<![{LETTERS}0-9])"
BR = f"(?![{LETTERS}0-9])"
NUM = r"(?:\d{1,3}(?:[ \u00A0\u202F]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
NUM_WORD = r"(?:une?|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|onze|douze|quinze|vingt|trente|soixante|demie?)"
QTY = rf"(?:{NUM}|{NUM_WORD})(?:\s*(?:à|a|-|–|ou)\s*(?:{NUM}|{NUM_WORD}))?(?:\s+et\s+demie?)?"
UNIT = r"(?:mg|µg|μg|mcg|g|kg|ml|cl|l|UI|IU|kcal|kJ|UFC|CFU|milliards?\s+d['’]UFC)"
GALENIC = (r"(?:g[ée]lules?|comprim[ée]s?|capsules?|softgels?|sticks?|sachets?|ampoules?|doses?|gouttes?"
           r"|pastilles?|gommes?|gummies|cuill[eè]res?(?:\s+(?:à|a)\s+(?:caf[ée]|soupe))?|mesurettes?"
           r"|pulv[ée]risations?|sprays?)")
FREQ = (rf"(?:\s*(?:par|/)\s*(?:jour|j|prise)|\s+{QTY}\s+fois\s+par\s+(?:jour|semaine)"
        r"|\s+(?:le\s+matin|le\s+soir|au\s+coucher|matin\s+et\s+soir"
        r"|avant\s+le\s+coucher|au\s+cours\s+du\s+repas|pendant\s+les\s+repas))")

IDENTIFIER_PATTERN = rf"{BL}(?:[A-Za-z]{{1,4}}-?\d{{1,3}}|om[ée]ga[- ]?\d{{1,2}}){BR}"

# Ordre = priorité. Une zone déjà prise n'est jamais recouverte par un motif suivant.
STATIC_PATTERNS: list[tuple[FrozenKind | None, str]] = [
    (FrozenKind.AVERTISSEMENT_OBLIGATOIRE,
     r"ne\s+pas\s+d[ée]passer\s+la\s+dose\s+journali[eè]re\s+(?:recommand[ée]e|conseill[ée]e|indiqu[ée]e)"),
    (FrozenKind.AVERTISSEMENT_OBLIGATOIRE,
     r"tenir\s+hors\s+de\s+(?:la\s+)?port[ée]e\s+des\s+(?:jeunes\s+)?enfants"),
    (FrozenKind.AVERTISSEMENT_OBLIGATOIRE,
     r"(?:les\s+)?compl[ée]ments?\s+alimentaires?\s+ne\s+(?:doivent|doit)\s+pas\s+[êe]tre\s+utilis[ée]s?\s+comme"
     r"\s+(?:des\s+)?substituts?\s+d['’]un\s+r[ée]gime\s+alimentaire\s+vari[ée](?:\s+et\s+[ée]quilibr[ée])?"
     r"(?:\s+et\s+d['’]un\s+mode\s+de\s+vie\s+sain)?"),
    (FrozenKind.AVERTISSEMENT_OBLIGATOIRE, r"d[ée]conseill[ée]e?s?\s+(?:aux|chez\s+les)\s+[^.!?⟦⟧]{3,200}"),
    (FrozenKind.AVERTISSEMENT_OBLIGATOIRE,
     r"(?:demander|demandez|prendre|prenez)\s+(?:l['’])?avis\s+(?:d['’]un|de\s+votre|[àa]\s+votre|[àa]\s+un)\s+"
     r"(?:m[ée]decin|pharmacien|professionnel\s+de\s+sant[ée])[^.!?⟦⟧]{0,200}"),
    (FrozenKind.URL, r"https?://[^\s<>\"'⟦⟧]+|www\.[^\s<>\"'⟦⟧]+"),
    (FrozenKind.EMAIL, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    (None, IDENTIFIER_PATTERN),  # identifiants de substance : masqués, jamais gelés
    (FrozenKind.PRIX, rf"(?<![{LETTERS}0-9])-\s?{NUM}\s*%"),                       # remise « -25% »
    (FrozenKind.POSOLOGIE, rf"{BL}{QTY}\s+(?:minutes?|min|heures?|h)\s+(?:avant|apr[eè]s)\s+"
                           r"(?:le|les|chaque|votre)\s+(?:coucher|repas|entra[iî]nement|effort)"),  # moment de prise
    (FrozenKind.DOSE, rf"{BL}(?:ratio\s+)?{NUM}\s*:\s*{NUM}{BR}"),                  # ratio plante/extrait
    (FrozenKind.PROMESSE_CHIFFREE,
     rf"{BL}(?:en|d[eè]s|apr[eè]s|sous)\s+(?:seulement\s+|moins\s+de\s+|à\s+peine\s+)?{QTY}\s+"
     rf"(?:jours?|nuits?|semaines?|heures?|h|minutes?|min|mois){BR}"),
    (FrozenKind.PROMESSE_CHIFFREE,
     rf"{BL}{NUM}\s*%\s*(?:efficaces?|naturel(?:le)?s?|de\s+satisfaction|de\s+r[ée]ussite|garanti(?:e|s)?"
     rf"|prouv[ée]e?s?){BR}"),
    (FrozenKind.POURCENTAGE_VNR,
     rf"{BL}{NUM}\s*%\s*(?:des\s+|de\s+la\s+)?(?:VNR|AR|AJR|valeurs?\s+nutritionnelles?\s+de\s+r[ée]f[ée]rence){BR}"),
    (FrozenKind.POSOLOGIE, rf"{BL}{QTY}\s+{GALENIC}(?:\s+(?:de\s+)?{NUM}\s*{UNIT})?{FREQ}{{0,2}}{BR}"),
    (FrozenKind.DUREE_CURE, rf"{BL}(?:cure\s+de\s+|pendant\s+)?{QTY}\s+(?:jours?|semaines?|mois){BR}"),
    (FrozenKind.PRIX, rf"{BL}{NUM}\s*(?:€|EUR|euros?)(?![{LETTERS}0-9])|€\s*{NUM}{BR}"),
    (FrozenKind.POIDS_PERTE, rf"{BL}{QTY}\s*(?:kg|kilos?|livres){BR}"),
    (FrozenKind.DOSE, rf"{BL}{NUM}\s*{UNIT}(?:\s*(?:/|par)\s*(?:jour|j|portion|dose|g[ée]lule|comprim[ée]))?{BR}"),
    (FrozenKind.NOMBRE, rf"{BL}\d+(?:ère|ere|ème|eme|er|re|e|nde?){BR}"),
    (FrozenKind.NOMBRE, rf"{BL}{NUM}{BR}"),
]
_FLAGS = re.IGNORECASE
_COMPILED = [(k, re.compile(p, _FLAGS)) for k, p in STATIC_PATTERNS]

_NUMWORDS = {"un": "1", "une": "1", "deux": "2", "trois": "3", "quatre": "4", "cinq": "5", "six": "6",
             "sept": "7", "huit": "8", "neuf": "9", "dix": "10", "onze": "11", "douze": "12",
             "quinze": "15", "vingt": "20", "trente": "30", "soixante": "60", "demi": "0.5", "demie": "0.5"}
_SINGULAR = {"gelules": "gelule", "comprimes": "comprime", "capsules": "capsule", "softgels": "softgel",
             "sticks": "stick", "sachets": "sachet", "ampoules": "ampoule", "doses": "dose",
             "gouttes": "goutte", "pastilles": "pastille", "gommes": "gomme", "cuilleres": "cuillere",
             "mesurettes": "mesurette", "pulverisations": "pulverisation", "sprays": "spray",
             "jours": "jour", "nuits": "nuit", "semaines": "semaine", "heures": "heure",
             "minutes": "minute", "euros": "euro", "kilos": "kilo", "milliards": "milliard"}
_UNITS = {"μg": "ug", "µg": "ug", "mcg": "ug", "iu": "ui", "cfu": "ufc", "eur": "euro", "€": "euro"}


class FrozenZoneViolation(Exception):
    def __init__(self, message: str, details: dict):
        super().__init__(f"{message} : {details}")
        self.details = details


@dataclass
class FrozenZone:
    kind: FrozenKind
    start: int
    end: int
    text: str
    canonical: str
    ref: str = ""

    @property
    def removable(self) -> bool:
        return self.kind in REMOVABLE_KINDS


@dataclass
class FrozenScan:
    zones: list[FrozenZone] = field(default_factory=list)
    identifier_spans: list[tuple[int, int]] = field(default_factory=list)


def canonical(kind: FrozenKind, text: str) -> str:
    t = norm(text)
    t = re.sub(r"(?<=\d) (?=\d{3}(?!\d))", "", t)
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    t = t.replace("μg", "ug").replace("µg", "ug")
    out = []
    for tok in re.findall(r"\d+(?:\.\d+)?|[a-z]+|[%€/]", t):
        if re.fullmatch(r"\d+\.\d+", tok):
            tok = tok.rstrip("0").rstrip(".")
        tok = _NUMWORDS.get(tok, tok)
        tok = _SINGULAR.get(tok, tok)
        tok = _UNITS.get(tok, tok)
        out.append(tok)
    return f"{kind.value}|{' '.join(out)}"


def scan_frozen(text: str, *, product_names: Iterable[str] = (),
                extra_identifiers: Iterable[str] = ()) -> FrozenScan:
    """Extrait les zones gelées d'un texte brut (sans marqueurs ⟦…⟧)."""
    scan = FrozenScan()
    taken: list[tuple[int, int]] = []

    def is_free(s: int, e: int) -> bool:
        return all(e <= a or s >= b for a, b in taken)

    dynamic: list[tuple[FrozenKind | None, re.Pattern]] = []
    names = sorted({n for n in product_names if n.strip()}, key=len, reverse=True)
    if names:
        dynamic.append((FrozenKind.NOM_PRODUIT,
                        re.compile(BL + "(?:" + "|".join(re.escape(n) for n in names) + ")" + BR, _FLAGS)))
    idents = sorted({i for i in extra_identifiers if re.search(r"\d", i)}, key=len, reverse=True)
    if idents:
        dynamic.append((None, re.compile(BL + "(?:" + "|".join(re.escape(i) for i in idents) + ")" + BR, _FLAGS)))

    ordered = _COMPILED[:4] + dynamic + _COMPILED[4:]
    for kind, rx in ordered:
        for m in rx.finditer(text):
            s, e = m.span()
            if s == e or not is_free(s, e):
                continue
            taken.append((s, e))
            if kind is None:
                scan.identifier_spans.append((s, e))
            else:
                scan.zones.append(FrozenZone(kind, s, e, m.group(0), canonical(kind, m.group(0))))
    scan.zones.sort(key=lambda z: z.start)
    for i, z in enumerate(scan.zones, start=1):
        z.ref = f"F{i}"
    return scan


def stray_digits(text: str, scan: FrozenScan) -> list[int]:
    covered = [(z.start, z.end) for z in scan.zones] + scan.identifier_spans
    return [m.start() for m in re.finditer(r"\d", text)
            if not any(a <= m.start() < b for a, b in covered)]


def assert_frozen_preserved(source: FrozenScan, output_text: str, *, product_names: Iterable[str] = (),
                            extra_identifiers: Iterable[str] = ()) -> FrozenScan:
    """Lève FrozenZoneViolation si un seul chiffre, une seule unité ou une seule zone
    protégée a été altéré, supprimé, dupliqué ou inventé."""
    out = scan_frozen(output_text, product_names=product_names, extra_identifiers=extra_identifiers)
    src_p = Counter(z.canonical for z in source.zones if not z.removable)
    out_p = Counter(z.canonical for z in out.zones if not z.removable)
    if src_p != out_p:
        raise FrozenZoneViolation("Zone protégée absente, modifiée ou dupliquée", {
            "manquantes": sorted((src_p - out_p).elements()),
            "en_trop": sorted((out_p - src_p).elements())})
    src_r = Counter(z.canonical for z in source.zones if z.removable)
    out_r = Counter(z.canonical for z in out.zones if z.removable)
    created = out_r - src_r
    if created:
        raise FrozenZoneViolation("Valeur chiffrée créée ou modifiée", {"en_trop": sorted(created.elements())})
    stray = stray_digits(output_text, out)
    if stray:
        raise FrozenZoneViolation("Chiffre hors zone gelée", {"positions": stray})
    return out


class SpacyPosologyLayer:
    """Couche complémentaire côté serveur : rattrape des posologies que les motifs ne
    couvrent pas (« 1 gélule ½ », « deux à trois prises »). spacy.blank('fr') suffit :
    l'attribut LIKE_NUM du français reconnaît chiffres et nombres en lettres. Elle ne
    peut qu'AJOUTER des zones protégées (jamais en retirer) : elle rend le contrôle plus
    strict, jamais plus permissif."""

    GALENIC_FORMS = ["gelule", "gelules", "gélule", "gélules", "comprime", "comprimes", "comprimé",
                     "comprimés", "capsule", "capsules", "prise", "prises", "sachet", "sachets",
                     "stick", "sticks", "ampoule", "ampoules", "goutte", "gouttes", "dose", "doses"]

    def __init__(self) -> None:
        import spacy
        from spacy.matcher import Matcher
        self.nlp = spacy.blank("fr")
        self.matcher = Matcher(self.nlp.vocab)
        g = {"LOWER": {"IN": self.GALENIC_FORMS}}
        self.matcher.add("POSOLOGIE", [
            [{"LIKE_NUM": True}, g],
            [{"LIKE_NUM": True}, {"LOWER": {"IN": ["à", "a", "ou"]}}, {"LIKE_NUM": True}, g],
            [{"LIKE_NUM": True}, g, {"TEXT": {"IN": ["½", "1/2"]}}],
        ])

    def extend(self, text: str, scan: FrozenScan) -> FrozenScan:
        doc = self.nlp(text)
        for _, s, e in sorted(self.matcher(doc), key=lambda m: (m[1], -m[2])):
            a, b = doc[s:e].start_char, doc[s:e].end_char
            inside = [z for z in scan.zones if a <= z.start and z.end <= b]
            partial = [z for z in scan.zones if z not in inside and not (b <= z.start or a >= z.end)]
            if partial:
                continue  # chevauchement ambigu : on garde la zone regex, plus conservatrice
            if any(z.start == a and z.end == b for z in inside):
                continue
            scan.zones = [z for z in scan.zones if z not in inside]
            scan.zones.append(FrozenZone(FrozenKind.POSOLOGIE, a, b, text[a:b],
                                         canonical(FrozenKind.POSOLOGIE, text[a:b])))
        scan.zones.sort(key=lambda z: z.start)
        for i, z in enumerate(scan.zones, start=1):
            z.ref = f"F{i}"
        return scan


def export_patterns_for_frontend() -> list[dict]:
    """Motifs livrés à l'éditeur (drapeaux JavaScript : 'giu')."""
    return [{"kind": k.value if k else "IDENTIFIANT", "source": p,
             "removable": bool(k and k in REMOVABLE_KINDS), "mask_only": k is None}
            for k, p in STATIC_PATTERNS]
