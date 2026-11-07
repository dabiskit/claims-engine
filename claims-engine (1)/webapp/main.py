"""
webapp/main.py — Démo « expert » : application web complète en un seul fichier.

Fonctions
  * Essai gratuit : création de compte, accès complet pendant TRIAL_DAYS jours (sans carte bancaire).
  * Analyser : texte collé, HTML ou URL publique -> pré-analyse (lexique, règles, doses, art. 10§3,
    vigilance) + allégations utilisables pour la composition déclarée, avec leurs variantes.
  * Base réglementaire : allégations, conditions, variantes, lexique, mentions, tâches de revue ;
    chaque élément peut recevoir un avis de l'expert (Valider / À corriger / Rejeter + commentaire).
  * Mes avis : historique et export CSV. Admin (ADMIN_EMAILS) : comptes et export de tous les avis.

Variables d'environnement
  DATABASE_URL    Postgres (Supabase) ; à défaut, SQLite local (données perdues à chaque redéploiement)
  SESSION_SECRET  longue chaîne aléatoire (obligatoire en ligne)
  ADMIN_EMAILS    e-mails administrateurs, séparés par des virgules
  APP_NAME, TRIAL_DAYS (14 par défaut)

Limites de cette version (affichées dans l'application) : pas de réécriture par IA, pas de détection
des promesses implicites, pas d'allégations botaniques « on-hold », base en brouillon non validée.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import html as html_lib
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import DictLoader, Environment, select_autoescape
from markupsafe import Markup, escape
from sqlalchemy import (Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, desc, func,
                        insert, select)
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dataclasses import asdict  # noqa: E402
from lxml import html as lxml_html  # noqa: E402

from engine.conditions import evaluate_claim  # noqa: E402
from engine.dom_extractor import extract_page  # noqa: E402
from eval.pipeline_adapter import KnowledgeBase, ReplayPipeline, product_of  # noqa: E402

APP_NAME = os.environ.get("APP_NAME", "Pré-analyse des allégations")
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "14"))
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
MICRO_G = "\u00b5g"
UNITS = ["mg", MICRO_G, "g"]
USER_AGENT = "PreAnalyseAllegations/0.1 (demo)"

SEED = json.loads((ROOT / "seed" / "out" / "lot1.json").read_text(encoding="utf-8"))
KB = KnowledgeBase(SEED)
AI_ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY"))
AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "40"))


def make_pipeline():
    if AI_ENABLED:
        from eval.pipeline_adapter import LivePipeline
        return LivePipeline(KB)
    return ReplayPipeline(KB, {"cases": {}})


PIPE = make_pipeline()
GOLDEN = json.loads((ROOT / "eval" / "golden_set.json").read_text(encoding="utf-8"))
SUBSTANCES = {s["code"]: s for s in SEED["substances"]}
LEXICON = {e["code"]: e for e in SEED["lexical_entries"]}

# -----------------------------------------------------------------------------
# Libellés
# -----------------------------------------------------------------------------
LEVELS = {"ROUGE": ("À revoir en priorité", "red"), "ORANGE": ("À revoir", "orange"),
          "CONDITIONNEL": ("À confirmer", "cond"), "INFO": ("Information", "info")}
CATEGORY_LABELS = {
    "VERBE_THERAPEUTIQUE": "Propriété thérapeutique (prévenir, traiter, guérir) attribuée à un aliment",
    "VOCABULAIRE_PHARMACEUTIQUE": "Vocabulaire médical ou pharmaceutique",
    "PATHOLOGIE": "Référence à une maladie",
    "TERME_MEDICAL": "Terme médical",
    "ART12_PERTE_DE_POIDS": "Rythme ou ampleur de perte de poids",
    "ART12_PROFESSIONNEL_SANTE": "Recommandation d'un professionnel de santé",
    "ART12_NON_CONSOMMATION": "Suggère que la santé pâtirait de la non-consommation",
    "SUPERLATIF_PROMESSE": "Promesse de résultat ou superlatif",
    "DOCTRINAL_RISQUE": "Terme à risque selon la pratique des contrôles",
    "ALLEGATION_NON_AUTORISEE": "Allégation de santé sans correspondance autorisée",
}
CODE_LABELS = {
    "CONDITION_NON_REMPLIE": "Allégation autorisée, mais sa condition d'utilisation n'est pas remplie",
    "CONDITION_A_CONFIRMER": "Allégation reconnue : condition d'utilisation à confirmer",
    "MENTION_OBLIGATOIRE_ABSENTE": "Allégation autorisée, mais la mention obligatoire associée est absente",
    "ALLEGATION_SUBSTANCE_ABSENTE": "Allégation sur une substance absente de la composition déclarée",
    "REFERENCE_GENERALE_ART10_3": "Référence générale non accompagnée d'une allégation spécifique",
    "VIGILANCE_MENTION_ABSENTE": "Mentions de vigilance attendues pour un ingrédient, absentes de la page",
    "PRESENTATION_MEDICAMENTEUSE": "Accumulation de termes médicaux : risque de présentation comme un médicament",
    "ARTICLE_CIBLE_PATHOLOGIE_AVEC_PONT": "Article ciblant une maladie et relié à un produit",
    "ASSOCIATION_INGREDIENT_PATHOLOGIE": "Ingrédient vendu par la marque associé à une maladie",
    "IA_PROPRIETE_THERAPEUTIQUE": "Propriété thérapeutique ou référence à une maladie, formulée sans mot interdit",
    "IA_ALLEGATION_SANS_CORRESPONDANCE": "Allégation de santé sans allégation autorisée correspondante",
    "IA_REDUCTION_RISQUE_NON_AUTORISEE": "Allégation de réduction d'un risque de maladie non autorisée",
    "IA_ALLEGATION_BOTANIQUE_NON_COUVERTE": "Allégation sur une plante dont les allégations « on-hold » ne sont pas encore intégrées",
    "IA_FORMULATION_NON_FIDELE": "Allégation autorisée reprise avec une formulation renforcée",
    "IA_REFERENCE_GENERALE": "Référence générale non accompagnée d'une allégation spécifique",
    "IA_ALLEGATION_NUTRITIONNELLE": "Allégation nutritionnelle (conditions non vérifiées par cette version)",
    "IA_INDISPONIBLE": "Extraction IA indisponible : seules les règles et le lexique ont été appliqués",
    "IA_CITATIONS_REJETEES": "Citations proposées par l'IA mais introuvables dans la page : ignorées",
}
LEGAL_LABELS = {
    "INCO_1169_2011_ART7_3": "Règl. (UE) 1169/2011, art. 7§3",
    "R1924_2006_ART10_1": "Règl. (CE) 1924/2006, art. 10§1",
    "R1924_2006_ART10_3": "Règl. (CE) 1924/2006, art. 10§3",
    "R1924_2006_ART12_A": "Règl. (CE) 1924/2006, art. 12 a)",
    "R1924_2006_ART12_B": "Règl. (CE) 1924/2006, art. 12 b)",
    "R1924_2006_ART12_C": "Règl. (CE) 1924/2006, art. 12 c)",
    "CSP_L5111_1_PRESENTATION": "Code de la santé publique, art. L5111-1",
    "CCONSO_PRATIQUE_TROMPEUSE": "Code de la consommation (pratiques commerciales trompeuses)",
    "DOCTRINE_ADMINISTRATIVE": "Pratique de contrôle (à confirmer par l'expert)",
    "R432_2012_CONDITIONS": "Règl. (UE) 432/2012, conditions d'utilisation",
    "VIGILANCE_SANITAIRE": "Vigilance sanitaire (sources à vérifier)",
    "R1924_2006_ART28": "Règl. (CE) 1924/2006, art. 28 (régime transitoire des plantes)",
    "R1924_2006_ART8": "Règl. (CE) 1924/2006, art. 8 et annexe (allégations nutritionnelles)",
}
REMEDIATIONS = {
    "REECRIRE_ALLEGATION": "Remplacer par une allégation autorisée applicable au produit",
    "RENOMMER_INTITULE": "Renommer l'intitulé (ex. « Posologie » → « Conseils d'utilisation »)",
    "SUPPRIMER_PONT": "Retirer le lien, l'encart ou le bouton produit",
    "SCINDER_ARTICLE": "Scinder : article d'information d'un côté, article sur la fonction physiologique de l'autre",
    "RETIRER_WIDGET_GABARIT": "Retirer le widget produit du gabarit des articles",
    "RECADRER_VERS_PONT_SEMANTIQUE": "Recentrer sur l'intention physiologique (pont sémantique)",
    "DISSOCIER_INGREDIENT_PATHOLOGIE": "Ne pas associer un ingrédient vendu à une maladie dans un même passage",
    "TRAITER_LES_ROUGES_DE_LA_PAGE": "Traiter les points prioritaires de la page",
    "SUPPRIMER_ALLEGATION": "Supprimer l'allégation, ou atteindre la dose requise",
}
DECISIONS = {"VALIDE": "Validé", "A_CORRIGER": "À corriger", "REJETE": "Rejeté"}
USABILITY = {"UTILISABLE": ("Utilisable", "ok"), "CONDITIONNELLE": ("À confirmer", "cond"),
             "INUTILISABLE": ("Non utilisable", "red")}

# -----------------------------------------------------------------------------
# Base de données (comptes, analyses, avis de l'expert)
# -----------------------------------------------------------------------------
def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{ROOT / 'webapp' / 'demo.db'}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


ENGINE = create_engine(database_url(), pool_pre_ping=True)
MD = MetaData()
USERS = Table("demo_users", MD,
              Column("id", Integer, primary_key=True), Column("email", String(255), unique=True, nullable=False),
              Column("name", String(255), nullable=False), Column("pw_hash", String(255), nullable=False),
              Column("plan", String(32), nullable=False), Column("trial_ends_at", DateTime(timezone=True)),
              Column("created_at", DateTime(timezone=True), nullable=False))
ANALYSES = Table("demo_analyses", MD,
                 Column("id", Integer, primary_key=True), Column("user_id", Integer, nullable=False),
                 Column("title", String(300), nullable=False), Column("result_json", Text, nullable=False),
                 Column("created_at", DateTime(timezone=True), nullable=False))
REVIEWS = Table("demo_reviews", MD,
                Column("id", Integer, primary_key=True), Column("user_id", Integer, nullable=False),
                Column("target_type", String(40), nullable=False), Column("target_code", String(120), nullable=False),
                Column("decision", String(20), nullable=False), Column("comment", Text),
                Column("created_at", DateTime(timezone=True), nullable=False))
MD.create_all(ENGINE)


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def hash_password(pw: str) -> str:
    salt, it = secrets.token_hex(16), 200_000
    return f"pbkdf2${it}${salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), it).hex()}"


def check_password(pw: str, stored: str) -> bool:
    _, it, salt, h = stored.split("$")
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex(), h)


# -----------------------------------------------------------------------------
# Application, session, accès
# -----------------------------------------------------------------------------
app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=False,
                   max_age=60 * 60 * 24 * 30)


def current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    with ENGINE.connect() as c:
        return c.execute(select(USERS).where(USERS.c.id == uid)).mappings().first()


def access(user) -> dict:
    if user is None:
        return {"active": False, "admin": False, "days_left": 0}
    admin = user["email"].lower() in ADMIN_EMAILS
    ends = aware(user["trial_ends_at"])
    active = admin or (ends is not None and now() < ends)
    days = max(0, (ends - now()).days + 1) if ends else 0
    return {"active": active, "admin": admin, "days_left": days}


def csrf(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


def csrf_ok(request: Request, token: str) -> bool:
    return bool(token) and hmac.compare_digest(token, request.session.get("csrf", ""))


def safe_next(path: str, default: str = "/app") -> str:
    return path if path.startswith("/app/") or path == "/app" else default


def latest_reviews(user_id: int) -> dict:
    with ENGINE.connect() as c:
        rows = c.execute(select(REVIEWS).where(REVIEWS.c.user_id == user_id).order_by(REVIEWS.c.created_at)).mappings().all()
    out = {}
    for r in rows:
        out[f"{r['target_type']}:{r['target_code']}"] = {
            "decision": r["decision"], "label": DECISIONS.get(r["decision"], r["decision"]),
            "comment": r["comment"], "date": aware(r["created_at"]).strftime("%d/%m %H:%M")}
    return out


# -----------------------------------------------------------------------------
# Analyse
# -----------------------------------------------------------------------------
class FetchError(Exception):
    pass


def _check_host(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchError("Adresse invalide : elle doit commencer par http:// ou https://")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        raise FetchError("Nom de domaine introuvable")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise FetchError("Adresse non autorisée")


def fetch_url(url: str) -> str:
    """Récupère une page publique (sans exécution de JavaScript), avec garde-fous : pas d'adresse
    interne, 3 redirections au plus, 3 Mo au plus."""
    for _ in range(4):
        _check_host(url)
        with httpx.Client(follow_redirects=False, timeout=15, headers={"User-Agent": USER_AGENT}) as client:
            r = client.get(url)
        if r.is_redirect and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            continue
        if r.status_code >= 400:
            raise FetchError(f"La page a répondu {r.status_code}")
        if len(r.content) > 3_000_000:
            raise FetchError("Page trop volumineuse")
        return r.text
    raise FetchError("Trop de redirections")


def text_to_html(text: str) -> str:
    if re.search(r"<[a-zA-Z][^>]*>", text):
        return text
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    return "<div>" + "".join(f"<p>{html_lib.escape(p)}</p>" for p in paragraphs) + "</div>"


def highlight(text: str, quotes: list[tuple[str, str]]) -> Markup:
    """Échappe le texte puis surligne chaque citation (première occurrence, insensible à la casse)."""
    out = str(escape(text))
    for quote, css in sorted(quotes, key=lambda q: -len(q[0])):
        if not quote:
            continue
        q = str(escape(quote))
        m = re.search(re.escape(q), out, re.IGNORECASE)
        if m and "<mark" not in out[max(0, m.start() - 30):m.start()]:
            out = out[:m.start()] + f'<mark class="{css}">' + out[m.start():m.end()] + "</mark>" + out[m.end():]
    return Markup(out)


def explain(f) -> str:
    entry = LEXICON.get(f.lexical_code or f.code)
    if entry:
        label = CATEGORY_LABELS.get(entry["category"], entry["category"])
        if entry["category"] == "ALLEGATION_NON_AUTORISEE" and entry.get("substance") in SUBSTANCES:
            label += f" (motif typique : {SUBSTANCES[entry['substance']]['name_fr']})"
        return label + (f". {entry['note']}" if entry.get("note") else "")
    return CODE_LABELS.get(f.code, f.code)


def run_analysis(source: str, text: str, url: str, mode: str, product_name: str, composition: list[dict]) -> dict:
    page_url = url if source == "url" else (
        "https://exemple.invalid/products/demo" if mode == "PRODUCT_PAGE" else "https://exemple.invalid/blogs/demo")
    html = fetch_url(url) if source == "url" else text_to_html(text)
    case = {"test_id": "demo", "page_mode": mode, "page_url": page_url,
            "product_names": [product_name] if product_name else [], "portions_per_day": 1,
            "product_composition": composition if mode == "PRODUCT_PAGE" else [],
            "linked_products": [] if mode == "PRODUCT_PAGE" else
            [{"path": "/products/produit-lie", "name": product_name or "produit", "composition": composition}]}
    analysis = PIPE.analyze(case, html)
    doc = html if html.lstrip()[:200].lower().startswith(("<!doctype", "<html")) else \
        f"<html><head><title></title></head><body><main>{html}</main></body></html>"
    page = extract_page(doc, page_url)
    tree = lxml_html.document_fromstring(doc).getroottree()
    flagged_keys = {f.node_key for f in analysis.findings if f.node_key and f.level in ("ROUGE", "ORANGE")}
    red_keys = {f.node_key for f in analysis.findings if f.node_key and f.level == "ROUGE"}
    nodes_index = {}
    for n in page.nodes:
        if n.key in flagged_keys:
            el = tree.xpath(n.host_xpath)
            nodes_index[n.key] = {"text": n.text_plain, "zone": n.zone, "in_description": n.in_description,
                                  "shopify_section": n.shopify_section, "has_red": n.key in red_keys,
                                  "host_html": lxml_html.tostring(el[0], encoding="unicode", with_tail=False) if el else None}

    nodes: dict[str, dict] = {}
    page_level = []
    counts = {k: 0 for k in LEVELS}
    for f in analysis.findings:
        counts[f.level] = counts.get(f.level, 0) + 1
        item = {"code": f.code, "ai": f.code.startswith("IA_"), "level": f.level, "level_label": LEVELS.get(f.level, (f.level, ""))[0],
                "css": LEVELS.get(f.level, ("", "info"))[1], "quote": f.quote, "explanation": explain(f),
                "legal": LEGAL_LABELS.get(f.legal_basis or "", f.legal_basis or ""),
                "substance": SUBSTANCES.get(f.substance or "", {}).get("name_fr"),
                "remediations": [REMEDIATIONS.get(r, r) for r in dict.fromkeys(f.remediation_codes)]}
        if f.node_key is None:
            page_level.append(item)
        else:
            nodes.setdefault(f.node_key, {"text": f.node_text, "findings": []})["findings"].append(item)

    recognized = [{"code": r.claim_code, "text": KB.claims[r.claim_code]["text_fr"],
                   "status": USABILITY.get(r.usability, (r.usability, "info"))} for r in analysis.recognized_claims]

    product = product_of(case, KB, composition)
    usable = []
    for item in composition:
        code = item["substance"]
        if code not in SUBSTANCES:
            continue
        claims = []
        for ccode, claim in KB.claims.items():
            if claim["substance"] != code:
                continue
            ev = evaluate_claim(ccode, code, KB.conditions.get(ccode, []), product)
            claims.append({"code": ccode, "text": claim["text_fr"], "status": USABILITY[ev.usability.value],
                           "reasons": [r.reason_fr for r in ev.results if r.condition.type != "MENTION_OBLIGATOIRE"],
                           "notices": [t for _, t in ev.required_notices],
                           "variants": [v["text_fr"] for v in KB.variants.get(ccode, []) if not v["is_official_wording"]]})
        usable.append({"substance": SUBSTANCES[code]["name_fr"], "amount": item["amount"], "unit": item["unit"],
                       "claims": claims, "pending_onhold": SUBSTANCES[code]["kind"] == "PLANTE"})

    return {"source": url if source == "url" else "Texte collé", "mode": mode, "product_name": product_name,
            "composition": composition, "counts": counts,
            "nodes": [{"html": str(highlight(n["text"], [(i["quote"], i["css"]) for i in n["findings"]])),
                       "findings": n["findings"]} for n in nodes.values()],
            "page_level": page_level, "recognized": recognized, "usable": usable,
            "kb_missing": analysis.kb_missing_substances, "kb_release": KB.release,
            "created": now().strftime("%d/%m/%Y %H:%M"), "flagged": len(nodes_index), "ai": AI_ENABLED,
            "has_points": any(f.level != "INFO" for f in analysis.findings),
            "models": sorted({u.model for u in analysis.usage}), "usage": [asdict(u) for u in analysis.usage],
            "raw": {"case": case, "findings": [asdict(f) for f in analysis.findings],
                    "recognized": [asdict(r) for r in analysis.recognized_claims],
                    "kb_missing": analysis.kb_missing_substances, "nodes_index": nodes_index}}


def analyses_today(user_id: int) -> int:
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    with ENGINE.connect() as c:
        return c.execute(select(func.count()).select_from(ANALYSES).where(ANALYSES.c.user_id == user_id,
                                                                            ANALYSES.c.created_at >= start)).scalar() or 0


def parse_composition(form) -> list[dict]:
    comp = []
    for i in range(1, 6):
        code = (form.get(f"sub_{i}") or "").strip()
        raw = (form.get(f"amount_{i}") or "").strip().replace(",", ".")
        unit = form.get(f"unit_{i}") or "mg"
        if not code or code not in SUBSTANCES or not raw:
            continue
        try:
            amount = float(raw)
        except ValueError:
            continue
        if amount <= 0 or unit not in UNITS:
            continue
        comp.append({"substance": code, "label": SUBSTANCES[code]["name_fr"], "amount": amount, "unit": unit,
                     "plant_part": None, "preparation": None, "der_ratio": None})
    return comp


def examples() -> list[dict]:
    out = []
    for cid, title in (("GS-001", "Fiche mélatonine (promesses frontales)"), ("GS-002", "Fiche magnésium sous-dosée"),
                       ("GS-003", "Badge « détox, immunité, vitalité »"), ("GS-008", "Article de blog avec lien produit"),
                       ("GS-010", "Fiche déjà correcte (aucun point attendu)")):
        case = next(c for c in GOLDEN["cases"] if c["test_id"] == cid)
        comp = case["product_composition"] or [i for p in case.get("linked_products", []) for i in p["composition"]]
        out.append({"id": cid, "title": title, "text": case["input_html_nodes"], "mode": case["page_mode"],
                    "product_name": (case.get("product_names") or [""])[0],
                    "composition": [c for c in comp if c["substance"] in SUBSTANCES]})
    return out


EXAMPLES = examples()

# -----------------------------------------------------------------------------
# Gabarits
# -----------------------------------------------------------------------------
CSS = """
:root{--bg:#fafaf8;--card:#fff;--fg:#1d1d1b;--muted:#66655f;--line:#e4e2dc;--accent:#1f5f8b;--red:#b42318;--redbg:#fde8e6;
--orange:#b54708;--orangebg:#fef0e1;--cond:#6941c6;--condbg:#f1ecfd;--ok:#067647;--okbg:#e6f6ee;--infobg:#eef2f6}
@media (prefers-color-scheme: dark){:root{--bg:#141413;--card:#1c1c1a;--fg:#ecebe6;--muted:#a3a29c;--line:#2f2e2b;--accent:#7cb4dc;
--red:#ff8a80;--redbg:#3b1714;--orange:#ffb870;--orangebg:#3a2410;--cond:#c3b1ff;--condbg:#251c3d;--ok:#6fd3a0;--okbg:#0f2e20;--infobg:#1d2630}}
*{box-sizing:border-box}body{margin:0;font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--fg)}
a{color:var(--accent)}header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 24px;border-bottom:1px solid var(--line);background:var(--card)}
header .brand{font-weight:700;text-decoration:none;color:var(--fg)}nav{display:flex;gap:16px;align-items:center;flex-wrap:wrap}nav form{margin:0}
main{max-width:1100px;margin:0 auto;padding:24px}.banner{background:var(--orangebg);color:var(--fg);padding:8px 24px;font-size:14px}
.trial{background:var(--okbg);padding:6px 24px;font-size:14px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin:0 0 16px}
.btn,button{display:inline-block;background:var(--accent);color:#fff;border:0;border-radius:8px;padding:9px 16px;font:inherit;cursor:pointer;text-decoration:none}
button.link{background:none;color:var(--accent);padding:0}input,select,textarea{font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);max-width:100%}
textarea{width:100%;min-height:200px;font-family:ui-monospace,Menlo,monospace;font-size:13px}label{display:block;margin:10px 0 4px;font-weight:600}
table{border-collapse:collapse;width:100%}th,td{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid var(--line)}.scroll{overflow-x:auto}
.tag{display:inline-block;border-radius:999px;padding:1px 10px;font-size:13px;font-weight:600}.red{background:var(--redbg);color:var(--red)}
.orange{background:var(--orangebg);color:var(--orange)}.cond{background:var(--condbg);color:var(--cond)}.ok{background:var(--okbg);color:var(--ok)}.info{background:var(--infobg)}
mark.red{background:var(--redbg);color:var(--red);font-weight:600}mark.orange{background:var(--orangebg);color:var(--orange)}mark.cond{background:var(--condbg)}mark.info{background:var(--infobg)}
.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:end}
form.review{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px}form.review input[name=comment]{flex:1;min-width:180px}
.error{background:var(--redbg);color:var(--red);padding:10px;border-radius:8px}.count{font-size:28px;font-weight:700}
"""

T = {
"base": """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} — {{ app_name }}</title><meta name="robots" content="noindex,nofollow"><style>{{ css }}</style></head><body>
<header><a class="brand" href="/">{{ app_name }}</a><nav>
{% if user %}<a href="/app">Analyser</a><a href="/app/base">Base réglementaire</a><a href="/app/avis">Mes avis</a><a href="/tarifs">Tarifs</a>
{% if acc.admin %}<a href="/admin">Admin</a>{% endif %}
<form method="post" action="/deconnexion"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="link">Se déconnecter</button></form>
{% else %}<a href="/tarifs">Tarifs</a><a href="/connexion">Connexion</a><a class="btn" href="/essai">Essai gratuit</a>{% endif %}</nav></header>
<div class="banner">Version de démonstration. Base réglementaire en brouillon, en cours de validation par un expert. Pré-analyse automatique et non exhaustive : ne constitue pas un avis juridique.</div>
{% if user and acc.active and not acc.admin %}<div class="trial">Essai gratuit : accès complet, encore {{ acc.days_left }} jour(s).</div>{% endif %}
<main>{% block content %}{% endblock %}</main></body></html>""",

"macros": """{% macro review(t, c, latest, csrf, next) -%}
<form method="post" action="/app/avis" class="review"><input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="target_type" value="{{ t }}"><input type="hidden" name="target_code" value="{{ c }}"><input type="hidden" name="next" value="{{ next }}">
<select name="decision" aria-label="Décision"><option value="VALIDE">Valider</option><option value="A_CORRIGER">À corriger</option><option value="REJETE">Rejeter</option></select>
<input name="comment" placeholder="Commentaire (facultatif)" maxlength="2000"><button>Enregistrer</button>
{% if latest %}<span class="tag {{ {'VALIDE':'ok','A_CORRIGER':'orange','REJETE':'red'}[latest.decision] }}">{{ latest.label }} · {{ latest.date }}</span>
{% if latest.comment %}<span class="muted">« {{ latest.comment }} »</span>{% endif %}{% endif %}</form>
{%- endmacro %}""",

"landing": """{% extends "base" %}{% block content %}
<div class="card"><h1>Repérez les allégations santé à risque, sans perdre vos mots-clés</h1>
<p>Collez une fiche produit, un article ou une URL : l'outil repère les formulations à revoir au regard des règlements européens sur les allégations, vérifie les conditions de dose et propose les allégations autorisées applicables à votre composition.</p>
<p><a class="btn" href="/essai">Essai gratuit : toutes les fonctionnalités pendant {{ trial_days }} jours</a> <span class="muted">Sans carte bancaire.</span></p></div>
<div class="grid"><div class="card"><h3>Pré-analyse</h3><p>Lexique, conditions de dose, mentions obligatoires, références générales, vigilance.</p></div>
<div class="card"><h3>Allégations utilisables</h3><p>Pour chaque ingrédient déclaré : les allégations autorisées, leur condition, et des formulations validées.</p></div>
<div class="card"><h3>Base réglementaire</h3><p>Consultable et commentable : chaque élément peut être validé, corrigé ou rejeté par un expert.</p></div></div>
{% endblock %}""",

"signup": """{% extends "base" %}{% block content %}<div class="card" style="max-width:520px">
<h1>Essai gratuit</h1><p>Accès complet à toutes les fonctionnalités pendant {{ trial_days }} jours. Sans carte bancaire.</p>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/essai"><input type="hidden" name="csrf" value="{{ csrf }}">
<label for="name">Nom</label><input id="name" name="name" required maxlength="200" value="{{ form.name or '' }}">
<label for="email">E-mail professionnel</label><input id="email" type="email" name="email" required maxlength="200" value="{{ form.email or '' }}">
<label for="password">Mot de passe (10 caractères minimum)</label><input id="password" type="password" name="password" required minlength="10">
<label><input type="checkbox" name="terms" value="1" required> J'ai compris qu'il s'agit d'une version de démonstration et que la pré-analyse ne constitue pas un avis juridique.</label>
<p><button>Démarrer l'essai</button></p></form><p class="muted">Déjà un compte ? <a href="/connexion">Connexion</a></p></div>{% endblock %}""",

