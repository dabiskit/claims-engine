"""
webapp/features.py — Réécriture IA, décisions (journal d'audit), rapport imprimable, tarifs, banc d'essai.

Chargé par webapp/main.py. Les propositions de réécriture passent toutes par l'assembleur et le
patch DOM du moteur ; une proposition refusée par le validateur est affichée comme telle, jamais
appliquée. Chaque décision est horodatée, nominative, et ne peut pas être modifiée.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict

from fastapi import Form, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape
from sqlalchemy import Column, DateTime, Integer, String, Table, Text, desc, func, insert, select

from engine.frozen_zones import FrozenZoneViolation, assert_frozen_preserved, scan_frozen
from engine.lexicon import SEV_RANK
from engine.mode_router import lexical_hits
from eval.pipeline_adapter import AnalysisResult, Finding, RecognizedClaim, visible_text
from webapp import main as m

MAX_REWRITES = int(os.environ.get("MAX_REWRITES_PER_RUN", "12"))
STATUS_LABELS = {"VALIDEE": ("Proposition contrôlée", "ok"), "REJETEE_VALIDATEUR": ("Refusée par le validateur : à rédiger à la main", "red"),
                 "IMPOSSIBLE": ("Impossible sans inventer : à rédiger à la main", "orange"),
                 "SANS_ENREGISTREMENT": ("Réécriture IA non activée", "info"), "ERREUR_IA": ("Erreur du service IA", "red"),
                 "NOEUD_INTROUVABLE": ("Passage introuvable", "red")}
DECISION_LABELS = {"ACCEPTEE": "Proposition acceptée", "MODIFIEE": "Version de l'utilisateur",
                   "MAINTENUE": "Texte d'origine maintenu sous la responsabilité de l'utilisateur"}
PLANS = [
    ("Marque Essentiel", "149 € HT / mois", ["100 pages surveillées", "50 analyses par mois", "3 utilisateurs, collaborateurs invités gratuits", "Réécritures, exports, rapports"]),
    ("Marque Pro", "349 € HT / mois", ["400 pages surveillées", "150 analyses par mois", "Search Console, rapport mensuel de vigilance", "Utilisateurs illimités"]),
    ("Laboratoire / multi-marques", "790 € HT / mois", ["1 500 pages surveillées", "350 analyses par mois", "Plusieurs sites"]),
    ("Agence spécialisée", "290 € HT / mois", ["150 analyses par mois", "Marques illimitées", "Rapports en marque blanche", "Programme partenaire : 20 % récurrents"]),
    ("Packs rédacteur", "49 € (15 pages) · 129 € (50 pages)", ["Sans abonnement", "Valables 12 mois"]),
]

PROPOSALS = Table("demo_proposals", m.MD,
                  Column("id", Integer, primary_key=True), Column("analysis_id", Integer, nullable=False),
                  Column("batch", Integer, nullable=False), Column("node_key", String(20), nullable=False),
                  Column("has_red", Integer, nullable=False), Column("placement", String(300)),
                  Column("status", String(30), nullable=False), Column("before_text", Text), Column("before_html", Text),
                  Column("after_text", Text), Column("after_html", Text), Column("details_json", Text),
                  Column("created_at", DateTime(timezone=True), nullable=False))
DECISIONS = Table("demo_decisions", m.MD,
                  Column("id", Integer, primary_key=True), Column("analysis_id", Integer, nullable=False),
                  Column("proposal_id", Integer, nullable=False), Column("user_id", Integer, nullable=False),
                  Column("decision", String(20), nullable=False), Column("final_text", Text), Column("reason", Text),
                  Column("created_at", DateTime(timezone=True), nullable=False))
BENCHMARKS = Table("demo_benchmarks", m.MD,
                   Column("id", Integer, primary_key=True), Column("status", String(30), nullable=False),
                   Column("result_json", Text), Column("started_at", DateTime(timezone=True), nullable=False),
                   Column("finished_at", DateTime(timezone=True)))
m.MD.create_all(m.ENGINE)
JOBS: dict[str, str] = {}


def run_job(fn, *args) -> None:
    if os.environ.get("SYNC_JOBS") == "1":
        fn(*args)
    else:
        threading.Thread(target=fn, args=args, daemon=True).start()


def analysis_from(result: dict) -> AnalysisResult:
    raw = result["raw"]
    findings = [Finding(**{**f, "remediation_codes": tuple(f.get("remediation_codes") or ())}) for f in raw["findings"]]
    return AnalysisResult(findings, [RecognizedClaim(**r) for r in raw["recognized"]], raw["kb_missing"], 0.0)


def placement(node: dict) -> str:
    parts = [f"Zone : {node['zone'].lower()}"]
    parts.append("dans la description produit" if node.get("in_description") else "hors description produit")
    if node.get("shopify_section"):
        parts.append(f"section Shopify {node['shopify_section']}")
    return " · ".join(parts)


# -----------------------------------------------------------------------------
# Réécriture
# -----------------------------------------------------------------------------
def rewrite_job(aid: int) -> None:
    JOBS[str(aid)] = "EN_COURS"
    try:
        with m.ENGINE.connect() as c:
            row = c.execute(select(m.ANALYSES).where(m.ANALYSES.c.id == aid)).mappings().first()
            batch = (c.execute(select(func.max(PROPOSALS.c.batch)).where(PROPOSALS.c.analysis_id == aid)).scalar() or 0) + 1
        result = json.loads(row["result_json"])
        analysis = analysis_from(result)
        case = result["raw"]["case"]
        nodes = sorted(result["raw"]["nodes_index"].items(), key=lambda kv: (0 if kv[1]["has_red"] else 1, kv[0]))
        for key, node in nodes[:MAX_REWRITES]:
            if not node.get("host_html"):
                continue
            ncase = {**case, "input_html_nodes": node["host_html"], "expected_ground_truth": None}
            rw = m.PIPE.rewrite(ncase, analysis, targets=[node["text"][:80]])
            t = rw.targets[0] if rw.targets else None
            status = t.status if t else "NOEUD_INTROUVABLE"
            ok = status == "VALIDEE"
            details = {"errors": t.errors if t else [], "used_claims": t.used_claims if t else [],
                       "notices": t.notices if t else [], "usage": [asdict(u) for u in rw.usage]}
            with m.ENGINE.begin() as c:
                c.execute(insert(PROPOSALS).values(
                    analysis_id=aid, batch=batch, node_key=key, has_red=int(node["has_red"]), placement=placement(node),
                    status=status, before_text=node["text"], before_html=node["host_html"],
                    after_text=visible_text(rw.output_html) if ok else None, after_html=rw.output_html if ok else None,
                    details_json=json.dumps(details, ensure_ascii=False), created_at=m.now()))
        JOBS[str(aid)] = "TERMINE"
    except Exception as exc:  # le job ne doit jamais laisser la page en attente
        JOBS[str(aid)] = f"ERREUR : {exc}"


def check_manual_text(before_text: str, new_text: str, case: dict) -> list[str]:
    """Contrôles sur une version saisie par l'utilisateur : lexique et zones gelées (déterministe),
    puis extraction IA si elle est active. Toute alerte rouge ou orange bloque l'enregistrement."""
    errors = []
    for h in lexical_hits(m.KB.lexicon, new_text):
        if SEV_RANK[h.entry.severity["PRODUIT"]] >= SEV_RANK["ORANGE"]:
            errors.append(f"« {h.surface} » : {m.CATEGORY_LABELS.get(h.entry.category, h.entry.category)}")
    names = case.get("product_names", [])
    try:
        assert_frozen_preserved(scan_frozen(before_text, product_names=names, extra_identifiers=m.KB.digit_aliases),
                                new_text, product_names=names, extra_identifiers=m.KB.digit_aliases)
    except FrozenZoneViolation as exc:
        errors.append(f"Valeur modifiée ou inventée : {exc}")
    if m.AI_ENABLED and not errors:
        html = "<p>" + str(escape(new_text)) + "</p>"
        res = m.PIPE.analyze({**case, "input_html_nodes": html}, html)
        errors += [f"« {f.quote} » : {m.CODE_LABELS.get(f.code, f.code)}" for f in res.findings
                   if f.level in ("ROUGE", "ORANGE") and f.quote]
    return errors


