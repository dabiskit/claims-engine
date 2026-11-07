"""
webapp/audit.py — Audit de site : analyse en lot d'URL, d'un sitemap ou de fichiers.

Entrées acceptées, combinables :
  * une liste d'URL (une par ligne) ;
  * un sitemap (ou index de sitemaps), filtrable par motif d'URL ;
  * des fichiers : .docx, .pdf, .html, .txt, .md (contenu à analyser) ou .txt / .csv contenant des URL.

Le traitement tourne en tâche de fond, page par page (une requête par seconde vers un même site),
et reprend au redémarrage du serveur. Chaque page produit une analyse complète, consultable comme
une analyse unitaire ; le tableau de bord classe les pages de la plus à la moins risquée.
"""
from __future__ import annotations

import csv
import html as html_lib
import io
import json
import os
import re
import threading
import time
import zipfile
from collections import Counter
from urllib.parse import urlparse

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from lxml import etree
from sqlalchemy import Column, DateTime, Integer, String, Table, Text, case, desc, func, insert, select, update

from webapp import main as m

MAX_PAGES_TRIAL = int(os.environ.get("AUDIT_MAX_PAGES", "100"))
MAX_PAGES_ADMIN = 500
MAX_FILE_BYTES = 10_000_000
_slots = threading.Semaphore(2)

AUDITS = Table("demo_audits", m.MD,
               Column("id", Integer, primary_key=True), Column("user_id", Integer, nullable=False),
               Column("title", String(300), nullable=False), Column("status", String(20), nullable=False),
               Column("use_ai", Integer, nullable=False), Column("mode", String(20), nullable=False),
               Column("created_at", DateTime(timezone=True), nullable=False), Column("finished_at", DateTime(timezone=True)))
ITEMS = Table("demo_audit_items", m.MD,
              Column("id", Integer, primary_key=True), Column("audit_id", Integer, nullable=False, index=True),
              Column("position", Integer, nullable=False), Column("kind", String(10), nullable=False),
              Column("source", String(2000), nullable=False), Column("payload", Text),
              Column("status", String(20), nullable=False), Column("analysis_id", Integer),
              Column("title", String(300)), Column("red", Integer), Column("orange", Integer), Column("cond", Integer),
              Column("summary_json", Text), Column("error", Text))
m.MD.create_all(m.ENGINE)

STATUS = {"EN_ATTENTE": ("En attente", "info"), "EN_COURS": ("En cours", "orange"), "OK": ("Analysée", "ok"),
          "ERREUR": ("Erreur", "red")}

m.T["audit_form"] = """{% extends "base" %}{% block content %}
<div class="page-head"><div><h1>Audit de site</h1><div class="sub">Analysez d'un coup plusieurs pages : une liste d'URL, un sitemap, ou des documents (fiches produit en .docx, PDF, HTML…). Chaque page reçoit une analyse complète ; un tableau de bord classe les pages par niveau de risque.</div></div></div>
<div class="layout"><div>
{% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}
<form method="post" action="/app/audit" enctype="multipart/form-data" class="card"><input type="hidden" name="csrf" value="{{ csrf }}">
<label for="title">Nom de l'audit</label><input class="w-full" id="title" name="title" maxlength="200" placeholder="Ex. Site marque X — novembre" value="{{ form.title or '' }}">
<label for="urls">URL (une par ligne)</label><textarea id="urls" name="urls" rows="5" class="short" placeholder="https://www.exemple.fr/products/…">{{ form.urls or '' }}</textarea>
<div class="row"><div class="grow"><label for="sitemap">Sitemap</label><input class="w-full" id="sitemap" name="sitemap" type="url" placeholder="https://www.exemple.fr/sitemap.xml" value="{{ form.sitemap or '' }}"></div>
<div><label for="contains">Garder les URL contenant</label><input id="contains" name="contains" placeholder="/products/" value="{{ form.contains or '' }}"></div></div>
<label for="files">Fichiers (.docx, .pdf, .html, .txt, .md ; ou .txt / .csv contenant des URL)</label><input id="files" name="files" type="file" multiple accept=".docx,.pdf,.html,.htm,.txt,.md,.csv">
<div class="row"><div><label for="max">Nombre maximal de pages</label><input id="max" name="max" inputmode="numeric" size="5" value="{{ form.max or 50 }}"><div class="hint">jusqu'à {{ cap }}</div></div>
<div><label for="mode">Type de pages</label><select id="mode" name="mode"><option value="AUTO">Détection automatique</option><option value="PRODUCT_PAGE">Fiches produit</option><option value="EDITORIAL_BLOG">Articles de blog</option></select></div></div>
{% if ai_enabled %}<label class="inline"><input type="checkbox" name="use_ai" value="1" checked> Inclure l'extraction IA des promesses implicites (plus précis, plus lent{% if not acc.admin %}, compte dans la limite de {{ ai_limit }} analyses IA par jour{% endif %})</label>{% endif %}
<div class="actions form-foot"><button>Lancer l'audit</button></div></form></div>
<aside class="stack"><div class="card"><h3>Mes audits</h3>{% if audits %}<ul class="list-plain small">{% for a in audits %}<li><a href="/app/audits/{{ a.id }}">{{ a.title }}</a>
<div class="faint xs">{{ a.date }} · {{ a.done }} / {{ a.total }} · {{ a.status }}</div></li>{% endfor %}</ul>{% else %}<p class="muted small">Aucun audit pour l'instant.</p>{% endif %}</div></aside></div>
{% endblock %}"""