"login": """{% extends "base" %}{% block content %}<div class="card" style="max-width:460px"><h1>Connexion</h1>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/connexion"><input type="hidden" name="csrf" value="{{ csrf }}">
<label for="email">E-mail</label><input id="email" type="email" name="email" required>
<label for="password">Mot de passe</label><input id="password" type="password" name="password" required>
<p><button>Se connecter</button></p></form></div>{% endblock %}""",

"expired": """{% extends "base" %}{% block content %}<div class="card"><h1>Votre essai est terminé</h1>
<p>Vos avis restent consultables et exportables depuis <a href="/app/avis">Mes avis</a>. Pour prolonger l'accès, contactez-nous.</p></div>{% endblock %}""",

"app": """{% extends "base" %}{% block content %}
<div class="card"><h1>Analyser une page</h1>
<p class="muted">Exemples prêts à l'emploi :
{% for e in examples %}<a href="/app?exemple={{ e.id }}">{{ e.title }}</a>{% if not loop.last %} · {% endif %}{% endfor %}</p>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/app/analyser"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Source</label>
<label style="font-weight:400"><input type="radio" name="source" value="texte" {% if src != 'url' %}checked{% endif %}> Texte ou HTML collé</label>
<label style="font-weight:400"><input type="radio" name="source" value="url" {% if src == 'url' %}checked{% endif %}> URL publique (sans exécution du JavaScript de la page)</label>
<label for="text">Texte ou HTML</label><textarea id="text" name="text">{{ prefill.text }}</textarea>
<label for="url">URL</label><input id="url" name="url" type="url" style="width:100%" placeholder="https://…">
<div class="row"><div><label for="mode">Type de page</label><select id="mode" name="mode">
<option value="PRODUCT_PAGE" {% if prefill.mode == 'PRODUCT_PAGE' %}selected{% endif %}>Fiche produit</option>
<option value="EDITORIAL_BLOG" {% if prefill.mode == 'EDITORIAL_BLOG' %}selected{% endif %}>Article de blog</option></select></div>
<div><label for="product_name">Nom du produit (facultatif)</label><input id="product_name" name="product_name" value="{{ prefill.product_name }}"></div></div>
<label>Composition par portion journalière (facultatif, sert à vérifier les doses)</label>
<div class="scroll"><table><tr><th>Ingrédient</th><th>Quantité</th><th>Unité</th></tr>
{% for i in range(1, 6) %}{% set c = prefill.composition[i-1] if prefill.composition|length >= i else None %}<tr>
<td><select name="sub_{{ i }}" aria-label="Ingrédient {{ i }}"><option value="">—</option>{% for s in substances %}<option value="{{ s.code }}" {% if c and c.substance == s.code %}selected{% endif %}>{{ s.name_fr }}</option>{% endfor %}</select></td>
<td><input name="amount_{{ i }}" inputmode="decimal" size="8" aria-label="Quantité {{ i }}" value="{{ c.amount if c else '' }}"></td>
<td><select name="unit_{{ i }}" aria-label="Unité {{ i }}">{% for u in units %}<option {% if c and c.unit == u %}selected{% endif %}>{{ u }}</option>{% endfor %}</select></td></tr>{% endfor %}</table></div>
<p><button>Lancer la pré-analyse</button></p></form></div>
{% if recent %}<div class="card"><h2>Analyses récentes</h2><ul>{% for a in recent %}<li><a href="/app/analyses/{{ a.id }}">{{ a.title }}</a> <span class="muted">{{ a.date }}</span></li>{% endfor %}</ul></div>{% endif %}
{% endblock %}""",