def _load(request: Request, aid: int):
    user, stop = m.guard(request)
    if stop:
        return None, None, stop
    with m.ENGINE.connect() as c:
        row = c.execute(select(m.ANALYSES).where(m.ANALYSES.c.id == aid, m.ANALYSES.c.user_id == user["id"])).mappings().first()
    if not row:
        return user, None, m.render(request, "expired", 404, title="Introuvable")
    return user, row, None


@m.app.post("/app/analyses/{aid}/reecrire")
async def start_rewrite(request: Request, aid: int):
    user, row, stop = _load(request, aid)
    if stop:
        return stop
    form = await request.form()
    if not m.csrf_ok(request, form.get("csrf", "")):
        return m.redirect(f"/app/analyses/{aid}")
    if JOBS.get(str(aid)) != "EN_COURS":
        run_job(rewrite_job, aid)
    return m.redirect(f"/app/analyses/{aid}/propositions")


def _proposals(aid: int) -> list[dict]:
    with m.ENGINE.connect() as c:
        batch = c.execute(select(func.max(PROPOSALS.c.batch)).where(PROPOSALS.c.analysis_id == aid)).scalar()
        if not batch:
            return []
        rows = c.execute(select(PROPOSALS).where(PROPOSALS.c.analysis_id == aid, PROPOSALS.c.batch == batch)
                         .order_by(desc(PROPOSALS.c.has_red), PROPOSALS.c.id)).mappings().all()
        decisions = c.execute(select(DECISIONS, m.USERS.c.email).join(m.USERS, m.USERS.c.id == DECISIONS.c.user_id)
                              .where(DECISIONS.c.analysis_id == aid).order_by(DECISIONS.c.created_at)).mappings().all()
    latest = {}
    for d in decisions:
        latest[d["proposal_id"]] = {"decision": d["decision"], "label": DECISION_LABELS.get(d["decision"], d["decision"]),
                                    "final_text": d["final_text"], "reason": d["reason"], "email": d["email"],
                                    "date": m.aware(d["created_at"]).strftime("%d/%m/%Y %H:%M")}
    out = []
    for r in rows:
        details = json.loads(r["details_json"] or "{}")
        label, css = STATUS_LABELS.get(r["status"], (r["status"], "info"))
        out.append({"id": r["id"], "node_key": r["node_key"], "has_red": bool(r["has_red"]), "placement": r["placement"],
                    "status": r["status"], "status_label": label, "css": css, "before_text": r["before_text"],
                    "after_text": r["after_text"], "after_html": r["after_html"], "errors": details.get("errors", []),
                    "used_claims": details.get("used_claims", []), "notices": details.get("notices", []),
                    "decision": latest.get(r["id"])})
    return out


