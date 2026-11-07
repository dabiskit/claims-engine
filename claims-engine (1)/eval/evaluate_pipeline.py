#!/usr/bin/env python3
"""
eval/evaluate_pipeline.py — Benchmark du pipeline sur le golden set et barrières de mise en production.

Usage
  python3 eval/evaluate_pipeline.py                         # auto-test du banc + benchmark (mode replay)
  python3 eval/evaluate_pipeline.py --self-test-only        # intégrité du banc uniquement
  python3 eval/evaluate_pipeline.py --mode live --adapter paquet.module:Classe --pricing eval/pricing.json

Codes de sortie : 0 barrières franchies · 1 barrière bloquante échouée · 2 banc non fiable (auto-test)
                  · 3 configuration invalide

Deux étages, dans cet ordre :
  1. AUTO-TEST : le banc doit prouver qu'il détecte des sorties corrompues (dose modifiée, attribut
     supprimé, verbe thérapeutique injecté…) et que l'assembleur rejette des sorties LLM hostiles
     (variante inventée, chiffre libre, zone gelée omise…). Un banc qui ne voit pas une corruption
     connue ne peut pas valider une release.
  2. BENCHMARK : métriques et barrières sur le golden set.

Unité de comptage
  Les constats du pipeline sont fusionnés en « segments signalés » : constats d'un même nœud dont les
  citations se recouvrent (l'interface surligne un seul passage). Les constats de niveau page (sans
  nœud) restent des segments distincts, appariés par code.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lxml import etree  # noqa: E402
from lxml import html as lhtml  # noqa: E402

from engine.frozen_zones import FrozenZoneViolation, assert_frozen_preserved, scan_frozen  # noqa: E402
from engine.textnorm import norm  # noqa: E402
from eval.pipeline_adapter import (AnalysisResult, Finding, KnowledgeBase, ReplayPipeline,  # noqa: E402
                                   RewriteResult, Usage, visible_text)

LEVEL_TO_SEV = {"ROUGE": "RED", "ORANGE": "ORANGE", "CONDITIONNEL": "CONDITIONAL", "INFO": "NONE"}
SEV_RANK = {"NONE": 0, "CONDITIONAL": 1, "ORANGE": 2, "RED": 3}

# nom -> (opérateur, seuil, bloquante)
GATES = {
    "critical_recall":             (">=", 0.95, True),
    "precision":                   (">=", 0.75, True),
    "false_red_on_true_negatives": ("==", 0, True),
    "frozen_zone_integrity":       ("==", 1.0, True),
    "dom_skeleton_preservation":   ("==", 1.0, True),
    "zero_residual_red":           ("==", 1.0, True),
    "rewrite_constraints":         ("==", 1.0, True),
    "seo_bridge_hit_rate":         (">=", 0.90, True),
    "avg_cost_eur_per_page":       ("<=", 0.25, True),
}
COST_ALERT_EUR = 0.20


# =============================================================================
# 1. Segments signalés et appariement
# =============================================================================
@dataclass
class PredSpan:
    node_key: str | None
    node_text: str
    quote: str
    severity: str
    codes: set = field(default_factory=set)
    legal_bases: set = field(default_factory=set)
    remediations: set = field(default_factory=set)


def merge_findings(findings: list[Finding]) -> list[PredSpan]:
    spans: list[PredSpan] = []
    for f in findings:
        sev = LEVEL_TO_SEV.get(f.level, "NONE")
        if sev not in ("RED", "ORANGE"):
            continue
        q = norm(f.quote)
        target = None
        if f.node_key is not None and q:
            for s in spans:
                sq = norm(s.quote)
                if s.node_key == f.node_key and sq and (q in sq or sq in q):
                    target = s
                    break
        if target is None:
            spans.append(PredSpan(f.node_key, f.node_text, f.quote, sev, {f.code}, {f.legal_basis},
                                  set(f.remediation_codes)))
            continue
        if len(q) > len(norm(target.quote)):
            target.quote = f.quote
        if SEV_RANK[sev] > SEV_RANK[target.severity]:
            target.severity = sev
        target.codes.add(f.code)
        target.legal_bases.add(f.legal_basis)
        target.remediations.update(f.remediation_codes)
    return spans


def span_matches(exp: dict, span: PredSpan) -> bool:
    if exp.get("finding_code"):
        return exp["finding_code"] in span.codes
    if span.node_key is None:
        return False
    if exp.get("node_startswith") and not norm(span.node_text).startswith(norm(exp["node_startswith"])):
        return False
    q, e = norm(span.quote), norm(exp["quote"])
    return bool(q) and bool(e) and (q in e or e in q) and e in norm(span.node_text)


def span_tolerated(span: PredSpan, rules: list[dict]) -> bool:
    for r in rules:
        if r.get("finding_code") and r["finding_code"] not in span.codes:
            continue
        if r.get("quote") and norm(r["quote"]) not in norm(span.quote) and norm(span.quote) not in norm(r["quote"]):
            continue
        return True
    return False


@dataclass
class DetectionScore:
    expected_red: int = 0
    tp_red: int = 0
    critical_fn: list = field(default_factory=list)
    expected_orange: int = 0
    tp_orange: int = 0
    orange_fn: list = field(default_factory=list)
    tp_spans: int = 0
    fp_spans: list = field(default_factory=list)
    tolerated_spans: int = 0
    false_red_true_negative: int = 0
    confusion: Counter = field(default_factory=Counter)
    by_stage: dict = field(default_factory=dict)          # couche -> [attendus, détectés au bon niveau]
    remediation_ok: bool = True


def score_detection(case: dict, analysis: AnalysisResult) -> DetectionScore:
    """Faux négatif critique : drapeau attendu RED qui n'est apparié à AUCUN segment RED. Un segment
    apparié mais classé ORANGE ou CONDITIONNEL compte comme faux négatif critique : l'interface ne
    bloquerait pas l'export, donc le client pourrait publier l'infraction."""
    gt = case["expected_ground_truth"]
    spans = merge_findings(analysis.findings)
    sc = DetectionScore()
    for exp in gt["expected_flags"]:
        matched = [s for s in spans if span_matches(exp, s)]
        best = max((s.severity for s in matched), key=lambda x: SEV_RANK[x], default="NONE")
        sc.confusion[(exp["severity"], best)] += 1
        stage = sc.by_stage.setdefault(exp["detectable_by"], [0, 0])
        stage[0] += 1
        entry = {"flag_id": exp["flag_id"], "quote": exp["quote"] or exp.get("finding_code"), "predicted": best,
                 "detectable_by": exp["detectable_by"]}
        if exp["severity"] == "RED":
            sc.expected_red += 1
            if best == "RED":
                sc.tp_red += 1
                stage[1] += 1
            else:
                sc.critical_fn.append(entry)
        else:
            sc.expected_orange += 1
            if SEV_RANK[best] >= SEV_RANK["ORANGE"]:
                sc.tp_orange += 1
                stage[1] += 1
            else:
                sc.orange_fn.append(entry)
    for s in spans:
        if any(span_matches(e, s) for e in gt["expected_flags"]):
            sc.tp_spans += 1
        elif span_tolerated(s, gt.get("tolerated_extra_flags", [])):
            sc.tolerated_spans += 1
        else:
            sc.fp_spans.append({"quote": s.quote, "severity": s.severity, "codes": sorted(s.codes)})
            sc.confusion[("NONE", s.severity)] += 1
            if not gt["expected_flags"] and s.severity == "RED":
                sc.false_red_true_negative += 1
    wanted = set(gt.get("expected_remediation_codes", []))
    sc.remediation_ok = wanted <= {r for f in analysis.findings for r in f.remediation_codes}
    return sc


# =============================================================================
# 2. Contrôles de sortie (indépendants du moteur quand c'est possible)
# =============================================================================
def dom_skeleton(fragment_html: str) -> list[tuple]:
    """Séquence (balise, attributs triés) des éléments et commentaires, dans l'ordre du document.
    Implémentation propre au banc : elle ne réutilise pas celle du moteur qu'elle contrôle."""
    wrapper = lhtml.fragment_fromstring(fragment_html, create_parent="div")
    out = []
    for el in wrapper.iter():
        if el is wrapper:
            continue
        if isinstance(el, etree._Comment):
            out.append(("C", el.text or ""))
        elif isinstance(el.tag, str):
            out.append(("E", el.tag.lower(), tuple(sorted(el.attrib.items()))))
    return out