"result": """{% extends "base" %}{% block content %}
<p><a href="/app">← Nouvelle analyse</a></p>
<div class="card"><h1>Résultat de la pré-analyse</h1><p class="muted">{{ r.source }} · {{ 'Fiche produit' if r.mode == 'PRODUCT_PAGE' else 'Article de blog' }} · base {{ r.kb_release }} · {{ r.created }}</p>
<div class="grid">{% for k, lbl in levels.items() if k != 'INFO' %}<div><span class="tag {{ lbl[1] }}">{{ lbl[0] }}</span><div class="count">{{ r.counts.get(k, 0) }}</div></div>{% endfor %}</div>
{% if not r.has_points %}<p>Aucun passage relevé par la pré-analyse automatique sur ce périmètre. Cela ne vaut pas validation.</p>{% endif %}
{% if r.kb_missing %}<p class="muted">Ingrédients hors de la base actuelle : {{ r.kb_missing|join(', ') }}.</p>{% endif %}
{% if r.flagged %}<p>{% if ai_enabled %}<form method="post" action="/app/analyses/{{ aid }}/reecrire" style="display:inline"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Proposer des réécritures (IA)</button></form> {% endif %}<a class="btn" href="/app/analyses/{{ aid }}/propositions">Propositions et décisions</a></p>{% endif %}
<p><a href="/app/analyses/{{ aid }}/rapport">Rapport imprimable</a></p></div>
{% for n in r.nodes %}<div class="card"><p>{{ n.html|safe }}</p>
{% for f in n.findings %}<div style="margin:8px 0"><span class="tag {{ f.css }}">{{ f.level_label }}</span>{% if f.ai %} <span class="tag info">IA</span>{% endif %} <strong>« {{ f.quote }} »</strong> — {{ f.explanation }}
{% if f.legal %}<div class="muted">Fondement : {{ f.legal }}</div>{% endif %}
{% if f.remediations %}<div class="muted">Pistes : {{ f.remediations|join(' · ') }}</div>{% endif %}</div>{% endfor %}</div>{% endfor %}
{% if r.page_level %}<div class="card"><h2>Constats sur l'ensemble de la page</h2>{% for f in r.page_level %}
<p><span class="tag {{ f.css }}">{{ f.level_label }}</span> {{ f.explanation }}{% if f.substance %} ({{ f.substance }}){% endif %}{% if f.legal %} <span class="muted">— {{ f.legal }}</span>{% endif %}</p>{% endfor %}</div>{% endif %}
{% if r.recognized %}<div class="card"><h2>Allégations autorisées reconnues sur la page</h2><ul>{% for c in r.recognized %}
<li><span class="tag {{ c.status[1] }}">{{ c.status[0] }}</span> {{ c.text }} <span class="muted">({{ c.code }})</span></li>{% endfor %}</ul></div>{% endif %}
{% for u in r.usable %}<div class="card"><h2>{{ u.substance }} — {{ u.amount }} {{ u.unit }} par jour</h2>
{% if u.pending_onhold %}<p class="muted">Plante : les allégations « on-hold » ne sont pas encore intégrées à la base.</p>{% endif %}
{% for c in u.claims %}<div style="margin:10px 0"><span class="tag {{ c.status[1] }}">{{ c.status[0] }}</span> <strong>{{ c.text }}</strong>
{% if c.reasons %}<div class="muted">{{ c.reasons|join(' · ') }}</div>{% endif %}
{% if c.notices %}<div class="muted">Mention à afficher : {{ c.notices|join(' / ') }}</div>{% endif %}
{% if c.variants and c.status[1] != 'red' %}<details><summary>Formulations validées ({{ c.variants|length }})</summary><ul>{% for v in c.variants %}<li>{{ v }}</li>{% endfor %}</ul></details>{% endif %}</div>{% endfor %}</div>{% endfor %}
<div class="card muted"><h3>Limites de cette version</h3>{% if r.ai %}<p>Extraction et réécriture IA actives ({{ r.models|join(', ') }}). Les allégations botaniques « on-hold » ne sont pas encore intégrées. La base réglementaire est en brouillon, en cours de validation.</p>{% else %}<p>L'IA n'est pas activée sur cette instance : seules les règles et le lexique ont été appliqués, les promesses implicites ne sont pas détectées.</p>{% endif %}</div>
{% endblock %}""",