@m.app.get("/app/analyses/{aid}/propositions", response_class=HTMLResponse)
def proposals_page(request: Request, aid: int):
    user, row, stop = _load(request, aid)
    if stop:
        return stop
    rows = _proposals(aid)
    pending = sum(1 for p in rows if p["has_red"] and not p["decision"])
    flash = request.session.pop("flash", None)
    return m.render(request, "proposals", title="Propositions", aid=aid, rows=rows, pending=pending,
                    job=JOBS.get(str(aid)), flash=flash, ai_enabled=m.AI_ENABLED)


@m.app.post("/app/propositions/{pid}/decision")
def decide(request: Request, pid: int, decision: str = Form(""), reason: str = Form(""), final_text: str = Form(""),
           csrf_token: str = Form("", alias="csrf")):
    user, stop = m.guard(request)
    if stop:
        return stop
    with m.ENGINE.connect() as c:
        p = c.execute(select(PROPOSALS).where(PROPOSALS.c.id == pid)).mappings().first()
        row = c.execute(select(m.ANALYSES).where(m.ANALYSES.c.id == p["analysis_id"], m.ANALYSES.c.user_id == user["id"])).mappings().first() if p else None
    if not p or not row or not m.csrf_ok(request, csrf_token) or decision not in DECISION_LABELS:
        return m.redirect("/app")
    back = f"/app/analyses/{p['analysis_id']}/propositions#p{pid}"
    case = json.loads(row["result_json"])["raw"]["case"]
    text = None
    if decision == "ACCEPTEE":
        if not p["after_text"]:
            request.session["flash"] = "Aucune proposition contrôlée à accepter pour ce passage."
            return m.redirect(back)
        text = p["after_text"]
    elif decision == "MODIFIEE":
        text = final_text.strip()
        errors = check_manual_text(p["before_text"], text, case) if text else ["Texte vide"]
        if errors:
            request.session["flash"] = "Version non enregistrée : " + " ; ".join(errors[:4])
            return m.redirect(back)
    elif decision == "MAINTENUE":
        if len(reason.strip()) < 10:
            request.session["flash"] = "Pour maintenir le texte d'origine, indiquez un motif (10 caractères minimum)."
            return m.redirect(back)
        text = p["before_text"]
    with m.ENGINE.begin() as c:
        c.execute(insert(DECISIONS).values(analysis_id=p["analysis_id"], proposal_id=pid, user_id=user["id"],
                                           decision=decision, final_text=text, reason=reason.strip()[:2000] or None,
                                           created_at=m.now()))
    return m.redirect(back)