m.T["audit"] = """{% extends "base" %}{% block content %}
<nav class="crumb" aria-label="Fil d'Ariane"><a href="/app/audit">← Audits</a></nav>
<section class="card"><div class="page-head"><div><h1>{{ a.title }}</h1><div class="sub">Créé le {{ a.date }} · {{ 'avec' if a.use_ai else 'sans' }} extraction IA</div></div>
<div class="actions"><a class="btn secondary" href="/app/audits/{{ a.id }}.csv">Exporter en CSV</a></div></div>
<div class="progress" role="progressbar" aria-valuenow="{{ pct }}" aria-valuemin="0" aria-valuemax="100"><span class="w-{{ (pct // 5) * 5 }}"></span></div>
<p class="small muted">{{ done }} / {{ total }} page(s) traitée(s){% if errors %}, dont {{ errors }} en erreur{% endif %}.{% if running %} Analyse en cours : la page se met à jour toute seule.{% endif %}</p>
{% if running %}<meta http-equiv="refresh" content="10">{% endif %}
<div class="kpis"><div class="kpi {{ 'red' if pages_red }}"><span class="tag red">Pages avec points prioritaires</span><div class="count">{{ pages_red }}</div></div>
<div class="kpi"><span class="tag red">À revoir en priorité</span><div class="count">{{ sum_red }}</div></div>
<div class="kpi"><span class="tag orange">À revoir</span><div class="count">{{ sum_orange }}</div></div>
<div class="kpi"><span class="tag cond">À confirmer</span><div class="count">{{ sum_cond }}</div></div></div></section>
<div class="grid">
{% if top_terms %}<section class="card"><h2>Formulations prioritaires les plus fréquentes</h2><div class="actions">{% for t, n in top_terms %}<span class="tag red">{{ t }} ×{{ n }}</span>{% endfor %}</div></section>{% endif %}
{% if rejected %}<section class="card"><h2>Ingrédients sans allégation de santé autorisée</h2><div class="actions">{% for t, n in rejected %}<span class="tag red">{{ t }} — {{ n }} page(s)</span>{% endfor %}</div></section>{% endif %}
</div>
<section class="card flush"><div class="card-head pad"><h2>Pages, de la plus à la moins risquée</h2></div><div class="scroll"><table><tr><th>Page</th><th class="num">Rouge</th><th class="num">Orange</th><th class="num">À confirmer</th><th>À traiter en premier</th><th>Statut</th></tr>
{% for it in items %}<tr><td>{% if it.analysis_id %}<a href="/app/analyses/{{ it.analysis_id }}">{{ it.title or it.source }}</a>{% else %}{{ it.title or it.source }}{% endif %}
<div class="faint xs break">{{ it.source }}</div></td>
<td class="num">{{ it.red if it.red is not none else '' }}</td><td class="num">{{ it.orange if it.orange is not none else '' }}</td><td class="num">{{ it.cond if it.cond is not none else '' }}</td>
<td class="muted small">{{ it.first|join(' · ') }}{% if it.error %}<span class="red">{{ it.error }}</span>{% endif %}</td>
<td><span class="tag {{ it.st[1] }}">{{ it.st[0] }}</span></td></tr>{% endfor %}</table></div></section>
{% endblock %}"""