"base_index": """{% extends "base" %}{% block content %}<div class="card"><h1>Base réglementaire</h1>
<p class="muted">Release {{ release }} — brouillon. Chaque élément peut recevoir votre avis ; tout est exportable depuis <a href="/app/avis">Mes avis</a>.</p>
<p><a href="/app/base/lexique">Lexique des formulations à risque ({{ n_lex }})</a> · <a href="/app/base/mentions">Mentions ({{ n_notices }})</a> · <a href="/app/base/taches">Tâches de revue ({{ n_tasks }})</a></p></div>
<div class="card scroll"><table><tr><th>Ingrédient</th><th>Type</th><th>Allégations</th><th>Formulations</th><th>Motifs à risque</th><th>Avis donnés</th></tr>
{% for s in rows %}<tr><td><a href="/app/base/{{ s.code }}">{{ s.name }}</a>{% if s.latin %}<div class="muted"><em>{{ s.latin }}</em></div>{% endif %}</td><td>{{ s.kind }}</td><td>{{ s.claims }}</td><td>{{ s.variants }}</td><td>{{ s.risks }}</td><td>{{ s.reviewed }}</td></tr>{% endfor %}</table></div>{% endblock %}""",

"substance": """{% import "macros" as m %}{% extends "base" %}{% block content %}
<p><a href="/app/base">← Base réglementaire</a></p>
<div class="card"><h1>{{ s.name_fr }}</h1>{% if s.scientific_name %}<p><em>{{ s.scientific_name }}</em> · {{ s.botanical_family }}</p>{% endif %}
{% if s.nrv_value %}<p>VNR : {{ s.nrv_value }} {{ s.nrv_unit }}</p>{% endif %}
<p class="muted">Alias reconnus : {{ s.aliases|map(attribute='alias')|join(', ') }}</p>{{ m.review('substance', s.code, rv.get('substance:' ~ s.code), csrf, here) }}</div>
{% if claims %}<div class="card"><h2>Allégations ({{ claims|length }})</h2>{% for c in claims %}
<div id="{{ c.code }}" style="border-top:1px solid var(--line);padding:12px 0"><span class="tag {{ 'ok' if c.confidence == 'HAUTE' else 'orange' }}">confiance de saisie : {{ c.confidence }}</span>
<p><strong>{{ c.text_fr }}</strong> <span class="muted">({{ c.code }} · {{ c.regime }} · {{ c.text_fr_status }})</span></p>
<p class="muted">Conditions : {% for k in c.conditions %}{{ k.type }}{% if k.params.amount is defined %} {{ k.params.amount }} {{ k.params.unit }} ({{ k.params.basis }}){% endif %}{% if k.notice %} — mention : « {{ k.notice }} »{% endif %}{% if not loop.last %} ; {% endif %}{% endfor %}</p>
<details><summary>Formulations ({{ c.variants|length }})</summary><ul>{% for v in c.variants %}<li>{{ v.text_fr }} <span class="muted">({{ v.code }})</span></li>{% endfor %}</ul></details>
{{ m.review('allegation', c.code, rv.get('allegation:' ~ c.code), csrf, here ~ '#' ~ c.code) }}</div>{% endfor %}</div>{% endif %}
{% if worklist %}<div class="card"><h2>Allégations « on-hold » à importer</h2><p class="muted">Aucune n'est intégrée. Thèmes à rechercher dans la liste officielle :</p>
{% for w in worklist %}<div id="{{ w.code }}" style="padding:6px 0"><strong>{{ w.theme_fr }}</strong> (partie : {{ w.plant_part }}, confiance : {{ w.theme_confidence }}) <span class="muted">— formulations de marché, détection uniquement : {{ w.market_phrasings_detection_only|join(' / ') }}</span>
{{ m.review('liste_botanique', w.code, rv.get('liste_botanique:' ~ w.code), csrf, here ~ '#' ~ w.code) }}</div>{% endfor %}</div>{% endif %}
{% if restrictions %}<div class="card"><h2>Vigilance</h2>{% for r in restrictions %}<div id="vig-{{ loop.index }}" style="padding:6px 0">
<p>{{ r.populations|join(', ') }} — {{ r.kind }}{% if r.detail %} — {{ r.detail }}{% endif %}</p><p class="muted">Mention proposée : « {{ r.notice_text }} » · Source : {{ r.source_ref }}</p>
{{ m.review('vigilance', s.code ~ '-' ~ r.notice, rv.get('vigilance:' ~ s.code ~ '-' ~ r.notice), csrf, here) }}</div>{% endfor %}</div>{% endif %}
{% if risks %}<div class="card"><h2>Formulations à risque propres à cet ingrédient</h2>{% for e in risks %}<div id="{{ e.code }}" style="padding:6px 0">
<strong>« {{ e.lemma }} »</strong> <span class="tag red">{{ e.severity_product }}</span>{{ m.review('lexique', e.code, rv.get('lexique:' ~ e.code), csrf, here ~ '#' ~ e.code) }}</div>{% endfor %}</div>{% endif %}
{% endblock %}""",

