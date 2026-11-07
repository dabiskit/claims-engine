"""tests/test_kb_sync.py — import officiel : lecture du registre de l'Union et de l'annexe 432/2012 (formats réels)."""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.conditions import ClaimUsability, evaluate_claim  # noqa: E402
from eval.pipeline_adapter import KnowledgeBase, product_of  # noqa: E402
from webapp import kb_sync  # noqa: E402

SEED = json.loads((ROOT / "seed" / "out" / "lot1.json").read_text(encoding="utf-8"))


def node(ident, value=None, children=()):
    return {"value": value, "valueIdentifier": ident, "childrenValues": list(children)}


def reg_item(code, status, ctype, sub, claim, entry="144", celex="32012R0432"):
    return {"value": None, "childrenValues": [
        node("policyItemCode", code),
        node("hcCharacteristicWrapper", None, [node("hcClaimStatus", status), node("hcClaimType", ctype),
                                              node("hcNutSubFoodCat", sub), node("hcEntryId", entry)]),
        node("hcClaimWrapper", None, [node("hcClaim", claim)]),
        node("hcLegislationsWrapper", None, [node("hcLegislationsComplex", None, [
            node("hcEurLexUrl", f"http://eur-lex.europa.eu/LexUriServ/LexUriServ.do?uri=CELEX:{celex}:EN:NOT")])]),
    ]}


ANNEX_ROW = ("<tr><td><p>{0}</p></td><td><p>{1}</p></td><td><p>{2}</p></td><td><p>{3}</p></td>"
             "<td><p>2009; 7(9):1226</p></td><td><p>{4}</p></td></tr>")
SOURCE = ("L’allégation ne peut être utilisée que pour une denrée alimentaire qui est au moins une source de {0} "
          "au sens de l’allégation SOURCE DE [NOM DES VITAMINES] ET/OU [NOM DES MINÉRAUX] définie dans l’annexe "
          "du règlement (CE) no 1924/2006.")


def annex_html(n_filler=160):
    rows = [ANNEX_ROW.format("Nutriment, substance, denrée alimentaire", "Allégation", "Conditions", "Restrictions", "Numéro")]
    rows.append(ANNEX_ROW.format("Vitamine A", "La vitamine À contribue au maintien d’une peau normale", SOURCE.format("vitamine A"), "", "15"))
    rows.append(ANNEX_ROW.format("Créatine", "La créatine augmente la performance physique lors d’exercices brefs et intenses",
                                 "L’allégation ne peut être utilisée que pour une denrée alimentaire qui garantit une consommation journalière de 3 g de créatine. L’allégation peut être utilisée si le consommateur est informé que l’effet bénéfique est obtenu par la consommation journalière de 3 g.", "", "144"))
    rows.append(ANNEX_ROW.format("Acide docosahexaénoïque (DHA)", "L’acide docosahexaénoïque (DHA) contribue au fonctionnement normal du cerveau",
                                 "L’allégation ne peut être utilisée que pour une denrée alimentaire contenant au moins 40 mg de DHA pour 100 g et pour 100 kcal.",
                                 "L’allégation ne peut être utilisée pour des denrées alimentaires destinées aux enfants.", "565, 626"))
    rows.append("<tr><td colspan='6'>▼M1</td></tr>")
    rows.append(ANNEX_ROW.format("Magnésium", "Le magnésium contribue à réduire la fatigue", SOURCE.format("magnésium"), "", "244"))
    rows += [ANNEX_ROW.format(f"Substance {i}", f"La substance {i} contribue à une fonction {i}", "Texte libre.", "", str(9000 + i)) for i in range(n_filler)]
    return f"<html><body><table class='borderOj'>{''.join(rows)}</table></body></html>"


def test_latest_consolidation():
    assert kb_sync.latest_consolidation("x 02012R0432-20140102 y 02012R0432-20250820 z 02012R0432-20210517") == "02012R0432-20250820"


