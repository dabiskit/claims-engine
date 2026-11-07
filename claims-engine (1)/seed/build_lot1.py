#!/usr/bin/env python3
"""
seed/build_lot1.py — Seed de production, lot 1 (sommeil, stress, énergie, système nerveux)
et dictionnaire transversal des interdits lexicaux.

Sorties (dans seed/out/) :
  lot1.json               seed d'import (consommé par seed/json_to_sql.py)
  lot1_revue_expert.md    liste de vérification lisible par l'expert

STATUT DU CONTENU
  Tout est importé en DRAFT et ouvre des tâches de revue : kb.publish_release() refuse
  de publier tant qu'elles ne sont pas résolues une par une. Les libellés du Règl.
  432/2012 sont saisis de mémoire par un assistant IA ; « confidence » est la confiance
  de l'auteur du brouillon, jamais une validation.

  Aucune allégation botanique on-hold n'est importée : ni identifiant, ni libellé.
  Elles vont dans la liste de travail botanique (thème à rechercher, partie de plante,
  formulations de marché utilisables pour la détection uniquement).
"""
from __future__ import annotations

import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.lexicon import LexiconIndex  # noqa: E402
from engine.mode_router import lexical_hits  # noqa: E402
from engine.textnorm import norm  # noqa: E402

NS = uuid.UUID("6f1c3a52-6d0e-4c5b-9a57-3f0b8c0d2a11")
MICRO = "\u00b5"
UG = MICRO + "g"
AUTHOR = "assistant-ia"
RELEASE = {"id": 1, "label": "KB-PROD-0.1-LOT1",
           "changelog": "Lot 1 (brouillon IA) : dictionnaire transversal, Mg, mélatonine, vitamines C/B6/B9/B12, "
                        "6 plantes en liste de travail on-hold."}
H, M, B = "HAUTE", "MOYENNE", "BASSE"


def uid(kind: str, code: str) -> str:
    return str(uuid.uuid5(NS, f"{kind}:{code}"))


# =============================================================================
# 1. VNR (Règl. (UE) 1169/2011, annexe XIII, partie A) et seuil « source de » (15 %)
# =============================================================================
NRV = {"MG": (Decimal("375"), "mg"), "VIT_C": (Decimal("80"), "mg"), "VIT_B6": (Decimal("1.4"), "mg"),
       "VIT_B9": (Decimal("200"), UG), "VIT_B12": (Decimal("2.5"), UG)}
EXPECTED_15 = {"MG": Decimal("56.25"), "VIT_C": Decimal("12"), "VIT_B6": Decimal("0.21"),
               "VIT_B9": Decimal("30"), "VIT_B12": Decimal("0.375")}


def source_of(code: str) -> dict:
    value, unit = NRV[code]
    amount = (value * Decimal("0.15")).normalize()
    assert amount == EXPECTED_15[code], (code, amount)
    return {"type": "DOSE_MIN_PORTION",
            "params": {"amount": float(amount), "unit": unit, "basis": "PORTION_JOURNALIERE",
                       "derivation": "SOURCE_15_PCT_VNR",
                       "expressed_as": "ELEMENT" if code == "MG" else "SUBSTANCE"},
            "source_ref": "Règl. (UE) 432/2012, annexe : au moins une « source de » au sens de l'annexe du Règl. (CE) "
                          "1924/2006, soit 15 % de la VNR (Règl. (UE) 1169/2011, annexe XIII)",
            "confidence": H}


# =============================================================================
# 2. Mentions (texte à afficher) — formulations de travail, statut à confirmer
# =============================================================================
NOTICES = [
    ("NE_PAS_DEPASSER", "Ne pas dépasser la dose journalière recommandée",
     "Décret n° 2006-352 du 20 mars 2006, art. 10", H),
    ("HORS_PORTEE_ENFANTS", "Tenir hors de portée des jeunes enfants",
     "Décret n° 2006-352 du 20 mars 2006, art. 10", H),
    ("SUBSTITUT_REGIME", "Les compléments alimentaires ne doivent pas être utilisés comme substituts d'un régime "
     "alimentaire varié et équilibré et d'un mode de vie sain", "Décret n° 2006-352 du 20 mars 2006, art. 10", H),
    ("MELATONINE_COUCHER", "L'effet bénéfique est obtenu par la consommation de 1 mg de mélatonine peu de temps "
     "avant le coucher", "Règl. (UE) 432/2012, annexe : condition de l'allégation mélatonine / endormissement", M),
    ("MELATONINE_DECALAGE", "L'effet bénéfique est obtenu par la consommation d'au moins 0,5 mg de mélatonine peu de "
     "temps avant le coucher le premier jour du voyage et les quelques jours suivant l'arrivée à destination",
     "Règl. (UE) 432/2012, annexe : condition de l'allégation mélatonine / décalage horaire", M),
    ("VITC_EXERCICE", "L'effet bénéfique est obtenu par une consommation quotidienne de 200 mg de vitamine C en plus "
     "de l'apport journalier recommandé", "Règl. (UE) 432/2012, annexe : condition de l'allégation vitamine C / "
     "système immunitaire et exercice intense", M),
    ("FOLATES_ART14_POPULATION", "Ce produit s'adresse aux femmes en âge de procréer",
     "Règlement d'autorisation art. 14 (acide folique / tube neural) : population cible", B),
    ("FOLATES_ART14_EFFET", "L'effet bénéfique est obtenu par une consommation quotidienne de 400 " + UG + " d'acide "
     "folique en complément, pendant au moins un mois avant et jusqu'à trois mois après la conception",
     "Règlement d'autorisation art. 14 (acide folique / tube neural) : conditions", M),
    ("VIG_MELATONINE", "Déconseillé aux femmes enceintes ou allaitantes, aux enfants et aux adolescents, ainsi qu'aux "
     "personnes souffrant de maladies inflammatoires ou auto-immunes. Demander l'avis d'un médecin en cas de "
     "traitement médicamenteux ou de pathologie. Prudence en cas d'activité nécessitant une vigilance soutenue",
     "Avis ANSES (2018) relatif aux compléments alimentaires contenant de la mélatonine — référence exacte à vérifier", M),
    ("VIG_ASHWAGANDHA", "Déconseillé aux femmes enceintes ou allaitantes, aux enfants et aux adolescents. "
     "Demander l'avis d'un médecin en cas de pathologie thyroïdienne, hépatique ou auto-immune, ou de traitement "
     "médicamenteux", "Signalements de nutrivigilance et avis d'autorités européennes — sources exactes à vérifier", B),
    ("VIG_ADAPTOGENE_DONNEES", "Déconseillé aux femmes enceintes ou allaitantes et aux enfants, faute de données "
     "suffisantes", "Prudence par défaut faute de données de sécurité — à confirmer par l'expert", M),
    ("VIG_SEDATIF_VEGETAL", "Déconseillé aux enfants de moins de 12 ans et aux femmes enceintes ou allaitantes. "
     "Peut entraîner une somnolence : prudence en cas de conduite. Demander l'avis d'un médecin en cas de "
     "traitement sédatif", "Monographies européennes (HMPC) valériane et passiflore — à vérifier", M),
    ("VIG_SAFRAN", "Déconseillé aux femmes enceintes. Demander l'avis d'un médecin en cas de traitement "
     "antidépresseur", "Données de sécurité à forte dose et interactions — sources exactes à vérifier", B),
    ("VIG_GINSENG", "Déconseillé aux enfants et adolescents de moins de 18 ans et aux femmes enceintes ou allaitantes. "
     "Demander l'avis d'un médecin en cas d'hypertension, de diabète ou de traitement anticoagulant",
     "Monographie européenne (HMPC) ginseng et interactions rapportées — à vérifier", M),
]
NOTICE_STATUS = "FORMULATION_DE_TRAVAIL_VALIDEE"


# =============================================================================
# 3. Dictionnaire transversal (formes déjà normalisées : minuscules, sans accents)
# =============================================================================
def L(code, lemma, forms, category, basis, sev, *, icd=None, pattern=None, neutral=(), note=None, conf=H,
      topic=None, substance=None):
    return {"code": code, "lemma": lemma, "surface_forms": list(forms), "pattern_regex": pattern,
            "category": category, "legal_basis": basis, "severity_product": sev[0],
            "severity_article_with_bridge": sev[1], "severity_article_without_bridge": sev[2],
            "icd10_code": icd, "topic_key": topic, "neutralizing_left_context": list(neutral),
            "note": note, "confidence": conf, "substance": substance}


RRO, RRI, OOI, NNN = ("ROUGE", "ROUGE", "ORANGE"), ("ROUGE", "ROUGE", "INFO"), ("ORANGE", "ORANGE", "INFO"), \
    ("NEUTRE", "NEUTRE", "NEUTRE")
INCO, CSP, CONSO, DOC = "INCO_1169_2011_ART7_3", "CSP_L5111_1_PRESENTATION", "CCONSO_PRATIQUE_TROMPEUSE", \
    "DOCTRINE_ADMINISTRATIVE"
VT, VP, PA, TM = "VERBE_THERAPEUTIQUE", "VOCABULAIRE_PHARMACEUTIQUE", "PATHOLOGIE", "TERME_MEDICAL"
SP, DR = "SUPERLATIF_PROMESSE", "DOCTRINAL_RISQUE"