"lexicon": """{% import "macros" as m %}{% extends "base" %}{% block content %}<p><a href="/app/base">← Base réglementaire</a></p>
<div class="card"><h1>Lexique des formulations à risque ({{ rows|length }})</h1><p class="muted">Gravité : fiche produit / article avec lien produit / article sans lien.</p></div>
{% for e in rows %}<div class="card" id="{{ e.code }}"><strong>{{ e.lemma }}</strong> <span class="muted">({{ e.code }} · {{ e.category_label }}{% if e.icd10_code %} · CIM-10 {{ e.icd10_code }}{% endif %})</span>
<p>Formes : {{ e.surface_forms|join(', ') }}{% if e.pattern_regex %} <span class="muted">+ motif</span>{% endif %}</p>
<p><span class="tag red">{{ e.severity_product }}</span> / {{ e.severity_article_with_bridge }} / {{ e.severity_article_without_bridge }} · {{ e.legal_label }} · confiance {{ e.confidence }}</p>
{% if e.note %}<p class="muted">{{ e.note }}</p>{% endif %}{{ m.review('lexique', e.code, rv.get('lexique:' ~ e.code), csrf, '/app/base/lexique#' ~ e.code) }}</div>{% endfor %}{% endblock %}""",

"notices": """{% import "macros" as m %}{% extends "base" %}{% block content %}<p><a href="/app/base">← Base réglementaire</a></p>
<div class="card"><h1>Mentions ({{ rows|length }})</h1></div>{% for n in rows %}<div class="card" id="{{ n.code }}"><strong>{{ n.code }}</strong> <span class="muted">confiance {{ n.confidence }}</span>
<p>« {{ n.text_fr }} »</p><p class="muted">{{ n.source_ref }}</p>{{ m.review('mention', n.code, rv.get('mention:' ~ n.code), csrf, '/app/base/mentions#' ~ n.code) }}</div>{% endfor %}{% endblock %}""",

