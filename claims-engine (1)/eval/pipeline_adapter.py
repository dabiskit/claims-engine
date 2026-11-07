"""
eval/pipeline_adapter.py — Ce que l'évaluateur exécute.

ReplayPipeline = pipeline réel, sauf les appels LLM :
  analyse   : extraction DOM, routeur produit/article, lexique, reconnaissance des allégations par
              correspondance de variantes, conditions (doses, mentions), art. 10§3, vigilance
              -> tout le déterministe du moteur, sans l'extracteur LLM (non livré)
  réécriture: sorties du réécrivain REJOUÉES depuis eval/replays.json, puis passées dans le VRAI
              assembleur (engine/rewrite_assembler.py) et le VRAI patch DOM (patch_fragment)

Un adaptateur « live » doit implémenter le même protocole (PipelineAdapter) avec de vrais appels
LLM et renseigner Usage ; il dépend des prompts système, pas encore livrés.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from engine.conditions import ClaimUsability, Condition, Ingredient, Product, evaluate_claim
from engine.dom_extractor import NOT_ANALYZED_ZONES, extract_page, fragment_nodes, patch_fragment, plain_from_marked
from engine.frozen_zones import FrozenKind, scan_frozen
from engine.lexicon import LexiconIndex
from engine.mode_router import Catalog, Mode, ModeDecision, lexical_hits, route_page
from engine.rewrite_assembler import UnsupportedSegment, assemble_and_validate, build_reference_text
from engine.textnorm import norm, norm_with_offsets, original_span

POPULATION_KEYWORDS = ("enceinte", "allait", "enfant")


@dataclass
class Usage:
    stage: str
    model: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    latency_s: float


@dataclass
class Finding:
    code: str
    level: str                    # ROUGE | ORANGE | CONDITIONNEL | INFO
    node_key: str | None          # None : constat de niveau page
    node_text: str
    quote: str
    legal_basis: str | None
    substance: str | None = None
    lexical_code: str | None = None
    remediation_codes: tuple[str, ...] = ()


@dataclass
class RecognizedClaim:
    claim_code: str
    variant_code: str
    node_key: str
    start: int
    end: int
    usability: str


@dataclass
class AnalysisResult:
    findings: list[Finding]
    recognized_claims: list[RecognizedClaim]
    kb_missing_substances: list[str]
    latency_s: float
    usage: list[Usage] = field(default_factory=list)


@dataclass
class TargetOutcome:
    node_startswith: str
    status: str                   # VALIDEE | REJETEE_VALIDATEUR | IMPOSSIBLE | SANS_ENREGISTREMENT | NOEUD_INTROUVABLE | ERREUR_IA
    errors: list[str]
    used_claims: list[str]
    node_key: str | None = None
    new_marked: str | None = None
    before_plain: str | None = None
    after_plain: str | None = None
    notices: list[str] = field(default_factory=list)


@dataclass
class RewriteResult:
    output_html: str
    targets: list[TargetOutcome]
    used_claims: list[str]
    appended_notices: list[str]
    latency_s: float
    usage: list[Usage] = field(default_factory=list)


class PipelineAdapter(Protocol):
    name: str

    def analyze(self, case: dict, html: str) -> AnalysisResult: ...

    def rewrite(self, case: dict, analysis: AnalysisResult) -> RewriteResult: ...


# ---------------------------------------------------------------------------
# Base de connaissances (seed JSON)
# ---------------------------------------------------------------------------
class KnowledgeBase:
    def __init__(self, seed: dict):
        self.release = seed["release"]["label"]
        entries = seed["lexical_entries"]
        self.compiled = {
            "lexicon": [{"code": e["code"], "category": e["category"], "legal_basis": e["legal_basis"],
                         "forms": e["surface_forms"], "pattern": e["pattern_regex"],
                         "severity": {"PRODUIT": e["severity_product"],
                                      "ARTICLE_AVEC_PONT": e["severity_article_with_bridge"],
                                      "ARTICLE_SANS_PONT": e["severity_article_without_bridge"]},
                         "topic": e["topic_key"], "neutralizers": e["neutralizing_left_context"]} for e in entries],
            "substance_aliases": [{"substance": s["code"], "norm": norm(a["alias"]), "ambiguous": a["is_ambiguous"]}
                                  for s in seed["substances"] for a in s["aliases"]]}
        self.lexicon = LexiconIndex.from_compiled_rules(self.compiled)
        self.substances = {s["code"]: s for s in seed["substances"]}
        self.claims = {c["code"]: c for c in seed["regulatory_claims"]}
        self.notices = {n["code"]: n["text_fr"] for n in seed["notices"]}
        self.conditions: dict[str, list[Condition]] = {}
        for c in seed["claim_conditions"]:
            self.conditions.setdefault(c["claim"], []).append(Condition(
                c["code"], c["claim"], c["type"], c["params"], c.get("notice_code"),
                self.notices.get(c.get("notice_code") or "")))
        self.variants: dict[str, list[dict]] = {}
        for v in seed["claim_variants"]:
            self.variants.setdefault(v["claim"], []).append(v)
        self.variant_norms = sorted(((norm(v["text_fr"]), v["code"], v["claim"]) for v in seed["claim_variants"]),
                                    key=lambda x: -len(x[0]))
        self.restrictions: dict[str, list[dict]] = {}
        for r in seed["population_restrictions"]:
            self.restrictions.setdefault(r["substance"], []).append(r)
        self.topic_anchors: dict[str, set[str]] = {}
        self.bridged_lexical_codes: set[str] = set()
        self.bridges = seed["seo_semantic_bridges"]
        for b in seed["seo_semantic_bridges"]:
            self.topic_anchors.setdefault(b["topic_key"], set()).update(b["anchor_claims"])
            for term in b["illegal_query_terms"]:
                self.bridged_lexical_codes.update(h.entry.code for h in self.lexicon.scan(term, exclude_control=True))
        self.digit_aliases = sorted({a["norm"] for a in self.compiled["substance_aliases"] if any(ch.isdigit() for ch in a["norm"])})

    def names_of(self, substance: str) -> list[str]:
        s = self.substances[substance]
        return [s["name_fr"], *(a["alias"] for a in s["aliases"])]

    def nrv(self) -> dict:
        return {k: (v["nrv_value"], v["nrv_unit"]) for k, v in self.substances.items() if v["nrv_value"]}


def product_of(case: dict, kb: KnowledgeBase, composition: list[dict] | None = None) -> Product:
    items = case["product_composition"] if composition is None else composition
    ingredients = tuple(Ingredient(
        substance_code=i["substance"] if i["substance"] in kb.substances else None, raw_label=i["label"],
        plant_part=i.get("plant_part"), preparation=i.get("preparation"),
        amount=Decimal(str(i["amount"])) if i.get("amount") is not None else None, unit=i.get("unit"),
        der_ratio=Decimal(str(i["der_ratio"])) if i.get("der_ratio") else None) for i in items)
    return Product(ingredients, Decimal(str(case.get("portions_per_day") or 1)), kb.nrv())


def effective_composition(case: dict) -> list[dict]:
    """Fiche produit : sa composition. Article : composition des produits liés."""
    if case["page_mode"] == "PRODUCT_PAGE":
        return case["product_composition"]
    return [i for p in case.get("linked_products", []) for i in p["composition"]]


def node_texts(html: str) -> list[tuple[str, str, str, dict]]:
    """(clé, texte marqué, texte brut, marqueurs) des nœuds d'un fragment."""
    return [(k, m, plain_from_marked(m, mk), mk) for k, m, mk in fragment_nodes(html)]