@m.app.get("/app/analyses/{aid}/rapport", response_class=HTMLResponse)
def report(request: Request, aid: int):
    user, row, stop = _load(request, aid)
    if stop:
        return stop
    r = json.loads(row["result_json"])
    rows = _proposals(aid)
    pending = sum(1 for p in rows if p["has_red"] and not p["decision"])
    return m.render(request, "report", title="Rapport", aid=aid, r=r, rows=rows, pending=pending, user_email=user["email"],
                    generated=m.now().strftime("%d/%m/%Y %H:%M"), levels=m.LEVELS)


@m.app.get("/tarifs", response_class=HTMLResponse)
def pricing(request: Request):
    return m.render(request, "pricing", title="Tarifs", plans=PLANS)


# -----------------------------------------------------------------------------
# Banc d'essai (administrateur) : golden set en mode live, depuis le navigateur
# -----------------------------------------------------------------------------
def benchmark_job(bid: int) -> None:
    from eval import evaluate_pipeline as ev
    try:
        pricing_env = os.environ.get("LLM_PRICING_JSON", "")
        pricing_cfg = json.loads(pricing_env) if pricing_env else {"models": {}}
        st = ev.self_test(m.GOLDEN, m.KB, json.loads((m.ROOT / "eval" / "replays.json").read_text(encoding="utf-8")))
        reports, metrics = ev.run_benchmark(m.GOLDEN, m.PIPE, m.KB, pricing_cfg)
        gates = ev.evaluate_gates(metrics, "live" if m.AI_ENABLED else "replay")
        blocking = [k for k, v in gates.items() if v["blocking"] and v["status"] != "OK"]
        result = {"self_test_passed": st["passed"], "metrics": metrics, "gates": gates, "blocking": blocking,
                  "cases": [asdict(x) for x in reports], "pipeline": m.PIPE.name}
        status = "TERMINE"
    except Exception as exc:
        result, status = {"error": str(exc)}, "ERREUR"
    with m.ENGINE.begin() as c:
        c.execute(BENCHMARKS.update().where(BENCHMARKS.c.id == bid)
                  .values(status=status, result_json=json.dumps(result, ensure_ascii=False, default=str), finished_at=m.now()))