"tasks": """{% import "macros" as m %}{% extends "base" %}{% block content %}<p><a href="/app/base">← Base réglementaire</a></p>
<div class="card"><h1>Tâches de revue ({{ rows|length }})</h1><p class="muted">Ce qu'il faut vérifier avant de publier la base. Votre avis sur chaque tâche est enregistré.</p></div>
<div class="card scroll"><table><tr><th>#</th><th>Élément</th><th>Confiance</th><th>Tâche et avis</th></tr>{% for t in rows %}
<tr id="TACHE-{{ t.n }}"><td>{{ t.n }}</td><td>{{ t.target_table }}<div class="muted">{{ t.target_code }}</div></td><td>{{ t.confidence }}</td>
<td>{{ t.task_fr }}{{ m.review('tache', 'TACHE-' ~ t.n, rv.get('tache:TACHE-' ~ t.n), csrf, '/app/base/taches#TACHE-' ~ t.n) }}</td></tr>{% endfor %}</table></div>{% endblock %}""",

"reviews": """{% extends "base" %}{% block content %}<div class="card"><h1>Mes avis ({{ rows|length }})</h1>
<p><a class="btn" href="/app/avis.csv">Exporter en CSV</a></p></div><div class="card scroll"><table><tr><th>Date</th><th>Élément</th><th>Décision</th><th>Commentaire</th></tr>
{% for r in rows %}<tr><td>{{ r.date }}</td><td>{{ r.target_type }}<div class="muted">{{ r.target_code }}</div></td><td>{{ r.label }}</td><td>{{ r.comment or '' }}</td></tr>{% endfor %}</table></div>{% endblock %}""",