BLACKLIST = [
    # Propriétés thérapeutiques (art. 7§3 INCO)
    L("LEX-SOIGNER", "soigner", ["soigne", "soignent", "soigner", "soignez", "soignera"], VT, INCO, RRO),
    L("LEX-GUERIR", "guérir", ["guerit", "guerissent", "guerir", "gueri", "guerie", "gueris", "guerison", "guerisse"],
      VT, INCO, RRO),
    L("LEX-TRAITER", "traiter", ["traite", "traitent", "traiter", "traitement", "traitements", "traitant", "traitante"],
      VT, INCO, RRO, neutral=["ne remplace pas un", "ne se substitue pas a un", "en cas de", "sous", "suivez un",
                              "suivant un", "non", "sans"]),
    L("LEX-SOULAGER", "soulager", ["soulage", "soulagent", "soulager", "soulagez", "soulagement"], VT, INCO, RRO),
    L("LEX-PREVENIR", "prévenir", ["previent", "previennent", "prevenir", "prevention", "preventif", "preventive",
                                   "preventifs"], VT, INCO, RRO,
      note="« Prévenez votre médecin » n'est pas couvert (forme exclue)."),
    L("LEX-CICATRISER", "cicatriser", ["cicatrise", "cicatrisent", "cicatriser", "cicatrisant", "cicatrisante",
                                       "cicatrisation"], VT, INCO, RRO),
    L("LEX-REMEDE", "remède", ["remede", "remedes"], VT, INCO, RRO),
    L("LEX-COMBATTRE", "combattre", ["combat", "combattent", "combattre", "lutte contre", "luttent contre",
                                     "lutter contre"], VT, INCO, OOI, conf=M,
      note="Seul : orange. Associé à une pathologie, l'entrée pathologie est rouge."),
    # Vocabulaire pharmaceutique et classes médicamenteuses (présentation : CSP L5111-1)
    L("LEX-ANTI-INFLAMMATOIRE", "anti-inflammatoire", ["anti-inflammatoire", "anti-inflammatoires",
      "antiinflammatoire", "antiinflammatoires", "anti inflammatoire"], VP, INCO, RRO),
    L("LEX-ANXIOLYTIQUE", "anxiolytique", ["anxiolytique", "anxiolytiques"], VP, CSP, RRO),
    L("LEX-ANTIDEPRESSEUR", "antidépresseur", ["antidepresseur", "antidepresseurs", "anti-depresseur",
                                               "anti-depresseurs"], VP, CSP, RRO,
      neutral=["en cas de traitement", "sous traitement"]),
    L("LEX-SOMNIFERE", "somnifère", ["somnifere", "somniferes"], VP, CSP, RRO),
    L("LEX-ANTI-INFECTIEUX", "anti-infectieux", ["antibiotique", "antibiotiques", "antiviral", "antivirale",
      "antiviraux", "antiseptique", "antiseptiques", "antibacterien", "antibacterienne", "antifongique",
      "antifongiques"], VP, CSP, RRO),
    L("LEX-ANTALGIQUE", "antalgique", ["antalgique", "antalgiques", "antidouleur", "antidouleurs", "anti-douleur",
                                       "anti-douleurs", "analgesique", "analgesiques"], VP, CSP, RRO),
    L("LEX-ALTERNATIVE-MEDICAMENT", "alternative aux médicaments", ["medicament naturel", "medicaments naturels"],
      VP, CSP, RRO,
      pattern=r"(?:alternative|remplace(?:r|z)?|substitut)\s+(?:naturel(?:le)?\s+)?(?:aux?|a\s+un|a\s+vos|vos|votre|les|des)\s+(?:medicaments?|traitements?)"),
    L("LEX-THERAPEUTIQUE", "thérapeutique", ["therapeutique", "therapeutiques", "therapie", "therapies"], VP, INCO, RRO),
    L("LEX-POSOLOGIE", "posologie", ["posologie", "posologies"], VP, CSP, OOI,
      note="Remédiation : renommer en « Conseils d'utilisation » ; les valeurs restent gelées."),
    L("LEX-VOCAB-PRESCRIPTION", "vocabulaire de prescription", ["patient", "patients", "patiente", "patientes",
      "ordonnance", "sans ordonnance", "prescription"], VP, CSP, OOI, conf=M),
    L("LEX-CONTRE-INDICATION", "contre-indication", ["contre-indication", "contre-indications", "contre-indique",
      "contre-indiquee", "contre-indiques", "contre-indiquees"], VP, CSP, OOI,
      note="Remédiation : « Précautions d'emploi »."),
    L("LEX-MEDICAMENT", "médicament", ["medicament", "medicaments"], VP, CSP, OOI,
      neutral=["si vous prenez des", "si vous prenez un", "prise de", "prenez des", "ne remplace pas un",
               "ne se substitue pas a un", "ne remplace pas les", "interactions avec des", "interaction avec un",
               "avec certains"]),
    # Pathologies (CIM-10 OMS)
    L("LEX-INSOMNIE", "insomnie", ["insomnie", "insomnies", "insomniaque", "insomniaques", "troubles du sommeil",
                                   "trouble du sommeil", "nuits blanches"], PA, INCO, RRI, icd="G47.0"),
    L("LEX-ANXIETE", "anxiété", ["anxiete", "anxietes", "anxieux", "anxieuse", "anxieuses", "angoisse", "angoisses",
      "crise d'angoisse", "crises d'angoisse", "attaque de panique", "attaques de panique"], PA, INCO, RRI, icd="F41.9"),
    L("LEX-DEPRESSION", "dépression", ["depression", "depressions", "depressif", "depressive", "depressives", "deprime",
                                       "deprimee"], PA, INCO, RRI, icd="F32.9"),
    L("LEX-EPUISEMENT", "épuisement", ["burn-out", "burnout", "burn out", "surmenage", "epuisement professionnel",
      "epuisement nerveux", "epuisement physique", "fatigue chronique", "syndrome de fatigue chronique", "asthenie"],
      PA, INCO, RRI, icd="Z73.0", note="« Fatigue » seule est autorisée (432/2012) ; « fatigue chronique » ne l'est pas."),
    L("LEX-ARTHROSE", "arthrose", ["arthrose", "arthroses", "arthrite", "arthrites", "polyarthrite", "rhumatisme",
      "rhumatismes", "rhumatismal", "rhumatismale", "rhumatismales", "rhumatismaux"], PA, INCO, RRI, icd="M19.9"),
    L("LEX-DIABETE", "diabète", ["diabete", "diabetes", "diabetique", "diabetiques", "prediabete", "hyperglycemie"],
      PA, INCO, RRI, icd="E14.9"),
    L("LEX-CHOLESTEROL", "hypercholestérolémie", ["hypercholesterolemie", "anti-cholesterol", "trop de cholesterol"],
      PA, INCO, RRI, icd="E78.0",
      pattern=r"(?:baisser|reduire|diminuer|combattre|lutter\s+contre)\s+(?:le\s+|son\s+|votre\s+|du\s+|les\s+)?(?:taux\s+de\s+)?cholesterol",
      note="« Cholestérolémie normale » (allégations autorisées) n'est pas couverte."),
    L("LEX-HYPERTENSION", "hypertension", ["hypertension", "hypertendu", "hypertendus", "hypertendue",
                                           "hypertendues", "tension arterielle elevee"], PA, INCO, RRI, icd="I10"),
    L("LEX-CANCER", "cancer", ["cancer", "cancers", "cancereux", "cancereuse", "cancereuses", "tumeur", "tumeurs",
                               "anticancer", "anti-cancer", "anticancereux"], PA, INCO, RRI, icd="C80.9"),
    L("LEX-MIGRAINE", "migraine", ["migraine", "migraines", "migraineux", "migraineuse", "maux de tete",
                                   "mal de tete", "cephalee", "cephalees"], PA, INCO, RRI, icd="G43.9"),
    L("LEX-NEURODEGENERATIF", "maladies neurodégénératives", ["alzheimer", "parkinson", "demence",
      "maladies neurodegeneratives", "maladie neurodegenerative", "perte de memoire", "troubles de la memoire"],
      PA, INCO, RRI, icd="G30.9"),
    L("LEX-OBESITE", "obésité", ["obesite", "obese", "obeses"], PA, INCO, RRI, icd="E66.9"),
    L("LEX-ANEMIE", "anémie", ["anemie", "anemies", "anemique", "anemiques"], PA, INCO, RRI, icd="D64.9"),
    L("LEX-TROUBLES-DIGESTIFS", "troubles digestifs", ["constipation", "constipe", "constipee", "constipes",
      "diarrhee", "diarrhees", "colopathie", "colon irritable", "syndrome de l'intestin irritable",
      "reflux gastrique", "ulcere", "ulceres"], PA, INCO, RRI, icd="K59.0"),
    L("LEX-CRAMPES", "crampes", ["crampe", "crampes", "spasmophilie", "tetanie", "spasme", "spasmes"], PA, INCO, RRI,
      icd="R25.2"),
    L("LEX-INFECTIONS-ORL", "infections hivernales", ["rhume", "rhumes", "grippe", "grippes", "grippal", "grippale",
      "grippaux", "etat grippal", "angine", "angines", "bronchite", "bronchites", "sinusite", "sinusites",
      "infections hivernales"], PA, INCO, RRI, icd="J00"),
    L("LEX-CYSTITE", "cystite", ["cystite", "cystites", "infection urinaire", "infections urinaires"], PA, INCO, RRI,
      icd="N30.9"),
    L("LEX-DERMATOSES", "dermatoses", ["acne", "eczema", "psoriasis", "dermatite", "dermatites", "rosacee"],
      PA, INCO, RRI, icd="L70.0"),
    L("LEX-CHUTE-CHEVEUX", "chute de cheveux", ["chute de cheveux", "chute des cheveux", "perte de cheveux",
                                                 "alopecie"], PA, INCO, RRI, icd="L65.9", conf=M,
      note="Les allégations autorisées visent le « maintien de cheveux normaux », jamais la chute."),
    # Termes médicaux (symptômes, notions)
    L("LEX-DOULEUR", "douleur", ["douleur", "douleurs", "douloureux", "douloureuse", "douloureuses"], TM, INCO, RRI),
    L("LEX-INFLAMMATION", "inflammation", ["inflammation", "inflammations", "inflammatoire", "inflammatoires"],
      TM, INCO, RRI, neutral=["maladies", "maladie"]),
    L("LEX-MALADIE", "maladie", ["maladie", "maladies", "pathologie", "pathologies"], TM, INCO, OOI,
      neutral=["en cas de", "atteintes de", "atteints de", "atteinte d'une", "atteint d'une", "souffrant de",
               "souffrant d'une", "personnes ayant une"]),
    L("LEX-SYMPTOME", "symptôme", ["symptome", "symptomes", "syndrome", "syndromes"], TM, INCO, OOI),
    L("LEX-INFECTION", "infection", ["infection", "infections", "infectieux", "infectieuse", "infectieuses"],
      TM, INCO, RRI),
    # Art. 12 du Règl. (CE) 1924/2006
    L("LEX-PERTE-POIDS", "perte de poids (rythme ou ampleur)", ["perte de poids rapide", "maigrir vite",
      "maigrir rapidement"], "ART12_PERTE_DE_POIDS", "R1924_2006_ART12_B", RRO,
      pattern=r"perd[a-z]*\s+(?:jusqu'a\s+)?\d+(?:[.,]\d+)?\s*(?:kg|kilos?)"),
    L("LEX-RECO-PROFESSIONNEL", "recommandation d'un professionnel de santé", ["recommande par les medecins",
      "recommande par les pharmaciens"], "ART12_PROFESSIONNEL_SANTE", "R1924_2006_ART12_C", RRO,
      pattern=r"(?:recommande|conseille|prescrit|approuve)e?s?\s+par\s+(?:les\s+|des\s+|un\s+|votre\s+)?(?:medecins?|pharmaciens?|docteurs?|dieteticiens?|nutritionnistes?)"),
    L("LEX-NON-CONSOMMATION", "santé affectée sans le produit", ["indispensable a votre sante",
      "votre sante en depend"], "ART12_NON_CONSOMMATION", "R1924_2006_ART12_A", RRO,
      pattern=r"sans\s+(?:ce|cet|notre|un)\s+(?:complement|apport|produit)[^.!?]{0,60}(?:risque|carence|malade|epuise|fatigue)"),
    # Promesses (pratiques commerciales trompeuses)
    L("LEX-RESULTATS-GARANTIS", "résultats garantis", ["resultats garantis", "resultat garanti", "efficacite garantie",
      "efficacite prouvee", "cliniquement prouve", "cliniquement prouvee", "100 % efficace", "100% efficace"],
      SP, CONSO, RRO),
    L("LEX-IMMEDIAT", "effet immédiat", ["immediat", "immediate", "immediatement", "instantane", "instantanee",
                                         "en quelques minutes"], SP, CONSO, OOI,
      pattern=r"des\s+(?:la\s+|le\s+)?(?:premiere|premier|1ere|1re|1er)\s+(?:nuit|prise|semaine|utilisation|gelule|jour)"),
    L("LEX-MIRACLE", "miracle", ["miracle", "miracles", "miraculeux", "miraculeuse", "miraculeuses",
                                 "produit miracle"], SP, CONSO, OOI),
    L("LEX-SANS-EFFETS-SECONDAIRES", "sans effets secondaires", ["sans effets secondaires", "sans effet secondaire",
      "sans effets indesirables", "sans accoutumance", "sans dependance", "non addictif"], SP, CSP, OOI),
    L("LEX-COMPARATIF-ABSOLU", "comparatif absolu", ["le plus puissant", "le plus efficace", "ultra-puissant",
                                                     "surpuissant", "numero 1", "inegale"], SP, CONSO, OOI, conf=M),
    # Termes doctrinaux à risque
    L("LEX-DETOX", "détox", ["detox", "detoxifiant", "detoxifiante", "detoxifiants", "detoxification", "depuratif",
      "depurative", "depuratives", "elimine les toxines", "eliminer les toxines", "toxines", "drainant", "drainante",
      "draineur", "drainage"], DR, DOC, OOI, conf=M),
    L("LEX-MINCEUR-RADICALE", "minceur radicale", ["brule-graisse", "brule-graisses", "brule graisse", "fat burner",
      "coupe-faim", "coupe faim", "capteur de graisses", "bloqueur de calories", "ventre plat"], DR, DOC, OOI, conf=M),
    L("LEX-REPARATEUR", "réparateur", ["reparateur", "reparatrice", "reparatrices", "reparateurs"], DR, DOC, OOI,
      conf=M, note="Exclut « sommeil réparateur » des ponts SEO légaux tant que l'expert n'a pas tranché."),
    L("LEX-BOOSTER", "booster", ["booster immunitaire", "booster d'energie", "coup de fouet"], DR, DOC, OOI, conf=M,
      pattern=r"boost[a-z]*\s+(?:l'|les\s+|vos\s+|votre\s+|ses\s+|son\s+|la\s+)?(?:immunite|defenses|systeme\s+immunitaire|energie|metabolisme|libido|testosterone)"),
    L("LEX-ANTI-PREFIXE", "anti-[état physiologique]", ["anti-stress", "antistress", "anti-fatigue", "antifatigue",
      "anti-age", "anti-ages", "anti-rides", "antirides", "anti-vieillissement"], DR, DOC, OOI, conf=M),
    L("LEX-CARENCE", "carence", ["carence", "carences", "combler les carences", "comble les carences"], DR, DOC, OOI,
      conf=M),
]