# ---------------------------------------------------------------------------
# Lecture des entrées
# ---------------------------------------------------------------------------
URL_RE = re.compile(r"https?://[^\s<>\"',;]+", re.I)


def parse_urls(text: str) -> list[str]:
    return [u.rstrip(").]") for u in URL_RE.findall(text or "")]


def sitemap_urls(url: str, contains: str, limit: int) -> list[str]:
    """Lit un sitemap ou un index de sitemaps (10 sous-sitemaps au plus, ceux des produits et du blog d'abord)."""
    def locs(xml_text: str) -> tuple[list[str], bool]:
        try:
            root = etree.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text,
                                    parser=etree.XMLParser(resolve_entities=False, no_network=True, recover=True))
        except etree.XMLSyntaxError:
            return [], False
        if root is None:
            return [], False
        is_index = etree.QName(root).localname == "sitemapindex"
        return [e.text.strip() for e in root.iter() if etree.QName(e).localname == "loc" and e.text], is_index

    found, is_index = locs(m.fetch_url(url))
    if is_index:
        subs = sorted(found, key=lambda u: (0 if re.search(r"product|produit|blog|article", u, re.I) else 1))
        pages: list[str] = []
        for sub in subs[:10]:
            time.sleep(0.5)
            try:
                pages += locs(m.fetch_url(sub))[0]
            except Exception:
                continue
            if len([p for p in pages if contains in p]) >= limit:
                break
        found = pages
    return [u for u in found if (contains or "") in u][:limit]