@m.app.get("/admin/banc", response_class=HTMLResponse)
def bench_page(request: Request):
    user = m.current_user(request)
    if user is None or not m.access(user)["admin"]:
        return m.redirect("/app")
    with m.ENGINE.connect() as c:
        last = c.execute(select(BENCHMARKS).order_by(desc(BENCHMARKS.c.id))).mappings().first()
    result = json.loads(last["result_json"]) if last and last["result_json"] else None
    return m.render(request, "bench", title="Banc d'essai", last=last, result=result, ai_enabled=m.AI_ENABLED,
                    running=bool(last and last["status"] == "EN_COURS"))


@m.app.post("/admin/banc")
def bench_start(request: Request, csrf_token: str = Form("", alias="csrf")):
    user = m.current_user(request)
    if user is None or not m.access(user)["admin"] or not m.csrf_ok(request, csrf_token):
        return m.redirect("/app")
    with m.ENGINE.begin() as c:
        running = c.execute(select(func.count()).select_from(BENCHMARKS).where(BENCHMARKS.c.status == "EN_COURS")).scalar()
        if running:
            return m.redirect("/admin/banc")
        bid = c.execute(insert(BENCHMARKS).values(status="EN_COURS", started_at=m.now())).inserted_primary_key[0]
    run_job(benchmark_job, bid)
    return m.redirect("/admin/banc")


# -----------------------------------------------------------------------------
# Gabarits
# -----------------------------------------------------------------------------
m.T["proposals"] = """{% extends "base" %}{% block content %}
<nav class="crumb" aria-label="Fil d\'Ariane"><a href="/app/analyses/{{ aid }}">← Résultat de l'analyse</a></nav>
<div class="card"><h1>Propositions de réécriture</h1>
{% if job == 'EN_COURS' %}<p><strong>Réécriture en cours…</strong> Cette page se met à jour toute seule.</p><meta http-equiv="refresh" content="6">{% elif job and job.startswith('ERREUR') %}<p class="error">{{ job }}</p>{% endif %}
{% if flash %}<p class="error">{{ flash }}</p>{% endif %}
{% if not rows and job != 'EN_COURS' %}<p>Aucune proposition pour cette analyse.{% if ai_enabled %} Lancez « Proposer des réécritures (IA) » depuis le résultat.{% else %} La réécriture IA n'est pas activée sur cette instance.{% endif %}</p>{% endif %}
<p class="muted">Chaque proposition a été contrôlée automatiquement (zones gelées, balises, allégations, vocabulaire). Rien n'est publié : vous décidez, puis vous copiez le bloc dans votre CMS.</p>
<p><strong>{{ pending }}</strong> passage(s) prioritaire(s) sans décision. <a href="/app/analyses/{{ aid }}/rapport">Rapport imprimable</a></p></div>
{% for p in rows %}<div class="card" id="p{{ p.id }}">
<p><span class="tag {{ p.css }}">{{ p.status_label }}</span>{% if p.has_red %} <span class="tag red">prioritaire</span>{% endif %} <span class="muted">{{ p.placement }}</span></p>
<div class="grid"><div><strong>Avant</strong><p>{{ p.before_text }}</p></div><div><strong>Après</strong><p>{{ p.after_text or '—' }}</p></div></div>
{% if p.used_claims %}<p class="muted">Allégations insérées : {{ p.used_claims|join(', ') }}</p>{% endif %}
{% if p.notices %}<p class="muted">Mention ajoutée : {{ p.notices|join(' / ') }}</p>{% endif %}
{% if p.errors %}<details><summary>Pourquoi aucune proposition n'a été retenue</summary><ul>{% for e in p.errors %}<li>{{ e }}</li>{% endfor %}</ul></details>{% endif %}
{% if p.after_html %}<details><summary>HTML du bloc, prêt à copier dans votre CMS</summary><textarea readonly rows="4">{{ p.after_html }}</textarea></details>{% endif %}
{% if p.decision %}<p><span class="tag ok">{{ p.decision.label }}</span> par {{ p.decision.email }} le {{ p.decision.date }}{% if p.decision.reason %} — motif : « {{ p.decision.reason }} »{% endif %}</p>
{% if p.decision.decision == 'MODIFIEE' %}<p><strong>Version retenue :</strong> {{ p.decision.final_text }}</p>{% endif %}{% endif %}
<form method="post" action="/app/propositions/{{ p.id }}/decision" class="decision"><input type="hidden" name="csrf" value="{{ csrf }}">
<div class="row"><select name="decision">{% if p.after_text %}<option value="ACCEPTEE">Accepter la proposition</option>{% endif %}<option value="MODIFIEE">Enregistrer ma version (ci-dessous, vérifiée avant enregistrement)</option><option value="MAINTENUE">Maintenir le texte d'origine sous ma responsabilité</option></select>
<input class="grow" name="reason" placeholder="Motif (obligatoire pour « maintenir »)"></div>
<textarea name="final_text" rows="3" class="short">{{ p.after_text or p.before_text }}</textarea>
<div><button>Enregistrer la décision</button></div></form></div>{% endfor %}
{% endblock %}"""