def test_parse_annex_and_conditions():
    rows = kb_sync.parse_annex(annex_html())
    assert rows[0]["substance_fr"] == "Vitamine A" and rows[0]["entry_ids"] == ["15"]
    assert len(rows) == 164  # la ligne de repère ▼M1 et l'en-tête sont ignorées
    register = [kb_sync.register_record(kb_sync.flatten_register_item(
        reg_item("POL-HC-6500", "HCCS_AUTHORISED", "HCLBT_13_1", "Vitamin C", "Vitamin C contributes …", "144"))),
        kb_sync.register_record(kb_sync.flatten_register_item(
            reg_item("POL-HC-1", "HCCS_NON_AUTHORISED", "HCLBT_13_1", "Collagen hydrolysate", "Collagen helps skin elasticity", "999")))]
    assert register[0]["celex"] == "32012R0432" and register[1]["status"] == "REFUSEE"
    ds = kb_sync.build_dataset("02012R0432-20250820", rows, register, SEED, "2026-11-07T12:00:00+00:00")
    claims = {c["text_fr"]: c for c in ds["regulatory_claims"]}
    # libellé déjà présent dans la base rédigée : pas de doublon
    assert "Le magnésium contribue à réduire la fatigue" not in claims
    vita = claims["La vitamine À contribue au maintien d'une peau normale"]
    assert vita["substance"] == "VITAMINE_A" and "20/08/2025" in vita["text_fr_source_ref"]
    cond = [c for c in ds["claim_conditions"] if c["claim"] == vita["code"]]
    assert cond[0]["params"]["unit"] == "PCT_VNR" and len(cond) == 1
    vitc = next(c for c in ds["regulatory_claims"] if c["substance"] == "CREATINE")
    assert vitc["register_id"] == "POL-HC-6500"
    kinds = [c["type"] for c in ds["claim_conditions"] if c["claim"] == vitc["code"]]
    assert kinds == ["DOSE_MIN_PORTION", "CONDITION_TEXTE"]
    dha = next(c for c in ds["regulatory_claims"] if "DHA" in c["text_fr"])
    assert [c["type"] for c in ds["claim_conditions"] if c["claim"] == dha["code"]] == ["CONDITION_TEXTE", "CONDITION_TEXTE"]
    assert ds["stats"]["rejected"] == 1

    seed = kb_sync.merge_seed(SEED, ds)
    kb = KnowledgeBase(seed)
    assert "VITAMINE_A" in kb.substances and kb.substances["VITAMINE_A"]["nrv_value"] == 800
    comp = [{"substance": "VITAMINE_A", "label": "Vitamine A", "amount": 120, "unit": "µg"}]
    ev = evaluate_claim(vita["code"], "VITAMINE_A", kb.conditions[vita["code"]], product_of({"product_composition": comp}, kb))
    assert ev.usability is ClaimUsability.UTILISABLE   # 120 µg ≥ 15 % de 800 µg
    comp[0]["amount"] = 100
    ev = evaluate_claim(vita["code"], "VITAMINE_A", kb.conditions[vita["code"]], product_of({"product_composition": comp}, kb))
    assert ev.usability is ClaimUsability.INUTILISABLE
    ev = evaluate_claim(dha["code"], dha["substance"], kb.conditions[dha["code"]],
                        product_of({"product_composition": [{"substance": dha["substance"], "label": "DHA", "amount": 250, "unit": "mg"}]}, kb))
    assert ev.usability is ClaimUsability.CONDITIONNELLE
    hits = kb_sync.rejected_for(seed, "collagene marin hydrolyse")
    assert hits and hits[0]["name"] == "Collagène" and hits[0]["count"] == 1


def test_register_stream(tmp_path, monkeypatch):
    items = [reg_item(f"POL-HC-{i}", "HCCS_NON_AUTHORISED", "HCLBT_13_1", "X", "Y", str(i)) for i in range(600)]
    payload = json.dumps(items).encode()

    class FakeResp:
        status_code = 200
        def iter_bytes(self, n):
            yield from (payload[i:i + n] for i in range(0, len(payload), n))
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeClient:
        def stream(self, method, url): return FakeResp()

    recs = kb_sync.fetch_register(FakeClient())
    assert len(recs) == 600 and recs[0]["id"] == "POL-HC-0"


def test_drafted_wording_is_corrected_by_official_text():
    rows = kb_sync.parse_annex(annex_html())
    rows.append({"substance_fr": "Vitamine C", "claim_fr": "La vitamine C contribue à la formation normale de collagène pour assurer le fonctionnement normal des vaisseaux sanguins",
                 "conditions_fr": kb_sync.SOURCE if hasattr(kb_sync, "SOURCE") else "x", "restrictions_fr": "", "efsa_ref": "2009", "entry_ids": ["130"]})
    rows.append({"substance_fr": "Vitamine C", "claim_fr": "La vitamine C contribue à la formation normale de collagène pour assurer la fonction normale des os",
                 "conditions_fr": "x", "restrictions_fr": "", "efsa_ref": "2009", "entry_ids": ["131"]})
    ds = kb_sync.build_dataset("02012R0432-20250820", rows, [], SEED, "2026-11-07T12:00:00+00:00")
    seed = kb_sync.merge_seed(SEED, ds)
    vc = {c["code"]: c for c in seed["regulatory_claims"] if c["substance"] == "VIT_C"}
    v = vc["VIT_C-COLL-VAISSEAUX"]
    assert v["text_fr"].endswith("le fonctionnement normal des vaisseaux sanguins") and v["text_fr_status"] == "OFFICIEL_EUR_LEX_CORRIGE"
    assert "fonction normale des vaisseaux" in v["text_fr_draft"]
    assert not any(c.get("origin") == "IMPORT_OFFICIEL" and "vaisseaux" in c["text_fr"] for c in vc.values())   # pas de doublon
    assert vc["VIT_C-COLL-OS"]["source_url"].startswith("https://eur-lex")      # libellé identique : source rattachée
    assert "fonction normale des os" in vc["VIT_C-COLL-OS"]["text_fr"]           # « os » n'a pas été confondu avec un autre libellé
