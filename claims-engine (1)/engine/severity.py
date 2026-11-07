"""
engine/severity.py — Grille de sévérité : quel niveau pour quel constat, et pourquoi.

La grille est une donnée, pas du code dispersé : elle est affichée telle quelle à l'expert
(/app/base/grille), qui valide, corrige ou rejette chaque règle. Les niveaux :
  ROUGE         à revoir en priorité : interdiction nette ou condition légale non remplie ;
  ORANGE        à revoir : allégation probable, ou formulation à rapprocher du libellé officiel ;
  CONDITIONNEL  à confirmer : le caractère d'allégation ou la condition dépend d'un élément à vérifier.

Statut : proposition de travail, à valider par l'expert avant la première release.
"""
from __future__ import annotations

LEVELS_ORDER = ["CONDITIONNEL", "ORANGE", "ROUGE"]

GRID = [
    {"id": "S01", "code": "IA_PROPRIETE_THERAPEUTIQUE", "level": "ROUGE", "basis": "INCO_1169_2011_ART7_3",
     "when": "Propriété de prévenir, traiter ou guérir une maladie, ou maladie présentée comme cible, même sans mot médical.",
     "why": "Interdiction générale pour toute denrée alimentaire, sans exception possible."},
    {"id": "S02", "code": "IA_REDUCTION_RISQUE_NON_AUTORISEE", "level": "ROUGE", "basis": "R1924_2006_ART10_1",
     "when": "Réduction d'un facteur de risque de maladie sans allégation art. 14 autorisée.",
     "why": "Seules les allégations de réduction de risque autorisées peuvent être utilisées."},
    {"id": "S03", "code": "IA_ALLEGATION_SANS_CORRESPONDANCE", "level": "ROUGE", "basis": "R1924_2006_ART10_1",
     "when": "Effet sur une fonction de l'organisme attribué au produit ou à un ingrédient, sans allégation autorisée correspondante, le caractère d'allégation étant net.",
     "why": "Une allégation de santé non autorisée est interdite."},
    {"id": "S04", "code": "IA_PROPRIETE_TECHNOLOGIQUE", "level": "CONDITIONNEL", "basis": "R1924_2006_ART10_1",
     "when": "Propriété du produit ou de la matière première (absorption, biodisponibilité, taille des peptides, procédé, forme galénique) sans bénéfice de santé énoncé.",
     "why": "N'est une allégation de santé que si un bénéfice pour l'organisme est suggéré : à confirmer au cas par cas."},
    {"id": "S05", "code": "IA_FORMULATION_NON_FIDELE", "level": "ORANGE", "basis": "R432_2012_CONDITIONS",
     "when": "Allégation autorisée reprise avec une formulation amplifiée (« essentiel », « renforce », « booste »).",
     "why": "Le sens doit rester celui du libellé officiel ; la correction consiste à reprendre ce libellé."},
    {"id": "S06", "code": "IA_REFERENCE_GENERALE", "level": "ORANGE", "basis": "R1924_2006_ART10_3",
     "when": "Bienfait général (« vitalité », « bien-être ») sans allégation spécifique à côté.", "why": "Art. 10§3 : la référence générale doit être accompagnée d'une allégation autorisée."},
    {"id": "S07", "code": "REFERENCE_GENERALE_ART10_3", "level": "ORANGE", "basis": "R1924_2006_ART10_3",
     "when": "Terme de fonction (« peau », « articulations ») sans allégation autorisée dans le même passage.", "why": "Idem S06, détection par le lexique."},
    {"id": "S08", "code": "CONDITION_NON_REMPLIE", "level": "ROUGE", "basis": "R432_2012_CONDITIONS",
     "when": "Allégation autorisée utilisée alors que sa condition (dose) n'est pas remplie.", "why": "L'allégation n'est permise que si la condition d'utilisation est respectée."},
    {"id": "S09", "code": "MENTION_OBLIGATOIRE_ABSENTE", "level": "ROUGE", "basis": "R432_2012_CONDITIONS",
     "when": "Allégation autorisée dont la mention obligatoire associée est absente.", "why": "La mention fait partie des conditions d'utilisation."},
    {"id": "S10", "code": "ALLEGATION_SUBSTANCE_ABSENTE", "level": "ROUGE", "basis": "R1924_2006_ART10_1",
     "when": "Allégation sur une substance absente de la composition.", "why": "L'allégation ne peut porter que sur une substance présente."},
    {"id": "S11", "code": "CONDITION_A_CONFIRMER", "level": "CONDITIONNEL", "basis": "R432_2012_CONDITIONS",
     "when": "Allégation autorisée dont la condition n'a pas pu être vérifiée (dose inconnue, condition rédigée).", "why": "À confirmer avec la composition exacte."},
    {"id": "S12", "code": "IA_ALLEGATION_BOTANIQUE_NON_COUVERTE", "level": "ORANGE", "basis": "R1924_2006_ART28",
     "when": "Allégation sur une plante dont les allégations « on-hold » ne sont pas encore intégrées.", "why": "Régime transitoire : à vérifier sur la liste des allégations en attente."},
    {"id": "S13", "code": "*IA*", "level": "-1", "basis": None,
     "when": "Constat de l'IA dont elle juge le caractère d'allégation incertain (certitude « basse »).",
     "why": "Un niveau en dessous (rouge → orange → à confirmer), pour ne pas mettre au même rang les cas nets et les cas discutables."},
    {"id": "S14", "code": "*DOUBLON*", "level": "=", "basis": None,
     "when": "Même passage relevé à la fois par le lexique et par l'IA.",
     "why": "Compté une seule fois : le constat de l'IA, plus précis, est gardé ; le terme du lexique y est rattaché."},
    {"id": "S15", "code": "*LEXIQUE*", "level": "lexique", "basis": None,
     "when": "Terme du lexique des formulations à risque.",
     "why": "Gravité fixée terme par terme (fiche produit / article lié à un produit / article sans lien) : voir le lexique."},
]
_BY_CODE = {r["code"]: r for r in GRID if not r["code"].startswith("*")}


def base_level(code: str, default: str) -> str:
    r = _BY_CODE.get(code)
    return r["level"] if r else default


def downgrade(level: str) -> str:
    if level not in LEVELS_ORDER:
        return level
    i = LEVELS_ORDER.index(level)
    return LEVELS_ORDER[max(0, i - 1)]


def level_for(code: str, default: str, certainty: str | None = None) -> str:
    """Niveau d'un constat de l'IA selon la grille, abaissé d'un cran si l'IA n'est pas certaine (S13)."""
    level = base_level(code, default)
    if code.startswith("IA_") and certainty == "BASSE":
        level = downgrade(level)
    return level


def rule_for(code: str) -> dict | None:
    return _BY_CODE.get(code)