m.T["report"] = """{% extends "base" %}{% block content %}
<style>@media print{header,.banner,.trial,.noprint{display:none!important}main{max-width:none;padding:0}.card{break-inside:avoid;border:1px solid #999}}</style>
<div class="actions noprint crumb"><a href="/app/analyses/{{ aid }}">← Résultat</a><button class="secondary sm" onclick="window.print()">Imprimer / enregistrer en PDF</button></div>
<div class="card"><h1>Rapport de pré-analyse des allégations</h1>
<p>{{ r.source }} · {{ 'Fiche produit' if r.mode == 'PRODUCT_PAGE' else 'Article de blog' }}{% if r.product_name %} · {{ r.product_name }}{% endif %}</p>
<p class="muted">Analyse du {{ r.created }} · rapport généré le {{ generated }} par {{ user_email }} · base réglementaire {{ r.kb_release }} (brouillon){% if r.models %} · modèles : {{ r.models|join(', ') }}{% endif %}</p>
<p>{% for k, lbl in levels.items() if k != 'INFO' %}<span class="tag {{ lbl[1] }}">{{ lbl[0] }} : {{ r.counts.get(k, 0) }}</span> {% endfor %}</p>
{% if pending %}<p class="error">{{ pending }} passage(s) prioritaire(s) sans décision : ce rapport n'est pas final.</p>{% else %}<p>Tous les passages prioritaires ont reçu une décision.</p>{% endif %}</div>
<div class="card"><h2>Composition déclarée</h2>{% if r.composition %}<ul>{% for c in r.composition %}<li>{{ c.label }} : {{ c.amount }} {{ c.unit }} par portion journalière</li>{% endfor %}</ul>{% else %}<p class="muted">Aucune composition déclarée : les conditions de dose n'ont pas pu être vérifiées.</p>{% endif %}</div>
<div class="card"><h2>Passages relevés</h2>{% for n in r.nodes %}<p>{{ n.html|safe }}</p><ul>{% for f in n.findings %}<li><span class="tag {{ f.css }}">{{ f.level_label }}</span> « {{ f.quote }} » — {{ f.explanation }}{% if f.legal %} <span class="muted">({{ f.legal }})</span>{% endif %}</li>{% endfor %}</ul>{% endfor %}
{% for f in r.page_level %}<p><span class="tag {{ f.css }}">{{ f.level_label }}</span> {{ f.explanation }}</p>{% endfor %}
{% if not r.nodes and not r.page_level %}<p>Aucun passage relevé par la pré-analyse automatique. Cela ne vaut pas validation.</p>{% endif %}</div>
<div class="card"><h2>Décisions</h2>{% if not rows %}<p class="muted">Aucune proposition de réécriture.</p>{% endif %}
{% for p in rows %}<div class="claim"><p class="muted">{{ p.placement }}</p>
<p><strong>Avant :</strong> {{ p.before_text }}</p>{% if p.after_text %}<p><strong>Proposition :</strong> {{ p.after_text }}</p>{% endif %}
{% if p.decision %}<p><strong>Décision :</strong> {{ p.decision.label }} — {{ p.decision.email }}, {{ p.decision.date }}{% if p.decision.reason %} — motif : {{ p.decision.reason }}{% endif %}</p>
{% if p.decision.decision == 'MODIFIEE' %}<p><strong>Version retenue :</strong> {{ p.decision.final_text }}</p>{% endif %}{% else %}<p><em>Sans décision.</em></p>{% endif %}
{% if p.notices %}<p class="muted">Mention à afficher : {{ p.notices|join(' / ') }}</p>{% endif %}</div>{% endfor %}</div>
<div class="card muted"><p>Pré-analyse automatique, non exhaustive, fondée sur une base réglementaire en cours de validation. Ne constitue pas un avis juridique. Les décisions enregistrées relèvent de la responsabilité de l'exploitant du produit.</p></div>
{% endblock %}"""