# Catégories de contrôle : neutres en détection, utilisées par les validateurs du texte libre.
CONTROL = [
    L("LEX-CTRL-VERBES-EFFET", "verbes d'effet", sorted(set(
        "contribue contribuent contribuer aide aident aider favorise favorisent favoriser soutient soutiennent soutenir "
        "reduit reduisent reduire ameliore ameliorent ameliorer booste boostent apaise apaisent apaiser calme calment "
        "calmer renforce renforcent renforcer optimise optimisent optimiser stimule stimulent stimuler maintient "
        "maintenir preserve preserver protege protegent proteger diminue diminuer augmente augmenter retrouver "
        "retrouvez regenere regenerer participe participent participer accompagne accompagnent accompagner equilibre "
        "equilibrent equilibrer relaxe relaxer detend detendre attenue attenuer".split())),
      "VERBE_EFFET", "REGLE_INTERNE_MOTEUR", NNN, conf=M),
    L("LEX-CTRL-ATTRIBUTS", "attributs factuels", ["bio", "biologique", "naturel", "naturelle", "naturels",
      "naturelles", "vegan", "vegetalien", "fabrique en france", "made in france", "brevete", "brevetee",
      "cliniquement", "teste", "testee", "scientifiquement", "laboratoire"], "ATTRIBUT_FACTUEL",
      "REGLE_INTERNE_MOTEUR", NNN),
]
THEMES = {
    "sommeil": ["sommeil", "endormissement", "endormir", "nuit", "nuits", "dormir"],
    "decalage_horaire": ["decalage horaire", "jet lag", "jet-lag"],
    "fatigue": ["fatigue", "fatigues"],
    "energie": ["energie", "energetique", "metabolisme energetique"],
    "vitalite": ["vitalite", "tonus", "vigueur"],
    "psychologique": ["psychologique", "psychologiques", "moral", "mental", "mentale"],
    "systeme_nerveux": ["systeme nerveux", "nerveux", "nerveuse", "nerfs"],
    "immunite": ["immunite", "immunitaire", "immunitaires", "defenses"],
    "muscle": ["muscle", "muscles", "musculaire", "musculaires"],
    "collagene_peau": ["peau"],  # « collagène » est un nom d'ingrédient, pas un thème
    "cartilage": ["cartilage", "cartilages", "articulation", "articulations", "articulaire", "articulaires"],
    "globules_rouges": ["globules rouges", "sang"],
    "relaxation": ["relaxation", "detente", "serenite", "zen"],
    "stress": ["stress", "stresse", "stressee"],
    "humeur": ["humeur", "emotionnel", "emotionnelle"],
    "performance": ["performance", "performances", "concentration", "memoire"],
}
for topic, forms in THEMES.items():
    CONTROL.append(L(f"LEX-CTRL-THEME-{topic.upper().replace('_', '-')}", topic, forms, "THEME_PHYSIOLOGIQUE",
                     "REGLE_INTERNE_MOTEUR", NNN, topic=topic, conf=M))


# =============================================================================
# 4. Actifs du lot 1
# =============================================================================
def claim(suffix, predicate, label, conf=H, conditions=None, regime="AUTHORIZED_432_2012", variants=True,
          full_text=None, verb=None, function=None):
    return {"suffix": suffix, "predicate": predicate, "label": label, "confidence": conf, "conditions": conditions,
            "regime": regime, "variants": variants, "full_text": full_text, "verb": verb, "function": function}


def notice_cond(code, placement="RENVOI_ASTERISQUE", conf=M):
    return {"type": "MENTION_OBLIGATOIRE", "params": {"placement": placement}, "notice_code": code,
            "source_ref": "Règl. (UE) 432/2012, annexe : information du consommateur", "confidence": conf}


def dose(amount, unit, basis, conf, ref, expressed_as="SUBSTANCE", **extra):
    return {"type": "DOSE_MIN_PORTION", "params": {"amount": amount, "unit": unit, "basis": basis,
                                                   "expressed_as": expressed_as, **extra},
            "source_ref": ref, "confidence": conf}


COLLAGENE = "contribue à la formation normale de collagène pour assurer la fonction normale "

