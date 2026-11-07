"""
webapp/kb_sync.py — Import automatique de la base réglementaire depuis les sources officielles.

Sources (téléchargées par le serveur, jamais saisies à la main) :
  1. Registre de l'Union des allégations nutritionnelles et de santé (Commission européenne,
     Food and Feed Information Portal) : toutes les demandes, autorisées, refusées, révoquées,
     avec leur type (art. 13.1, 13.5, 14.1.a, 14.1.b), l'avis de l'EFSA et l'acte juridique.
     Texte en anglais.
  2. Règlement (UE) n° 432/2012, version consolidée la plus récente sur EUR-Lex, en français :
     libellé officiel français de chaque allégation art. 13.1 autorisée et ses conditions.
  3. VNR : règlement (UE) n° 1169/2011, annexe XIII, partie A (table fixe ci-dessous).

Ce qui est automatisé : la liste, le libellé officiel, la condition chiffrée quand elle est
standard (« source de » = 15 % de la VNR par portion journalière ; « consommation journalière de
X mg »), la citation de la source et la date de la version.
Ce qui ne l'est pas, et reste marqué « à confirmer » : les conditions non chiffrables (texte
officiel recopié), les rapprochements entre noms commerciaux et substances, l'interprétation.
Tout l'import est en statut IMPORT_OFFICIEL, distinct de la validation de l'expert.

Les allégations botaniques « on-hold » ne figurent pas dans ce registre : elles ne sont pas importées.
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from lxml import html as lhtml

from engine.textnorm import norm

REGISTER_URL = ("https://ec.europa.eu/food/food-feed-portal/backend/api/policy-items"
                "?foodDomain=nut&authorisationType=nut_auth")
REGISTER_PAGE = "https://ec.europa.eu/food/food-feed-portal/screen/health-claims/eu-register"
EURLEX_ELI_FR = "https://eur-lex.europa.eu/eli/reg/2012/432/oj/fra"
EURLEX_HTML = "https://eur-lex.europa.eu/legal-content/FR/TXT/HTML/?uri=CELEX:{celex}"
EURLEX_PAGE = "https://eur-lex.europa.eu/legal-content/FR/TXT/?uri=CELEX:{celex}"
USER_AGENT = "PreAnalyseAllegations/0.2 (import du registre officiel)"
STATUS_LABELS = {"HCCS_AUTHORISED": "AUTORISEE", "HCCS_NON_AUTHORISED": "REFUSEE", "HCCS_REVOKED": "REVOQUEE"}
TYPE_LABELS = {"HCLBT_13_1": "Art. 13.1", "HCLBT_13_5": "Art. 13.5", "HCLBT_14_1_A": "Art. 14.1.a",
               "HCLBT_14_1_B": "Art. 14.1.b"}

# VNR — Règl. (UE) n° 1169/2011, annexe XIII, partie A, point 1.
NRV = {
    "vitamine a": (800, "µg"), "vitamine d": (5, "µg"), "vitamine e": (12, "mg"), "vitamine k": (75, "µg"),
    "vitamine c": (80, "mg"), "thiamine": (1.1, "mg"), "riboflavine": (1.4, "mg"), "niacine": (16, "mg"),
    "vitamine b6": (1.4, "mg"), "folate": (200, "µg"), "vitamine b12": (2.5, "µg"), "biotine": (50, "µg"),
    "acide pantothenique": (6, "mg"), "potassium": (2000, "mg"), "chlorure": (800, "mg"), "calcium": (800, "mg"),
    "phosphore": (700, "mg"), "magnesium": (375, "mg"), "fer": (14, "mg"), "zinc": (10, "mg"), "cuivre": (1, "mg"),
    "manganese": (2, "mg"), "fluorure": (3.5, "mg"), "selenium": (55, "µg"), "chrome": (40, "µg"),
    "molybdene": (50, "µg"), "iode": (150, "µg"),
}
VITAMINS = {"vitamine a", "vitamine d", "vitamine e", "vitamine k", "vitamine c", "thiamine", "riboflavine", "niacine",
            "vitamine b6", "folate", "vitamine b12", "biotine", "acide pantothenique"}
# Codes déjà utilisés par la base rédigée (lot 1) : l'import les réutilise pour ne pas dédoubler.
KNOWN_CODES = {"magnesium": "MG", "melatonine": "MELATONINE", "vitamine c": "VIT_C", "vitamine b6": "VIT_B6",
               "folate": "VIT_B9", "vitamine b12": "VIT_B12"}
# Noms usuels sur les étiquettes (proposés, à valider par l'expert) : ce sont des alias de recherche,
# jamais des libellés d'allégation.
EXTRA_ALIASES = {
    "vitamine a": ["rétinol", "bêta-carotène"], "vitamine d": ["vitamine d3", "cholécalciférol", "vitamine d2"],
    "vitamine e": ["tocophérol", "d-alpha-tocophérol"], "vitamine k": ["vitamine k1", "vitamine k2", "ménaquinone"],
    "thiamine": ["vitamine b1"], "riboflavine": ["vitamine b2"], "niacine": ["vitamine b3", "nicotinamide"],
    "acide pantothenique": ["vitamine b5", "pantothénate de calcium"], "biotine": ["vitamine b8"],
    "folate": ["vitamine b9", "acide folique", "folates", "méthylfolate"], "vitamine b12": ["cobalamine", "méthylcobalamine"],
    "selenium": ["sélénométhionine"], "zinc": ["bisglycinate de zinc", "gluconate de zinc", "citrate de zinc"],
    "fer": ["bisglycinate de fer", "fumarate ferreux", "gluconate ferreux"], "iode": ["iodure de potassium"],
    "chrome": ["picolinate de chrome"], "cuivre": ["gluconate de cuivre"],
}
AMBIGUOUS = {"fer", "eau", "zinc", "chrome", "chlorure", "proteines", "iode", "cuivre", "pectines", "noix", "fibres"}
# Substances fréquentes sans allégation autorisée : rapprochement nom français -> mots du registre (anglais).
REJECTED_SUBJECTS = {
    "Collagène": ["collagen"], "Acide hyaluronique": ["hyaluron"], "Glucosamine": ["glucosamine"],
    "Chondroïtine": ["chondroitin"], "Coenzyme Q10": ["coenzyme q10", "ubiquinone", "coq10"],
    "L-carnitine": ["carnitine"], "Spiruline": ["spirulina"], "Probiotiques / ferments lactiques": ["lactobacillus", "bifidobacterium", "probiotic", "streptococcus thermophilus"],
    "Lutéine": ["lutein"], "Lycopène": ["lycopene"], "Glutathion": ["glutathione"], "MSM": ["methylsulfonylmethane", "msm"],
    "Taurine": ["taurine"], "L-arginine": ["arginine"], "L-glutamine": ["glutamine"], "Resvératrol": ["resveratrol"],
    "Quercétine": ["quercetin"], "Gelée royale": ["royal jelly"], "Propolis": ["propolis"], "Pollen": ["pollen"],
    "Kératine": ["keratin"], "Silicium": ["silicon", "silica"], "Bore": ["boron"], "Acide alpha-lipoïque": ["lipoic"],
    "CLA (acide linoléique conjugué)": ["conjugated linoleic", "cla"], "Caféine": ["caffeine"], "Thé vert": ["green tea", "camellia sinensis"],
    "Levure de bière": ["brewer", "yeast"], "Chlorella": ["chlorella"], "Oméga-3 (autres que EPA/DHA/ALA)": ["omega-3"],
    "Astaxanthine": ["astaxanthin"], "NAC (N-acétylcystéine)": ["acetylcysteine", "acetyl cysteine"],
    "GABA": ["gaba", "gamma-aminobutyric"], "L-théanine": ["theanine"], "5-HTP": ["5-htp", "hydroxytryptophan"],
}

_APOS = str.maketrans({"’": "'", "‘": "'", " ": " ", " ": " "})
_SOURCE_RE = re.compile(r"^L'allégation ne peut être utilisée que pour une denrée alimentaire qui est au moins une "
                        r"source d(?:e |')(?P<sub>.{1,80}?) au sens de l'allégation SOURCE DE \[NOM DES VITAMINES\] "
                        r"ET/OU \[NOM DES MINÉRAUX\] définie dans l'annexe du règlement \(CE\) no? ?1924/2006\.?$", re.I)
_DAILY_RE = re.compile(r"(?:consommation|apport|dose) journali(?:er|ère) d(?:e |')(?:au moins )?(\d+(?:[,.]\d+)?)\s*(mg|µg|g)\b", re.I)
_PORTION_RE = re.compile(r"contenant (?:au moins )?(\d+(?:[,.]\d+)?)\s*(mg|µg|g)\b[^.]{0,80}?par portion quantifiée", re.I)


class SyncError(Exception):
    pass


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(120.0, connect=20.0), follow_redirects=True,
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html"})


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").translate(_APOS)).strip()


def slug(name: str) -> str:
    n = norm(name)
    return re.sub(r"[^a-z0-9]+", "_", n).strip("_").upper()[:60] or "SUBSTANCE"


# ---------------------------------------------------------------------------
# 1. Registre de l'Union
# ---------------------------------------------------------------------------
def flatten_register_item(node: dict) -> dict[str, list[str]]:
    """Le registre renvoie un arbre générique {valueIdentifier, value, childrenValues}. On l'aplatit en
    chemin -> valeurs, ex. 'hcClaimWrapper/hcClaim' -> ['Vitamin C contributes to …']."""
    out: dict[str, list[str]] = {}

    def walk(n: dict, path: str) -> None:
        for c in n.get("childrenValues") or []:
            ident = c.get("valueIdentifier") or "?"
            p = f"{path}/{ident}" if path else ident
            v = c.get("value")
            if v is not None and v != "":
                out.setdefault(p, []).append(str(v))
            walk(c, p)

    walk(node, "")
    return out


def register_record(f: dict[str, list[str]]) -> dict:
    def one(key: str) -> str | None:
        v = f.get(key)
        return _clean(v[0]) if v else None

    urls = f.get("hcLegislationsWrapper/hcLegislationsComplex/hcEurLexUrl") or []
    celex = None
    for u in urls:
        m = re.search(r"CELEX[:%3A]+(\d{5}[A-Z]\d{4})", u, re.I)
        if m:
            celex = m.group(1)
            break
    return {
        "id": one("policyItemCode") or one("hcCharacteristicWrapper/hcClaimId"),
        "status": STATUS_LABELS.get(one("hcCharacteristicWrapper/hcClaimStatus") or "", one("hcCharacteristicWrapper/hcClaimStatus")),
        "type": TYPE_LABELS.get(one("hcCharacteristicWrapper/hcClaimType") or "", one("hcCharacteristicWrapper/hcClaimType")),
        "substance_en": one("hcCharacteristicWrapper/hcNutSubFoodCat"),
        "claim_en": one("hcClaimWrapper/hcClaim"),
        "conditions_en": one("hcClaimWrapper/hcCondOfUse"),
        "restrictions_en": one("hcClaimWrapper/hcRestrictionOfUse"),
        "health_relationship_en": one("hcClaimWrapper/hcHealthRelationship"),
        "entry_ids": sorted({x.strip() for v in f.get("hcCharacteristicWrapper/hcEntryId", []) for x in re.split(r"[,;]", v) if x.strip()}),
        "efsa_question": one("hcEfsaOpinionReferenceWrapper/hcEfsaOpinionReferenceMap/hcEfsaQuestionNbr"),
        "celex": celex,
        "legal_act_url": _clean(urls[0]) if urls else None,
    }


def fetch_register(client: httpx.Client) -> list[dict]:
    """Téléchargement en flux (≈ 20 Mo) vers un fichier temporaire, puis lecture élément par élément :
    la mémoire reste faible même sur une petite instance."""
    import ijson
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        path = Path(tmp.name)
        with client.stream("GET", REGISTER_URL) as r:
            if r.status_code != 200:
                raise SyncError(f"Registre de l'Union : HTTP {r.status_code}")
            for chunk in r.iter_bytes(1 << 16):
                tmp.write(chunk)
    try:
        with path.open("rb") as fh:
            records = [register_record(flatten_register_item(item)) for item in ijson.items(fh, "item")]
    finally:
        path.unlink(missing_ok=True)
    if len(records) < 500:
        raise SyncError(f"Registre de l'Union : {len(records)} entrées seulement, import interrompu")
    return records


# ---------------------------------------------------------------------------
# 2. Règlement 432/2012 consolidé (FR)
# ---------------------------------------------------------------------------
def latest_consolidation(eli_html: str) -> str | None:
    found = sorted(set(re.findall(r"02012R0432-(\d{8})", eli_html)))
    return f"02012R0432-{found[-1]}" if found else None


def parse_annex(html: str) -> list[dict]:
    """Annexe du règlement 432/2012 : tableau à 6 colonnes (substance, allégation, conditions,
    restrictions, référence EFSA, numéros d'entrée). Les lignes de repère de consolidation (▼M1…)
    sont ignorées."""
    root = lhtml.document_fromstring(html)
    tables = sorted(root.xpath("//table"), key=lambda t: -len(t.xpath(".//tr")))
    if not tables:
        raise SyncError("EUR-Lex : aucun tableau dans le texte consolidé")
    rows = []
    for tr in tables[0].xpath(".//tr"):
        cells = [_clean(td.text_content()) for td in tr.xpath("./td|./th")]
        if len(cells) != 6 or norm(cells[1]) in ("allegation", ""):
            continue
        if cells[0].startswith("Nutriment, substance"):
            continue
        rows.append({"substance_fr": cells[0], "claim_fr": cells[1], "conditions_fr": cells[2],
                     "restrictions_fr": cells[3] if cells[3] not in ("—", "-") else "",
                     "efsa_ref": cells[4],
                     "entry_ids": sorted({x.strip() for x in re.split(r"[,;]", cells[5]) if x.strip()})})
    if len(rows) < 150:
        raise SyncError(f"EUR-Lex : {len(rows)} allégations lues seulement dans l'annexe, import interrompu")
    return rows


def fetch_annex(client: httpx.Client) -> tuple[str, list[dict]]:
    r = client.get(EURLEX_ELI_FR)
    if r.status_code != 200:
        raise SyncError(f"EUR-Lex : HTTP {r.status_code}")
    celex = latest_consolidation(r.text) or "32012R0432"
    time.sleep(1)
    r = client.get(EURLEX_HTML.format(celex=celex))
    if r.status_code != 200:
        raise SyncError(f"EUR-Lex ({celex}) : HTTP {r.status_code}")
    return celex, parse_annex(r.text)


# ---------------------------------------------------------------------------
# 3. Construction des données au format de la base
# ---------------------------------------------------------------------------
def _base_name(substance_fr: str) -> str:
    """« Acide docosahexaénoïque (DHA) » -> clé « acide docosahexaenoique (dha) » ; vitamines : clé de la VNR."""
    n = norm(substance_fr)
    n = re.sub(r"\s+", " ", n)
    return n


def _aliases(substance_fr: str, key: str) -> list[dict]:
    names = [substance_fr]
    m = re.search(r"\(([^)]{2,20})\)", substance_fr)
    if m:
        names.append(m.group(1))
        names.append(re.sub(r"\s*\([^)]*\)", "", substance_fr).strip())
    names += EXTRA_ALIASES.get(key, [])
    out, seen = [], set()
    for a in names:
        a = a.strip()
        if not a or norm(a) in seen or len(norm(a)) < 2:
            continue
        seen.add(norm(a))
        out.append({"alias": a.lower() if a.isupper() is False else a, "alias_kind": "NOM_OFFICIEL" if a == substance_fr else "ALIAS_PROPOSE",
                    "is_ambiguous": norm(a) in AMBIGUOUS or len(norm(a)) <= 3, "note": None})
    return out


def _conditions(code: str, row: dict, nrv: tuple | None, source_ref: str) -> list[dict]:
    text = row["conditions_fr"].translate(_APOS)
    conds: list[dict] = []
    if _SOURCE_RE.match(text) and nrv:
        conds.append({"code": f"{code}-C1", "claim": code, "type": "DOSE_MIN_PORTION",
                      "params": {"amount": 15, "unit": "PCT_VNR", "basis": "PORTION_JOURNALIERE",
                                 "derivation": "SOURCE_15_PCT_VNR", "expressed_as": "ELEMENT"},
                      "source_ref": source_ref + " ; seuil « source de » : 15 % de la VNR (Règl. (CE) 1924/2006, annexe ; Règl. (UE) 1169/2011, annexe XIII)",
                      "confidence": "HAUTE"})
    else:
        m = _DAILY_RE.search(text)
        basis = "PORTION_JOURNALIERE"
        if not m:
            m = _PORTION_RE.search(text)
            basis = "PORTION_QUANTIFIEE"
        if m:
            conds.append({"code": f"{code}-C1", "claim": code, "type": "DOSE_MIN_PORTION",
                          "params": {"amount": float(m.group(1).replace(",", ".")), "unit": m.group(2),
                                     "basis": basis, "derivation": "TEXTE_OFFICIEL", "expressed_as": "SUBSTANCE"},
                          "source_ref": source_ref, "confidence": "MOYENNE"})
        if text:
            conds.append({"code": f"{code}-C2", "claim": code, "type": "CONDITION_TEXTE",
                          "params": {"text_fr": row["conditions_fr"]}, "source_ref": source_ref, "confidence": "HAUTE"})
    if row.get("restrictions_fr"):
        conds.append({"code": f"{code}-C3", "claim": code, "type": "CONDITION_TEXTE",
                      "params": {"text_fr": "Restriction ou avertissement : " + row["restrictions_fr"]},
                      "source_ref": source_ref, "confidence": "HAUTE"})
    return conds


def build_dataset(annex_celex: str, annex_rows: list[dict], register: list[dict], existing_seed: dict,
                  retrieved_at: str) -> dict:
    """Produit un complément de seed : substances, allégations, conditions, formulations officielles,
    et la liste des demandes refusées. Les éléments déjà présents dans la base rédigée (même libellé
    normalisé) ne sont pas dupliqués."""
    date = annex_celex.rsplit("-", 1)[-1] if "-" in annex_celex else ""
    version = f"{date[6:8]}/{date[4:6]}/{date[:4]}" if len(date) == 8 else "originale"
    annex_url = EURLEX_PAGE.format(celex=annex_celex)
    source_ref = f"Règl. (UE) 432/2012, annexe, version consolidée du {version} (EUR-Lex {annex_celex})"
    existing_subs = {s["code"]: s for s in existing_seed["substances"]}
    existing_claim_norms = {norm(c["text_fr"]) for c in existing_seed["regulatory_claims"]}
    by_entry: dict[str, dict] = {}
    for rec in register:
        if rec["status"] == "AUTORISEE" and rec["type"] == "Art. 13.1":
            for e in rec["entry_ids"]:
                by_entry.setdefault(e, rec)

    substances: dict[str, dict] = {}
    claims, conditions, variants, confirmations = [], [], [], []
    counters: dict[str, int] = {}
    for row in annex_rows:
        key = _base_name(row["substance_fr"])
        code = KNOWN_CODES.get(key) or slug(row["substance_fr"])
        nrv = NRV.get(key)
        if code not in existing_subs and code not in substances:
            substances[code] = {
                "code": code, "kind": "VITAMINE" if key in VITAMINS else "MINERAL" if key in NRV else "AUTRE_SUBSTANCE",
                "name_fr": row["substance_fr"][:200], "scientific_name": None, "botanical_family": None,
                "nrv_value": nrv[0] if nrv else None, "nrv_unit": nrv[1] if nrv else None,
                "source_ref": ("VNR : Règl. (UE) 1169/2011, annexe XIII, partie A" if nrv else source_ref),
                "confidence": "HAUTE", "aliases": _aliases(row["substance_fr"], key), "origin": "IMPORT_OFFICIEL"}
        if norm(row["claim_fr"]) in existing_claim_norms:
            confirmations.append({"text_norm": norm(row["claim_fr"]), "source_ref": source_ref, "source_url": annex_url,
                                  "efsa_ref": row["efsa_ref"], "conditions_fr": row["conditions_fr"]})
            continue
        counters[code] = counters.get(code, 0) + 1
        ccode = f"{code}-R432-{counters[code]:02d}"
        reg = next((by_entry[e] for e in row["entry_ids"] if e in by_entry), None)
        claims.append({
            "code": ccode, "regime": "AUTHORIZED_432_2012", "substance": code, "text_fr": row["claim_fr"],
            "text_fr_status": "OFFICIEL_EUR_LEX", "text_fr_source_ref": source_ref, "verb_fr": None,
            "function_fr": None, "function_label_fr": None, "confidence": "HAUTE", "origin": "IMPORT_OFFICIEL",
            "source_url": annex_url, "efsa_ref": row["efsa_ref"], "entry_ids": row["entry_ids"],
            "register_id": reg["id"] if reg else None, "claim_en": reg["claim_en"] if reg else None,
            "conditions_fr": row["conditions_fr"], "restrictions_fr": row["restrictions_fr"]})
        conditions += _conditions(ccode, row, nrv, source_ref)
        variants.append({"code": f"{ccode}-V00", "claim": ccode, "text_fr": row["claim_fr"].rstrip(". "),
                         "syntactic_form": "PHRASE_COMPLETE", "is_official_wording": True, "equivalence_note": None})

    annex_norms = {norm(r["claim_fr"]) for r in annex_rows}
    other_authorised = [r for r in register if r["status"] == "AUTORISEE" and r["type"] != "Art. 13.1"]
    rejected = [r for r in register if r["status"] in ("REFUSEE", "REVOQUEE")]
    return {
        "format": 2, "retrieved_at": retrieved_at, "confirmations": confirmations,
        "sources": [
            {"name": "Règlement (UE) n° 432/2012 — version consolidée (FR)", "url": annex_url, "version": version,
             "items": len(annex_rows)},
            {"name": "Registre de l'Union des allégations nutritionnelles et de santé", "url": REGISTER_PAGE,
             "api": REGISTER_URL, "items": len(register),
             "authorised": sum(1 for r in register if r["status"] == "AUTORISEE"),
             "rejected": sum(1 for r in register if r["status"] == "REFUSEE")},
            {"name": "VNR — Règlement (UE) n° 1169/2011, annexe XIII", "url": EURLEX_PAGE.format(celex="32011R1169"),
             "items": len(NRV)},
        ],
        "substances": list(substances.values()), "regulatory_claims": claims, "claim_conditions": conditions,
        "claim_variants": variants, "other_authorised": other_authorised, "rejected": rejected,
        "stats": {"annex_rows": len(annex_rows), "new_substances": len(substances), "new_claims": len(claims),
                  "already_in_base": len(annex_rows) - len(claims), "other_authorised": len(other_authorised),
                  "rejected": len(rejected), "annex_claims_norm": len(annex_norms)},
    }


def run_sync(existing_seed: dict) -> dict:
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _client() as client:
        celex, rows = fetch_annex(client)
        register = fetch_register(client)
    return build_dataset(celex, rows, register, existing_seed, retrieved_at)


_STOP = {"fonction", "fonctionnement", "normal", "normale", "normaux", "normales", "des", "les", "une", "pour", "dans", "aux"}


def _sig(text: str) -> frozenset:
    """Mots porteurs de sens : deux libellés d'une même allégation ne diffèrent que par des mots outils."""
    return frozenset(w for w in re.findall(r"[a-z]+", norm(text)) if len(w) >= 3 and w not in _STOP)


def merge_seed(base_seed: dict, dataset: dict | None) -> dict:
    """Seed effectif = base rédigée (prioritaire) + import officiel."""
    if not dataset:
        return base_seed
    seed = {k: (list(v) if isinstance(v, list) else v) for k, v in base_seed.items()}
    known = {s["code"] for s in seed["substances"]}
    seed["substances"] += [s for s in dataset["substances"] if s["code"] not in known]
    # Libellés de la base rédigée : le texte officiel fait foi.
    #  * identique : la source officielle est rattachée ;
    #  * quasi identique (même substance, écart de rédaction) : le libellé rédigé est corrigé, l'ancien est gardé
    #    pour l'expert, et le doublon officiel n'est pas ajouté.
    from difflib import SequenceMatcher
    seed["regulatory_claims"] = [dict(c) for c in seed["regulatory_claims"]]
    conf = {x["text_norm"]: x for x in dataset.get("confirmations", [])}
    for c in seed["regulatory_claims"]:
        x = conf.get(norm(c["text_fr"]))
        if x:
            c.update(source_url=x["source_url"], text_fr_source_ref=x["source_ref"], efsa_ref=x.get("efsa_ref"))
    corrected: set[str] = set()
    for nc in dataset["regulatory_claims"]:
        target = norm(nc["text_fr"]).rstrip(". ")
        best, score = None, 0.0
        for c in seed["regulatory_claims"]:
            if (c["substance"] != nc["substance"] or c.get("origin") == "IMPORT_OFFICIEL" or c["code"] in corrected
                    or norm(c["text_fr"]) in conf or _sig(c["text_fr"]) != _sig(nc["text_fr"])):
                continue
            r = SequenceMatcher(None, norm(c["text_fr"]).rstrip(". "), target).ratio()
            if r > score:
                best, score = c, r
        if best is not None and score >= 0.85:
            corrected.add(best["code"])
            best.update(text_fr_draft=best["text_fr"], text_fr=nc["text_fr"], text_fr_status="OFFICIEL_EUR_LEX_CORRIGE",
                        text_fr_source_ref=nc["text_fr_source_ref"], source_url=nc["source_url"], efsa_ref=nc.get("efsa_ref"),
                        register_id=nc.get("register_id"))
            seed["claim_variants"].append({"code": f"{best['code']}-VOFF", "claim": best["code"], "text_fr": nc["text_fr"].rstrip(". "),
                                           "syntactic_form": "PHRASE_COMPLETE", "is_official_wording": True,
                                           "equivalence_note": "Libellé officiel (EUR-Lex), remplace la saisie"})
            nc["_dup_of"] = best["code"]
    known_claims = {c["code"] for c in seed["regulatory_claims"]}
    new_claims = [c for c in dataset["regulatory_claims"] if c["code"] not in known_claims and not c.get("_dup_of")]
    for c in dataset["regulatory_claims"]:
        c.pop("_dup_of", None)
    seed["regulatory_claims"] += new_claims
    codes = {c["code"] for c in new_claims}
    seed["claim_conditions"] += [c for c in dataset["claim_conditions"] if c["claim"] in codes]
    seed["claim_variants"] += [v for v in dataset["claim_variants"] if v["claim"] in codes]
    seed["official_import"] = {"retrieved_at": dataset["retrieved_at"], "sources": dataset["sources"],
                               "stats": {**dataset["stats"], "corrected_drafts": len(corrected)}, "format": dataset.get("format", 1)}
    seed["rejected_claims"] = dataset.get("rejected", [])
    seed["other_authorised"] = dataset.get("other_authorised", [])
    rel = dict(seed["release"])
    rel["label"] = f"{rel['label']}+UE-{dataset['retrieved_at'][:10]}"
    seed["release"] = rel
    return seed


def rejected_for(seed: dict, label_norm_text: str) -> list[dict]:
    """Substances courantes sans allégation autorisée, présentes dans un texte (normalisé)."""
    out = []
    rejected = seed.get("rejected_claims") or []
    if not rejected:
        return out
    for fr, keys in REJECTED_SUBJECTS.items():
        fr_names = [norm(x) for x in re.split(r"\s*/\s*|\s*\(|\)", fr) if len(norm(x)) >= 3]
        if not any(re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", label_norm_text) for n in fr_names):
            continue
        hits = [r for r in rejected if r.get("substance_en") and any(k in r["substance_en"].lower() for k in keys)]
        if hits:
            out.append({"name": fr, "count": len(hits), "examples": [
                {"id": h["id"], "substance": h["substance_en"], "claim": (h["claim_en"] or "")[:220]} for h in hits[:5]]})
    return out
