"""tests/test_severity_replacement.py — grille de sévérité et remplacement choisi par l'IA (contrôlé par les règles)."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.ai import LLMUsage  # noqa: E402
from engine.severity import level_for  # noqa: E402
from eval.pipeline_adapter import KnowledgeBase, LivePipeline  # noqa: E402

SEED = json.loads((Path(__file__).resolve().parents[1] / "seed" / "out" / "lot1.json").read_text(encoding="utf-8"))
KB = KnowledgeBase(SEED)
HTML = ("<p>Le collagène aide à prévenir la perte osseuse.</p><p>Absorption facilitée grâce aux peptides courts.</p>"
        "<p>Notre vitamine C est essentielle à vos défenses.</p><p>Il booste la peau.</p>")
CASE = {"test_id": "t", "page_mode": "PRODUCT_PAGE", "page_url": "https://x.invalid/products/p", "product_names": ["Produit"],
        "portions_per_day": 1, "linked_products": [],
        "product_composition": [{"substance": "VIT_C", "label": "Vitamine C", "amount": 80, "unit": "mg"}]}


class Fake:
    def call_tool(self, *, stage, model, system, user_content, tool_name, tool_description, input_schema, max_tokens=0):
        nodes = {n["text"][:12]: n["key"] for n in json.loads(user_content)["nodes"]}
        k = lambda start: next(v for t, v in nodes.items() if t.startswith(start[:12]))  # noqa: E731
        base = {"substance_code": None, "matched_claim_code": "AUCUNE", "wording_faithful": False, "rationale": "test"}
        claims = [
            {**base, "node_key": k("Le collagène"), "quote": "aide à prévenir la perte osseuse", "claim_type": "PROPRIETE_THERAPEUTIQUE_INTERDITE",
             "certainty": "HAUTE", "replacement_action": "REMPLACER_PAR_ALLEGATION", "replacement_claim_code": "VIT_C-COLL-OS",
             "replacement_text": "La vitamine C contribue à la formation normale de collagène pour assurer la fonction normale des os."},
            {**base, "node_key": k("Absorption f"), "quote": "Absorption facilitée", "claim_type": "PROPRIETE_TECHNOLOGIQUE",
             "certainty": "MOYENNE", "replacement_action": "REFORMULER", "replacement_text": "Peptides de faible poids moléculaire."},
            {**base, "node_key": k("Notre vitami"), "quote": "est essentielle à vos défenses", "claim_type": "SANTE_FONCTIONNELLE",
             "substance_code": "VIT_C", "matched_claim_code": "VIT_C-IMMUN", "certainty": "HAUTE",
             "replacement_action": "REMPLACER_PAR_ALLEGATION", "replacement_claim_code": "VIT_C-IMMUN",
             "replacement_text": "La vitamine C renforce le système immunitaire."},            # libellé modifié -> officiel repris
            {**base, "node_key": k("Il booste la"), "quote": "Il booste la peau", "claim_type": "SANTE_FONCTIONNELLE",
             "certainty": "BASSE", "replacement_action": "REFORMULER", "replacement_text": "Il soigne la peau."},  # terme à risque
        ]
        return {"claims": claims}, LLMUsage(stage, "m", 1, 0, 1, 0.0)


def test_grid_levels():
    assert level_for("IA_ALLEGATION_SANS_CORRESPONDANCE", "ROUGE", "HAUTE") == "ROUGE"
    assert level_for("IA_ALLEGATION_SANS_CORRESPONDANCE", "ROUGE", "BASSE") == "ORANGE"
    assert level_for("IA_PROPRIETE_TECHNOLOGIQUE", "ROUGE") == "CONDITIONNEL"
    assert level_for("CONDITION_NON_REMPLIE", "ROUGE", "BASSE") == "ROUGE"          # règle déterministe : jamais abaissée


def test_ai_replacement_is_checked_by_rules():
    res = LivePipeline(KB, client=Fake()).analyze(CASE, HTML)
    by_quote = {f.quote: f for f in res.findings if f.code.startswith("IA_")}
    os_ = by_quote["aide à prévenir la perte osseuse"]
    assert os_.level == "ROUGE" and os_.replacement["status"] == "VALIDEE" and os_.replacement["claim_code"] == "VIT_C-COLL-OS"
    tech = by_quote["Absorption facilitée"]
    assert tech.code == "IA_PROPRIETE_TECHNOLOGIQUE" and tech.level == "CONDITIONNEL" and tech.replacement["status"] == "VALIDEE"
    imm = [f for f in res.findings if f.quote == "est essentielle à vos défenses"][0]
    assert imm.replacement["text"].startswith(KB.claims["VIT_C-IMMUN"]["text_fr"].rstrip(". "))   # libellé officiel imposé
    booste = by_quote["Il booste la peau"]
    assert booste.level == "ORANGE"                                                  # certitude basse : un cran en dessous
    assert booste.replacement["status"] == "REJETEE" and "soigne" in booste.replacement["reason"]


def test_webapp_shows_controlled_replacement_and_grid(monkeypatch):
    from fastapi.testclient import TestClient
    from webapp import main
    saved = (main.PIPE, main.AI_ENABLED)
    main.PIPE, main.AI_ENABLED = LivePipeline(main.KB, client=Fake()), True
    try:
        c = TestClient(main.app)
        t = re.search(r'name="csrf" value="([^"]+)"', c.get("/essai").text).group(1)
        c.post("/essai", data={"csrf": t, "name": "G", "email": "grille@exemple.fr", "password": "motdepasse-long", "terms": "1"})
        t = re.search(r'name="csrf" value="([^"]+)"', c.get("/app").text).group(1)
        html = c.post("/app/analyser", data={"csrf": t, "source": "texte", "text": HTML, "mode": "PRODUCT_PAGE",
                                            "sub_1": "VIT_C", "amount_1": "80", "unit_1": "mg"}).text
        assert "IA · contrôlée" in html and "fonction normale des os" in html
        assert "Proposition de l'IA écartée par les règles" in html
        assert 'href="/app/base/grille#S01"' in html
        # « prévenir » du lexique est rattaché au constat de l'IA : compté une fois
        assert html.count("« prévenir »") == 0 and "Terme du lexique en cause : prévenir" in html
        g = c.get("/app/base/grille").text
        assert "Grille de sévérité" in g and 'id="S04"' in g and 'name="target_type" value="grille"' in g
    finally:
        main.PIPE, main.AI_ENABLED = saved