SUBSTANCES = [
    {"code": "MG", "kind": "MINERAL", "name_fr": "Magnésium", "subject": "Le magnésium",
     "source_ref": "VNR : Règl. (UE) 1169/2011, annexe XIII, partie A",
     "aliases": [("magnésium", "NOM_COMMUN_FR"), ("magnésium marin", "NOM_COMMERCIAL"),
                 ("bisglycinate de magnésium", "FORME_CHIMIQUE"), ("glycérophosphate de magnésium", "FORME_CHIMIQUE"),
                 ("citrate de magnésium", "FORME_CHIMIQUE"), ("malate de magnésium", "FORME_CHIMIQUE"),
                 ("oxyde de magnésium", "FORME_CHIMIQUE"), ("chlorure de magnésium", "FORME_CHIMIQUE"),
                 ("pidolate de magnésium", "FORME_CHIMIQUE"), ("lactate de magnésium", "FORME_CHIMIQUE"),
                 ("carbonate de magnésium", "FORME_CHIMIQUE")],
     "claims": [
         claim("FATIGUE", "contribue à réduire la fatigue", "fatigue"),
         claim("ELECTROLYTES", "contribue à l'équilibre électrolytique", "équilibre électrolytique"),
         claim("ENERGIE", "contribue à un métabolisme énergétique normal", "métabolisme énergétique"),
         claim("SYSNERV", "contribue au fonctionnement normal du système nerveux", "système nerveux"),
         claim("MUSCLE", "contribue à une fonction musculaire normale", "fonction musculaire"),
         claim("PROTEINES", "contribue à la synthèse protéique normale", "synthèse protéique"),
         claim("PSY", "contribue à des fonctions psychologiques normales", "fonctions psychologiques"),
         claim("OS", "contribue au maintien d'une ossature normale", "ossature"),
         claim("DENTS", "contribue au maintien d'une dentition normale", "dentition"),
         claim("DIVISION", "joue un rôle dans le processus de division cellulaire", "division cellulaire"),
     ],
     "risk_phrases": ["calme les nerfs", "anti-crampes", "contre les crampes", "stoppe les crampes",
                      "contre la spasmophilie", "magnesium anti-stress"],
     "review": ["Dose exprimée en magnésium élément : l'écran des seuils doit le préciser (un bisglycinate de 1 000 mg "
                "n'apporte pas 1 000 mg de magnésium).",
                "Dose journalière maximale française (arrêté du 9 mai 2006) à saisir pour le contrôle des doses (lot ultérieur)."]},

    {"code": "MELATONINE", "kind": "AUTRE_SUBSTANCE", "name_fr": "Mélatonine", "subject": "La mélatonine",
     "source_ref": "Règl. (UE) 432/2012, annexe",
     "aliases": [("mélatonine", "NOM_COMMUN_FR"), ("mélatonine végétale", "NOM_COMMERCIAL"),
                 ("phytomélatonine", "NOM_COMMERCIAL")],
     "claims": [
         claim("ENDORM", "contribue à réduire le temps d'endormissement", "endormissement", H,
               [dose(1, "mg", "PORTION_QUANTIFIEE", H, "Règl. (UE) 432/2012, annexe : au moins 1 mg par portion quantifiée"),
                notice_cond("MELATONINE_COUCHER")]),
         claim("JETLAG", "contribue à atténuer les effets du décalage horaire", "décalage horaire", M,
               [dose(0.5, "mg", "PORTION_QUANTIFIEE", M, "Règl. (UE) 432/2012, annexe : au moins 0,5 mg par portion quantifiée"),
                notice_cond("MELATONINE_DECALAGE")]),
     ],
     "risk_phrases": ["somnifere naturel", "contre l'insomnie", "dormir toute la nuit", "sommeil profond",
                      "sommeil garanti", "fini les nuits blanches", "melatonine anti-stress"],
     "restrictions": [{"populations": ["FEMMES_ENCEINTES", "FEMMES_ALLAITANTES", "ENFANTS", "ADOLESCENTS"],
                       "kind": "DECONSEILLE", "notice": "VIG_MELATONINE", "confidence": M,
                       "detail": "Aussi : maladies inflammatoires ou auto-immunes ; avis médical en cas de traitement "
                                 "ou de pathologie ; activités exigeant une vigilance soutenue.",
                       "source_ref": "Avis ANSES (2018) mélatonine — référence exacte à vérifier"}],
     "review": ["Les allégations mélatonine ne visent que l'endormissement et le décalage horaire : « sommeil profond », "
                "« durée du sommeil » ou « qualité du sommeil » sont des allégations sans correspondance (motifs de risque).",
                "Vérifier la position française sur la dose journalière maximale de mélatonine en complément alimentaire "
                "(au-delà, risque de requalification en médicament).",
                "Alias « mélatonine végétale » : même substance, même régime 432/2012 (ce n'est pas une botanique on-hold)."]},

    {"code": "VIT_C", "kind": "VITAMINE", "name_fr": "Vitamine C", "subject": "La vitamine C",
     "source_ref": "VNR : Règl. (UE) 1169/2011, annexe XIII, partie A",
     "aliases": [("vitamine C", "NOM_COMMUN_FR"), ("acide ascorbique", "FORME_CHIMIQUE"),
                 ("acide L-ascorbique", "FORME_CHIMIQUE"), ("ascorbate de sodium", "FORME_CHIMIQUE"),
                 ("ascorbate de calcium", "FORME_CHIMIQUE"), ("palmitate d'ascorbyle", "FORME_CHIMIQUE"),
                 ("vitamine C liposomale", "NOM_COMMERCIAL"), ("vit C", "ABREVIATION"),
                 ("acérola", "NOM_COMMERCIAL", True,
                  "Source végétale de vitamine C : la dose se compte en vitamine C, pas en poudre d'acérola")],
     "claims": [
         claim("IMMUN-EXERCICE", "contribue à maintenir le fonctionnement normal du système immunitaire pendant et "
               "après un exercice physique intense", "système immunitaire et exercice", M,
               [dose(200, "mg", "PORTION_JOURNALIERE", M, "Règl. (UE) 432/2012, annexe : apport quotidien de 200 mg en "
                     "plus de l'apport journalier recommandé"), notice_cond("VITC_EXERCICE")]),
         claim("COLL-VAISSEAUX", COLLAGENE + "des vaisseaux sanguins", "collagène et vaisseaux sanguins"),
         claim("COLL-OS", COLLAGENE + "des os", "collagène et os"),
         claim("COLL-CARTILAGE", COLLAGENE + "des cartilages", "collagène et cartilages"),
         claim("COLL-GENCIVES", COLLAGENE + "des gencives", "collagène et gencives"),
         claim("COLL-PEAU", COLLAGENE + "de la peau", "collagène et peau"),
         claim("COLL-DENTS", COLLAGENE + "des dents", "collagène et dents"),
         claim("ENERGIE", "contribue à un métabolisme énergétique normal", "métabolisme énergétique"),
         claim("SYSNERV", "contribue au fonctionnement normal du système nerveux", "système nerveux"),
         claim("PSY", "contribue à des fonctions psychologiques normales", "fonctions psychologiques"),
         claim("IMMUN", "contribue au fonctionnement normal du système immunitaire", "système immunitaire"),
         claim("STRESS-OX", "contribue à protéger les cellules contre le stress oxydatif", "stress oxydatif"),
         claim("FATIGUE", "contribue à réduire la fatigue", "fatigue"),
         claim("VIT-E", "contribue à la régénération de la forme réduite de la vitamine E", "régénération vitamine E"),
         claim("FER", "accroît l'absorption du fer", "absorption du fer"),
     ],
     "risk_phrases": ["contre le rhume", "previent les infections", "anti-oxydant puissant", "booste l'immunite",
                      "renforce les defenses immunitaires", "vitamine c anti-fatigue"],
     "review": ["Lot 2 : la vitamine C est l'ancre « collagène » des formules collagène/acide hyaluronique ; l'effet ne "
                "doit jamais être attribué au collagène lui-même."]},

    {"code": "VIT_B6", "kind": "VITAMINE", "name_fr": "Vitamine B6", "subject": "La vitamine B6",
     "source_ref": "VNR : Règl. (UE) 1169/2011, annexe XIII, partie A",
     "aliases": [("vitamine B6", "NOM_COMMUN_FR"), ("B6", "ABREVIATION"), ("pyridoxine", "FORME_CHIMIQUE"),
                 ("chlorhydrate de pyridoxine", "FORME_CHIMIQUE"), ("pyridoxal-5'-phosphate", "FORME_CHIMIQUE"),
                 ("P5P", "ABREVIATION")],
     "claims": [
         claim("CYSTEINE", "contribue à la synthèse normale de la cystéine", "cystéine"),
         claim("ENERGIE", "contribue à un métabolisme énergétique normal", "métabolisme énergétique"),
         claim("SYSNERV", "contribue au fonctionnement normal du système nerveux", "système nerveux"),
         claim("HOMOCYSTEINE", "contribue au métabolisme normal de l'homocystéine", "homocystéine"),
         claim("PROT-GLYCOGENE", "contribue au métabolisme normal des protéines et du glycogène", "protéines et glycogène"),
         claim("PSY", "contribue à des fonctions psychologiques normales", "fonctions psychologiques"),
         claim("GR", "contribue à la formation normale de globules rouges", "globules rouges"),
         claim("IMMUN", "contribue au fonctionnement normal du système immunitaire", "système immunitaire"),
         claim("FATIGUE", "contribue à réduire la fatigue", "fatigue"),
         claim("HORMONES", "contribue à réguler l'activité hormonale", "activité hormonale"),
     ],
     "risk_phrases": ["contre le syndrome premenstruel", "regule les hormones feminines", "anti-stress"],
     "review": ["Vitamine B6 à forte dose : neuropathies rapportées ; limite supérieure de sécurité et dose maximale "
                "française à saisir pour le contrôle des doses (lot ultérieur)."]},

    {"code": "VIT_B9", "kind": "VITAMINE", "name_fr": "Vitamine B9 (folates)", "subject": "Les folates",
     "source_ref": "VNR (acide folique) : Règl. (UE) 1169/2011, annexe XIII, partie A",
     "aliases": [("vitamine B9", "NOM_COMMUN_FR"), ("B9", "ABREVIATION"), ("folates", "NOM_COMMUN_FR"),
                 ("folate", "NOM_COMMUN_FR"), ("acide folique", "FORME_CHIMIQUE"),
                 ("méthylfolate", "FORME_CHIMIQUE"), ("L-méthylfolate de calcium", "FORME_CHIMIQUE"),
                 ("5-MTHF", "ABREVIATION"), ("Quatrefolic", "NOM_COMMERCIAL", False,
                                              "Marque d'un sel de 5-MTHF : à confirmer")],
     "claims": [
         claim("TISSUS-MATERNELS", "contribuent à la croissance des tissus maternels durant la grossesse",
               "tissus maternels et grossesse", M,
               [dose(400, UG, "PORTION_JOURNALIERE", B, "Règl. (UE) 432/2012, annexe : seuil propre à cette allégation "
                     "— valeur saisie de mémoire, à vérifier")]),
         claim("ACIDES-AMINES", "contribuent à la synthèse normale des acides aminés", "acides aminés"),
         claim("SANG", "contribuent à la formation normale du sang", "formation du sang", B),
         claim("HOMOCYSTEINE", "contribuent au métabolisme normal de l'homocystéine", "homocystéine"),
         claim("PSY", "contribuent à des fonctions psychologiques normales", "fonctions psychologiques"),
         claim("IMMUN", "contribuent au fonctionnement normal du système immunitaire", "système immunitaire"),
         claim("FATIGUE", "contribuent à réduire la fatigue", "fatigue"),
         claim("DIVISION", "jouent un rôle dans le processus de division cellulaire", "division cellulaire"),
         claim("ART14-TUBE-NEURAL", None, "tube neural (réduction d'un facteur de risque)", M,
               [dose(400, UG, "PORTION_JOURNALIERE", M, "Règlement d'autorisation art. 14 : 400 " + UG +
                     " d'acide folique par jour", form_required="ACIDE_FOLIQUE"),
                notice_cond("FOLATES_ART14_POPULATION", "A_PROXIMITE", B),
                notice_cond("FOLATES_ART14_EFFET", "A_PROXIMITE", M)],
               regime="AUTHORIZED_ART14", variants=False,
               full_text="La supplémentation en acide folique augmente le taux de folates maternels. Un faible taux "
                         "de folates maternels est un facteur de risque de malformation du tube neural chez le fœtus "
                         "en développement",
               verb="augmente", function="le taux de folates maternels"),
     ],
     "risk_phrases": ["previent les malformations", "evite le spina bifida", "favorise la fertilite",
                      "aide a tomber enceinte"],
     "review": ["Libellé « formation normale du sang » : traduction FR exacte incertaine (« hématopoïèse » ?), à "
                "vérifier mot à mot.",
                "Allégation art. 14 : réservée à l'acide folique (forme synthétique) — conditions.py n'évalue pas "
                "encore « form_required » ; l'allégation doit rester CONDITIONNELLE tant que la forme n'est pas confirmée.",
                "Équivalence méthylfolate / acide folique pour les seuils : à arbitrer."]},

    {"code": "VIT_B12", "kind": "VITAMINE", "name_fr": "Vitamine B12", "subject": "La vitamine B12",
     "source_ref": "VNR : Règl. (UE) 1169/2011, annexe XIII, partie A",
     "aliases": [("vitamine B12", "NOM_COMMUN_FR"), ("B12", "ABREVIATION"), ("cobalamine", "FORME_CHIMIQUE"),
                 ("cyanocobalamine", "FORME_CHIMIQUE"), ("méthylcobalamine", "FORME_CHIMIQUE"),
                 ("hydroxocobalamine", "FORME_CHIMIQUE"), ("adénosylcobalamine", "FORME_CHIMIQUE")],
     "claims": [
         claim("ENERGIE", "contribue à un métabolisme énergétique normal", "métabolisme énergétique"),
         claim("SYSNERV", "contribue au fonctionnement normal du système nerveux", "système nerveux"),
         claim("HOMOCYSTEINE", "contribue au métabolisme normal de l'homocystéine", "homocystéine"),
         claim("PSY", "contribue à des fonctions psychologiques normales", "fonctions psychologiques"),
         claim("GR", "contribue à la formation normale de globules rouges", "globules rouges"),
         claim("IMMUN", "contribue au fonctionnement normal du système immunitaire", "système immunitaire"),
         claim("FATIGUE", "contribue à réduire la fatigue", "fatigue"),
         claim("DIVISION", "joue un rôle dans le processus de division cellulaire", "division cellulaire"),
     ],
     "risk_phrases": ["ameliore la memoire", "contre l'anemie", "energie immediate", "boost d'energie"],
     "review": []},
]