def check_dom(input_html: str, output_html: str) -> tuple[bool, list[str]]:
    a, b = dom_skeleton(input_html), dom_skeleton(output_html)
    if a == b:
        return True, []
    diffs = [f"nombre de nœuds {len(a)} -> {len(b)}"] if len(a) != len(b) else []
    diffs += [f"#{i}: {x} -> {y}" for i, (x, y) in enumerate(zip(a, b)) if x != y][:5]
    return False, diffs


def strip_appended(text: str, appended: list[str]) -> str:
    for a in appended:
        text = text.replace(" " + a, "").replace(a, "")
    return text


def check_frozen(case: dict, input_html: str, output_html: str, appended: list[str],
                 kb: KnowledgeBase) -> tuple[bool, list[str]]:
    """(a) Chaînes de la vérité terrain : au caractère près, même nombre d'occurrences.
    (b) Toutes les valeurs extraites par le moteur (forme canonique) : rattrape les chiffres que
    l'annotateur n'a pas listés. Les mentions ajoutées depuis la base sont retirées au préalable."""
    before = visible_text(input_html)
    after = strip_appended(visible_text(output_html), appended).replace("*", "")
    violations = [f"« {s} » : {before.count(s)} en entrée, {after.count(s)} en sortie"
                  for s in case["expected_ground_truth"]["frozen_zones"] if before.count(s) != after.count(s)]
    names = case.get("product_names", [])
    try:
        assert_frozen_preserved(scan_frozen(before, product_names=names, extra_identifiers=kb.digit_aliases), after,
                                product_names=names, extra_identifiers=kb.digit_aliases)
    except FrozenZoneViolation as exc:
        violations.append(f"moteur : {exc}")
    return not violations, violations


