"""
webapp/suggest.py — Liens vers les textes officiels et propositions de remplacement.

Les propositions sont déterministes et ne viennent que de la base :
  1. un libellé de substitution pour les quelques termes dont la remédiation est connue (« Posologie »
     -> « Conseils d'utilisation »), et les ponts sémantiques de la base rédigée (« insomnie » ->
     « endormissement ») ;
  2. les allégations autorisées qui portent sur le même thème que le passage, d'abord celles des
     ingrédients de la composition, avec leur statut (utilisable, à confirmer, non utilisable) ;
  3. à défaut, les ingrédients qui disposent d'une allégation autorisée sur ce thème.
Rien n'est inventé : une proposition reprend un libellé officiel, ou dit qu'il faut supprimer.
Le réécrivain IA reste disponible pour reformuler la phrase entière.
"""
from __future__ import annotations

import re

from engine.textnorm import norm

EURLEX = "https://eur-lex.europa.eu/legal-content/FR/TXT/HTML/?uri=CELEX:{celex}#art_{art}"
LEGAL_URLS = {
    "INCO_1169_2011_ART7_3": EURLEX.format(celex="32011R1169", art=7),
    "R1924_2006_ART10_1": EURLEX.format(celex="32006R1924", art=10),
    "R1924_2006_ART10_3": EURLEX.format(celex="32006R1924", art=10),
    "R1924_2006_ART12_A": EURLEX.format(celex="32006R1924", art=12),
    "R1924_2006_ART12_B": EURLEX.format(celex="32006R1924", art=12),
    "R1924_2006_ART12_C": EURLEX.format(celex="32006R1924", art=12),
    "R1924_2006_ART28": EURLEX.format(celex="32006R1924", art=28),
    "R1924_2006_ART8": EURLEX.format(celex="32006R1924", art=8),
    "R432_2012_CONDITIONS": "https://eur-lex.europa.eu/legal-content/FR/TXT/?uri=CELEX:32012R0432",
    "CSP_L5111_1_PRESENTATION": "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000006689867",
    "CCONSO_PRATIQUE_TROMPEUSE": "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000044563114",
}
REGISTER_URL = "https://ec.europa.eu/food/food-feed-portal/screen/health-claims/eu-register"

# Thèmes : (mots du passage, mots d'une allégation autorisée), en texte normalisé.
TOPICS = {
    "os": (["os", "osseuse", "osseux", "ossature", "squelette"], ["des os", "os normaux", "osseuse"]),
    "peau": (["peau", "cutane", "cutanee", "rides", "teint", "elasticite", "fermete", "repulpe"], ["peau"]),
    "articulations": (["articulation", "articulations", "articulaire", "cartilage", "cartilages", "mobilite", "raideur", "raideurs"],
                      ["cartilage"]),
    "immunite": (["immunite", "immunitaire", "defenses"], ["immunitaire"]),
    "fatigue": (["fatigue", "energie", "tonus", "vitalite", "epuisement", "coup de pompe"], ["fatigue", "energetique"]),
    "psychisme": (["stress", "anxiete", "angoisse", "humeur", "moral", "nerveux", "nerveuse", "nerfs", "charge mentale", "zen"],
                  ["psychologiques", "systeme nerveux"]),
    "cognition": (["memoire", "concentration", "cerveau", "cognitif", "cognitive", "cognitives"], ["cognitive", "cerveau"]),
    "sommeil": (["sommeil", "endormissement", "insomnie", "insomnies", "dormir", "nuits"], ["endormissement", "decalage horaire"]),
    "cheveux": (["cheveux", "ongles", "chute de cheveux"], ["cheveux", "ongles"]),
    "vision": (["vision", "yeux", "oculaire", "vue"], ["vision"]),
    "muscles": (["muscle", "muscles", "musculaire", "crampe", "crampes", "recuperation"], ["musculaire", "muscles"]),
    "coeur": (["coeur", "cardiaque", "cardiovasculaire"], ["cardiaque"]),
    "cholesterol": (["cholesterol", "cholesterolemie"], ["cholesterolemie"]),
    "glycemie": (["glycemie", "sucre sanguin", "diabete"], ["glycemie"]),
    "digestion": (["digestion", "digestif", "transit", "intestin", "intestinal", "ballonnements"], ["digestion", "intestinal", "transit"]),
    "oxydation": (["oxydatif", "antioxydant", "antioxydants", "radicaux libres", "vieillissement", "anti-age", "anti age"],
                  ["stress oxydatif"]),
    "vaisseaux": (["vaisseaux", "circulation", "jambes lourdes"], ["vaisseaux sanguins"]),
    "dents": (["dents", "gencives", "dentaire"], ["dents", "gencives"]),
    "thyroide": (["thyroide"], ["thyroidienne"]),
}

