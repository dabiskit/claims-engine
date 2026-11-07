"""
engine/conditions.py — Évaluation déterministe des conditions d'utilisation (sans LLM).

Statuts d'une condition : REMPLIE, NON_REMPLIE, INCONNUE, A_DOCUMENTER.
Statut d'une allégation pour un produit :
  UTILISABLE      toutes les conditions remplies            -> proposable, vert possible
  CONDITIONNELLE  au moins une INCONNUE / A_DOCUMENTER       -> proposable, 🟡 jusqu'à confirmation
  INUTILISABLE    au moins une NON_REMPLIE                   -> jamais proposée au réécrivain
La règle par défaut est « inconnu », jamais « conforme » : rien n'est supposé rempli.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Iterable

_MASS_TO_MG = {"mg": Decimal(1), "µg": Decimal("0.001"), "g": Decimal(1000)}


class CondStatus(str, Enum):
    REMPLIE = "REMPLIE"
    NON_REMPLIE = "NON_REMPLIE"
    INCONNUE = "INCONNUE"
    A_DOCUMENTER = "A_DOCUMENTER"


class ClaimUsability(str, Enum):
    UTILISABLE = "UTILISABLE"
    CONDITIONNELLE = "CONDITIONNELLE"
    INUTILISABLE = "INUTILISABLE"


@dataclass(frozen=True)
class Condition:
    id: str
    claim_code: str
    type: str
    params: dict
    notice_code: str | None = None
    notice_text: str | None = None


@dataclass(frozen=True)
class Ingredient:
    substance_code: str | None
    raw_label: str
    plant_part: str | None = None
    preparation: str | None = None
    amount: Decimal | None = None             # par portion journalière
    unit: str | None = None
    der_ratio: Decimal | None = None          # plante/extrait : 10 pour « 10:1 »
    marker_name: str | None = None
    marker_pct: Decimal | None = None


@dataclass(frozen=True)
class Product:
    ingredients: tuple[Ingredient, ...]
    portions_per_day: Decimal | None = None
    nrv_by_substance: dict = field(default_factory=dict, hash=False)   # code -> (valeur, unité)


@dataclass
class ConditionResult:
    condition: Condition
    status: CondStatus
    reason_fr: str
    computed: dict = field(default_factory=dict)


@dataclass
class ClaimEvaluation:
    claim_code: str
    usability: ClaimUsability
    results: list[ConditionResult]
    required_notices: list[tuple[str, str]]    # (code, texte)
    required_contexts: list[str]


def _to_mg(amount: Decimal, unit: str) -> Decimal | None:
    factor = _MASS_TO_MG.get(unit)
    return amount * factor if factor is not None else None


def _fmt(d: Decimal) -> str:
    s = f"{d.normalize():f}"
    return s.replace(".", ",")


def _matching(product: Product, substance_code: str) -> list[Ingredient]:
    return [i for i in product.ingredients if i.substance_code == substance_code]


def _eval_dose(c: Condition, product: Product, substance_code: str) -> ConditionResult:
    ings = _matching(product, substance_code)
    if not ings:
        return ConditionResult(c, CondStatus.NON_REMPLIE, "Substance absente de la composition connue")
    if any(i.amount is None for i in ings):
        return ConditionResult(c, CondStatus.INCONNUE, "Quantité non renseignée")
    threshold = Decimal(str(c.params["amount"]))
    unit = c.params["unit"]
    if unit == "PCT_VNR":
        nrv = product.nrv_by_substance.get(substance_code)
        if not nrv:
            return ConditionResult(c, CondStatus.INCONNUE, "VNR inconnue pour cette substance")
        threshold, unit = threshold * Decimal(str(nrv[0])) / 100, nrv[1]
    if unit in _MASS_TO_MG:
        totals = [_to_mg(i.amount, i.unit) for i in ings]
        if any(t is None for t in totals):
            return ConditionResult(c, CondStatus.INCONNUE, "Unités incompatibles (ex. UI) : confirmation requise")
        total, thr = sum(totals), _to_mg(threshold, unit)
    else:
        if any(i.unit != unit for i in ings):
            return ConditionResult(c, CondStatus.INCONNUE, f"Unité attendue : {unit}")
        total, thr = sum(i.amount for i in ings), threshold
    if c.params["basis"] == "PORTION_QUANTIFIEE":
        if not product.portions_per_day:
            return ConditionResult(c, CondStatus.INCONNUE, "Nombre de portions par jour inconnu")
        total = total / product.portions_per_day
    status = CondStatus.REMPLIE if total >= thr else CondStatus.NON_REMPLIE
    return ConditionResult(c, status, f"{_fmt(total)} vs seuil {_fmt(thr)}", {"value": str(total), "threshold": str(thr)})


def _eval_plant_equivalent(c: Condition, product: Product, substance_code: str) -> ConditionResult:
    ings = [i for i in _matching(product, substance_code) if i.plant_part == c.params["plant_part"]]
    if not ings:
        return ConditionResult(c, CondStatus.INCONNUE, "Partie de plante ou ingrédient non confirmé")
    total = Decimal(0)
    for i in ings:
        if i.amount is None or i.unit not in _MASS_TO_MG:
            return ConditionResult(c, CondStatus.INCONNUE, "Quantité massique non renseignée")
        mg = _to_mg(i.amount, i.unit)
        if i.preparation == "POUDRE_PLANTE_SECHE":
            total += mg
        elif i.preparation in ("EXTRAIT_SEC", "EXTRAIT_SEC_TITRE") and i.der_ratio:
            total += mg * i.der_ratio
        else:
            return ConditionResult(c, CondStatus.INCONNUE,
                                   "Préparation ou ratio plante/extrait (DER) inconnu : équivalent plante incalculable")
    thr = _to_mg(Decimal(str(c.params["amount"])), c.params["unit"])
    status = CondStatus.REMPLIE if total >= thr else CondStatus.NON_REMPLIE
    return ConditionResult(c, status, f"équivalent plante {_fmt(total)} mg vs seuil {_fmt(thr)} mg",
                           {"plant_equivalent_mg": str(total), "threshold_mg": str(thr)})


def _eval_marker(c: Condition, product: Product, substance_code: str) -> ConditionResult:
    ings = [i for i in _matching(product, substance_code)
            if i.marker_name and i.marker_name.lower() == str(c.params["marker"]).lower()]
    if not ings or any(i.amount is None or i.marker_pct is None or i.unit not in _MASS_TO_MG for i in ings):
        return ConditionResult(c, CondStatus.INCONNUE, "Titrage en marqueur non renseigné")
    total = sum(_to_mg(i.amount, i.unit) * i.marker_pct / 100 for i in ings)
    thr = _to_mg(Decimal(str(c.params["amount"])), c.params["unit"])
    return ConditionResult(c, CondStatus.REMPLIE if total >= thr else CondStatus.NON_REMPLIE,
                           f"marqueur {_fmt(total)} mg vs seuil {_fmt(thr)} mg")


def _eval_part(c: Condition, product: Product, substance_code: str) -> ConditionResult:
    ings = _matching(product, substance_code)
    parts = {i.plant_part for i in ings}
    if not ings or None in parts:
        return ConditionResult(c, CondStatus.INCONNUE, "Partie de plante non déclarée")
    allowed = set(c.params["plant_parts"])
    if parts <= allowed:
        return ConditionResult(c, CondStatus.REMPLIE, "Partie de plante concordante")
    return ConditionResult(c, CondStatus.NON_REMPLIE, f"Partie déclarée {sorted(parts)} hors de {sorted(allowed)}")


def evaluate_claim(claim_code: str, substance_code: str, conditions: Iterable[Condition], product: Product,
                   threshold_answers: dict[str, str] | None = None) -> ClaimEvaluation:
    """threshold_answers : condition_id -> 'OUI' | 'NON' | 'NE_SAIT_PAS' (écran des seuils groupés).
    Une réponse explicite prime sur la donnée extraite de la page."""
    answers = threshold_answers or {}
    results: list[ConditionResult] = []
    notices: list[tuple[str, str]] = []
    contexts: list[str] = []
    for c in conditions:
        if c.id in answers and c.type in ("DOSE_MIN_PORTION", "EQUIVALENT_PLANTE_MIN", "TENEUR_MARQUEUR_MIN"):
            a = answers[c.id]
            status = {"OUI": CondStatus.REMPLIE, "NON": CondStatus.NON_REMPLIE}.get(a, CondStatus.INCONNUE)
            results.append(ConditionResult(c, status, f"Réponse utilisateur : {a}"))
            continue
        if c.type == "DOSE_MIN_PORTION":
            results.append(_eval_dose(c, product, substance_code))
        elif c.type == "EQUIVALENT_PLANTE_MIN":
            results.append(_eval_plant_equivalent(c, product, substance_code))
        elif c.type == "TENEUR_MARQUEUR_MIN":
            results.append(_eval_marker(c, product, substance_code))
        elif c.type == "PARTIE_PLANTE_REQUISE":
            results.append(_eval_part(c, product, substance_code))
        elif c.type == "MENTION_OBLIGATOIRE":
            notices.append((c.notice_code or "", c.notice_text or ""))
            results.append(ConditionResult(c, CondStatus.REMPLIE, "Mention ajoutée par l'assembleur"))
        elif c.type == "CONTEXTE_OBLIGATOIRE":
            contexts.append(c.params["context_fr"])
            results.append(ConditionResult(c, CondStatus.REMPLIE, "Seules les variantes contenant le contexte sont proposées"))
        elif c.type == "AUCUNE_CONDITION_CHIFFREE":
            results.append(ConditionResult(c, CondStatus.A_DOCUMENTER,
                                           "La source ne fixe aucune condition chiffrée : niveau de preuve à documenter"))
        else:
            results.append(ConditionResult(c, CondStatus.INCONNUE, f"Type de condition non géré : {c.type}"))
    statuses = {r.status for r in results}
    if CondStatus.NON_REMPLIE in statuses:
        usability = ClaimUsability.INUTILISABLE
    elif statuses & {CondStatus.INCONNUE, CondStatus.A_DOCUMENTER}:
        usability = ClaimUsability.CONDITIONNELLE
    else:
        usability = ClaimUsability.UTILISABLE
    return ClaimEvaluation(claim_code, usability, results, notices, contexts)


@dataclass(frozen=True)
class ThresholdQuestion:
    key: str
    text_fr: str
    condition_ids: tuple[str, ...]
    product_ids: tuple[str, ...]


def build_threshold_questions(pending: Iterable[tuple[str, ConditionResult, str, str]]) -> list[ThresholdQuestion]:
    """pending : (product_id, résultat INCONNUE, nom FR de la substance, 'ELEMENT'|'SUBSTANCE').
    Regroupe par (substance, seuil, unité, base) : une question binaire couvre tout le catalogue."""
    groups: dict[tuple, dict] = {}
    for product_id, r, substance_name, expressed_as in pending:
        c = r.condition
        if r.status is not CondStatus.INCONNUE or c.type not in ("DOSE_MIN_PORTION", "EQUIVALENT_PLANTE_MIN"):
            continue
        key = (substance_name, c.type, str(c.params["amount"]), c.params["unit"], c.params.get("basis", ""))
        g = groups.setdefault(key, {"conditions": set(), "products": set(), "expressed_as": expressed_as})
        g["conditions"].add(c.id)
        g["products"].add(product_id)
    questions = []
    for (name, ctype, amount, unit, basis), g in sorted(groups.items()):
        qty = f"{_fmt(Decimal(amount))} {unit}"
        if ctype == "EQUIVALENT_PLANTE_MIN":
            text = f"La portion journalière apporte-t-elle au moins {qty} d'équivalent plante sèche de {name} ?"
        else:
            portion = "la portion journalière" if basis == "PORTION_JOURNALIERE" else "chaque portion"
            element = " (exprimé en élément, pas en sel)" if g["expressed_as"] == "ELEMENT" else ""
            text = f"{portion.capitalize()} contient-elle au moins {qty} de {name.lower()}{element} ?"
        questions.append(ThresholdQuestion(f"{name}|{ctype}|{amount}|{unit}|{basis}", text,
                                           tuple(sorted(g["conditions"])), tuple(sorted(g["products"]))))
    return questions
