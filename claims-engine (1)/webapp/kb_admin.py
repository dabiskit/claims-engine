"""
webapp/kb_admin.py — Base officielle : import depuis les sources officielles, mise à jour, citation.

  * Au démarrage : le dernier import réussi (table demo_kb_imports) est fusionné avec la base rédigée.
  * Mise à jour : bouton administrateur, et automatiquement si le dernier import a plus de
    KB_SYNC_DAYS jours (7 par défaut ; AUTO_SYNC=0 pour désactiver).
  * /app/base/sources : sources, versions, date de téléchargement, historique des imports.
  * /app/base/refusees : demandes d'allégation refusées par la Commission (texte officiel anglais).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import timedelta

from fastapi import Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, insert, select, update

from engine.textnorm import norm
from webapp import kb_sync
from webapp import main as m

SYNC_DAYS = int(os.environ.get("KB_SYNC_DAYS", "7"))
_lock = threading.Lock()

m.T["sources"] = """{% extends "base" %}{% block content %}<nav class="crumb" aria-label="Fil d\'Ariane"><a href="/app/base">← Base réglementaire</a></nav>
<div class="card"><h1>Sources officielles et mise à jour</h1>
<p>La base combine la <strong>base rédigée</strong> (lexique des formulations à risque, variantes, vigilance : en cours de validation par l'expert)
et un <strong>import automatique des textes officiels</strong> : libellés et conditions d'utilisation des allégations autorisées, et liste des demandes refusées.
Chaque allégation importée cite sa source et la version du texte.</p>
{% if official %}<table><tr><th>Source</th><th>Version</th><th>Éléments</th></tr>{% for src in official.sources %}
<tr><td><a href="{{ src.url }}" rel="noopener" target="_blank">{{ src.name }}</a>{% if src.api %}<div class="faint xs break">API : {{ src.api }}</div>{% endif %}</td><td>{{ src.version or '—' }}</td>
<td>{{ src.items }}{% if src.authorised is defined %} <span class="muted">({{ src.authorised }} autorisées, {{ src.rejected }} refusées)</span>{% endif %}</td></tr>{% endfor %}</table>
<p class="muted">Téléchargé le {{ official.retrieved_at[:16]|replace('T', ' à ') }} UTC. Ajouts à la base : {{ official.stats.new_claims }} allégations, {{ official.stats.new_substances }} ingrédients
({{ official.stats.already_in_base }} libellés déjà présents dans la base rédigée, conservés tels quels).</p>
{% else %}<p><span class="tag orange">Aucun import réussi pour l'instant.</span></p>{% endif %}
<p class="muted">Non automatisable à ce jour et donc absent : les allégations botaniques « on-hold » (hors registre), la liste française des plantes autorisées (arrêté du 24 juin 2014, accessible via l'API Légifrance après inscription),
les doses maximales recommandées par la DGCCRF. L'interprétation (rattacher une formulation commerciale à une allégation) reste le travail de l'expert.</p>
{% if acc.admin %}<form method="post" action="/admin/base/sync"><input type="hidden" name="csrf" value="{{ csrf }}"><button {% if running %}disabled{% endif %}>{{ 'Mise à jour en cours…' if running else 'Mettre à jour depuis les sources officielles' }}</button></form>
{% if running %}<p class="muted">Téléchargement en cours (1 à 3 minutes). <a href="/app/base/sources">Actualiser</a></p><meta http-equiv="refresh" content="15">{% endif %}{% endif %}</div>
{% if history %}<div class="card scroll"><h2>Historique des imports</h2><table><tr><th>Début</th><th>Fin</th><th>Statut</th><th>Détail</th></tr>
{% for h in history %}<tr><td>{{ h.started }}</td><td>{{ h.finished }}</td><td><span class="tag {{ {'OK':'ok','ERREUR':'red','EN_COURS':'orange'}.get(h.status, 'info') }}">{{ h.status }}</span></td><td class="muted">{{ h.message }}</td></tr>{% endfor %}</table></div>{% endif %}
{% endblock %}"""

m.T["rejected"] = """{% extends "base" %}{% block content %}<nav class="crumb" aria-label="Fil d\'Ariane"><a href="/app/base">← Base réglementaire</a></nav>
<div class="card"><h1>Demandes d'allégation refusées ({{ total }})</h1>
<p class="muted">Registre de l'Union (Commission européenne), texte officiel en anglais. Une allégation refusée ne peut pas être utilisée, ni telle quelle ni reformulée.</p>
<form method="get" class="row"><input class="grow" name="q" value="{{ q }}" placeholder="Ingrédient ou mot de l'allégation (en anglais : collagen, skin, joint…)"><button>Rechercher</button></form>
<p class="muted">{{ n }} résultat(s){% if n > rows|length %}, {{ rows|length }} affichés{% endif %}.</p></div>
<div class="card flush scroll"><table><tr><th>Ingrédient</th><th>Allégation demandée</th><th>Type</th><th>Référence</th></tr>
{% for r in rows %}<tr><td>{{ r.substance_en }}</td><td>{{ r.claim_en }}</td><td>{{ r.type }}<div class="muted">{{ r.status }}</div></td><td class="muted">{{ r.id }}{% if r.efsa_question %}<div>{{ r.efsa_question }}</div>{% endif %}</td></tr>{% endfor %}</table></div>{% endblock %}"""


def latest_ok():
    with m.ENGINE.connect() as c:
        return c.execute(select(m.KB_IMPORTS).where(m.KB_IMPORTS.c.status == "OK")
                         .order_by(desc(m.KB_IMPORTS.c.id)).limit(1)).mappings().first()


def is_running() -> bool:
    with m.ENGINE.connect() as c:
        row = c.execute(select(m.KB_IMPORTS).where(m.KB_IMPORTS.c.status == "EN_COURS")
                        .order_by(desc(m.KB_IMPORTS.c.id)).limit(1)).mappings().first()
    return bool(row) and m.now() - m.aware(row["started_at"]) < timedelta(minutes=20)


def load_latest() -> bool:
    row = latest_ok()
    if not row or not row["data_json"]:
        return False
    try:
        m.apply_seed(kb_sync.merge_seed(m.BASE_SEED, json.loads(row["data_json"])))
        return True
    except Exception as exc:  # une donnée corrompue ne doit pas empêcher l'application de démarrer
        print(f"[kb] import {row['id']} illisible : {exc}", flush=True)
        return False


def sync_job(import_id: int) -> None:
    if not _lock.acquire(blocking=False):
        return
    try:
        dataset = kb_sync.run_sync(m.BASE_SEED)
        msg = (f"{dataset['stats']['new_claims']} allégations ajoutées, {dataset['stats']['new_substances']} ingrédients, "
               f"{dataset['stats']['rejected']} demandes refusées ; annexe 432/2012 version du {dataset['sources'][0]['version']}")
        with m.ENGINE.begin() as c:
            c.execute(update(m.KB_IMPORTS).where(m.KB_IMPORTS.c.id == import_id).values(
                status="OK", message=msg, data_json=json.dumps(dataset, ensure_ascii=False), finished_at=m.now()))
        m.apply_seed(kb_sync.merge_seed(m.BASE_SEED, dataset))
    except Exception as exc:
        with m.ENGINE.begin() as c:
            c.execute(update(m.KB_IMPORTS).where(m.KB_IMPORTS.c.id == import_id).values(
                status="ERREUR", message=f"{type(exc).__name__} : {str(exc)[:500]}", finished_at=m.now()))
    finally:
        _lock.release()


def start_sync() -> int | None:
    if is_running():
        return None
    with m.ENGINE.begin() as c:
        iid = c.execute(insert(m.KB_IMPORTS).values(status="EN_COURS", started_at=m.now())).inserted_primary_key[0]
    threading.Thread(target=sync_job, args=(iid,), daemon=True).start()
    return iid


def _auto_sync_loop() -> None:
    time.sleep(20)
    while True:
        try:
            row = latest_ok()
            outdated = (m.SEED.get("official_import") or {}).get("format", 0) < 2   # nouveau format : réimport
            if (row is None or outdated or m.now() - m.aware(row["started_at"]) > timedelta(days=SYNC_DAYS)) and not is_running():
                start_sync()
        except Exception as exc:
            print(f"[kb] mise à jour automatique impossible : {exc}", flush=True)
        time.sleep(6 * 3600)


load_latest()
if os.environ.get("AUTO_SYNC", "1") != "0":
    threading.Thread(target=_auto_sync_loop, daemon=True).start()


@m.app.get("/app/base/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    user, stop = m.guard(request)
    if stop:
        return stop
    with m.ENGINE.connect() as c:
        rows = c.execute(select(m.KB_IMPORTS.c.id, m.KB_IMPORTS.c.status, m.KB_IMPORTS.c.message, m.KB_IMPORTS.c.started_at,
                                m.KB_IMPORTS.c.finished_at).order_by(desc(m.KB_IMPORTS.c.id)).limit(10)).mappings().all()
    history = [{"status": r["status"], "message": r["message"] or "",
                "started": m.aware(r["started_at"]).strftime("%d/%m/%Y %H:%M"),
                "finished": m.aware(r["finished_at"]).strftime("%d/%m/%Y %H:%M") if r["finished_at"] else "—"} for r in rows]
    return m.render(request, "sources", title="Sources officielles", official=m.SEED.get("official_import"),
                    history=history if m.access(user)["admin"] else [], running=is_running())


@m.app.post("/admin/base/sync")
def sync_now(request: Request, csrf_token: str = Form("", alias="csrf")):
    user = m.current_user(request)
    if user is None or not m.access(user)["admin"] or not m.csrf_ok(request, csrf_token):
        return m.redirect("/app")
    start_sync()
    return m.redirect("/app/base/sources")


@m.app.get("/app/base/refusees", response_class=HTMLResponse)
def rejected_page(request: Request, q: str = ""):
    user, stop = m.guard(request)
    if stop:
        return stop
    rows = m.SEED.get("rejected_claims") or []
    total = len(rows)
    if q.strip():
        terms = norm(q).split()
        rows = [r for r in rows if all(t in norm(f"{r.get('substance_en') or ''} {r.get('claim_en') or ''}") for t in terms)]
    return m.render(request, "rejected", title="Demandes refusées", rows=rows[:300], n=len(rows), total=total, q=q)


@m.app.get("/sante/base")
def health_base():
    """État public (sans donnée sensible) : moteur de base de données et dernier import officiel."""
    from fastapi.responses import JSONResponse
    with m.ENGINE.connect() as c:
        row = c.execute(select(m.KB_IMPORTS.c.status, m.KB_IMPORTS.c.message, m.KB_IMPORTS.c.started_at,
                               m.KB_IMPORTS.c.finished_at).order_by(desc(m.KB_IMPORTS.c.id)).limit(1)).mappings().first()
    return JSONResponse({"db": m.db_backend(), "db_config": m.DB_STATUS.get("config"), "db_detail": m.DB_STATUS.get("detail"), "release": m.KB.release, "claims": len(m.SEED["regulatory_claims"]),
                         "substances": len(m.SEED["substances"]), "rejected": len(m.SEED.get("rejected_claims") or []),
                         "last_import": {"status": row["status"], "message": row["message"],
                                         "started_at": str(row["started_at"]), "finished_at": str(row["finished_at"])} if row else None})


# Ces pages doivent passer avant la route générique /app/base/{code} (fiche ingrédient).
_paths = {"/app/base/sources", "/app/base/refusees"}
_mine = [r for r in m.app.router.routes if getattr(r, "path", None) in _paths]
m.app.router.routes[:] = _mine + [r for r in m.app.router.routes if r not in _mine]