@dataclass
class RewriteScore:
    applicable: bool
    frozen_ok: bool = True
    frozen_violations: list = field(default_factory=list)
    dom_ok: bool = True
    dom_diffs: list = field(default_factory=list)
    residual_red: int = 0
    must_not_hits: list = field(default_factory=list)
    zero_residual_ok: bool = True
    constraints_ok: bool = True
    constraint_failures: list = field(default_factory=list)
    bridge_applicable: bool = False
    bridge_ok: bool = True
    unexpected_claims: list = field(default_factory=list)
    target_statuses: list = field(default_factory=list)
    output_text: str = ""


def score_rewrite(case: dict, pipeline, kb: KnowledgeBase, analysis: AnalysisResult, rw: RewriteResult) -> RewriteScore:
    gt = case["expected_ground_truth"]
    cons = gt["expected_rewrite_constraints"]
    is_true_negative = not gt["expected_flags"]
    sc = RewriteScore(applicable=cons["rewrite_expected"] or is_true_negative)
    if not sc.applicable:
        return sc
    inp, out = case["input_html_nodes"], rw.output_html
    sc.output_text = visible_text(out)
    sc.target_statuses = [asdict(t) for t in rw.targets]
    sc.dom_ok, sc.dom_diffs = check_dom(inp, out)
    sc.frozen_ok, sc.frozen_violations = check_frozen(case, inp, out, rw.appended_notices, kb)

    residual = pipeline.analyze(case, out)
    sc.residual_red = sum(1 for s in merge_findings(residual.findings) if s.severity == "RED")
    text_wo_notices = norm(strip_appended(sc.output_text, rw.appended_notices))
    sc.must_not_hits = [t for t in cons["must_not_contain"] if norm(t) in text_wo_notices]
    sc.zero_residual_ok = sc.residual_red == 0 and not sc.must_not_hits

    fails = []
    if is_true_negative and out != inp:
        fails.append("Vrai négatif modifié par le pipeline")
    for t in rw.targets:
        if t.status != "VALIDEE":
            fails.append(f"Cible « {t.node_startswith} » : {t.status} {t.errors[:3]}")
    present = {r.claim_code for r in residual.recognized_claims}
    for cid in cons["expected_claim_ids"]:
        if cid not in kb.claims:
            fails.append(f"Allégation attendue {cid} absente de la base (KB_MISSING)")
        elif cid not in present:
            fails.append(f"Allégation attendue {cid} absente de la sortie")
    for code in cons["mandatory_notice_codes"]:
        if code not in kb.notices:
            fails.append(f"Mention {code} absente de la base (KB_MISSING)")
        elif norm(kb.notices[code])[:60] not in norm(sc.output_text):
            fails.append(f"Mention obligatoire {code} absente de la sortie")
    if not score_detection(case, analysis).remediation_ok:
        fails.append(f"Remédiations attendues non proposées : {gt['expected_remediation_codes']}")
    sc.constraints_ok = not fails
    sc.constraint_failures = fails
    sc.unexpected_claims = sorted(present - set(cons["expected_claim_ids"]) - set(cons["allowed_claim_ids"])
                                  - {r.claim_code for r in analysis.recognized_claims})

    bridge = cons["seo_bridge"]
    sc.bridge_applicable = bool(bridge["must_contain_any"] or bridge["must_contain_all"])
    if sc.bridge_applicable:
        any_ok = not bridge["must_contain_any"] or any(norm(t) in text_wo_notices for t in bridge["must_contain_any"])
        sc.bridge_ok = any_ok and all(norm(t) in text_wo_notices for t in bridge["must_contain_all"])
    return sc