# Remédiations connues pour un terme précis (proposition à valider par l'expert).
TERM_FIXES = {
    "posologie": ("Conseils d'utilisation", "Intitulé pharmaceutique : renommer l'intitulé, les valeurs restent inchangées."),
    "contre-indication": ("Précautions d'emploi", "Vocabulaire médical : renommer."),
    "contre-indications": ("Précautions d'emploi", "Vocabulaire médical : renommer."),
    "patient": ("utilisateur", "Vocabulaire médical : renommer."),
    "patients": ("utilisateurs", "Vocabulaire médical : renommer."),
    "therapies": (None, "Si le terme figure dans une référence bibliographique, retirer la référence de la fiche ou la déplacer vers une page d'information sans lien produit."),
}
CATEGORY_ADVICE = {
    "VERBE_THERAPEUTIQUE": "Supprimer la propriété thérapeutique (prévenir, traiter, guérir) et la remplacer par la fonction physiologique normale visée, via une allégation autorisée.",
    "PATHOLOGIE": "Retirer la référence à la maladie. Décrire la fonction physiologique normale, avec une allégation autorisée.",
    "TERME_MEDICAL": "Retirer le terme médical. Décrire la fonction physiologique normale, avec une allégation autorisée.",
    "VOCABULAIRE_PHARMACEUTIQUE": "Remplacer le vocabulaire du médicament par celui de l'aliment.",
    "SUPERLATIF_PROMESSE": "Supprimer la promesse ou la remplacer par un fait vérifiable (teneur, origine, label), sans promesse de résultat.",
    "DOCTRINAL_RISQUE": "Terme sanctionné en pratique : le retirer ou le remplacer par une allégation autorisée.",
    "ALLEGATION_NON_AUTORISEE": "Allégation sans équivalent autorisé : la supprimer, ou la remplacer par une allégation autorisée de la composition.",
    "ART12_PERTE_DE_POIDS": "Retirer le rythme ou l'ampleur de la perte de poids.",
    "ART12_PROFESSIONNEL_SANTE": "Retirer la recommandation d'un professionnel de santé.",
    "ART12_NON_CONSOMMATION": "Retirer l'idée que la santé pâtirait de la non-consommation.",
}
CODE_ADVICE = {
    "CONDITION_NON_REMPLIE": "Atteindre la dose requise, ou retirer l'allégation.",
    "MENTION_OBLIGATOIRE_ABSENTE": "Ajouter la mention obligatoire à proximité de l'allégation.",
    "ALLEGATION_SUBSTANCE_ABSENTE": "Retirer l'allégation, ou déclarer la substance si elle figure bien dans la composition.",
    "REFERENCE_GENERALE_ART10_3": "Accompagner ce bienfait général d'une allégation autorisée spécifique, dans le même passage.",
    "IA_PROPRIETE_THERAPEUTIQUE": CATEGORY_ADVICE["VERBE_THERAPEUTIQUE"],
    "IA_ALLEGATION_SANS_CORRESPONDANCE": CATEGORY_ADVICE["ALLEGATION_NON_AUTORISEE"],
    "IA_REDUCTION_RISQUE_NON_AUTORISEE": "Retirer la réduction d'un risque de maladie (aucune allégation art. 14 autorisée correspondante).",
    "IA_FORMULATION_NON_FIDELE": "Reprendre le libellé officiel de l'allégation, sans l'amplifier.",
    "IA_REFERENCE_GENERALE": "Accompagner ce bienfait général d'une allégation autorisée spécifique, dans le même passage.",
}


def _has(text_norm: str, word: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text_norm) is not None


def topics_of(text: str) -> list[str]:
    t = norm(text)
    return [k for k, (words, _) in TOPICS.items() if any(_has(t, w) for w in words)]


class Suggester:
    """claims : code -> allégation (base effective) ; usable : code allégation -> (libellé statut, classe css)
    pour les substances de la composition ; substances : code -> fiche ; bridges : ponts sémantiques ;
    lexicon : code -> entrée du lexique."""

    def __init__(self, claims: dict, usable: dict, substances: dict, bridges: list, lexicon: dict):
        self.claims, self.usable, self.substances, self.lexicon = claims, usable, substances, lexicon
        self.bridges = [(norm(t), b) for b in bridges for t in b.get("illegal_query_terms", [])]
        self.by_topic: dict[str, list[str]] = {}
        for code, c in claims.items():
            if c.get("regime") == "REJECTED_EU_REGISTER":
                continue
            ct = norm(c["text_fr"])
            for k, (_, claim_words) in TOPICS.items():
                if any(w in ct for w in claim_words):
                    self.by_topic.setdefault(k, []).append(code)

    def _claim_view(self, code: str) -> dict:
        c = self.claims[code]
        status = self.usable.get(code)
        return {"code": code, "text": c["text_fr"], "substance": self.substances.get(c["substance"], {}).get("name_fr", ""),
                "status": status[0] if status else None, "css": status[1] if status else "info",
                "source": c.get("text_fr_source_ref"), "source_url": c.get("source_url")}

    def claims_for(self, text: str, limit: int = 3) -> tuple[list[dict], list[str]]:
        topics = topics_of(text)
        mine, others = [], []
        rank = {"ok": 0, "cond": 1, "red": 2}
        for t in topics:
            for code in self.by_topic.get(t, []):
                if code in self.usable:
                    mine.append(code)
                else:
                    others.append(self.claims[code]["substance"])
        mine = sorted(dict.fromkeys(mine), key=lambda c: rank.get(self.usable[c][1], 3))[:limit]
        names = list(dict.fromkeys(self.substances.get(s, {}).get("name_fr", s) for s in others))[:6]
        return [self._claim_view(c) for c in mine], names

    def suggest(self, code: str, lexical_code: str | None, quote: str, node_text: str) -> dict | None:
        q = norm(quote or "")
        entry = self.lexicon.get(lexical_code or code) or {}
        fix = TERM_FIXES.get(q)
        replace_with, advice = (fix[0], fix[1]) if fix else (None, None)
        if not advice:
            advice = CATEGORY_ADVICE.get(entry.get("category", "")) or CODE_ADVICE.get(code)
        bridge = next((b for t, b in self.bridges if t and (t == q or t in q)), None)
        if bridge and not replace_with:
            replace_with = bridge.get("legal_term")
        context = node_text or quote or ""
        claims, others = self.claims_for(context)
        if bridge:
            for cc in bridge.get("anchor_claims", []):
                if cc in self.claims and all(c["code"] != cc for c in claims):
                    claims.insert(0, self._claim_view(cc))
        if not (replace_with or advice or claims or others):
            return None
        return {"replace_with": replace_with, "advice": advice, "claims": claims[:3],
                "others": [] if claims else others}