def visible_text(html: str) -> str:
    return "\n".join(plain for _, _, plain, _ in node_texts(html))


# ---------------------------------------------------------------------------
# Pipeline rejoué
# ---------------------------------------------------------------------------
class ReplayPipeline:
    name = "deterministe+replay"

    def __init__(self, kb: KnowledgeBase, replays: dict, overrides: dict | None = None):
        self.kb = kb
        self.replays = replays["cases"]
        self.overrides = overrides or {}

    # ---------------------------------------------------------------- analyse
    def analyze(self, case: dict, html: str) -> AnalysisResult:
        t0 = time.perf_counter()
        kb = self.kb
        mode = Mode.PRODUIT if case["page_mode"] == "PRODUCT_PAGE" else Mode.ARTICLE
        # Page complète (URL) : analysée telle quelle ; fragment (texte collé) : enveloppé.
        is_document = html.lstrip()[:200].lower().startswith(("<!doctype", "<html"))
        page = extract_page(html if is_document else
                            f"<html><head><title></title></head><body><main>{html}</main></body></html>",
                            case["page_url"])
        composition = effective_composition(case)
        product = product_of(case, kb, composition)
        in_kb = sorted({i["substance"] for i in composition if i["substance"] in kb.substances})
        missing = sorted({i["substance"] for i in composition if i["substance"] not in kb.substances})
        domain = case["page_url"].split("/")[2]
        catalog = Catalog(domain, {p["path"].rstrip("/") for p in case.get("linked_products", [])},
                          {norm(n) for n in case.get("product_names", [])},
                          {norm(a): s for s in in_kb for a in kb.names_of(s)})
        texts = {n.key: n.text_plain for n in page.nodes}
        findings: list[Finding] = []

        for f in route_page(page, ModeDecision(mode, 0, ("golden",)), kb.lexicon, catalog,
                            seo_bridged_lexical_codes=frozenset(kb.bridged_lexical_codes)):
            key = f.node_keys[0] if f.node_keys else None
            findings.append(Finding(f.code if f.code != "LEXIQUE" else (f.lexical_code or "LEXIQUE"), f.level, key,
                                    texts.get(key, ""), f.quote, f.legal_basis, lexical_code=f.lexical_code,
                                    remediation_codes=tuple(r.code for r in f.remediations)))

        recognized: list[RecognizedClaim] = []
        for n in page.nodes:
            normalized, offsets = norm_with_offsets(n.text_plain)
            taken: list[tuple[int, int]] = []
            for vnorm, vcode, ccode in kb.variant_norms:
                start = normalized.find(vnorm)
                while start != -1:
                    end = start + len(vnorm)
                    if all(end <= a or start >= b for a, b in taken):
                        taken.append((start, end))
                        os_, oe = original_span(offsets, start, end)
                        claim = kb.claims[ccode]
                        ev = evaluate_claim(ccode, claim["substance"], kb.conditions.get(ccode, []), product) \
                            if claim["substance"] in in_kb else None
                        usability = ev.usability.value if ev else "SUBSTANCE_ABSENTE"
                        recognized.append(RecognizedClaim(ccode, vcode, n.key, os_, oe, usability))
                        quote = n.text_plain[os_:oe]
                        if ev is None:
                            findings.append(Finding("ALLEGATION_SUBSTANCE_ABSENTE", "ROUGE", n.key, n.text_plain, quote,
                                                    "R1924_2006_ART10_1", claim["substance"]))
                        elif ev.usability is ClaimUsability.INUTILISABLE:
                            findings.append(Finding("CONDITION_NON_REMPLIE", "ROUGE", n.key, n.text_plain, quote,
                                                    "R432_2012_CONDITIONS", claim["substance"],
                                                    remediation_codes=("SUPPRIMER_ALLEGATION",)))
                        elif ev.usability is ClaimUsability.CONDITIONNELLE:
                            findings.append(Finding("CONDITION_A_CONFIRMER", "CONDITIONNEL", n.key, n.text_plain, quote,
                                                    "R432_2012_CONDITIONS", claim["substance"]))
                        if ev is not None:
                            page_norm = norm("\n".join(texts.values()))
                            for code, text in ev.required_notices:
                                if norm(text)[:60] not in page_norm:
                                    findings.append(Finding("MENTION_OBLIGATOIRE_ABSENTE", "ROUGE", n.key, n.text_plain,
                                                            quote, "R432_2012_CONDITIONS", claim["substance"]))
                    start = normalized.find(vnorm, start + 1)

        if mode is Mode.PRODUIT:
            anchored = {t for t, claims in kb.topic_anchors.items()
                        if claims & {r.claim_code for r in recognized if r.usability != "INUTILISABLE"}}
            for n in page.nodes:
                spans = [(r.start, r.end) for r in recognized if r.node_key == n.key]
                for h in lexical_hits(kb.lexicon, n.text_plain, exclude_control=False):
                    if h.entry.category != "THEME_PHYSIOLOGIQUE" or h.entry.topic in anchored:
                        continue
                    if any(a <= h.start < b for a, b in spans):
                        continue
                    findings.append(Finding("REFERENCE_GENERALE_ART10_3", "ORANGE", n.key, n.text_plain,
                                            n.text_plain[h.start:h.end], "R1924_2006_ART10_3"))
            warnings = " ".join(norm(z.text) for n in page.nodes for z in scan_frozen(n.text_plain).zones
                                if z.kind is FrozenKind.AVERTISSEMENT_OBLIGATOIRE)
            for s in in_kb:
                if kb.restrictions.get(s) and not any(k in warnings for k in POPULATION_KEYWORDS):
                    findings.append(Finding("VIGILANCE_MENTION_ABSENTE", "ORANGE", None, "", "", "VIGILANCE_SANITAIRE", s))

        return AnalysisResult(findings, recognized, missing, time.perf_counter() - t0)

    # ---------------------------------------------------------------- réécriture
    max_attempts = 1

    def rewrite(self, case: dict, analysis: AnalysisResult, targets: list[str] | None = None) -> RewriteResult:
        """targets : débuts (normalisés) des nœuds à réécrire ; par défaut, les cibles de la vérité terrain."""
        t0 = time.perf_counter()
        kb = self.kb
        html = case["input_html_nodes"]
        if targets is None:
            gt = case.get("expected_ground_truth") or {}
            targets = [t["node_startswith"] for t in gt.get("expected_rewrite_constraints", {}).get("rewrite_targets", [])]
        composition = effective_composition(case)
        product = product_of(case, kb, composition)
        in_kb = sorted({i["substance"] for i in composition if i["substance"] in kb.substances})

        allowed_claims, notices_by_claim = [], {}
        for code, claim in kb.claims.items():
            if claim["substance"] not in in_kb or claim["regime"] == "REJECTED_EU_REGISTER":
                continue
            ev = evaluate_claim(code, claim["substance"], kb.conditions.get(code, []), product)
            if ev.usability is ClaimUsability.INUTILISABLE:
                continue
            allowed_claims.append({"claim_ref": code, "usability": ev.usability.value,
                                   "variants": [{"variant_ref": v["code"], "text": v["text_fr"]} for v in kb.variants[code]]})
            notices_by_claim[code] = [t for _, t in ev.required_notices]
        allowed_refs = {c["claim_ref"] for c in allowed_claims}
        allowed_topics = [{"topic_key": t, "anchor_claim_refs": sorted(c)} for t, c in kb.topic_anchors.items() if c]
        seo_terms = [{"term": b["legal_term"], "variants": b["legal_term_variants"],
                      "anchor_claim_refs": sorted(set(b["anchor_claims"]) & allowed_refs)}
                     for b in kb.bridges if set(b["anchor_claims"]) & allowed_refs]
        substances = [{"code": s, "names": kb.names_of(s)} for s in in_kb]
        accompanying = [r.claim_code for r in analysis.recognized_claims if r.usability == "UTILISABLE"]

        outcomes: list[TargetOutcome] = []
        used_all, appended, usage_all = [], [], []
        for target in targets:
            nodes = [n for n in node_texts(html) if norm(n[2]).startswith(norm(target))]
            if not nodes:
                outcomes.append(TargetOutcome(target, "NOEUD_INTROUVABLE", ["Nœud introuvable dans le fragment"], []))
                continue
            key, marked, plain, markers = nodes[0]
            scan = scan_frozen(plain, product_names=case.get("product_names", []), extra_identifiers=kb.digit_aliases)
            payload = {
                "mode": "PRODUIT" if case["page_mode"] == "PRODUCT_PAGE" else "ARTICLE",
                "segment": {"original_plain": plain},
                "allowed_claims": allowed_claims,
                "frozen_zones": [{"ref": z.ref, "text": z.text, "removable": z.removable} for z in scan.zones],
                "product_names": case.get("product_names", []),
                "substances_in_product": substances,
                # Un terme relevé au titre de l'art. 10§3 n'est pas interdit : il doit être ACCOMPAGNÉ d'une
                # allégation spécifique (contrôlé par le validateur de thèmes), pas supprimé.
                "forbidden_terms": sorted({f.quote for f in analysis.findings
                                           if f.node_text == plain and f.quote and f.level in ("ROUGE", "ORANGE")
                                           and f.code not in ("REFERENCE_GENERALE_ART10_3", "IA_REFERENCE_GENERALE")}),
                "allowed_topics": allowed_topics,
                "accompanying_claim_refs": sorted(set(accompanying)),
                "seo_terms": seo_terms,
            }
            try:
                reference = build_reference_text(marked, markers, scan)
            except UnsupportedSegment as exc:
                outcomes.append(TargetOutcome(target, "IMPOSSIBLE", [str(exc)], [], node_key=key, before_plain=plain))
                continue
            ctx = {"case": case, "analysis": analysis, "key": key, "marked": marked, "plain": plain,
                   "markers": markers, "scan": scan, "payload": payload, "reference": reference}
            status, errors, result, previous, new_html = "SANS_ENREGISTREMENT", ["Aucune sortie de réécrivain disponible pour ce nœud"], None, [], None
            for _ in range(self.max_attempts):
                out, usage, err = self._produce(target, ctx, previous)
                usage_all += usage
                if out is None:
                    if err:
                        status, errors = "ERREUR_IA", [err]
                    break
                result = assemble_and_validate(_resolve_frozen_refs(out, scan), payload, original_marked=marked,
                                               markers=markers, source_scan=scan, lexicon=kb.lexicon,
                                               compiled_rules=kb.compiled, notices_by_claim=notices_by_claim)
                status, errors = result.status, list(result.errors)
                if status == "VALIDEE":
                    try:
                        new_html = patch_fragment(html, key, result.marked)
                        break
                    except Exception as exc:  # violation structurelle : rien n'est appliqué
                        status, errors = "REJETEE_VALIDATEUR", [f"Patch DOM refusé : {exc}"]
                if status == "IMPOSSIBLE":
                    errors = [result.report.get("notes_fr", "") or "Réécriture impossible sans inventer"]
                    break
                previous = errors
            ok = status == "VALIDEE" and new_html is not None
            if ok:
                html = new_html
                used_all += result.used_claim_refs
                appended += result.appended_notices
                accompanying += result.used_claim_refs
            outcomes.append(TargetOutcome(target, status if ok else status, errors, result.used_claim_refs if ok else [],
                                          node_key=key, new_marked=result.marked if ok else None, before_plain=plain,
                                          after_plain=result.plain if ok else None,
                                          notices=list(result.appended_notices) if ok else []))
        return RewriteResult(html, outcomes, used_all, appended, time.perf_counter() - t0, usage_all)

    def _produce(self, target: str, ctx: dict, previous_errors: list[str]):
        """Replay : la sortie enregistrée pour ce nœud, jamais de seconde tentative. Renvoie (sortie, usages, erreur)."""
        if previous_errors:
            return None, [], None
        case = ctx["case"]
        recorded = self.overrides.get(case["test_id"]) or self.replays.get(case["test_id"], {}).get("targets", [])
        rec = {norm(t["node_startswith"]): t for t in recorded}.get(norm(target))
        return (rec["llm_output"] if rec else None), [], None