# =============================================================================
# 3. Coûts
# =============================================================================
def usage_cost_eur(u: Usage, pricing: dict) -> Decimal | None:
    """Coût d'un appel. Prix manquant -> None (NON MESURÉ), jamais 0."""
    p = pricing.get("models", {}).get(u.model)
    keys = ("input_eur_per_mtok", "cached_input_eur_per_mtok", "output_eur_per_mtok")
    if not p or any(p.get(k) is None for k in keys):
        return None
    uncached = max(u.prompt_tokens - u.cached_tokens, 0)
    total = (Decimal(uncached) * Decimal(str(p["input_eur_per_mtok"]))
             + Decimal(u.cached_tokens) * Decimal(str(p["cached_input_eur_per_mtok"]))
             + Decimal(u.completion_tokens) * Decimal(str(p["output_eur_per_mtok"])))
    return total / Decimal(1_000_000)


# =============================================================================
# 4. Exécution
# =============================================================================
@dataclass
class CaseReport:
    test_id: str
    category: str
    detection: dict
    rewrite: dict
    kb_missing: list
    latency_s: float
    tokens: dict
    cost_eur: float | None


def ratio(a, b):
    return (a / b) if b else None


def run_benchmark(golden: dict, pipeline, kb: KnowledgeBase, pricing: dict) -> tuple[list[CaseReport], dict]:
    reports: list[CaseReport] = []
    agg, confusion = Counter(), Counter()
    by_stage: dict[str, list[int]] = {}
    costs: list[Decimal] = []
    unmeasured = 0
    for case in golden["cases"]:
        analysis = pipeline.analyze(case, case["input_html_nodes"])
        det = score_detection(case, analysis)
        rw = pipeline.rewrite(case, analysis)
        rws = score_rewrite(case, pipeline, kb, analysis, rw)
        usage = analysis.usage + rw.usage
        tokens = {k: sum(getattr(u, k) for u in usage) for k in ("prompt_tokens", "cached_tokens", "completion_tokens")}
        parts = [usage_cost_eur(u, pricing) for u in usage]
        page_cost = sum(parts) if usage and all(p is not None for p in parts) else None
        if page_cost is None:
            unmeasured += 1
        else:
            costs.append(page_cost)
        agg.update({"expected_red": det.expected_red, "tp_red": det.tp_red, "tp_spans": det.tp_spans,
                    "fp_spans": len(det.fp_spans), "false_red_tn": det.false_red_true_negative,
                    "expected_orange": det.expected_orange, "tp_orange": det.tp_orange})
        confusion.update(det.confusion)
        for st, (n, ok) in det.by_stage.items():
            s = by_stage.setdefault(st, [0, 0])
            s[0] += n
            s[1] += ok
        if rws.applicable:
            agg.update({"rw_cases": 1, "frozen_ok": int(rws.frozen_ok), "dom_ok": int(rws.dom_ok),
                        "residual_ok": int(rws.zero_residual_ok), "constraints_ok": int(rws.constraints_ok)})
            if rws.bridge_applicable:
                agg.update({"bridge_cases": 1, "bridge_ok": int(rws.bridge_ok)})
        reports.append(CaseReport(
            case["test_id"], case["category"],
            {"expected_red": det.expected_red, "tp_red": det.tp_red, "critical_fn": det.critical_fn,
             "expected_orange": det.expected_orange, "tp_orange": det.tp_orange, "orange_fn": det.orange_fn,
             "tp_spans": det.tp_spans, "fp_spans": det.fp_spans, "tolerated_spans": det.tolerated_spans,
             "remediation_ok": det.remediation_ok},
            asdict(rws), sorted(set(analysis.kb_missing_substances + case.get("requires_kb", []))),
            round(analysis.latency_s + rw.latency_s + sum(u.latency_s for u in usage), 4), tokens,
            float(page_cost) if page_cost is not None else None))

    metrics = {
        "critical_recall": ratio(agg["tp_red"], agg["expected_red"]),
        "precision": ratio(agg["tp_spans"], agg["tp_spans"] + agg["fp_spans"]),
        "false_red_on_true_negatives": agg["false_red_tn"],
        "orange_recall": ratio(agg["tp_orange"], agg["expected_orange"]),
        "frozen_zone_integrity": ratio(agg["frozen_ok"], agg["rw_cases"]),
        "dom_skeleton_preservation": ratio(agg["dom_ok"], agg["rw_cases"]),
        "zero_residual_red": ratio(agg["residual_ok"], agg["rw_cases"]),
        "rewrite_constraints": ratio(agg["constraints_ok"], agg["rw_cases"]),
        "seo_bridge_hit_rate": ratio(agg["bridge_ok"], agg["bridge_cases"]),
        "avg_cost_eur_per_page": float(sum(costs) / len(costs)) if costs and not unmeasured else None,
        "recall_by_stage": {k: {"attendus": n, "detectes": ok, "rappel": ratio(ok, n)}
                            for k, (n, ok) in sorted(by_stage.items())},
        "confusion_matrix": {f"{e}->{p}": n for (e, p), n in sorted(confusion.items())},
        "cost_measured_pages": len(costs), "cost_unmeasured_pages": unmeasured,
        "avg_latency_s": round(sum(r.latency_s for r in reports) / len(reports), 4),
    }
    return reports, metrics


