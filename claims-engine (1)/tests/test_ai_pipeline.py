"""tests/test_ai_pipeline.py — couche IA avec un faux client (aucun appel réseau) : extraction, rejet
des citations inventées, repli sans IA, nouvelles tentatives de réécriture, parcours complet dans l'app."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(tempfile.mkdtemp()) / "ai.db")
os.environ["ADMIN_EMAILS"] = "admin@exemple.fr"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["SYNC_JOBS"] = "1"

from engine.ai import LLMUsage  # noqa: E402
from eval import evaluate_pipeline as ev  # noqa: E402
from eval.pipeline_adapter import KnowledgeBase, LivePipeline  # noqa: E402

SEED = json.loads((ROOT / "seed" / "out" / "lot1.json").read_text(encoding="utf-8"))
GOLDEN = json.loads((ROOT / "eval" / "golden_set.json").read_text(encoding="utf-8"))
REPLAYS = json.loads((ROOT / "eval" / "replays.json").read_text(encoding="utf-8"))
KB = KnowledgeBase(SEED)
CASES = {c["test_id"]: c for c in GOLDEN["cases"]}

RULES = [  # (fragment présent dans le segment, type, substance, code apparié, fidèle)
    ("régénère les cartilages usés", "SANTE_FONCTIONNELLE", None, "AUCUNE", False),
    ("comble les rides profondes", "SANTE_FONCTIONNELLE", None, "AUCUNE", False),
    ("Soigne l'insomnie chronique", "PROPRIETE_THERAPEUTIQUE_INTERDITE", None, "AUCUNE", False),
    ("réduit le cortisol de 40 %", "SANTE_FONCTIONNELLE", None, "AUCUNE", False),
    ("Booste votre énergie", "SANTE_FONCTIONNELLE", "VIT_B12", "VIT_B12-ENERGIE", False),
    ("fait chuter le cortisol", "SANTE_FONCTIONNELLE", "WITHANIA_SOMNIFERA", "AUCUNE", False),
    ("soigne l'insomnie", "PROPRIETE_THERAPEUTIQUE_INTERDITE", None, "AUCUNE", False),
]


class FakeClient:
    def __init__(self, fail_first_rewrite=False, bad_code=False):
        self.fail_first_rewrite, self.bad_code, self.calls, self.seen = fail_first_rewrite, bad_code, [], set()

    def call_tool(self, *, stage, model, system, user_content, tool_name, tool_description, input_schema, max_tokens=0):
        self.calls.append(stage)
        usage = LLMUsage(stage, "modele-fictif", 1000, 0, 200, 0.01)
        data = json.loads(user_content)
        if stage == "extracteur":
            allowed_codes = set(input_schema["properties"]["claims"]["items"]["properties"]["matched_claim_code"]["enum"])
            allowed_subs = set(input_schema["properties"]["claims"]["items"]["properties"]["substance_code"]["enum"])
            claims = [{"node_key": data["nodes"][0]["key"], "quote": "texte qui n'existe pas", "claim_type": "SANTE_FONCTIONNELLE",
                       "substance_code": None, "matched_claim_code": "INVENTE" if self.bad_code else "AUCUNE",
                       "wording_faithful": False, "rationale": "citation inventée (test)"}]
            for n in data["nodes"]:
                for frag, ctype, sub, code, faithful in RULES:
                    if frag in n["text"]:
                        claims.append({"node_key": n["key"], "quote": frag, "claim_type": ctype,
                                       "substance_code": sub if sub in allowed_subs else None,
                                       "matched_claim_code": code if code in allowed_codes else "AUCUNE",
                                       "wording_faithful": faithful, "rationale": "règle de test"})
            return {"claims": claims}, usage
        segment = data["segment"]
        key = segment[:40]
        if self.fail_first_rewrite and key not in self.seen:
            self.seen.add(key)
            return {"status": "OK", "notes_fr": "mauvaise sortie", "spans": [{"kind": "BALISE_OUVRANTE", "ref": "M1"},
                    {"kind": "TEXTE_LIBRE", "text": "Dès 1 mg, ça soigne"}, {"kind": "BALISE_FERMANTE", "ref": "M1"}]}, usage
        for cid, rec in REPLAYS["cases"].items():
            for t in rec["targets"]:
                if segment.replace("⟦M1⟧", "").lower().startswith(t["node_startswith"].lower()[:12]):
                    return {"status": "OK", "notes_fr": "replay", "spans": t["llm_output"]["spans"]}, usage
        return {"status": "IMPOSSIBLE_SANS_INVENTER", "notes_fr": "aucun script de test", "spans": []}, usage


def test_extractor_findings_and_dropped_quote():
    case = CASES["GS-004"]
    res = LivePipeline(KB, client=FakeClient()).analyze(case, case["input_html_nodes"])
    reds = [f for f in res.findings if f.code == "IA_ALLEGATION_SANS_CORRESPONDANCE" and f.level == "ROUGE"]
    assert {f.quote for f in reds} == {"régénère les cartilages usés", "comble les rides profondes"}
    assert any(f.code == "IA_CITATIONS_REJETEES" for f in res.findings)
    assert res.usage and res.usage[0].stage == "extracteur"


def test_schema_violation_fails_safe():
    case = CASES["GS-001"]
    res = LivePipeline(KB, client=FakeClient(bad_code=True)).analyze(case, case["input_html_nodes"])
    assert any(f.code == "IA_INDISPONIBLE" for f in res.findings)
    assert any(f.code == "LEX-SOIGNER" for f in res.findings)          # le déterministe reste là


def test_rewrite_retries_until_valid():
    case = CASES["GS-001"]
    pipe = LivePipeline(KB, client=FakeClient(fail_first_rewrite=True))
    res = pipe.analyze(case, case["input_html_nodes"])
    rw = pipe.rewrite(case, res)
    assert rw.targets[0].status == "VALIDEE", rw.targets[0].errors
    assert [u.stage for u in rw.usage] == ["reecrivain", "reecrivain"]
    assert "endormissement" in rw.output_html and "peu de temps avant le coucher" in rw.output_html


def test_benchmark_live_smoke():
    reports, metrics = ev.run_benchmark(GOLDEN, LivePipeline(KB, client=FakeClient()), KB, {"models": {}})
    gates = ev.evaluate_gates(metrics, "live")
    assert 0 < metrics["critical_recall"] <= 1 and metrics["frozen_zone_integrity"] == 1.0
    assert gates["avg_cost_eur_per_page"]["status"] == "ECHEC"   # en live, un coût non mesuré bloque


def test_webapp_ai_flow():
    from fastapi.testclient import TestClient
    from webapp import main
    saved = (main.PIPE, main.AI_ENABLED)
    main.PIPE, main.AI_ENABLED = LivePipeline(KB, client=FakeClient()), True
    try:
        _ai_flow(TestClient(main.app), main)
    finally:
        main.PIPE, main.AI_ENABLED = saved


def _ai_flow(c, main):
    t = re.search(r'name="csrf" value="([^"]+)"', c.get("/essai").text).group(1)
    c.post("/essai", data={"csrf": t, "name": "Test", "email": "ia@exemple.fr", "password": "motdepasse-long", "terms": "1"})
    page = c.get("/app?exemple=GS-001").text
    t = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post("/app/analyser", data={"csrf": t, "source": "texte", "text": main.EXAMPLES[0]["text"], "mode": "PRODUCT_PAGE",
               "product_name": "Mélatonine Pure", "sub_1": "MELATONINE", "amount_1": "1,9", "unit_1": "mg"})
    aid = int(r.url.path.rsplit("/", 1)[1])
    assert 'class="tag info">IA' in r.text and "Proposer des réécritures" in r.text
    r = c.post(f"/app/analyses/{aid}/reecrire", data={"csrf": t})
    assert "endormissement" in r.text and "Proposition contrôlée" in r.text
    pid = int(re.search(r'id="p(\d+)"', r.text).group(1))
    r = c.post(f"/app/propositions/{pid}/decision", data={"csrf": t, "decision": "MODIFIEE", "final_text": "Ce produit soigne l'insomnie."})
    assert "Version non enregistrée" in r.text
    r = c.post(f"/app/propositions/{pid}/decision", data={"csrf": t, "decision": "MAINTENUE", "reason": "court"})
    assert "indiquez un motif" in r.text
    r = c.post(f"/app/propositions/{pid}/decision", data={"csrf": t, "decision": "ACCEPTEE"})
    assert "Proposition acceptée" in r.text and "0</strong> passage(s)" in r.text
    r = c.get(f"/app/analyses/{aid}/rapport")
    assert "Tous les passages prioritaires ont reçu une décision" in r.text and "Proposition acceptée" in r.text
    assert c.get("/tarifs").status_code == 200