def _resolve_frozen_refs(llm_output: dict, scan) -> dict:
    """Les fixtures désignent une zone gelée par son texte (« match ») ; une sortie LLM réelle porte F1…Fn."""
    spans = []
    for s in llm_output.get("spans", []):
        if s.get("kind") == "ZONE_GELEE" and "match" in s:
            ref = next((z.ref for z in scan.zones if z.text == s["match"]), f"INCONNUE:{s['match']}")
            spans.append({"kind": "ZONE_GELEE", "ref": ref})
        else:
            spans.append(s)
    return {**llm_output, "spans": spans}


# ---------------------------------------------------------------------------
# Pipeline réel : déterministe + IA (Claude Fable par défaut)
# ---------------------------------------------------------------------------
class LivePipeline(ReplayPipeline):
    """Analyse = déterministe + extracteur IA ; réécriture = réécrivain IA, jusqu'à 3 tentatives, chaque
    sortie passant par l'assembleur et le patch DOM. Si l'IA est indisponible, l'analyse déterministe
    est rendue telle quelle et le signale."""
    name = "deterministe+IA"
    max_attempts = 3
    MAX_NODE_CHARS = 15000

    def __init__(self, kb: KnowledgeBase, client=None):
        super().__init__(kb, {"cases": {}})
        if client is None:
            from engine.ai import AnthropicClient
            client = AnthropicClient()
        self.client = client

    def analyze(self, case: dict, html: str) -> AnalysisResult:
        from engine.ai import LLMError, extract_claims
        base = super().analyze(case, html)
        t0 = time.perf_counter()
        kb = self.kb
        is_document = html.lstrip()[:200].lower().startswith(("<!doctype", "<html"))
        page = extract_page(html if is_document else f"<html><head><title></title></head><body><main>{html}</main></body></html>",
                            case["page_url"])
        texts = {n.key: n.text_plain for n in page.nodes}
        payload_nodes, total = [], 0
        for n in page.nodes:
            if n.zone in NOT_ANALYZED_ZONES or len(n.text_plain.strip()) < 3:
                continue
            if total > self.MAX_NODE_CHARS:
                break
            payload_nodes.append({"key": n.key, "text": n.text_plain[:1500]})
            total += len(n.text_plain)
        composition = effective_composition(case)
        in_kb = sorted({i["substance"] for i in composition if i["substance"] in kb.substances})
        others = sorted({i["label"] for i in composition if i["substance"] not in kb.substances})
        closed = [c for c in kb.claims.values() if c["regime"] != "REJECTED_EU_REGISTER" and (not in_kb or c["substance"] in in_kb)]
        try:
            items, usage = extract_claims(
                self.client, mode="PRODUIT" if case["page_mode"] == "PRODUCT_PAGE" else "ARTICLE", nodes=payload_nodes,
                substances=[{"code": s, "name": kb.substances[s]["name_fr"]} for s in in_kb], other_ingredients=others,
                claims=[{"code": c["code"], "substance": c["substance"], "text": c["text_fr"]} for c in closed])
        except LLMError as exc:
            base.findings.append(Finding("IA_INDISPONIBLE", "INFO", None, "", str(exc)[:200], None))
            base.usage += exc.usage
            base.latency_s += time.perf_counter() - t0
            return base
        base.usage += usage
        product = product_of(case, kb, composition)
        has_specific = any(r.usability == "UTILISABLE" for r in base.recognized_claims)
        page_norm = norm("\n".join(texts.values()))
        dropped = 0
        for it in items:
            text = texts.get(it["node_key"], "")
            start = text.find(it["quote"])
            if start >= 0:
                end = start + len(it["quote"])
            else:
                nt, offs = norm_with_offsets(text)
                qn = norm(it["quote"])
                i = nt.find(qn) if qn else -1
                if i < 0:
                    dropped += 1
                    continue
                start, end = original_span(offs, i, i + len(qn))
            if any(r.node_key == it["node_key"] and not (end <= r.start or start >= r.end) for r in base.recognized_claims):
                continue
            finding = self._verdict(it, text[start:end], it["node_key"], text, start, end, product, in_kb, has_specific,
                                    page_norm, base)
            if finding is not None:
                base.findings.append(finding)
        if dropped:
            base.findings.append(Finding("IA_CITATIONS_REJETEES", "INFO", None, "",
                                         f"{dropped} citation(s) introuvable(s) dans la page, ignorée(s)", None))
        base.latency_s += time.perf_counter() - t0
        return base

    def _verdict(self, it, quote, key, text, start, end, product, in_kb, has_specific, page_norm, base):
        kb = self.kb
        ctype, sub, code = it["claim_type"], it.get("substance_code"), it["matched_claim_code"]

        def mk(fcode, level, basis, rem=()):
            return Finding(fcode, level, key, text, quote, basis, sub, remediation_codes=tuple(rem))

        if ctype == "PROPRIETE_THERAPEUTIQUE_INTERDITE":
            return mk("IA_PROPRIETE_THERAPEUTIQUE", "ROUGE", "INCO_1169_2011_ART7_3", ("REECRIRE_ALLEGATION",))
        if ctype == "NUTRITIONNELLE":
            return mk("IA_ALLEGATION_NUTRITIONNELLE", "INFO", "R1924_2006_ART8")
        if ctype == "REFERENCE_GENERALE_ART10_3":
            return None if has_specific else mk("IA_REFERENCE_GENERALE", "ORANGE", "R1924_2006_ART10_3")
        if code == "AUCUNE":
            if sub and kb.substances.get(sub, {}).get("kind") == "PLANTE" and not any(c["substance"] == sub for c in kb.claims.values()):
                return mk("IA_ALLEGATION_BOTANIQUE_NON_COUVERTE", "ORANGE", "R1924_2006_ART28")
            if ctype == "REDUCTION_RISQUE_MALADIE":
                return mk("IA_REDUCTION_RISQUE_NON_AUTORISEE", "ROUGE", "R1924_2006_ART10_1", ("REECRIRE_ALLEGATION",))
            return mk("IA_ALLEGATION_SANS_CORRESPONDANCE", "ROUGE", "R1924_2006_ART10_1", ("REECRIRE_ALLEGATION",))
        claim = kb.claims[code]
        if claim["substance"] not in in_kb:
            return mk("ALLEGATION_SUBSTANCE_ABSENTE", "ROUGE", "R1924_2006_ART10_1")
        ev = evaluate_claim(code, claim["substance"], kb.conditions.get(code, []), product)
        if ev.usability is ClaimUsability.INUTILISABLE:
            return mk("CONDITION_NON_REMPLIE", "ROUGE", "R432_2012_CONDITIONS", ("SUPPRIMER_ALLEGATION",))
        if any(norm(t)[:60] not in page_norm for _, t in ev.required_notices):
            return mk("MENTION_OBLIGATOIRE_ABSENTE", "ROUGE", "R432_2012_CONDITIONS")
        if not it["wording_faithful"]:
            return mk("IA_FORMULATION_NON_FIDELE", "ORANGE", "R432_2012_CONDITIONS", ("REECRIRE_ALLEGATION",))
        base.recognized_claims.append(RecognizedClaim(code, "IA", key, start, end, ev.usability.value))
        if ev.usability is ClaimUsability.CONDITIONNELLE:
            return mk("CONDITION_A_CONFIRMER", "CONDITIONNEL", "R432_2012_CONDITIONS")
        return None

    def _produce(self, target: str, ctx: dict, previous_errors: list[str]):
        from engine.ai import LLMError, rewrite_spans
        p = ctx["payload"]
        problems = [{"quote": f.quote, "code": f.code} for f in ctx["analysis"].findings
                    if f.node_text == ctx["plain"] and f.quote and f.level in ("ROUGE", "ORANGE")]
        llm_payload = {"mode": p["mode"], "segment": ctx["reference"], "frozen_zones": p["frozen_zones"],
                       "problems": problems,
                       "allowed_claims": [{"claim_ref": c["claim_ref"], "variants": c["variants"]} for c in p["allowed_claims"]],
                       "seo_terms": p["seo_terms"], "target_query": ctx["case"].get("target_seo_query"),
                       "previous_errors": previous_errors}
        try:
            out, usage = rewrite_spans(self.client, llm_payload)
        except LLMError as exc:
            return None, exc.usage, str(exc)
        return out, [usage], None