def evaluate_gates(metrics: dict, mode: str) -> dict:
    out = {}
    for name, (op, threshold, blocking) in GATES.items():
        value = metrics.get(name)
        if value is None:
            if name == "avg_cost_eur_per_page" and mode == "replay":
                out[name] = {"status": "NON_MESURE", "value": None, "threshold": f"{op} {threshold}",
                             "blocking": False, "note": "Aucun appel LLM en mode replay : mesure obligatoire en mode live"}
            else:
                out[name] = {"status": "ECHEC", "value": None, "threshold": f"{op} {threshold}",
                             "blocking": blocking, "note": "Métrique non calculable"}
            continue
        ok = {">=": value >= threshold, "<=": value <= threshold, "==": value == threshold}[op]
        entry = {"status": "OK" if ok else "ECHEC", "value": value, "threshold": f"{op} {threshold}", "blocking": blocking}
        if name == "avg_cost_eur_per_page" and value > COST_ALERT_EUR:
            entry["alert"] = f"coût moyen > {COST_ALERT_EUR} € HT/page"
        out[name] = entry
    return out


# =============================================================================
# 5. Auto-test du banc
# =============================================================================
def self_test(golden: dict, kb: KnowledgeBase, replays: dict) -> dict:
    cases = {c["test_id"]: c for c in golden["cases"]}
    pipe = ReplayPipeline(kb, replays)
    results = []

    # (a) Le banc doit voir chaque corruption connue d'une sortie valide.
    outputs = {cid: pipe.rewrite(cases[cid], pipe.analyze(cases[cid], cases[cid]["input_html_nodes"]))
               for cid in ("GS-001", "GS-008", "GS-009")}
    baseline_ok = all(t.status == "VALIDEE" for rw in outputs.values() for t in rw.targets)
    results.append({"type": "sortie_de_reference", "case": "GS-001/008/009",
                    "label": "les sorties rejouées de référence sont acceptées", "detected": baseline_ok})
    mutations = [
        ("GS-001", "dose modifiée 1,9 mg -> 1,8 mg", "frozen", lambda h: h.replace("1,9 mg", "1,8 mg", 1)),
        ("GS-001", "moment de prise supprimé", "frozen", lambda h: h.replace(" 30 minutes avant le coucher", "")),
        ("GS-001", "verbe thérapeutique injecté", "residual",
         lambda h: h.replace("contribue à réduire le temps d'endormissement", "soigne l'insomnie")),
        ("GS-009", "attribut data-variant-id supprimé", "dom", lambda h: h.replace(' data-variant-id="409281"', "")),
        ("GS-009", "remise -25% devenue -30%", "frozen", lambda h: h.replace("-25%", "-30%")),
        ("GS-009", "balise <em> ajoutée", "dom", lambda h: h.replace("Flacon de 30 ml", "<em>Flacon</em> de 30 ml")),
        ("GS-008", "lien produit retiré", "dom",
         lambda h: h.replace('<a href="/products/complexe-harpagophytum">', "").replace("</a>", "")),
        ("GS-008", "prix inventé dans le texte", "frozen", lambda h: h.replace("misez sur", "misez dès 19 € sur")),
    ]
    for cid, label, check, mutate in mutations:
        case, rw = cases[cid], outputs[cid]
        mutated = mutate(rw.output_html)
        if mutated == rw.output_html:
            results.append({"type": "mutation_sortie", "case": cid, "label": label + " (mutation sans effet)", "detected": False})
            continue
        sc = score_rewrite(case, pipe, kb, pipe.analyze(case, case["input_html_nodes"]),
                           RewriteResult(mutated, rw.targets, rw.used_claims, rw.appended_notices, 0.0))
        detected = {"frozen": not sc.frozen_ok, "dom": not sc.dom_ok, "residual": not sc.zero_residual_ok}[check]
        results.append({"type": "mutation_sortie", "case": cid, "label": label, "detected": detected})

    # (b) L'assembleur doit rejeter les sorties LLM hostiles (rien ne doit être appliqué).
    def attacked(cid: str, transform) -> list:
        targets = json.loads(json.dumps(replays["cases"][cid]["targets"]))
        targets[0]["llm_output"]["spans"] = transform(targets[0]["llm_output"]["spans"])
        return targets

    def swap_text(old: str, new: str):
        return lambda spans: [({**s, "text": new} if s.get("text") == old else s) for s in spans]

    attacks = [
        ("GS-001", "variante inventée",
         lambda sp: [({**s, "ref": "MELATONINE-ENDORM-V99"} if s["kind"] == "ALLEGATION" else s) for s in sp]),
        ("GS-001", "verbe d'effet dans le texte libre",
         swap_text(" grâce à notre formule ", " qui aide à mieux dormir grâce à notre formule ")),
        ("GS-001", "chiffre dans le texte libre", swap_text(" grâce à notre formule ", " dès 1 mg grâce à notre formule ")),
        ("GS-001", "terme interdit réintroduit", swap_text(" grâce à notre formule ", " contre l'insomnie grâce à notre formule ")),
        ("GS-001", "substance absente du produit", swap_text(" grâce à notre formule ", " avec du magnésium grâce à notre formule ")),
        ("GS-001", "balise inline supprimée", lambda sp: [s for s in sp if s.get("ref") != "M2"]),
        ("GS-002", "zone gelée remplacée par du texte libre",
         lambda sp: [{"kind": "TEXTE_LIBRE", "text": "Magnésium Marin"} if s["kind"] == "ZONE_GELEE" else s for s in sp]),
        ("GS-002", "allégation dont la condition n'est pas remplie",
         lambda sp: sp + [{"kind": "ALLEGATION", "ref": "MG-FATIGUE-V00"}]),
        ("GS-003", "thème sans allégation d'ancrage",
         lambda sp: [{"kind": "TEXTE_LIBRE", "text": "Votre allié vitalité au quotidien"}]),
    ]
    for cid, label, transform in attacks:
        pipe_a = ReplayPipeline(kb, replays, overrides={cid: attacked(cid, transform)})
        case = cases[cid]
        rw = pipe_a.rewrite(case, pipe_a.analyze(case, case["input_html_nodes"]))
        rejected = any(t.status != "VALIDEE" for t in rw.targets) and rw.output_html == case["input_html_nodes"]
        results.append({"type": "attaque_reecrivain", "case": cid, "label": label, "detected": rejected,
                        "errors": [e for t in rw.targets for e in t.errors][:2]})

    # (c) Formule de coût, sur des prix FICTIFS : seule l'arithmétique est testée.
    fake = {"models": {"modele-fictif": {"input_eur_per_mtok": 3, "cached_input_eur_per_mtok": 0.3, "output_eur_per_mtok": 15}}}
    results.append({"type": "formule_cout", "case": "-", "label": "(4000×3 + 6000×0,3 + 2000×15) / 1e6 = 0,0438 €",
                    "detected": usage_cost_eur(Usage("t", "modele-fictif", 10_000, 6_000, 2_000, 0.0), fake) == Decimal("0.0438")})
    results.append({"type": "formule_cout", "case": "-", "label": "prix inconnu -> coût NON MESURÉ (jamais 0)",
                    "detected": usage_cost_eur(Usage("t", "modele-inconnu", 1, 0, 1, 0.0), fake) is None})
    return {"passed": all(r["detected"] for r in results), "checks": results}