"admin": """{% extends "base" %}{% block content %}<div class="card"><h1>Administration</h1><p><a class="btn" href="/admin/avis.csv">Exporter tous les avis (CSV)</a></p></div>
<div class="card scroll"><table><tr><th>E-mail</th><th>Nom</th><th>Créé le</th><th>Fin d'essai</th><th>Analyses</th><th>Avis</th></tr>
{% for u in rows %}<tr><td>{{ u.email }}</td><td>{{ u.name }}</td><td>{{ u.created }}</td><td>{{ u.ends }}</td><td>{{ u.analyses }}</td><td>{{ u.reviews }}</td></tr>{% endfor %}</table></div>{% endblock %}""",
}

ENV = Environment(loader=DictLoader(T), autoescape=select_autoescape(default=True, default_for_string=True))


def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    user = current_user(request)
    base = {"request": request, "user": user, "acc": access(user), "csrf": csrf(request), "app_name": APP_NAME,
            "css": Markup(CSS), "trial_days": TRIAL_DAYS, "title": ctx.pop("title", APP_NAME)}
    return HTMLResponse(ENV.get_template(name).render(**base, **ctx), status_code=status)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def guard(request: Request):
    """Renvoie (utilisateur, réponse) : réponse non nulle = redirection ou page d'essai terminé."""
    user = current_user(request)
    if user is None:
        return None, redirect("/connexion")
    if not access(user)["active"]:
        return user, render(request, "expired", title="Essai terminé")
    return user, None


# -----------------------------------------------------------------------------
# Routes publiques
# -----------------------------------------------------------------------------
@app.get("/sante", response_class=PlainTextResponse)
def health():
    return "ok"


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    if current_user(request):
        return redirect("/app")
    return render(request, "landing", title="Accueil")


@app.get("/essai", response_class=HTMLResponse)
def signup_form(request: Request):
    return render(request, "signup", title="Essai gratuit", form={}, error=None)


@app.post("/essai", response_class=HTMLResponse)
def signup(request: Request, name: str = Form(""), email: str = Form(""), password: str = Form(""),
           terms: str = Form(""), csrf_token: str = Form("", alias="csrf")):
    form = {"name": name, "email": email}
    if not csrf_ok(request, csrf_token):
        return render(request, "signup", 400, title="Essai gratuit", form=form, error="Session expirée, réessayez.")
    email = email.strip().lower()
    if not name.strip() or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return render(request, "signup", 400, title="Essai gratuit", form=form, error="Nom ou e-mail invalide.")
    if len(password) < 10:
        return render(request, "signup", 400, title="Essai gratuit", form=form, error="Mot de passe trop court (10 caractères minimum).")
    if terms != "1":
        return render(request, "signup", 400, title="Essai gratuit", form=form, error="Merci de cocher la case.")
    with ENGINE.begin() as c:
        if c.execute(select(USERS.c.id).where(USERS.c.email == email)).first():
            return render(request, "signup", 400, title="Essai gratuit", form=form, error="Un compte existe déjà pour cet e-mail.")
        uid = c.execute(insert(USERS).values(email=email, name=name.strip()[:200], pw_hash=hash_password(password),
                                             plan="ESSAI_COMPLET", trial_ends_at=now() + timedelta(days=TRIAL_DAYS),
                                             created_at=now())).inserted_primary_key[0]
    request.session.clear()
    request.session["uid"] = uid
    return redirect("/app")