PLANTS = [
    {"code": "WITHANIA_SOMNIFERA", "name_fr": "Ashwagandha", "scientific_name": "Withania somnifera (L.) Dunal",
     "family": "Solanaceae", "parts": [("RACINE", M)],
     "aliases": [("ashwagandha", "NOM_COMMUN_FR"), ("withania", "SYNONYME_BOTANIQUE"),
                 ("withania somnifera", "SYNONYME_BOTANIQUE"), ("ginseng indien", "NOM_VERNACULAIRE_ETRANGER"),
                 ("KSM-66", "NOM_COMMERCIAL"), ("Sensoril", "NOM_COMMERCIAL", False, "Marque d'extrait : à confirmer")],
     "themes": [("relaxation", B, ["aide a la relaxation", "favorise la detente", "aide a gerer le stress"]),
                ("vitalité", B, ["contribue a la vitalite", "soutient l'energie"])],
     "risk_phrases": ["contre l'anxiete", "baisse le cortisol", "reduit le cortisol", "booste la testosterone",
                      "contre le burn-out", "adaptogene puissant"],
     "restrictions": [{"populations": ["FEMMES_ENCEINTES", "FEMMES_ALLAITANTES", "ENFANTS", "ADOLESCENTS"],
                       "kind": "DECONSEILLE", "notice": "VIG_ASHWAGANDHA", "confidence": B,
                       "detail": "Aussi : pathologies thyroïdiennes, hépatiques ou auto-immunes ; traitements "
                                 "(immunosuppresseurs, sédatifs, hormones thyroïdiennes).",
                       "source_ref": "Nutrivigilance et avis d'autorités européennes — à vérifier"}],
     "review": ["Statut de la plante en France (arrêté du 24 juin 2014, partie racine ; feuilles ?) et dernières "
                "positions ANSES : à vérifier avant tout import d'allégation.",
                "« Baisse le cortisol » : biomarqueur, allégation de santé sans correspondance — rouge."]},
    {"code": "RHODIOLA_ROSEA", "name_fr": "Rhodiola", "scientific_name": "Rhodiola rosea L.", "family": "Crassulaceae",
     "parts": [("RACINE", M), ("RHIZOME", M)],
     "aliases": [("rhodiola", "NOM_COMMUN_FR"), ("rhodiola rosea", "SYNONYME_BOTANIQUE"),
                 ("orpin rose", "NOM_COMMUN_FR")],
     "themes": [("stress", M, ["aide l'organisme a s'adapter au stress", "aide a resister au stress"]),
                ("performance mentale et physique", M, ["contribue a la performance mentale et physique",
                                                        "soutient la concentration"]),
                ("fatigue", B, ["aide a reduire la fatigue"])],
     "risk_phrases": ["antidepresseur naturel", "contre le burn-out", "contre la depression"],
     "restrictions": [{"populations": ["FEMMES_ENCEINTES", "FEMMES_ALLAITANTES", "ENFANTS"], "kind": "DECONSEILLE",
                       "notice": "VIG_ADAPTOGENE_DONNEES", "confidence": M, "detail": None,
                       "source_ref": "Prudence faute de données — à confirmer"}],
     "review": []},
    {"code": "VALERIANA_OFFICINALIS", "name_fr": "Valériane", "scientific_name": "Valeriana officinalis L.",
     "family": "Caprifoliaceae (anciennement Valerianaceae)", "parts": [("RACINE", M), ("RHIZOME", M)],
     "aliases": [("valériane", "NOM_COMMUN_FR"), ("valériane officinale", "NOM_COMMUN_FR"),
                 ("valeriana officinalis", "SYNONYME_BOTANIQUE"),
                 ("herbe aux chats", "NOM_COMMUN_FR", True, "Désigne aussi la cataire (Nepeta cataria)")],
     "themes": [("sommeil", M, ["favorise l'endormissement", "aide a trouver le sommeil",
                                "contribue a un sommeil de qualite"]),
                ("relaxation", M, ["aide a se relaxer", "favorise la relaxation"])],
     "risk_phrases": ["contre l'insomnie", "somnifere naturel", "calmant naturel", "valeriane anxiolytique"],
     "restrictions": [{"populations": ["ENFANTS", "FEMMES_ENCEINTES", "FEMMES_ALLAITANTES"], "kind": "DECONSEILLE",
                       "notice": "VIG_SEDATIF_VEGETAL", "confidence": M,
                       "detail": "Enfants de moins de 12 ans ; somnolence et conduite ; traitements sédatifs.",
                       "source_ref": "Monographie européenne (HMPC) Valerianae radix — à vérifier"}],
     "review": []},
    {"code": "PASSIFLORA_INCARNATA", "name_fr": "Passiflore", "scientific_name": "Passiflora incarnata L.",
     "family": "Passifloraceae", "parts": [("PARTIES_AERIENNES", M)],
     "aliases": [("passiflore", "NOM_COMMUN_FR"), ("passiflore officinale", "NOM_COMMUN_FR"),
                 ("passiflora incarnata", "SYNONYME_BOTANIQUE"),
                 ("fleur de la passion", "NOM_COMMUN_FR", True, "Désigne aussi d'autres Passiflora (P. edulis)")],
     "themes": [("relaxation", M, ["aide a se detendre", "favorise la relaxation"]),
                ("sommeil", B, ["favorise un sommeil paisible"])],
     "risk_phrases": ["anxiolytique naturel", "contre les crises d'angoisse", "calmant naturel", "aide au sevrage"],
     "restrictions": [{"populations": ["ENFANTS", "FEMMES_ENCEINTES", "FEMMES_ALLAITANTES"], "kind": "DECONSEILLE",
                       "notice": "VIG_SEDATIF_VEGETAL", "confidence": M,
                       "detail": "Enfants de moins de 12 ans ; somnolence et conduite ; traitements sédatifs.",
                       "source_ref": "Monographie européenne (HMPC) Passiflorae herba — à vérifier"}],
     "review": []},
    {"code": "CROCUS_SATIVUS", "name_fr": "Safran", "scientific_name": "Crocus sativus L.", "family": "Iridaceae",
     "parts": [("STIGMATE", M)],
     "aliases": [("safran", "NOM_COMMUN_FR"), ("crocus sativus", "SYNONYME_BOTANIQUE"),
                 ("affron", "NOM_COMMERCIAL", False, "Marque d'extrait de safran : à confirmer")],
     "themes": [("humeur / équilibre émotionnel", M, ["contribue a l'equilibre emotionnel",
                                                      "aide a maintenir une humeur positive"])],
     "risk_phrases": ["antidepresseur naturel", "contre la depression", "contre le syndrome premenstruel",
                      "safran coupe-faim"],
     "restrictions": [{"populations": ["FEMMES_ENCEINTES", "PERSONNES_SOUS_TRAITEMENT"], "kind": "DECONSEILLE",
                       "notice": "VIG_SAFRAN", "confidence": B,
                       "detail": "Forte dose pendant la grossesse ; traitements antidépresseurs.",
                       "source_ref": "Données de sécurité et interactions — à vérifier"}],
     "review": ["Piège d'alias : « safran des Indes » désigne le curcuma (lot 2). Le moteur doit préférer la "
                "correspondance la plus longue et ne pas rattacher ce terme au Crocus sativus."]},
    {"code": "PANAX_GINSENG", "name_fr": "Ginseng", "scientific_name": "Panax ginseng C.A.Mey.", "family": "Araliaceae",
     "parts": [("RACINE", M)],
     "aliases": [("panax ginseng", "SYNONYME_BOTANIQUE"), ("ginseng rouge", "NOM_COMMUN_FR"),
                 ("ginseng coréen", "NOM_COMMUN_FR"), ("ginseng asiatique", "NOM_COMMUN_FR"),
                 ("ginseng", "NOM_COMMUN_FR", True, "Peut désigner Panax quinquefolius ; « ginseng sibérien » = "
                                                    "Eleutherococcus, « ginseng indien » = ashwagandha")],
     "themes": [("vitalité", M, ["contribue a la vitalite", "aide a retrouver tonus et vitalite"]),
                ("performance physique et cognitive", M, ["soutient les performances physiques et intellectuelles"])],
     "risk_phrases": ["viagra naturel", "contre les troubles de l'erection", "booste la testosterone",
                      "aphrodisiaque puissant"],
     "restrictions": [{"populations": ["ENFANTS", "ADOLESCENTS", "FEMMES_ENCEINTES", "FEMMES_ALLAITANTES",
                                       "PERSONNES_SOUS_TRAITEMENT"], "kind": "DECONSEILLE", "notice": "VIG_GINSENG",
                       "confidence": M, "detail": "Moins de 18 ans ; hypertension ; diabète ; anticoagulants.",
                       "source_ref": "Monographie européenne (HMPC) Ginseng radix — à vérifier"}],
     "review": []},
]