m.T["pricing"] = """{% extends "base" %}{% block content %}<div class="card"><h1>Tarifs</h1><p>Essai gratuit avec toutes les fonctionnalités pendant {{ trial_days }} jours, sans carte bancaire. Audit flash gratuit de 3 URLs ; audit catalogue à 490 € HT (jusqu'à 50 pages), offert avec un engagement annuel.</p>
{% if not user %}<p><a class="btn" href="/essai">Démarrer l'essai gratuit</a></p>{% endif %}</div>
<div class="grid plans">{% for name, price, items in plans %}<div class="card"><h3>{{ name }}</h3><p class="price">{{ price }}</p><ul>{% for i in items %}<li>{{ i }}</li>{% endfor %}</ul></div>{% endfor %}</div>
<p class="muted">Facturation en ligne à venir : pendant la période de lancement, les abonnements sont ouverts sur devis.</p>{% endblock %}"""

m.T["bench"] = """{% extends "base" %}{% block content %}<div class="card"><h1>Banc d'essai</h1>
<p class="muted">Rejoue les 10 cas du golden set avec le pipeline actuel ({{ 'IA active' if ai_enabled else 'IA inactive : résultats déterministes seulement' }}) et vérifie les barrières de mise en production.</p>
{% if running %}<p><strong>Banc en cours…</strong> Rechargez dans une ou deux minutes.</p><meta http-equiv="refresh" content="15">{% else %}
<form method="post" action="/admin/banc"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Lancer le banc (consomme des crédits API)</button></form>{% endif %}</div>
{% if last %}<div class="card"><p class="muted">Dernier lancement : {{ last.started_at }} · statut {{ last.status }}</p>
{% if result and result.error %}<p class="error">{{ result.error }}</p>{% elif result %}
<p>Auto-test du banc : {{ 'OK' if result.self_test_passed else 'ÉCHEC' }} · Verdict : <strong>{{ 'RELEASE AUTORISÉE' if not result.blocking else 'RELEASE BLOQUÉE — ' ~ result.blocking|join(', ') }}</strong></p>
<div class="scroll"><table><tr><th>Barrière</th><th>Valeur</th><th>Seuil</th><th>Statut</th></tr>
{% for k, g in result.gates.items() %}<tr><td>{{ k }}</td><td>{% if g.value is none %}n/a{% elif g.value is number and g.value <= 1 and k != 'false_red_on_true_negatives' %}{{ '%.1f'|format(g.value * 100) }} %{% else %}{{ g.value }}{% endif %}</td><td>{{ g.threshold }}</td><td>{{ g.status }}</td></tr>{% endfor %}</table></div>
<h3>Rappel par couche</h3><div class="scroll"><table><tr><th>Couche</th><th>Attendus</th><th>Détectés</th></tr>{% for k, v in result.metrics.recall_by_stage.items() %}<tr><td>{{ k }}</td><td>{{ v.attendus }}</td><td>{{ v.detectes }}</td></tr>{% endfor %}</table></div>
<p class="muted">Coût : {{ result.metrics.cost_measured_pages }} page(s) mesurée(s), {{ result.metrics.cost_unmeasured_pages }} non mesurée(s) (renseigner LLM_PRICING_JSON pour mesurer) · latence moyenne {{ result.metrics.avg_latency_s }} s.</p>
{% endif %}</div>{% endif %}{% endblock %}"""