# =============================================================================
# 6. Rapports
# =============================================================================
def md_table(headers: list[str], rows: list[list]) -> str:
    cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]

    def line(r):
        return "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths)) + " |"
    return "\n".join([line(cells[0]), "|" + "|".join("-" * (w + 2) for w in widths) + "|"] + [line(r) for r in cells[1:]])


def fmt(v) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:.1f} %" if isinstance(v, float) else str(v)


def print_self_test(st: dict) -> None:
    ok = sum(c["detected"] for c in st["checks"])
    print(f"\n## Auto-test du banc : {'OK' if st['passed'] else 'ÉCHEC'} ({ok}/{len(st['checks'])})\n")
    print(md_table(["Type", "Cas", "Contrôle", "Détecté"],
                   [[c["type"], c["case"], c["label"], "oui" if c["detected"] else "NON"] for c in st["checks"]]))


def print_report(reports: list[CaseReport], metrics: dict, gates: dict, meta: dict) -> None:
    print(f"\n# Benchmark — {meta['pipeline']} — base {meta['kb_release']} — {meta['timestamp']}")
    if not meta["golden_expert_validated"]:
        print("\n> Golden set NON validé par l'expert : résultats indicatifs.")
    print("\n## Cas\n")
    rows = []
    for r in reports:
        rw = r.rewrite
        cell = lambda k: ("ok" if rw[k] else "ÉCHEC") if rw["applicable"] else "-"  # noqa: E731
        rows.append([r.test_id, r.category[:36], f"{r.detection['tp_red']}/{r.detection['expected_red']}",
                     f"{r.detection['tp_orange']}/{r.detection['expected_orange']}", len(r.detection["fp_spans"]),
                     cell("frozen_ok"), cell("dom_ok"), cell("zero_residual_ok"), cell("constraints_ok"),
                     ("ok" if rw["bridge_ok"] else "ÉCHEC") if rw["bridge_applicable"] else "-",
                     ", ".join(r.kb_missing) or "-"])
    print(md_table(["Cas", "Catégorie", "Rouges", "Oranges", "FP", "Gelées", "DOM", "Résiduel", "Contraintes",
                    "Pont SEO", "Base manquante"], rows))
    print("\n## Barrières\n")
    print(md_table(["Métrique", "Valeur", "Seuil", "Statut", "Bloquante"],
                   [[k, fmt(v["value"]) if k != "avg_cost_eur_per_page" else (v["value"] if v["value"] is not None else "n/a"),
                     v["threshold"], v["status"] + (f" ({v['alert']})" if v.get("alert") else ""),
                     "oui" if v["blocking"] else "non"] for k, v in gates.items()]))
    print("\n## Rappel par couche de détection\n")
    print(md_table(["Couche", "Attendus", "Détectés au bon niveau", "Rappel"],
                   [[k, v["attendus"], v["detectes"], fmt(v["rappel"])] for k, v in metrics["recall_by_stage"].items()]))
    print("\n## Matrice de confusion (attendu -> prédit)\n")
    print(md_table(["Attendu -> prédit", "Nombre"], [[k, v] for k, v in metrics["confusion_matrix"].items()]))
    fns = [(r.test_id, f) for r in reports for f in r.detection["critical_fn"]]
    if fns:
        print("\n## Faux négatifs critiques\n")
        print(md_table(["Cas", "Drapeau", "Citation", "Prédit", "Couche attendue"],
                       [[cid, f["flag_id"], f["quote"], f["predicted"], f["detectable_by"]] for cid, f in fns]))
    fps = [(r.test_id, f) for r in reports for f in r.detection["fp_spans"]]
    if fps:
        print("\n## Faux positifs\n")
        print(md_table(["Cas", "Citation", "Gravité", "Codes"], [[cid, f["quote"], f["severity"], ", ".join(f["codes"])] for cid, f in fps]))
    fails = [(r.test_id, x) for r in reports for x in r.rewrite.get("constraint_failures", []) + r.rewrite.get("frozen_violations", []) + r.rewrite.get("dom_diffs", [])]
    if fails:
        print("\n## Échecs de réécriture\n")
        print(md_table(["Cas", "Détail"], [[cid, x[:160]] for cid, x in fails]))
    print(f"\nCoût : {metrics['cost_measured_pages']} page(s) mesurée(s), {metrics['cost_unmeasured_pages']} non mesurée(s) · "
          f"latence moyenne {metrics['avg_latency_s']} s/page")
    blocking_failed = [k for k, v in gates.items() if v["blocking"] and v["status"] != "OK"]
    print(f"\n## Verdict : {'RELEASE AUTORISÉE' if not blocking_failed else 'RELEASE BLOQUÉE — ' + ', '.join(blocking_failed)}\n")


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fingerprint(paths: list[Path]) -> str | None:
    if not paths:
        return None
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# =============================================================================
# 7. Point d'entrée
# =============================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", type=Path, default=ROOT / "eval" / "golden_set.json")
    ap.add_argument("--kb", type=Path, default=ROOT / "seed" / "out" / "lot1.json")
    ap.add_argument("--replays", type=Path, default=ROOT / "eval" / "replays.json")
    ap.add_argument("--pricing", type=Path, default=ROOT / "eval" / "pricing.example.json")
    ap.add_argument("--mode", choices=("replay", "live"), default="replay")
    ap.add_argument("--adapter", help="module:Classe d'un adaptateur live (protocole PipelineAdapter)")
    ap.add_argument("--report", type=Path, default=ROOT / "eval" / "out" / "benchmark_report.json")
    ap.add_argument("--self-test-only", action="store_true")
    ap.add_argument("--skip-self-test", action="store_true")
    args = ap.parse_args(argv)

    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    kb = KnowledgeBase(json.loads(args.kb.read_text(encoding="utf-8")))
    replays = json.loads(args.replays.read_text(encoding="utf-8"))
    pricing = json.loads(args.pricing.read_text(encoding="utf-8")) if args.pricing.exists() else {"models": {}}

    if args.mode == "live":
        if not args.adapter:
            print("Mode live : --adapter module:Classe requis (aucun adaptateur live tant que les prompts "
                  "système ne sont pas livrés).", file=sys.stderr)
            return 3
        module, cls = args.adapter.split(":")
        pipeline = getattr(importlib.import_module(module), cls)(kb)
    else:
        pipeline = ReplayPipeline(kb, replays)

    st = None
    if not args.skip_self_test:
        st = self_test(golden, kb, replays)
        print_self_test(st)
        if not st["passed"]:
            print("\nBanc non fiable : au moins une corruption connue n'est pas détectée. Benchmark annulé.",
                  file=sys.stderr)
            return 2
        if args.self_test_only:
            return 0

    reports, metrics = run_benchmark(golden, pipeline, kb, pricing)
    gates = evaluate_gates(metrics, args.mode)
    meta = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": args.mode,
            "pipeline": pipeline.name, "kb_release": kb.release, "git_commit": git_commit(),
            "golden_sha256": sha256_file(args.golden), "kb_sha256": sha256_file(args.kb),
            "replays_sha256": sha256_file(args.replays) if args.mode == "replay" else None,
            "engine_fingerprint": fingerprint(list((ROOT / "engine").glob("*.py"))),
            "prompts_fingerprint": fingerprint([p for p in (ROOT / "prompts").glob("*") if p.is_file()]),
            "cases": len(golden["cases"]),
            "golden_expert_validated": all(c["annotation"]["expert_validated"] for c in golden["cases"])}
    print_report(reports, metrics, gates, meta)
    blocking_failed = [k for k, v in gates.items() if v["blocking"] and v["status"] != "OK"]
    report = {"meta": meta, "self_test": st, "metrics": metrics, "gates": gates,
              "verdict": "RELEASE_AUTORISEE" if not blocking_failed else "RELEASE_BLOQUEE",
              "blocking_failures": blocking_failed, "cases": [asdict(r) for r in reports]}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Rapport : {args.report}")
    return 0 if not blocking_failed else 1


if __name__ == "__main__":
    sys.exit(main())