GLOBAL_REVIEW = [
    ("GLOBAL", "LIBELLES-432", H, "Comparer CHAQUE libellé OFFICIEL_JOUE au texte français consolidé du Règl. (UE) "
     "432/2012 sur EUR-Lex (accents, articles, ponctuation compris). Le statut OFFICIEL_JOUE est une intention, pas "
     "une preuve."),
    ("GLOBAL", "BASE-SOURCE-DE", M, "Confirmer la base de calcul de la « source de » pour un complément alimentaire "
     "(portion journalière recommandée) et l'expression en élément pour les minéraux."),
    ("GLOBAL", "VERBES-SUBSTITUTION", M, "Arbitrer les substitutions de verbe : « participe à », « aide à » pour "
     "« contribue à ». Si une substitution est acceptée, le générateur ajoute une variante V04 par allégation."),
    ("GLOBAL", "ONHOLD-IMPORT", H, "Importer la liste officielle des allégations botaniques en attente (identifiants, "
     "libellé source, conditions d'utilisation) pour les 6 plantes du lot, puis produire les traductions de travail."),
    ("GLOBAL", "VOLUMES-SEO", M, "Mesurer les volumes de recherche des termes des ponts (source et date), sinon "
     "laisser NULL."),
    ("GLOBAL", "DOCTRINE-LEXIQUE", M, "Valider la gravité des entrées doctrinales (détox, réparateur, anti-…, booster) "
     "et des vocabulaires pharmaceutiques."),
]

# Ponts SEO : (code, intention illégale, termes de requête illégaux, terme légal, variantes, thème, ancrages, ancrages
# botaniques en attente, confiance, note)
BRIDGES = [
    ("BR-INSOMNIE-ENDORMISSEMENT", "insomnie / somnifère", ["insomnie", "somnifere naturel", "troubles du sommeil",
     "insomnie traitement naturel"], "endormissement", ["temps d'endormissement", "mélatonine endormissement",
     "complément endormissement"], "sommeil", ["MELATONINE-ENDORM"],
     [("VALERIANA_OFFICINALIS", "sommeil"), ("PASSIFLORA_INCARNATA", "sommeil")], H,
     "L'allégation mélatonine vise le temps d'endormissement, pas la qualité ni la durée du sommeil."),
    ("BR-INSOMNIE-SOMMEIL", "insomnie", ["insomnie", "insomnies", "nuits blanches"], "sommeil",
     ["complément sommeil", "complément alimentaire sommeil"], "sommeil", ["MELATONINE-ENDORM"],
     [("VALERIANA_OFFICINALIS", "sommeil")], M,
     "Référence générale (art. 10§3) : n'est utilisable qu'accompagnée d'une allégation spécifique dans le même segment."),
    ("BR-DECALAGE-HORAIRE", "somnifère de voyage", ["somnifere avion", "insomnie decalage horaire"],
     "décalage horaire", ["jet lag", "mélatonine décalage horaire"], "decalage_horaire", ["MELATONINE-JETLAG"], [], H, None),
    ("BR-EPUISEMENT-FATIGUE", "fatigue chronique / burn-out", ["fatigue chronique", "burn-out", "burnout", "surmenage",
     "epuisement physique"], "fatigue", ["réduire la fatigue", "magnésium fatigue", "vitamine B12 fatigue"], "fatigue",
     ["MG-FATIGUE", "VIT_C-FATIGUE", "VIT_B6-FATIGUE", "VIT_B9-FATIGUE", "VIT_B12-FATIGUE"],
     [("RHODIOLA_ROSEA", "fatigue")], H, "« Fatigue chronique » est une pathologie ; « fatigue » est autorisée."),
    ("BR-ENERGIE", "coup de fouet", ["coup de fouet", "booster d'energie"], "métabolisme énergétique",
     ["énergie", "vitamines énergie"], "energie", ["MG-ENERGIE", "VIT_C-ENERGIE", "VIT_B6-ENERGIE", "VIT_B12-ENERGIE"],
     [("PANAX_GINSENG", "vitalité")], M, "« Énergie » seule est une référence générale (art. 10§3)."),
    ("BR-ANXIETE-SYSTEME-NERVEUX", "anxiété / angoisse", ["anxiete", "angoisse", "anxiolytique naturel",
     "crise d'angoisse"], "système nerveux", ["magnésium système nerveux", "vitamines B système nerveux"],
     "systeme_nerveux", ["MG-SYSNERV", "VIT_C-SYSNERV", "VIT_B6-SYSNERV", "VIT_B12-SYSNERV"],
     [("PASSIFLORA_INCARNATA", "relaxation")], H,
     "Récupération d'intention faible : le volume de « système nerveux » n'a rien de commun avec « anxiété »."),
    ("BR-DEPRESSION-FONCTIONS-PSY", "dépression / déprime", ["depression", "antidepresseur naturel", "deprime"],
     "fonctions psychologiques", ["magnésium fonctions psychologiques"], "psychologique",
     ["MG-PSY", "VIT_C-PSY", "VIT_B6-PSY", "VIT_B9-PSY", "VIT_B12-PSY"],
     [("CROCUS_SATIVUS", "humeur / équilibre émotionnel")], H,
     "Volume très faible : la vraie récupération SEO passe par les requêtes ingrédient (« magnésium B6 »)."),
    ("BR-STRESS", "anti-stress", ["anti-stress", "antistress", "stress anxiete"], "stress", ["gestion du stress"],
     "stress", [], [("RHODIOLA_ROSEA", "stress"), ("WITHANIA_SOMNIFERA", "relaxation")], M,
     "Aucune allégation vitamine/minéral ne vise le stress : pont publiable seulement après import on-hold."),
    ("BR-CRAMPES-FONCTION-MUSCULAIRE", "crampes / spasmophilie", ["crampes", "anti-crampes", "spasmophilie",
     "tetanie"], "fonction musculaire", ["magnésium muscles", "magnésium sportif"], "muscle", ["MG-MUSCLE"], [], H, None),
    ("BR-INFECTIONS-SYSTEME-IMMUNITAIRE", "rhume / grippe", ["rhume", "grippe", "anti-grippe", "antiviral naturel",
     "booster immunite"], "système immunitaire", ["immunité", "vitamine C immunité", "défenses immunitaires"],
     "immunite", ["VIT_C-IMMUN", "VIT_B6-IMMUN", "VIT_B9-IMMUN", "VIT_B12-IMMUN"], [], M,
     "« Immunité » et « défenses immunitaires » : équivalence de sens à arbitrer ; « défenses naturelles » exclu."),
    ("BR-ANEMIE-GLOBULES-ROUGES", "anémie", ["anemie", "anemique"], "globules rouges",
     ["formation des globules rouges", "vitamine B12 globules rouges"], "globules_rouges",
     ["VIT_B6-GR", "VIT_B12-GR"], [], H, "Le fer (lot 3) complétera les ancrages."),
    ("BR-RIDES-COLLAGENE", "anti-rides / anti-âge", ["anti-rides", "anti-age", "antirides"], "collagène",
     ["vitamine C collagène", "collagène peau"], "collagene_peau", ["VIT_C-COLL-PEAU"], [], H,
     "Ancre des formules collagène du lot 2 : l'effet est porté par la vitamine C, jamais par le collagène."),
    ("BR-ARTHROSE-CARTILAGE", "arthrose / douleurs articulaires", ["arthrose", "douleurs articulaires",
     "anti-inflammatoire naturel articulations"], "cartilage", ["cartilages", "vitamine C cartilage"], "cartilage",
     ["VIT_C-COLL-CARTILAGE"], [], H,
     "« Confort articulaire » attend les plantes du lot 2 (harpagophytum, boswellia)."),
]