def docx_html(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml")
    root = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    parts = []
    for p in root.iter("{%s}p" % ns["w"]):
        t = "".join(x.text or "" for x in p.iter("{%s}t" % ns["w"])).strip()
        if not t:
            continue
        style = p.find(".//w:pStyle", ns)
        lvl = style.get("{%s}val" % ns["w"]) if style is not None else ""
        tag = "h2" if re.match(r"(Heading|Titre)\s*[12]$", lvl or "", re.I) else "p"
        parts.append(f"<{tag}>{html_lib.escape(t)}</{tag}>")
    return "<div>" + "".join(parts) + "</div>"


def pdf_html(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    paras = []
    for page in reader.pages[:60]:
        text = page.extract_text() or ""
        for block in re.split(r"\n\s*\n", text):
            block = re.sub(r"\s*\n\s*", " ", block).strip()
            if block:
                paras.append(f"<p>{html_lib.escape(block)}</p>")
    return "<div>" + "".join(paras) + "</div>"


def file_items(name: str, data: bytes) -> list[tuple[str, str, str | None]]:
    """(kind, source, payload) : URL trouvées dans un fichier texte, ou contenu à analyser."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext == "docx":
        return [("FICHIER", name, docx_html(data))]
    if ext == "pdf":
        return [("FICHIER", name, pdf_html(data))]
    text = data.decode("utf-8", errors="replace")
    if ext in ("txt", "csv"):
        urls = parse_urls(text)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if urls and len(urls) >= max(1, len(lines) // 2):
            return [("URL", u, None) for u in urls]
    if ext in ("html", "htm"):
        return [("FICHIER", name, text)]
    return [("FICHIER", name, m.text_to_html(text))]


# ---------------------------------------------------------------------------
# Traitement
# ---------------------------------------------------------------------------
def _summary(result: dict) -> dict:
    return {"first": [f"« {p['quote']} »" for p in (result.get("priorities") or [])[:2]],
            "terms": [p["quote"].lower() for p in (result.get("priorities") or []) if p.get("quote")],
            "rejected": [x["name"] for x in (result.get("rejected") or [])],
            "product": (result.get("product") or {}).get("name")}


def process_audit(audit_id: int) -> None:
    with _slots:
        with m.ENGINE.connect() as c:
            a = c.execute(select(AUDITS).where(AUDITS.c.id == audit_id)).mappings().first()
            user = c.execute(select(m.USERS).where(m.USERS.c.id == a["user_id"])).mappings().first() if a else None
        if not a or not user:
            return
        admin = m.access(user)["admin"]
        last_fetch: dict[str, float] = {}
        while True:
            with m.ENGINE.connect() as c:
                it = c.execute(select(ITEMS).where(ITEMS.c.audit_id == audit_id, ITEMS.c.status.in_(("EN_ATTENTE", "EN_COURS")))
                               .order_by(ITEMS.c.position).limit(1)).mappings().first()
            if it is None:
                break
            with m.ENGINE.begin() as c:
                c.execute(update(ITEMS).where(ITEMS.c.id == it["id"]).values(status="EN_COURS"))
            use_ai = bool(a["use_ai"]) and m.AI_ENABLED and (admin or m.analyses_today(user["id"]) < m.AI_DAILY_LIMIT)
            try:
                if it["kind"] == "URL":
                    host = urlparse(it["source"]).hostname or ""
                    wait = 1.0 - (time.time() - last_fetch.get(host, 0))
                    if wait > 0:
                        time.sleep(wait)
                    last_fetch[host] = time.time()
                    result = m.run_analysis("url", "", it["source"], a["mode"], "", [], use_ai=use_ai)
                else:
                    result = m.run_analysis("texte", it["payload"] or "", "", a["mode"], "", [], use_ai=use_ai)
                    result["source"] = it["source"]
                result["audit_id"] = audit_id
                title = result.get("product_name") or it["source"]
                with m.ENGINE.begin() as c:
                    aid = c.execute(insert(m.ANALYSES).values(user_id=user["id"], title=title[:300],
                                                              result_json=json.dumps(result, ensure_ascii=False),
                                                              created_at=m.now())).inserted_primary_key[0]
                    counts = result.get("counts", {})
                    c.execute(update(ITEMS).where(ITEMS.c.id == it["id"]).values(
                        status="OK", analysis_id=aid, title=title[:300], red=counts.get("ROUGE", 0),
                        orange=counts.get("ORANGE", 0), cond=counts.get("CONDITIONNEL", 0),
                        summary_json=json.dumps(_summary(result), ensure_ascii=False), payload=None))
            except m.FetchError as exc:
                _fail(it["id"], f"Page non récupérée : {exc}")
            except httpx.HTTPError:
                _fail(it["id"], "Page non récupérée (délai dépassé ou connexion refusée)")
            except Exception as exc:  # une page en échec n'arrête pas l'audit
                _fail(it["id"], f"Erreur d'analyse : {type(exc).__name__}")
        with m.ENGINE.begin() as c:
            c.execute(update(AUDITS).where(AUDITS.c.id == audit_id).values(status="TERMINE", finished_at=m.now()))


def _fail(item_id: int, msg: str) -> None:
    with m.ENGINE.begin() as c:
        c.execute(update(ITEMS).where(ITEMS.c.id == item_id).values(status="ERREUR", error=msg[:500], payload=None))


def start(audit_id: int) -> None:
    if os.environ.get("SYNC_JOBS") == "1":
        process_audit(audit_id)
    else:
        threading.Thread(target=process_audit, args=(audit_id,), daemon=True).start()


def resume_all() -> None:
    with m.ENGINE.connect() as c:
        ids = [r[0] for r in c.execute(select(AUDITS.c.id).where(AUDITS.c.status == "EN_COURS")).all()]
    for i in ids:
        start(i)


if os.environ.get("SYNC_JOBS") != "1":
    threading.Thread(target=lambda: (time.sleep(10), resume_all()), daemon=True).start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _audits_of(user_id: int) -> list[dict]:
    with m.ENGINE.connect() as c:
        rows = c.execute(select(AUDITS).where(AUDITS.c.user_id == user_id).order_by(desc(AUDITS.c.id)).limit(20)).mappings().all()
        stats = dict(((r[0], r[1:]) for r in c.execute(
            select(ITEMS.c.audit_id, func.count(), func.sum(case((ITEMS.c.status.in_(("OK", "ERREUR")), 1), else_=0)))
            .where(ITEMS.c.audit_id.in_([r["id"] for r in rows] or [0])).group_by(ITEMS.c.audit_id)).all()))
    return [{"id": r["id"], "title": r["title"], "date": m.aware(r["created_at"]).strftime("%d/%m/%Y %H:%M"),
             "total": stats.get(r["id"], (0, 0))[0], "done": stats.get(r["id"], (0, 0))[1] or 0,
             "status": "Terminé" if r["status"] == "TERMINE" else "En cours"} for r in rows]


def _form_page(request, user, error=None, form=None, status=200):
    cap = MAX_PAGES_ADMIN if m.access(user)["admin"] else MAX_PAGES_TRIAL
    return m.render(request, "audit_form", status, title="Audit de site", error=error, form=form or {}, cap=cap,
                    audits=_audits_of(user["id"]), ai_enabled=m.AI_ENABLED, ai_limit=m.AI_DAILY_LIMIT)


@m.app.get("/app/audit", response_class=HTMLResponse)
def audit_form(request: Request):
    user, stop = m.guard(request)
    if stop:
        return stop
    return _form_page(request, user)


@m.app.post("/app/audit", response_class=HTMLResponse)
async def audit_create(request: Request):
    user, stop = m.guard(request)
    if stop:
        return stop
    form = await request.form()
    if not m.csrf_ok(request, form.get("csrf", "")):
        return m.redirect("/app/audit")
    cap = MAX_PAGES_ADMIN if m.access(user)["admin"] else MAX_PAGES_TRIAL
    try:
        limit = max(1, min(cap, int(form.get("max") or 50)))
    except ValueError:
        limit = 50
    keep = {k: form.get(k) for k in ("title", "urls", "sitemap", "contains", "max")}
    items: list[tuple[str, str, str | None]] = [("URL", u, None) for u in parse_urls(form.get("urls") or "")]
    if (form.get("sitemap") or "").strip():
        try:
            items += [("URL", u, None) for u in sitemap_urls(form.get("sitemap").strip(), (form.get("contains") or "").strip(), limit)]
        except m.FetchError as exc:
            return _form_page(request, user, f"Sitemap illisible : {exc}", keep, 400)
        except httpx.HTTPError:
            return _form_page(request, user, "Sitemap inaccessible (délai dépassé ou connexion refusée).", keep, 400)
    for up in form.getlist("files"):
        if not getattr(up, "filename", None):
            continue
        data = await up.read()
        if len(data) > MAX_FILE_BYTES:
            return _form_page(request, user, f"Fichier trop volumineux : {up.filename} (10 Mo au plus).", keep, 400)
        try:
            items += file_items(up.filename, data)
        except Exception:
            return _form_page(request, user, f"Fichier illisible : {up.filename}.", keep, 400)
    seen, unique = set(), []
    for kind, src, payload in items:
        key = (kind, src)
        if key in seen:
            continue
        seen.add(key)
        unique.append((kind, src, payload))
    unique = unique[:limit]
    if not unique:
        return _form_page(request, user, "Indiquez au moins une URL, un sitemap ou un fichier.", keep, 400)
    mode = form.get("mode") if form.get("mode") in ("PRODUCT_PAGE", "EDITORIAL_BLOG") else "AUTO"
    title = (form.get("title") or "").strip() or (urlparse(unique[0][1]).hostname or unique[0][1]) + f" — {len(unique)} page(s)"
    with m.ENGINE.begin() as c:
        aid = c.execute(insert(AUDITS).values(user_id=user["id"], title=title[:300], status="EN_COURS",
                                              use_ai=1 if form.get("use_ai") else 0, mode=mode,
                                              created_at=m.now())).inserted_primary_key[0]
        c.execute(insert(ITEMS), [{"audit_id": aid, "position": i, "kind": k, "source": s[:2000], "payload": p,
                                   "status": "EN_ATTENTE"} for i, (k, s, p) in enumerate(unique)])
    start(aid)
    return m.redirect(f"/app/audits/{aid}")


def _load(request: Request, audit_id: int):
    user, stop = m.guard(request)
    if stop:
        return None, None, stop
    with m.ENGINE.connect() as c:
        a = c.execute(select(AUDITS).where(AUDITS.c.id == audit_id, AUDITS.c.user_id == user["id"])).mappings().first()
        rows = c.execute(select(ITEMS.c.id, ITEMS.c.position, ITEMS.c.source, ITEMS.c.status, ITEMS.c.analysis_id, ITEMS.c.title,
                                ITEMS.c.red, ITEMS.c.orange, ITEMS.c.cond, ITEMS.c.summary_json, ITEMS.c.error)
                         .where(ITEMS.c.audit_id == audit_id)).mappings().all() if a else []
    if not a:
        return None, None, m.render(request, "expired", 404, title="Introuvable")
    return a, [dict(r) for r in rows], None


def _sorted(rows: list[dict]) -> list[dict]:
    order = {"OK": 0, "ERREUR": 1, "EN_COURS": 2, "EN_ATTENTE": 3}
    return sorted(rows, key=lambda r: (order.get(r["status"], 4), -(r["red"] or 0), -(r["orange"] or 0), r["position"]))


@m.app.get("/app/audits/{audit_id:int}", response_class=HTMLResponse)
def audit_page(request: Request, audit_id: int):
    a, rows, stop = _load(request, audit_id)
    if stop:
        return stop
    terms, rejected = Counter(), Counter()
    for r in rows:
        s = json.loads(r["summary_json"]) if r["summary_json"] else {}
        r["first"] = s.get("first", [])
        r["st"] = STATUS.get(r["status"], (r["status"], "info"))
        terms.update(set(s.get("terms", [])))
        rejected.update(set(s.get("rejected", [])))
    done = sum(1 for r in rows if r["status"] in ("OK", "ERREUR"))
    total = len(rows)
    return m.render(request, "audit", title=a["title"], a={**a, "date": m.aware(a["created_at"]).strftime("%d/%m/%Y %H:%M")},
                    items=_sorted(rows), done=done, total=total, pct=int(100 * done / total) if total else 100,
                    errors=sum(1 for r in rows if r["status"] == "ERREUR"), running=a["status"] == "EN_COURS",
                    pages_red=sum(1 for r in rows if (r["red"] or 0) > 0), sum_red=sum(r["red"] or 0 for r in rows),
                    sum_orange=sum(r["orange"] or 0 for r in rows), sum_cond=sum(r["cond"] or 0 for r in rows),
                    top_terms=terms.most_common(15), rejected=rejected.most_common(10))


@m.app.get("/app/audits/{audit_id:int}.csv")
def audit_csv(request: Request, audit_id: int):
    a, rows, stop = _load(request, audit_id)
    if stop:
        return stop
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Page", "Source", "Statut", "À revoir en priorité", "À revoir", "À confirmer", "À traiter en premier",
                "Ingrédients sans allégation autorisée", "Analyse"])
    base = str(request.base_url).rstrip("/")
    for r in _sorted(rows):
        s = json.loads(r["summary_json"]) if r["summary_json"] else {}
        w.writerow([r["title"] or "", r["source"], STATUS.get(r["status"], (r["status"],))[0], r["red"] or 0, r["orange"] or 0,
                    r["cond"] or 0, " | ".join(s.get("first", [])) or (r["error"] or ""), ", ".join(s.get("rejected", [])),
                    f"{base}/app/analyses/{r['analysis_id']}" if r["analysis_id"] else ""])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="audit-{audit_id}.csv"'})
