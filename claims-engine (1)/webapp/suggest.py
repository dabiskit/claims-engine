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
    "sommeil": (["sommeil", "endormissement", "insomnie", "insomnies", "dormir"], ["endormissement", "decalage horaire"]),
    "cheveux": (["cheveux", "ongles", "chute de cheveux"], ["cheveux", "ongles"]),
    "vision": (["vision", "yeux", "oculaire", "vue"], ["vision"]),
    "muscles": (["muscle", "muscles", "musculaire", "crampe", "crampes", "recuperation"], ["musculaire", "muscles"]),
    "coeur": (["coeur", "cardiaque", "cardiovasculaire"], ["cardiaque"]),
    "cholesterol": (["cholesterol", "cholesterolemie"], ["cholesterolemie"]),
    "glycemie": (["glycemie", "sucre sanguin", "diabete"], ["glycemie"]),
    "digestion": (["digestion", "digestif", "transit", "intestin", "intestinal", "ballonnements"], ["digestion", "intestinal", "transit"]),
    "oxydation": (["oxydatif", "antioxydant", "antioxydants", "radicaux libres"],
                  ["stress oxydatif"]),
    "vaisseaux": (["vaisseaux", "jambes lourdes", "veineux", "veineuse"], ["vaisseaux sanguins"]),
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


def sentence_of(quote: str, node_text: str) -> str:
    """La phrase du passage qui contient la citation (et non tout le paragraphe)."""
    if not node_text:
        return quote or ""
    i = node_text.find(quote) if quote else -1
    if i < 0:
        return quote or node_text[:300]
    start = max(node_text.rfind(".", 0, i), node_text.rfind("!", 0, i), node_text.rfind("?", 0, i)) + 1
    ends = [j for j in (node_text.find(c, i + len(quote)) for c in ".!?") if j != -1]
    return node_text[start:(min(ends) + 1) if ends else len(node_text)].strip()


class Suggester:
    """claims : code -> allégation (base effective) ; usable : code allégation -> (libellé statut, classe css)
    pour les substances de la composition ; substances : code -> fiche ; bridges : ponts sémantiques ;
    lexicon : code -> entrée du lexique."""

    def __init__(self, claims: dict, usable: dict, substances: dict, bridges: list, lexicon: dict):
        self.claims, self.usable, self.substances, self.lexicon = claims, usable, substances, lexicon
        self.bridges = [(norm(t), b) for b in bridges for t in b.get("illegal_query_terms", [])]
        self.by_topic: dict[str, list[str]] = {}
        seen_text: set[str] = set()
        for code, c in claims.items():
            ct = norm(c["text_fr"]).rstrip(". ")
            if c.get("regime") == "REJECTED_EU_REGISTER" or ct in seen_text:
                continue
            seen_text.add(ct)
            for k, (_, claim_words) in TOPICS.items():
                if any(w in ct for w in claim_words):
                    self.by_topic.setdefault(k, []).append(code)

    def _claim_view(self, code: str, why: str = "") -> dict:
        c = self.claims[code]
        status = self.usable.get(code)
        return {"code": code, "text": c["text_fr"], "substance": self.substances.get(c["substance"], {}).get("name_fr", ""),
                "status": status[0] if status else None, "css": status[1] if status else "info",
                "source": c.get("text_fr_source_ref"), "source_url": c.get("source_url"), "why": why}

    def claims_for(self, text: str, limit: int = 2) -> tuple[list[dict], list[str]]:
        """Allégations autorisées du thème du texte : d'abord celles de la composition (utilisables en tête),
        sinon le nom des ingrédients qui en disposent."""
        topics = topics_of(text)
        mine, others = [], []
        rank = {"ok": 0, "cond": 1, "red": 2}
        for t in topics:
            for code in self.by_topic.get(t, []):
                (mine if code in self.usable else others).append(code)
        mine = sorted(dict.fromkeys(mine), key=lambda c: rank.get(self.usable[c][1], 3))[:limit]
        names = list(dict.fromkeys(self.substances.get(self.claims[c]["substance"], {}).get("name_fr", "") for c in others))
        return [self._claim_view(c) for c in mine], [n for n in names if n][:6]

    def suggest(self, code: str, lexical_code: str | None, quote: str, node_text: str,
                claim_code: str | None = None) -> dict | None:
        q = norm(quote or "")
        entry = self.lexicon.get(lexical_code or code) or {}
        fix = TERM_FIXES.get(q)
        replace_with, advice = (fix[0], fix[1]) if fix else (None, None)
        if not advice:
            advice = CATEGORY_ADVICE.get(entry.get("category", "")) or CODE_ADVICE.get(code)
        bridge = next((b for t, b in self.bridges if t and (t == q or t in q)), None)
        if bridge and not replace_with:
            replace_with = bridge.get("legal_term")
        claims: list[dict] = []
        if claim_code and claim_code in self.claims:
            # L'IA a rapproché la phrase d'une allégation autorisée précise : c'est son libellé officiel qu'il faut reprendre.
            claims.append(self._claim_view(claim_code, "libellé officiel à reprendre tel quel"))
        for cc in (bridge or {}).get("anchor_claims", []):
            if cc in self.claims and all(c["code"] != cc for c in claims):
                claims.append(self._claim_view(cc))
        others: list[str] = []
        if not claims:
            # thème de la citation d'abord ; à défaut (« prévenir », « leur résistance »), celui de sa phrase
            basis = quote if topics_of(quote or "") else sentence_of(quote, node_text)
            claims, others = self.claims_for(basis)
        if not (replace_with or advice or claims or others):
            return None
        return {"replace_with": replace_with, "advice": advice, "claims": claims[:2], "others": [] if claims else others}


def dedupe_in_passage(findings: list[dict]) -> None:
    """Dans un même passage, une allégation ou un conseil n'est proposé qu'une fois (au premier constat)."""
    seen_claims: set[str] = set()
    seen_advice: set[str] = set()
    seen_others: set[str] = set()
    for f in findings:
        s = f.get("suggestion")
        if not s:
            continue
        s["claims"] = [c for c in s["claims"] if c["code"] not in seen_claims]
        seen_claims.update(c["code"] for c in s["claims"])
        if s.get("advice") in seen_advice:
            s["advice"] = None
        elif s.get("advice"):
            seen_advice.add(s["advice"])
        key = ",".join(s.get("others") or [])
        if key and key in seen_others:
            s["others"] = []
        elif key:
            seen_others.add(key)
        if not (s.get("replace_with") or s.get("advice") or s["claims"] or s.get("others")):
            f["suggestion"] = None