# =============================================================================
# 5. Construction
# =============================================================================
def lower_first(s: str) -> str:
    return s[0].lower() + s[1:]


def partitive(subject: str) -> str:
    for art, part in (("Le ", "du "), ("La ", "de la "), ("L'", "de l'"), ("Les ", "des ")):
        if subject.startswith(art):
            return part + subject[len(art):]
    raise ValueError(subject)


def build() -> dict:
    review: list[dict] = []

    def task(table, code, conf, text):
        review.append({"target_table": table, "target_code": code, "confidence": conf, "task_fr": text,
                       "author": AUTHOR})

    notices = [{"code": c, "text_fr": t, "text_status": NOTICE_STATUS, "source_ref": s, "confidence": conf}
               for c, t, s, conf in NOTICES]
    for n in notices:
        task("mandatory_notices", n["code"], n["confidence"], f"Vérifier la formulation et la source : {n['source_ref']}")

    substances, claims, conditions, variants, restrictions, parts, worklist, risk = [], [], [], [], [], [], [], []
    for s in SUBSTANCES:
        nrv = NRV.get(s["code"])
        substances.append({"code": s["code"], "kind": s["kind"], "name_fr": s["name_fr"], "scientific_name": None,
                           "botanical_family": None, "nrv_value": float(nrv[0]) if nrv else None,
                           "nrv_unit": nrv[1] if nrv else None, "source_ref": s["source_ref"], "confidence": H,
                           "aliases": [_alias(a) for a in s["aliases"]]})
        for c in s["claims"]:
            code = f"{s['code']}-{c['suffix']}"
            text = c["full_text"] or f"{s['subject']} {c['predicate']}"
            verb, function = (c["verb"], c["function"]) if c["verb"] else c["predicate"].split(" ", 1)
            claims.append({"code": code, "regime": c["regime"], "substance": s["code"], "text_fr": text,
                           "text_fr_status": "OFFICIEL_JOUE",
                           "text_fr_source_ref": "Règl. (UE) 432/2012, annexe, version FR" if c["regime"] ==
                           "AUTHORIZED_432_2012" else "Règlement d'autorisation art. 14 — référence à compléter",
                           "verb_fr": verb, "function_fr": function, "function_label_fr": c["label"],
                           "confidence": c["confidence"]})
            if c["confidence"] != H:
                task("regulatory_claims", code, c["confidence"], f"Libellé saisi avec une confiance {c['confidence']} : "
                     f"vérifier « {text} » mot à mot.")
            conds = c["conditions"] if c["conditions"] is not None else [source_of(s["code"])]
            for i, cond in enumerate(conds, start=1):
                conditions.append({"code": f"{code}-C{i}", "claim": code, **cond})
                if cond["confidence"] != H:
                    task("claim_conditions", f"{code}-C{i}", cond["confidence"],
                         f"Vérifier la condition {cond['type']} {json.dumps(cond.get('params'), ensure_ascii=False)}.")
            variants.append({"code": f"{code}-V00", "claim": code, "text_fr": text, "syntactic_form": "PHRASE_COMPLETE",
                             "is_official_wording": True, "equivalence_note": None})
            if c["variants"]:
                for vcode, vtext, form in (
                        ("V01", f"{lower_first(s['subject'])}, qui {c['predicate']}", "GROUPE_NOMINAL_DEFINI"),
                        ("V02", f"{partitive(s['subject'])}, qui {c['predicate']}", "GROUPE_NOMINAL_PARTITIF"),
                        ("V03", f"avec {partitive(s['subject'])}, qui {c['predicate']}", "GROUPE_PREPOSITIONNEL")):
                    variants.append({"code": f"{code}-{vcode}", "claim": code, "text_fr": vtext, "syntactic_form": form,
                                     "is_official_wording": False,
                                     "equivalence_note": "Prédicat officiel reproduit à l'identique ; seule la "
                                                         "construction syntaxique du sujet change."})
        for r in s.get("restrictions", []):
            restrictions.append({"substance": s["code"], "plant_part": None, **r})
        for i, phrase in enumerate(s.get("risk_phrases", []), start=1):
            risk.append(_risk(s["code"], i, phrase))
        for t in s.get("review", []):
            task("substances", s["code"], M, t)

    for p in PLANTS:
        substances.append({"code": p["code"], "kind": "PLANTE", "name_fr": p["name_fr"],
                           "scientific_name": p["scientific_name"], "botanical_family": p["family"], "nrv_value": None,
                           "nrv_unit": None, "source_ref": "Arrêté du 24 juin 2014, annexe I — ligne à contrôler",
                           "confidence": M, "aliases": [_alias(a) for a in p["aliases"]]})
        task("substances", p["code"], M, f"Contrôler {p['scientific_name']} dans l'arrêté du 24 juin 2014 (parties "
             "autorisées, restrictions, mentions).")
        for part, conf in p["parts"]:
            parts.append({"substance": p["code"], "plant_part": part, "authorized_fr": True, "confidence": conf,
                          "source_ref": "Arrêté du 24 juin 2014, annexe I — à vérifier"})
        for i, (theme, conf, phrasings) in enumerate(p["themes"], start=1):
            worklist.append({"code": f"WL-{p['code']}-{i}", "substance": p["code"], "plant_part": p["parts"][0][0],
                             "theme_fr": theme, "theme_confidence": conf, "efsa_ids": [],
                             "efsa_id_status": "TO_VERIFY_IN_EU_REGISTER",
                             "market_phrasings_detection_only": phrasings,
                             "note": "Rechercher les entrées de la liste on-hold pour cette plante et ce thème ; ne "
                                     "jamais réutiliser les formulations de marché comme libellé."})
        for r in p["restrictions"]:
            restrictions.append({"substance": p["code"], "plant_part": p["parts"][0][0], **r})
        for i, phrase in enumerate(p["risk_phrases"], start=1):
            risk.append(_risk(p["code"], i, phrase))
        for t in p["review"]:
            task("substances", p["code"], M, t)

    for r in restrictions:
        task("population_restrictions", f"{r['substance']}-{r['notice']}", r["confidence"],
             f"Vérifier le fondement et la portée de la restriction : {r['source_ref']}")

    lexicon = BLACKLIST + CONTROL + risk
    for e in lexicon:
        if e["confidence"] != H:
            task("lexical_entries", e["code"], e["confidence"], "Valider la gravité et les formes.")

    bridges = []
    for code, label, illegal, legal, legal_vars, topic, anchors, pending, conf, note in BRIDGES:
        bridges.append({"code": code, "illegal_intent_label": label, "illegal_query_terms": [norm(t) for t in illegal],
                        "legal_term": legal, "legal_term_variants": legal_vars, "topic_key": topic,
                        "search_volume_fr": None, "volume_source": None, "usage_scope": "LES_DEUX",
                        "anchor_claims": anchors,
                        "pending_anchors": [{"substance": s, "theme_fr": t} for s, t in pending],
                        "confidence": conf, "note": note})
        if conf != H:
            task("seo_semantic_bridges", code, conf, f"Arbitrer l'équivalence de sens du terme légal « {legal} ».")
        if not anchors:
            task("seo_semantic_bridges", code, M, "Pont sans ancrage publié : reporter à la release qui importera les "
                 "allégations botaniques correspondantes.")

    for table, code, conf, text in GLOBAL_REVIEW:
        task(table, code, conf, text)

    return {"seed_format_version": "1.0", "release": RELEASE, "author": AUTHOR,
            "content_status": "BROUILLON_NON_VERIFIE",
            "notices": notices, "substances": substances, "plant_part_authorizations": parts,
            "population_restrictions": restrictions, "regulatory_claims": claims, "claim_conditions": conditions,
            "claim_variants": variants, "lexical_entries": lexicon, "seo_semantic_bridges": bridges,
            "botanical_import_worklist": worklist, "review_items": review}