@app.get("/connexion", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login", title="Connexion", error=None)


@app.post("/connexion", response_class=HTMLResponse)
def login(request: Request, email: str = Form(""), password: str = Form(""), csrf_token: str = Form("", alias="csrf")):
    if not csrf_ok(request, csrf_token):
        return render(request, "login", 400, title="Connexion", error="Session expirée, réessayez.")
    with ENGINE.connect() as c:
        user = c.execute(select(USERS).where(USERS.c.email == email.strip().lower())).mappings().first()
    if not user or not check_password(password, user["pw_hash"]):
        return render(request, "login", 401, title="Connexion", error="E-mail ou mot de passe incorrect.")
    request.session.clear()
    request.session["uid"] = user["id"]
    return redirect("/app")


@app.post("/deconnexion")
def logout(request: Request, csrf_token: str = Form("", alias="csrf")):
    if csrf_ok(request, csrf_token):
        request.session.clear()
    return redirect("/")


# -----------------------------------------------------------------------------
# Application
# -----------------------------------------------------------------------------
@app.get("/app", response_class=HTMLResponse)
def dashboard(request: Request, exemple: str = ""):
    user, stop = guard(request)
    if stop:
        return stop
    prefill = next((e for e in EXAMPLES if e["id"] == exemple), {"text": "", "mode": "PRODUCT_PAGE", "product_name": "", "composition": []})
    with ENGINE.connect() as c:
        recent = c.execute(select(ANALYSES.c.id, ANALYSES.c.title, ANALYSES.c.created_at).where(ANALYSES.c.user_id == user["id"])
                           .order_by(desc(ANALYSES.c.created_at)).limit(10)).mappings().all()
    return render(request, "app", title="Analyser", examples=EXAMPLES, prefill=prefill, src="texte", error=None,
                  substances=sorted(SUBSTANCES.values(), key=lambda s: s["name_fr"]), units=UNITS,
                  recent=[{"id": a["id"], "title": a["title"], "date": aware(a["created_at"]).strftime("%d/%m %H:%M")} for a in recent])


@app.post("/app/analyser", response_class=HTMLResponse)
async def analyse(request: Request):
    user, stop = guard(request)
    if stop:
        return stop
    form = await request.form()
    if not csrf_ok(request, form.get("csrf", "")):
        return redirect("/app")
    source = form.get("source", "texte")
    text, url = (form.get("text") or "").strip(), (form.get("url") or "").strip()
    mode = form.get("mode") if form.get("mode") in ("PRODUCT_PAGE", "EDITORIAL_BLOG") else "PRODUCT_PAGE"
    product_name = (form.get("product_name") or "").strip()[:200]
    composition = parse_composition(form)
    error = None
    if source == "url" and not url:
        error = "Indiquez une URL."
    elif source != "url" and not text:
        error = "Collez un texte ou du HTML."
    elif len(text) > 200_000:
        error = "Texte trop long (200 000 caractères maximum)."
    elif AI_ENABLED and not access(user)["admin"] and analyses_today(user["id"]) >= AI_DAILY_LIMIT:
        error = f"Limite quotidienne atteinte ({AI_DAILY_LIMIT} analyses par jour pendant l'essai)."
    if not error:
        try:
            result = run_analysis(source, text, url, mode, product_name, composition)
        except FetchError as exc:
            error = f"Impossible de récupérer la page : {exc}. Vous pouvez coller son texte à la place."
        except httpx.HTTPError:
            error = "Impossible de récupérer la page (délai dépassé ou connexion refusée). Collez son texte à la place."
    if error:
        prefill = {"text": text, "mode": mode, "product_name": product_name, "composition": composition}
        return render(request, "app", 400, title="Analyser", examples=EXAMPLES, prefill=prefill, src=source, error=error,
                      substances=sorted(SUBSTANCES.values(), key=lambda s: s["name_fr"]), units=UNITS, recent=[])
    title = (product_name or (url if source == "url" else text[:60].replace("\n", " "))) or "Analyse"
    with ENGINE.begin() as c:
        aid = c.execute(insert(ANALYSES).values(user_id=user["id"], title=title[:300], result_json=json.dumps(result, ensure_ascii=False),
                                                created_at=now())).inserted_primary_key[0]
    return redirect(f"/app/analyses/{aid}")


@app.get("/app/analyses/{aid}", response_class=HTMLResponse)
def analysis_page(request: Request, aid: int):
    user, stop = guard(request)
    if stop:
        return stop
    with ENGINE.connect() as c:
        row = c.execute(select(ANALYSES).where(ANALYSES.c.id == aid, ANALYSES.c.user_id == user["id"])).mappings().first()
    if not row:
        return render(request, "expired", 404, title="Introuvable")
    return render(request, "result", title="Résultat", r=json.loads(row["result_json"]), levels=LEVELS, aid=aid,
                  ai_enabled=AI_ENABLED)


@app.get("/app/base", response_class=HTMLResponse)
def base_index(request: Request):
    user, stop = guard(request)
    if stop:
        return stop
    rv = latest_reviews(user["id"])
    rows = []
    for s in SEED["substances"]:
        claims = [c for c in SEED["regulatory_claims"] if c["substance"] == s["code"]]
        codes = {c["code"] for c in claims}
        rows.append({"code": s["code"], "name": s["name_fr"], "latin": s["scientific_name"], "kind": s["kind"],
                     "claims": len(claims), "variants": sum(1 for v in SEED["claim_variants"] if v["claim"] in codes),
                     "risks": sum(1 for e in SEED["lexical_entries"] if e.get("substance") == s["code"]),
                     "reviewed": sum(1 for k in rv if k.split(":", 1)[1] in codes or k.split(":", 1)[1] == s["code"])})
    return render(request, "base_index", title="Base réglementaire", rows=rows, release=KB.release,
                  n_lex=sum(1 for e in SEED["lexical_entries"] if e["category"] not in ("VERBE_EFFET", "THEME_PHYSIOLOGIQUE", "ATTRIBUT_FACTUEL")),
                  n_notices=len(SEED["notices"]), n_tasks=len(SEED["review_items"]))


@app.get("/app/base/lexique", response_class=HTMLResponse)
def lexicon_page(request: Request):
    user, stop = guard(request)
    if stop:
        return stop
    rows = [{**e, "category_label": CATEGORY_LABELS.get(e["category"], e["category"]),
             "legal_label": LEGAL_LABELS.get(e["legal_basis"], e["legal_basis"])}
            for e in SEED["lexical_entries"] if e["category"] not in ("VERBE_EFFET", "THEME_PHYSIOLOGIQUE", "ATTRIBUT_FACTUEL", "ALLEGATION_NON_AUTORISEE")]
    return render(request, "lexicon", title="Lexique", rows=rows, rv=latest_reviews(user["id"]))


@app.get("/app/base/mentions", response_class=HTMLResponse)
def notices_page(request: Request):
    user, stop = guard(request)
    if stop:
        return stop
    return render(request, "notices", title="Mentions", rows=SEED["notices"], rv=latest_reviews(user["id"]))


@app.get("/app/base/taches", response_class=HTMLResponse)
def tasks_page(request: Request):
    user, stop = guard(request)
    if stop:
        return stop
    rows = [{**t, "n": i} for i, t in enumerate(SEED["review_items"], start=1)]
    return render(request, "tasks", title="Tâches de revue", rows=rows, rv=latest_reviews(user["id"]))


@app.get("/app/base/{code}", response_class=HTMLResponse)
def substance_page(request: Request, code: str):
    user, stop = guard(request)
    if stop:
        return stop
    s = SUBSTANCES.get(code)
    if not s:
        return render(request, "expired", 404, title="Introuvable")
    notices = {n["code"]: n["text_fr"] for n in SEED["notices"]}
    claims = []
    for c in SEED["regulatory_claims"]:
        if c["substance"] != code:
            continue
        conds = [{**k, "notice": notices.get(k.get("notice_code") or "")} for k in SEED["claim_conditions"] if k["claim"] == c["code"]]
        claims.append({**c, "conditions": conds, "variants": [v for v in SEED["claim_variants"] if v["claim"] == c["code"]]})
    return render(request, "substance", title=s["name_fr"], s=s, claims=claims, here=f"/app/base/{code}",
                  worklist=[w for w in SEED["botanical_import_worklist"] if w["substance"] == code],
                  restrictions=[{**r, "notice_text": notices.get(r["notice"], "")} for r in SEED["population_restrictions"] if r["substance"] == code],
                  risks=[e for e in SEED["lexical_entries"] if e.get("substance") == code], rv=latest_reviews(user["id"]))


@app.post("/app/avis")
def save_review(request: Request, target_type: str = Form(""), target_code: str = Form(""), decision: str = Form(""),
                comment: str = Form(""), next_url: str = Form("/app/base", alias="next"), csrf_token: str = Form("", alias="csrf")):
    user, stop = guard(request)
    if stop:
        return stop
    if csrf_ok(request, csrf_token) and decision in DECISIONS and target_type and target_code:
        with ENGINE.begin() as c:
            c.execute(insert(REVIEWS).values(user_id=user["id"], target_type=target_type[:40], target_code=target_code[:120],
                                             decision=decision, comment=comment.strip()[:2000] or None, created_at=now()))
    return redirect(safe_next(next_url, "/app/base"))


def _reviews_rows(user_id: int | None) -> list[dict]:
    q = select(REVIEWS, USERS.c.email).join(USERS, USERS.c.id == REVIEWS.c.user_id).order_by(desc(REVIEWS.c.created_at))
    if user_id is not None:
        q = q.where(REVIEWS.c.user_id == user_id)
    with ENGINE.connect() as c:
        rows = c.execute(q).mappings().all()
    return [{"date": aware(r["created_at"]).strftime("%d/%m/%Y %H:%M"), "email": r["email"], "target_type": r["target_type"],
             "target_code": r["target_code"], "decision": r["decision"], "label": DECISIONS.get(r["decision"], r["decision"]),
             "comment": r["comment"]} for r in rows]


def _csv(rows: list[dict]) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["date", "email", "type", "code", "decision", "commentaire"])
    for r in rows:
        w.writerow([r["date"], r["email"], r["target_type"], r["target_code"], r["label"], r["comment"] or ""])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="avis-expert.csv"'})


@app.get("/app/avis", response_class=HTMLResponse)
def my_reviews(request: Request):
    user = current_user(request)
    if user is None:
        return redirect("/connexion")
    return render(request, "reviews", title="Mes avis", rows=_reviews_rows(user["id"]))


@app.get("/app/avis.csv")
def my_reviews_csv(request: Request):
    user = current_user(request)
    if user is None:
        return redirect("/connexion")
    return _csv(_reviews_rows(user["id"]))


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    user = current_user(request)
    if user is None or not access(user)["admin"]:
        return redirect("/app")
    with ENGINE.connect() as c:
        users = c.execute(select(USERS).order_by(desc(USERS.c.created_at))).mappings().all()
        n_an = dict(c.execute(select(ANALYSES.c.user_id, func.count()).group_by(ANALYSES.c.user_id)).all())
        n_rv = dict(c.execute(select(REVIEWS.c.user_id, func.count()).group_by(REVIEWS.c.user_id)).all())
    rows = [{"email": u["email"], "name": u["name"], "created": aware(u["created_at"]).strftime("%d/%m/%Y"),
             "ends": aware(u["trial_ends_at"]).strftime("%d/%m/%Y") if u["trial_ends_at"] else "-",
             "analyses": n_an.get(u["id"], 0), "reviews": n_rv.get(u["id"], 0)} for u in users]
    return render(request, "admin", title="Administration", rows=rows)


@app.get("/admin/avis.csv")
def admin_csv(request: Request):
    user = current_user(request)
    if user is None or not access(user)["admin"]:
        return redirect("/app")
    return _csv(_reviews_rows(None))


from webapp import features  # noqa: E402,F401  (routes supplémentaires : réécriture, décisions, rapport, tarifs, banc)