def _alias(a) -> dict:
    alias, kind = a[0], a[1]
    return {"alias": alias, "alias_kind": kind, "is_ambiguous": a[2] if len(a) > 2 else False,
            "note": a[3] if len(a) > 3 else None}


def _risk(substance: str, i: int, phrase: str) -> dict:
    return L(f"RISK-{substance.replace('_', '-')}-{i:02d}", phrase, [phrase], "ALLEGATION_NON_AUTORISEE",
             "R1924_2006_ART10_1", RRO, conf=M, substance=substance,
             note="Allégation de santé typique du marché sans correspondance autorisée pour cette substance.")


# =============================================================================
# 6. Validations (bloquantes)
# =============================================================================
UNIT_TOKENS = {"mg", "g", "ug", "ui", "ml", "kg", "l"}


def validate(seed: dict) -> dict:
    errors: list[str] = []
    codes: dict[str, set] = {}
    for section in ("notices", "substances", "regulatory_claims", "claim_conditions", "claim_variants",
                    "lexical_entries", "seo_semantic_bridges", "botanical_import_worklist"):
        seen = codes.setdefault(section, set())
        for row in seed[section]:
            if row["code"] in seen:
                errors.append(f"{section} : code dupliqué {row['code']}")
            seen.add(row["code"])

    alias_norms: dict[str, str] = {}
    for s in seed["substances"]:
        for a in s["aliases"]:
            n = norm(a["alias"])
            if n in UNIT_TOKENS:
                errors.append(f"Alias « {a['alias']} » identique à une unité : il casserait l'extraction des doses")
            if n in alias_norms and alias_norms[n] != s["code"]:
                errors.append(f"Alias « {n} » partagé par {alias_norms[n]} et {s['code']}")
            alias_norms[n] = s["code"]

    for e in seed["lexical_entries"]:
        for f in e["surface_forms"] + e["neutralizing_left_context"]:
            if norm(f) != f:
                errors.append(f"{e['code']} : forme non normalisée « {f} »")
        if e["category"] == "PATHOLOGIE" and not e["icd10_code"]:
            errors.append(f"{e['code']} : code CIM-10 manquant")

    compiled = {"lexicon": [{"code": e["code"], "category": e["category"], "legal_basis": e["legal_basis"],
                             "forms": e["surface_forms"], "pattern": e["pattern_regex"],
                             "severity": {"PRODUIT": e["severity_product"],
                                          "ARTICLE_AVEC_PONT": e["severity_article_with_bridge"],
                                          "ARTICLE_SANS_PONT": e["severity_article_without_bridge"]},
                             "topic": e["topic_key"], "neutralizers": e["neutralizing_left_context"]}
                            for e in seed["lexical_entries"]]}
    lex = LexiconIndex.from_compiled_rules(compiled)

    # a) Aucun libellé autorisé ni aucune variante ne doit déclencher le lexique.
    for row in seed["regulatory_claims"] + seed["claim_variants"]:
        hits = [h for h in lex.scan(row["text_fr"], exclude_control=True)]
        if hits:
            errors.append(f"{row['code']} déclenche le lexique : {[(h.entry.code, h.surface) for h in hits]}")
    # b) Mentions : rien en dehors des zones d'avertissement (sauf verbes thérapeutiques neutralisés).
    for n in seed["notices"]:
        for h in lexical_hits(lex, n["text_fr"]):
            errors.append(f"Mention {n['code']} déclenche {h.entry.code} (« {h.surface} ») hors avertissement")
    # c) Variantes : substance nommée, aucun chiffre hors identifiant.
    subj_names = {s["code"]: {norm(a["alias"]) for a in s["aliases"]} | {norm(s["name_fr"])} for s in seed["substances"]}
    claim_sub = {c["code"]: c["substance"] for c in seed["regulatory_claims"]}
    for v in seed["claim_variants"]:
        t = norm(v["text_fr"])
        names = sorted(subj_names[claim_sub[v["claim"]]], key=len, reverse=True)
        if not any(n in t for n in names):
            errors.append(f"{v['code']} : substance absente")
        for n in names:
            t = t.replace(n, " ")
        if any(ch.isdigit() for ch in t):
            errors.append(f"{v['code']} : chiffre hors identifiant")
    # d) Ponts : terme légal propre, intention illégale détectée, ancrages existants.
    claim_codes = {c["code"] for c in seed["regulatory_claims"]}
    plant_codes = {s["code"] for s in seed["substances"] if s["kind"] == "PLANTE"}
    for b in seed["seo_semantic_bridges"]:
        for term in [b["legal_term"], *b["legal_term_variants"]]:
            hits = lex.scan(term, exclude_control=True)
            if hits:
                errors.append(f"{b['code']} : terme légal « {term} » détecté par {hits[0].entry.code}")
        for term in b["illegal_query_terms"]:
            if not lex.scan(term, exclude_control=True):
                errors.append(f"{b['code']} : intention « {term} » non détectée par le lexique")
        for a in b["anchor_claims"]:
            if a not in claim_codes:
                errors.append(f"{b['code']} : ancrage inconnu {a}")
        for p in b["pending_anchors"]:
            if p["substance"] not in plant_codes:
                errors.append(f"{b['code']} : ancrage botanique inconnu {p['substance']}")
        if b["search_volume_fr"] is not None:
            errors.append(f"{b['code']} : volume non sourcé")
    # e) Motifs de risque : détectés, et jamais présents dans un libellé autorisé.
    official = " ¤ ".join(norm(c["text_fr"]) for c in seed["regulatory_claims"])
    for e in seed["lexical_entries"]:
        if e["category"] == "ALLEGATION_NON_AUTORISEE" and any(f in official for f in e["surface_forms"]):
            errors.append(f"{e['code']} recouvre un libellé autorisé")

    if errors:
        raise SystemExit("VALIDATION ÉCHOUÉE :\n  " + "\n  ".join(errors))
    blacklist = [e for e in seed["lexical_entries"] if e["category"] not in
                 ("VERBE_EFFET", "THEME_PHYSIOLOGIQUE", "ATTRIBUT_FACTUEL", "ALLEGATION_NON_AUTORISEE")]
    return {"blacklist_transversale": len(blacklist),
            "motifs_de_risque_par_substance": sum(e["category"] == "ALLEGATION_NON_AUTORISEE"
                                                  for e in seed["lexical_entries"]),
            "entrees_de_controle": sum(e["category"] in ("VERBE_EFFET", "THEME_PHYSIOLOGIQUE", "ATTRIBUT_FACTUEL")
                                       for e in seed["lexical_entries"]),
            "substances": len(seed["substances"]), "allegations": len(seed["regulatory_claims"]),
            "variantes": len(seed["claim_variants"]), "conditions": len(seed["claim_conditions"]),
            "mentions": len(seed["notices"]), "restrictions": len(seed["population_restrictions"]),
            "ponts_seo": len(seed["seo_semantic_bridges"]),
            "liste_travail_botanique": len(seed["botanical_import_worklist"]),
            "taches_de_revue": len(seed["review_items"])}


def review_markdown(seed: dict) -> str:
    out = [f"# Revue expert — {seed['release']['label']}", "",
           "Brouillon généré par un assistant IA. Rien n'est publiable avant résolution de chaque tâche "
           "(`kb.v_release_blockers`).", ""]
    by_sub: dict[str, list] = {}
    for c in seed["regulatory_claims"]:
        by_sub.setdefault(c["substance"], []).append(c)
    conds: dict[str, list] = {}
    for c in seed["claim_conditions"]:
        conds.setdefault(c["claim"], []).append(c)
    for s in seed["substances"]:
        out += [f"## {s['name_fr']} (`{s['code']}`)", ""]
        if s["code"] in by_sub:
            out += ["| Code | Libellé (à vérifier sur EUR-Lex) | Conditions | Confiance |", "|---|---|---|---|"]
            for c in by_sub[s["code"]]:
                cs = "; ".join(f"{x['type']} {x.get('params', {}).get('amount', '')} {x.get('params', {}).get('unit', '')}"
                               f"{' ' + x['notice_code'] if x.get('notice_code') else ''}".strip() for x in conds[c["code"]])
                out.append(f"| {c['code']} | {c['text_fr']} | {cs} | {c['confidence']} |")
            out.append("")
        wl = [w for w in seed["botanical_import_worklist"] if w["substance"] == s["code"]]
        if wl:
            out += ["Allégations on-hold : **aucune importée**. Thèmes à rechercher dans la liste officielle :", ""]
            out += [f"- {w['theme_fr']} (partie {w['plant_part']}, confiance du thème {w['theme_confidence']})" for w in wl]
            out.append("")
    out += ["## Tâches de revue", "", "| Table | Code | Confiance | Tâche |", "|---|---|---|---|"]
    out += [f"| {r['target_table']} | {r['target_code']} | {r['confidence']} | {r['task_fr']} |" for r in seed["review_items"]]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    seed = build()
    stats = validate(seed)
    out_dir = ROOT / "seed" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "lot1.json").write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "lot1_revue_expert.md").write_text(review_markdown(seed), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
