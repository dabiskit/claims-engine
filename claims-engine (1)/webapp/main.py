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
from engine.mode_router import lexical_hits  # noqa: E402
from engine.page_scope import detect_composition, isolate  # noqa: E402
from engine.textnorm import norm  # noqa: E402
from webapp import kb_sync  # noqa: E402
from eval.pipeline_adapter import KnowledgeBase, ReplayPipeline, product_of  # noqa: E402

APP_NAME = os.environ.get("APP_NAME", "Pré-analyse des allégations")
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "14"))
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
MICRO_G = "\u00b5g"
UNITS = ["mg", MICRO_G, "g"]
USER_AGENT = "PreAnalyseAllegations/0.1 (demo)"

BASE_SEED = json.loads((ROOT / "seed" / "out" / "lot1.json").read_text(encoding="utf-8"))
SEED = BASE_SEED
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


def apply_seed(seed: dict) -> None:
    """Remplace la base en mémoire (base rédigée + import officiel). Les analyses en cours gardent
    l'ancienne base ; les suivantes utilisent la nouvelle."""
    global SEED, KB, PIPE, SUBSTANCES, LEXICON
    kb = KnowledgeBase(seed)
    SEED, KB = seed, kb
    SUBSTANCES = {s["code"]: s for s in seed["substances"]}
    LEXICON = {e["code"]: e for e in seed["lexical_entries"]}
    PIPE = make_pipeline()

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
SQLITE_URL = f"sqlite:///{ROOT / 'webapp' / 'demo.db'}"
DB_STATUS = {"config": "absente", "detail": ""}   # affiché dans /sante/base et l'administration, sans secret


def database_url() -> str:
    """Normalise DATABASE_URL : espaces, guillemets, préfixe « DATABASE_URL= » collé par erreur,
    mot de passe contenant des caractères spéciaux (encodé), crochets du modèle Supabase."""
    from urllib.parse import quote, unquote
    raw = os.environ.get("DATABASE_URL")
    if raw is None:
        DB_STATUS.update(config="absente", detail="variable DATABASE_URL non définie")
        return SQLITE_URL
    url = raw.strip().strip('"').strip("'").strip()
    if url.upper().startswith("DATABASE_URL="):
        url = url.split("=", 1)[1].strip().strip('"').strip("'")
    if not url:
        DB_STATUS.update(config="vide", detail="variable DATABASE_URL définie mais vide")
        return SQLITE_URL
    m_ = re.match(r"^(postgres(?:ql)?(?:\+psycopg)?)://([^:/@]+):(.*)@([^@/]+)(/.*)?$", url)
    if not m_:
        DB_STATUS.update(config="invalide", detail="DATABASE_URL ne commence pas par postgresql://utilisateur:motdepasse@hôte")
        return SQLITE_URL
    _, user, password, hostport, path = m_.groups()
    if "[YOUR-PASSWORD]" in password or "MOT_DE_PASSE" in password:
        DB_STATUS.update(config="invalide", detail="le mot de passe n'a pas été mis à la place de [YOUR-PASSWORD]")
        return SQLITE_URL
    if password.startswith("[") and password.endswith("]"):
        password = password[1:-1]
    password = quote(unquote(password), safe="")
    DB_STATUS.update(config="ok", detail=f"hôte {hostport.split(':')[0]}")
    return f"postgresql+psycopg://{user}:{password}@{hostport}{path or '/postgres'}"


def _make_engine():
    url = database_url()
    if url == SQLITE_URL:
        return create_engine(url, pool_pre_ping=True)
    eng = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})
    try:
        with eng.connect():
            pass
        return eng
    except Exception as exc:  # pas de panne totale : on démarre, et l'erreur est affichée (sans le mot de passe)
        msg = re.sub(r"://[^@\s]*@", "://***@", str(exc).splitlines()[0])[:300]
        DB_STATUS.update(config="erreur_connexion", detail=msg)
        print(f"[db] connexion PostgreSQL impossible : {msg}", flush=True)
        return create_engine(SQLITE_URL, pool_pre_ping=True)


ENGINE = _make_engine()
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
KB_IMPORTS = Table("demo_kb_imports", MD,
                   Column("id", Integer, primary_key=True), Column("status", String(20), nullable=False),
                   Column("message", Text), Column("data_json", Text),
                   Column("started_at", DateTime(timezone=True), nullable=False),
                   Column("finished_at", DateTime(timezone=True)))
MD.create_all(ENGINE)


def db_backend() -> str:
    return ENGINE.dialect.name


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


def _is_document(html: str) -> bool:
    return html.lstrip()[:300].lower().startswith(("<!doctype", "<html"))


def _substance_names() -> tuple[dict, dict]:
    aliases = {c: [s["name_fr"], *(a["alias"] for a in s["aliases"] if not a.get("is_ambiguous"))]
               for c, s in SUBSTANCES.items() if s["kind"] != "PLANTE"}
    return aliases, {c: s["name_fr"] for c, s in SUBSTANCES.items()}


LAST_AI_ERROR: dict = {}


def run_analysis(source: str, text: str, url: str, mode: str, product_name: str, composition: list[dict],
                 fetched_html: str | None = None, use_ai: bool = True) -> dict:
    """Analyse une page. URL (ou document HTML complet collé) : la page est d'abord réduite à son contenu
    principal (engine/page_scope.py) ; la composition est détectée si elle n'est pas fournie."""
    page_url = url if source == "url" and url else (
        "https://exemple.invalid/products/demo" if mode != "EDITORIAL_BLOG" else "https://exemple.invalid/blogs/demo")
    raw_html = fetched_html if fetched_html is not None else (fetch_url(url) if source == "url" else text_to_html(text))
    scope = isolate(raw_html, page_url) if _is_document(raw_html) else None
    html = scope.html if scope else raw_html
    info = scope.product if scope else None
    if mode not in ("PRODUCT_PAGE", "EDITORIAL_BLOG"):
        mode = "EDITORIAL_BLOG" if info and info.kind == "ARTICLE" else "PRODUCT_PAGE"
    product_name = product_name or (info.title if info else "") or ""

    doc = html if _is_document(html) else f"<html><head><title></title></head><body><main>{html}</main></body></html>"
    page = extract_page(doc, page_url)
    composition_source = "declaree" if composition else None
    detected = []
    if not composition:
        texts = [n.text_plain for n in page.nodes]
        # lignes de tableau (composition, valeurs nutritionnelles) : cellules réunies sur une ligne
        texts += [" ".join(td.text_content().strip() for td in tr.xpath("./td|./th"))
                  for tr in lxml_html.document_fromstring(doc).xpath("//tr")]
        if info and info.description:
            texts.append(info.description)
        aliases, labels = _substance_names()
        detected = detect_composition(texts, aliases, labels)
        composition = [{"substance": d.substance, "label": d.label, "amount": d.amount, "unit": d.unit,
                        "plant_part": None, "preparation": None, "der_ratio": None} for d in detected]
        composition_source = "detectee" if composition else "absente"

    case = {"test_id": "demo", "page_mode": mode, "page_url": page_url,
            "product_names": [product_name] if product_name else [], "portions_per_day": 1,
            "product_composition": composition if mode == "PRODUCT_PAGE" else [],
            "linked_products": [] if mode == "PRODUCT_PAGE" else
            [{"path": "/products/produit-lie", "name": product_name or "produit", "composition": composition}]}
    pipe = PIPE if use_ai else ReplayPipeline(KB, {"cases": {}})
    analysis = pipe.analyze(case, html)
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
    general_refs: dict[str, dict] = {}
    counts = {k: 0 for k in LEVELS}
    ai_error = None
    priorities = []
    for f in analysis.findings:
        if f.code == "IA_INDISPONIBLE":
            ai_error = f.quote or "erreur inconnue"
            LAST_AI_ERROR["msg"] = f"{now():%d/%m %H:%M} — {ai_error}"
        item = {"code": f.code, "ai": f.code.startswith("IA_"), "level": f.level, "level_label": LEVELS.get(f.level, (f.level, ""))[0],
                "css": LEVELS.get(f.level, ("", "info"))[1], "quote": f.quote, "explanation": explain(f),
                "legal": LEGAL_LABELS.get(f.legal_basis or "", f.legal_basis or ""),
                "substance": SUBSTANCES.get(f.substance or "", {}).get("name_fr"),
                "remediations": [REMEDIATIONS.get(r, r) for r in dict.fromkeys(f.remediation_codes)]}
        if f.code in ("REFERENCE_GENERALE_ART10_3", "IA_REFERENCE_GENERALE") and f.node_key:
            term = norm(f.quote or "")
            g = general_refs.setdefault(term, {"term": f.quote, "count": 0, "examples": [], "explanation": item["explanation"],
                                               "legal": item["legal"]})
            g["count"] += 1
            if len(g["examples"]) < 3 and f.node_text not in g["examples"]:
                g["examples"].append(f.node_text[:220])
            counts["ORANGE_REF"] = counts.get("ORANGE_REF", 0) + 1
            continue
        counts[f.level] = counts.get(f.level, 0) + 1
        if f.node_key is None:
            page_level.append(item)
        else:
            key = norm(f.node_text)[:300]          # un même texte répété (menu mobile, bloc dupliqué) : une fois
            node = nodes.setdefault(key, {"text": f.node_text, "findings": [], "seen": set()})
            sig = (f.code, norm(f.quote or ""))
            if sig in node["seen"]:
                continue
            node["seen"].add(sig)
            node["findings"].append(item)
            if f.level == "ROUGE" and len(priorities) < 6:
                priorities.append(item)
    counts["ORANGE"] = counts.get("ORANGE", 0) + counts.get("ORANGE_REF", 0)

    recognized = [{"code": r.claim_code, "text": KB.claims[r.claim_code]["text_fr"],
                   "status": USABILITY.get(r.usability, (r.usability, "info"))} for r in analysis.recognized_claims]
    recognized = list({r["code"]: r for r in recognized}.values())

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
                           "source": claim.get("text_fr_source_ref"), "source_url": claim.get("source_url"),
                           "variants": [v["text_fr"] for v in KB.variants.get(ccode, []) if not v["is_official_wording"]]})
        order = {"ok": 0, "cond": 1, "red": 2}
        claims.sort(key=lambda c: order.get(c["status"][1], 3))
        usable.append({"substance": SUBSTANCES[code]["name_fr"], "amount": item["amount"], "unit": item["unit"],
                       "claims": claims, "pending_onhold": SUBSTANCES[code]["kind"] == "PLANTE",
                       "n_ok": sum(1 for c in claims if c["status"][1] == "ok")})

    # Substances fréquentes sans allégation autorisée (registre de l'Union) : nom, description, tableaux.
    label_text = " ".join(filter(None, [product_name, info.description if info else None, info.category if info else None,
                                        *[n.text_plain for n in page.nodes if n.zone in ("TABLEAU", "TITRE")]]))
    rejected = kb_sync.rejected_for(SEED, norm(label_text))

    reviews = {"count": 0, "flagged": []}
    if scope and scope.reviews:
        reviews["count"] = len(scope.reviews)
        for rv in scope.reviews:
            hits = [h for h in lexical_hits(KB.lexicon, rv) if h.entry.severity.get("PRODUIT") == "ROUGE"]
            if hits:
                reviews["flagged"].append({"html": str(highlight(rv, [(rv[h.start:h.end], "red") for h in hits])),
                                           "terms": sorted({rv[h.start:h.end].lower() for h in hits})})

    node_list = []
    for n in nodes.values():
        node_list.append({"html": str(highlight(n["text"], [(i["quote"], i["css"]) for i in n["findings"]])),
                          "findings": n["findings"], "has_red": any(i["level"] == "ROUGE" for i in n["findings"])})
    node_list.sort(key=lambda n: 0 if n["has_red"] else 1)
    product_card = None
    if info:
        product_card = {"name": info.title, "brand": info.brand, "category": info.category, "image": info.image,
                        "description": (info.description or "")[:400], "kind": info.kind, "gtin": info.gtin}
    return {"source": url if source == "url" else "Texte collé", "mode": mode, "product_name": product_name,
            "composition": composition, "composition_source": composition_source,
            "detected": [{"label": d.label, "amount": d.amount, "unit": d.unit, "evidence": d.evidence} for d in detected],
            "counts": counts, "nodes": node_list, "priorities": priorities,
            "general_refs": sorted(general_refs.values(), key=lambda g: -g["count"]),
            "page_level": page_level, "recognized": recognized, "usable": usable, "rejected": rejected,
            "product": product_card, "reviews": reviews,
            "scope": {"dropped": scope.dropped_summary() if scope else [], "used_main": bool(scope and scope.used_main),
                      "nodes": len(page.nodes)},
            "kb_missing": analysis.kb_missing_substances, "kb_release": KB.release,
            "created": now().strftime("%d/%m/%Y %H:%M"), "flagged": len(nodes_index), "ai": AI_ENABLED and use_ai,
            "ai_error": ai_error,
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
    for i in range(1, 9):
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
{% if user %}<a href="/app">Analyser</a><a href="/app/audit">Audit de site</a><a href="/app/base">Base réglementaire</a><a href="/app/avis">Mes avis</a><a href="/tarifs">Tarifs</a>
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
<label for="url">URL</label><input id="url" name="url" type="url" style="width:100%" placeholder="https://…" value="{{ prefill.url or '' }}">
<p class="muted">Pour plusieurs pages, un sitemap ou des fichiers : <a href="/app/audit">audit de site</a>.</p>
<div class="row"><div><label for="mode">Type de page</label><select id="mode" name="mode">
<option value="AUTO" {% if prefill.mode not in ('PRODUCT_PAGE', 'EDITORIAL_BLOG') %}selected{% endif %}>Détection automatique</option>
<option value="PRODUCT_PAGE" {% if prefill.mode == 'PRODUCT_PAGE' %}selected{% endif %}>Fiche produit</option>
<option value="EDITORIAL_BLOG" {% if prefill.mode == 'EDITORIAL_BLOG' %}selected{% endif %}>Article de blog</option></select></div>
<div><label for="product_name">Nom du produit (facultatif)</label><input id="product_name" name="product_name" value="{{ prefill.product_name }}"></div></div>
<label>Composition par portion journalière (facultatif : sinon, elle est détectée dans la page)</label>
<div class="scroll"><table><tr><th>Ingrédient</th><th>Quantité</th><th>Unité</th></tr>
{% for i in range(1, 9) %}{% set c = prefill.composition[i-1] if prefill.composition|length >= i else None %}<tr>
<td><select name="sub_{{ i }}" aria-label="Ingrédient {{ i }}"><option value="">—</option>{% for s in substances %}<option value="{{ s.code }}" {% if c and c.substance == s.code %}selected{% endif %}>{{ s.name_fr }}</option>{% endfor %}</select></td>
<td><input name="amount_{{ i }}" inputmode="decimal" size="8" aria-label="Quantité {{ i }}" value="{{ c.amount if c else '' }}"></td>
<td><select name="unit_{{ i }}" aria-label="Unité {{ i }}">{% for u in units %}<option {% if c and c.unit == u %}selected{% endif %}>{{ u }}</option>{% endfor %}</select></td></tr>{% endfor %}</table></div>
<p><button>Lancer la pré-analyse</button></p></form></div>
{% if recent %}<div class="card"><h2>Analyses récentes</h2><ul>{% for a in recent %}<li><a href="/app/analyses/{{ a.id }}">{{ a.title }}</a> <span class="muted">{{ a.date }}</span></li>{% endfor %}</ul></div>{% endif %}
{% endblock %}""",

"result": """{% extends "base" %}{% block content %}
<p><a href="/app">← Nouvelle analyse</a>{% if r.audit_id %} · <a href="/app/audits/{{ r.audit_id }}">← Retour à l'audit</a>{% endif %}</p>
<div class="card">
{% if r.product and r.product.name %}<div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap">
{% if r.product.image %}<img src="{{ r.product.image }}" alt="" style="width:96px;height:96px;object-fit:contain;border:1px solid var(--line);border-radius:8px;background:#fff" referrerpolicy="no-referrer">{% endif %}
<div style="flex:1;min-width:220px"><p class="muted" style="margin:0">{{ 'Fiche produit détectée' if r.mode == 'PRODUCT_PAGE' else 'Article détecté' }}{% if r.product.brand %} · {{ r.product.brand }}{% endif %}{% if r.product.category %} · {{ r.product.category }}{% endif %}</p>
<h1 style="margin:4px 0">{{ r.product.name }}</h1>
<p class="muted" style="margin:0;word-break:break-all">{{ r.source }}</p></div></div>
{% else %}<h1>Résultat de la pré-analyse</h1><p class="muted">{{ r.source }} · {{ 'Fiche produit' if r.mode == 'PRODUCT_PAGE' else 'Article de blog' }}</p>{% endif %}
<div class="grid" style="margin-top:12px">{% for k, lbl in levels.items() if k != 'INFO' %}<div><span class="tag {{ lbl[1] }}">{{ lbl[0] }}</span><div class="count">{{ r.counts.get(k, 0) }}</div></div>{% endfor %}</div>
{% if not r.has_points %}<p>Aucun passage relevé par la pré-analyse automatique sur ce périmètre. Cela ne vaut pas validation.</p>{% endif %}
{% if r.scope and r.scope.used_main %}<p class="muted">Périmètre : contenu principal de la page ({{ r.scope.nodes }} passages).{% if r.scope.dropped %} Écartés : {{ r.scope.dropped|join(', ') }}.{% endif %}{% if r.reviews and r.reviews.count %} Avis clients : {{ r.reviews.count }}, présentés à part.{% endif %}</p>{% endif %}
<p class="muted">Base {{ r.kb_release }} · {{ r.created }}</p>
{% if r.flagged %}<p>{% if ai_enabled %}<form method="post" action="/app/analyses/{{ aid }}/reecrire" style="display:inline"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Proposer des réécritures (IA)</button></form> {% endif %}<a class="btn" href="/app/analyses/{{ aid }}/propositions">Propositions et décisions</a> <a href="/app/analyses/{{ aid }}/rapport">Rapport imprimable</a></p>{% else %}<p><a href="/app/analyses/{{ aid }}/rapport">Rapport imprimable</a></p>{% endif %}
</div>

{% if r.composition_source %}<div class="card"><h2>Composition {{ {'declaree': 'déclarée', 'detectee': 'détectée dans la page', 'absente': ''}[r.composition_source] }}</h2>
{% if r.composition_source == 'detectee' %}<p class="muted">Repérée automatiquement : vérifiez-la. Les doses servent à décider si une allégation est utilisable.</p>
<ul>{% for d in r.detected %}<li><strong>{{ d.label }}</strong> : {{ d.amount|num }} {{ d.unit }} <span class="muted">— « {{ d.evidence }} »</span></li>{% endfor %}</ul>
{% elif r.composition_source == 'declaree' %}<ul>{% for c in r.composition %}<li>{{ c.label }} : {{ c.amount|num }} {{ c.unit }}</li>{% endfor %}</ul>
{% else %}<p class="muted">Aucune quantité d'ingrédient de la base n'a été trouvée dans la page. Sans composition, les allégations ne peuvent pas être vérifiées.</p>{% endif %}
{% if r.source != 'Texte collé' %}<p><a href="/app?reprise={{ aid }}">Relancer avec une composition corrigée</a></p>{% endif %}</div>{% endif %}

{% if r.rejected %}<div class="card"><h2>Ingrédients sans allégation de santé autorisée</h2>
{% for x in r.rejected %}<div style="margin:8px 0"><span class="tag red">Aucune allégation autorisée</span> <strong>{{ x.name }}</strong>
<p>Le registre de l'Union recense {{ x.count }} demande(s) d'allégation refusée(s) pour cet ingrédient. Toute promesse de santé qui lui est attribuée est à revoir.</p>
<details><summary>Exemples de demandes refusées (texte officiel, en anglais)</summary><ul>{% for e in x.examples %}<li>« {{ e.claim }} » <span class="muted">({{ e.substance }} · {{ e.id }})</span></li>{% endfor %}</ul></details></div>{% endfor %}
<p class="muted">Source : registre de l'Union des allégations nutritionnelles et de santé (Commission européenne).</p></div>{% endif %}

{% if r.ai_error %}<div class="card muted"><strong>Extraction IA indisponible</strong> : seules les règles et le lexique ont été appliqués. Détail technique : {{ r.ai_error }}</div>{% endif %}

{% if r.priorities %}<div class="card"><h2>À traiter en premier</h2><ol>{% for f in r.priorities %}<li style="margin:6px 0"><strong>« {{ f.quote }} »</strong> — {{ f.explanation }}{% if f.legal %} <span class="muted">({{ f.legal }})</span>{% endif %}</li>{% endfor %}</ol></div>{% endif %}

{% set reds = r.nodes|selectattr('has_red')|list if r.nodes and r.nodes[0].has_red is defined else r.nodes %}
{% set others = r.nodes|rejectattr('has_red')|list if r.nodes and r.nodes[0].has_red is defined else [] %}
{% macro node_card(n) %}<div class="card"><p>{{ n.html|safe }}</p>
{% for f in n.findings %}<div style="margin:8px 0"><span class="tag {{ f.css }}">{{ f.level_label }}</span>{% if f.ai %} <span class="tag info">IA</span>{% endif %} <strong>« {{ f.quote }} »</strong> — {{ f.explanation }}
{% if f.legal %}<div class="muted">Fondement : {{ f.legal }}</div>{% endif %}
{% if f.remediations %}<div class="muted">Pistes : {{ f.remediations|join(' · ') }}</div>{% endif %}</div>{% endfor %}</div>{% endmacro %}
{% if reds %}<h2>Passages à revoir en priorité ({{ reds|length }})</h2>{% for n in reds %}{{ node_card(n) }}{% endfor %}{% endif %}
{% if others %}<details class="card"><summary><strong>Autres passages à revoir ({{ others|length }})</strong></summary>{% for n in others %}{{ node_card(n) }}{% endfor %}</details>{% endif %}

{% if r.general_refs %}<div class="card"><h2>Références générales à la santé ({{ r.general_refs|map(attribute='count')|sum }} mentions)</h2>
<p class="muted">Un bienfait général (« peau », « articulations », « immunité ») n'est permis que s'il est accompagné d'une allégation autorisée spécifique sur la page (Règl. (CE) 1924/2006, art. 10§3). Regroupées par terme :</p>
<div class="scroll"><table><tr><th>Terme</th><th>Occurrences</th><th>Exemples</th></tr>{% for g in r.general_refs %}<tr><td><span class="tag orange">{{ g.term }}</span></td><td>{{ g.count }}</td>
<td><details><summary class="muted">voir</summary>{% for e in g.examples %}<p class="muted">… {{ e }} …</p>{% endfor %}</details></td></tr>{% endfor %}</table></div></div>{% endif %}

{% if r.page_level %}<div class="card"><h2>Constats sur l'ensemble de la page</h2>{% for f in r.page_level if f.code != 'IA_INDISPONIBLE' %}
<p><span class="tag {{ f.css }}">{{ f.level_label }}</span> {{ f.explanation }}{% if f.substance %} ({{ f.substance }}){% endif %}{% if f.legal %} <span class="muted">— {{ f.legal }}</span>{% endif %}</p>{% endfor %}</div>{% endif %}
{% if r.recognized %}<div class="card"><h2>Allégations autorisées reconnues sur la page</h2><ul>{% for c in r.recognized %}
<li><span class="tag {{ c.status[1] }}">{{ c.status[0] }}</span> {{ c.text }} <span class="muted">({{ c.code }})</span></li>{% endfor %}</ul></div>{% endif %}
{% if r.usable %}<div class="card"><h2>Allégations utilisables pour cette composition</h2>
{% for u in r.usable %}<details style="border-top:1px solid var(--line);padding:8px 0"><summary><strong>{{ u.substance }}</strong> — {{ u.amount|num }} {{ u.unit }} par jour{% if u.n_ok is defined %} · {{ u.n_ok }} utilisable(s) sur {{ u.claims|length }}{% endif %}</summary>
{% if u.pending_onhold %}<p class="muted">Plante : les allégations « on-hold » ne sont pas encore intégrées à la base.</p>{% endif %}
{% for c in u.claims %}<div style="margin:10px 0"><span class="tag {{ c.status[1] }}">{{ c.status[0] }}</span> <strong>{{ c.text }}</strong>
{% if c.reasons %}<div class="muted">{{ c.reasons|join(' · ') }}</div>{% endif %}
{% if c.notices %}<div class="muted">Mention à afficher : {{ c.notices|join(' / ') }}</div>{% endif %}
{% if c.source %}<div class="muted">Source : {% if c.source_url %}<a href="{{ c.source_url }}" rel="noopener" target="_blank">{{ c.source }}</a>{% else %}{{ c.source }}{% endif %}</div>{% endif %}
{% if c.variants and c.status[1] != 'red' %}<details><summary>Formulations validées ({{ c.variants|length }})</summary><ul>{% for v in c.variants %}<li>{{ v }}</li>{% endfor %}</ul></details>{% endif %}</div>{% endfor %}</details>{% endfor %}</div>{% endif %}
{% if r.reviews and r.reviews.count %}<div class="card"><h2>Avis clients ({{ r.reviews.count }})</h2>
<p class="muted">Écartés de l'analyse principale. Un avis repris et mis en avant par la marque peut toutefois être considéré comme une allégation : {{ r.reviews.flagged|length }} contien(nen)t un terme médical.</p>
{% if r.reviews.flagged %}<details><summary>Voir les avis concernés</summary>{% for v in r.reviews.flagged %}<p>« {{ v.html|safe }} »</p>{% endfor %}</details>{% endif %}</div>{% endif %}
<div class="card muted"><h3>Limites de cette version</h3>{% if r.ai %}<p>Extraction et réécriture IA {% if r.ai_error %}indisponibles pour cette analyse{% else %}actives ({{ r.models|join(', ') }}){% endif %}. Les allégations botaniques « on-hold » ne sont pas encore intégrées. La base réglementaire est en cours de validation par un expert.</p>{% else %}<p>L'IA n'est pas activée pour cette analyse : seules les règles et le lexique ont été appliqués, les promesses implicites ne sont pas détectées.</p>{% endif %}</div>
{% endblock %}""",

"base_index": """{% extends "base" %}{% block content %}<div class="card"><h1>Base réglementaire</h1>
<p class="muted">Release {{ release }}. {{ n_claims }} allégations autorisées, {{ rows|length }} ingrédients. Chaque élément peut recevoir votre avis ; tout est exportable depuis <a href="/app/avis">Mes avis</a>.</p>
{% if official %}<p><span class="tag ok">Import officiel</span> Sources officielles téléchargées le {{ official.retrieved_at[:10] }} : {% for src in official.sources %}<a href="{{ src.url }}" rel="noopener" target="_blank">{{ src.name }}</a>{% if src.version %} (version du {{ src.version }}){% endif %}{% if not loop.last %} · {% endif %}{% endfor %}. <a href="/app/base/sources">Détail et mise à jour</a></p>
{% else %}<p><span class="tag orange">Import officiel non effectué</span> Seule la base rédigée (lot 1) est chargée. <a href="/app/base/sources">Sources et mise à jour</a></p>{% endif %}
<p><a href="/app/base/lexique">Lexique des formulations à risque ({{ n_lex }})</a> · <a href="/app/base/mentions">Mentions ({{ n_notices }})</a> · <a href="/app/base/taches">Tâches de revue ({{ n_tasks }})</a>{% if n_rejected %} · <a href="/app/base/refusees">Demandes refusées ({{ n_rejected }})</a>{% endif %}</p>
<p><input id="flt" placeholder="Filtrer les ingrédients…" oninput="for(const tr of document.querySelectorAll('#subs tr[data-n]'))tr.style.display=tr.dataset.n.includes(this.value.toLowerCase())?'':'none'" style="width:100%;max-width:360px"></p></div>
<div class="card scroll"><table id="subs"><tr><th>Ingrédient</th><th>Type</th><th>Origine</th><th>Allégations</th><th>Formulations</th><th>Motifs à risque</th><th>Avis donnés</th></tr>
{% for s in rows %}<tr data-n="{{ s.name|lower }}"><td><a href="/app/base/{{ s.code }}">{{ s.name }}</a>{% if s.latin %}<div class="muted"><em>{{ s.latin }}</em></div>{% endif %}</td><td>{{ s.kind }}</td><td><span class="tag {{ 'ok' if s.origin == 'Import officiel' else 'info' }}">{{ s.origin }}</span></td><td>{{ s.claims }}</td><td>{{ s.variants }}</td><td>{{ s.risks }}</td><td>{{ s.reviewed }}</td></tr>{% endfor %}</table></div>{% endblock %}""",

"substance": """{% import "macros" as m %}{% extends "base" %}{% block content %}
<p><a href="/app/base">← Base réglementaire</a></p>
<div class="card"><h1>{{ s.name_fr }}</h1>{% if s.scientific_name %}<p><em>{{ s.scientific_name }}</em> · {{ s.botanical_family }}</p>{% endif %}
{% if s.nrv_value %}<p>VNR : {{ s.nrv_value }} {{ s.nrv_unit }}</p>{% endif %}
<p class="muted">Alias reconnus : {{ s.aliases|map(attribute='alias')|join(', ') }}</p>{{ m.review('substance', s.code, rv.get('substance:' ~ s.code), csrf, here) }}</div>
{% if claims %}<div class="card"><h2>Allégations ({{ claims|length }})</h2>{% for c in claims %}
<div id="{{ c.code }}" style="border-top:1px solid var(--line);padding:12px 0">{% if c.origin == 'IMPORT_OFFICIEL' %}<span class="tag ok">texte officiel importé</span>{% else %}<span class="tag {{ 'ok' if c.confidence == 'HAUTE' else 'orange' }}">confiance de saisie : {{ c.confidence }}</span>{% endif %}
<p><strong>{{ c.text_fr }}</strong> <span class="muted">({{ c.code }} · {{ c.regime }} · {{ c.text_fr_status }})</span></p>
{% if c.source_url %}<p class="muted">Source : <a href="{{ c.source_url }}" rel="noopener" target="_blank">{{ c.text_fr_source_ref }}</a>{% if c.register_id %} · registre de l'Union {{ c.register_id }}{% endif %}{% if c.efsa_ref %} · avis EFSA {{ c.efsa_ref }}{% endif %}</p>{% endif %}
<p class="muted">Conditions : {% for k in c.conditions %}{% if k.type == 'CONDITION_TEXTE' %}« {{ k.params.text_fr }} »{% elif k.type == 'DOSE_MIN_PORTION' %}dose minimale {{ k.params.amount }} {{ '% de la VNR' if k.params.unit == 'PCT_VNR' else k.params.unit }} ({{ 'par jour' if k.params.basis == 'PORTION_JOURNALIERE' else 'par portion' }}){% else %}{{ k.type }}{% if k.params.amount is defined %} {{ k.params.amount }} {{ k.params.unit }} ({{ k.params.basis }}){% endif %}{% endif %}{% if k.notice %} — mention : « {{ k.notice }} »{% endif %}{% if not loop.last %} ; {% endif %}{% endfor %}</p>
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

"admin": """{% extends "base" %}{% block content %}<div class="card"><h1>Administration</h1><p><a class="btn" href="/admin/avis.csv">Exporter tous les avis (CSV)</a> <a href="/admin/banc">Banc d'essai</a> · <a href="/app/base/sources">Base officielle : import et mise à jour</a></p></div>
<div class="card"><h2>État du service</h2>
<p>Base de données : {% if diag.db == 'postgresql' %}<span class="tag ok">PostgreSQL</span> {{ diag.db_host }}{% else %}<span class="tag red">SQLite locale</span> — comptes, analyses et avis sont <strong>effacés à chaque redéploiement</strong>. Cause : {{ diag.db_detail }}. Vérifiez la variable DATABASE_URL (chaîne Supabase « Session pooler »).{% endif %}</p>
<p>IA : {% if diag.ai %}<span class="tag ok">clé présente</span> modèle {{ diag.model }}{% if diag.last_ai_error %} · dernière erreur : <span class="muted">{{ diag.last_ai_error }}</span>{% endif %}{% else %}<span class="tag orange">désactivée</span> (ANTHROPIC_API_KEY absente){% endif %}</p></div>
<div class="card scroll"><table><tr><th>E-mail</th><th>Nom</th><th>Créé le</th><th>Fin d'essai</th><th>Analyses</th><th>Avis</th></tr>
{% for u in rows %}<tr><td>{{ u.email }}</td><td>{{ u.name }}</td><td>{{ u.created }}</td><td>{{ u.ends }}</td><td>{{ u.analyses }}</td><td>{{ u.reviews }}</td></tr>{% endfor %}</table></div>{% endblock %}""",
}

ENV = Environment(loader=DictLoader(T), autoescape=select_autoescape(default=True, default_for_string=True))


def _num(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return (f"{f:.3f}".rstrip("0").rstrip(".") if f != int(f) else str(int(f))).replace(".", ",")


ENV.filters["num"] = _num


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
def dashboard(request: Request, exemple: str = "", reprise: int = 0):
    user, stop = guard(request)
    if stop:
        return stop
    prefill = next((e for e in EXAMPLES if e["id"] == exemple), {"text": "", "url": "", "mode": "AUTO", "product_name": "", "composition": []})
    src = "texte"
    if reprise:
        with ENGINE.connect() as c:
            row = c.execute(select(ANALYSES).where(ANALYSES.c.id == reprise, ANALYSES.c.user_id == user["id"])).mappings().first()
        if row:
            r = json.loads(row["result_json"])
            if r.get("source", "").startswith("http"):
                prefill = {"text": "", "url": r["source"], "mode": r.get("mode", "AUTO"), "product_name": r.get("product_name", ""),
                           "composition": [c for c in r.get("composition", []) if c["substance"] in SUBSTANCES]}
                src = "url"
    with ENGINE.connect() as c:
        recent = c.execute(select(ANALYSES.c.id, ANALYSES.c.title, ANALYSES.c.created_at).where(ANALYSES.c.user_id == user["id"])
                           .order_by(desc(ANALYSES.c.created_at)).limit(10)).mappings().all()
    return render(request, "app", title="Analyser", examples=EXAMPLES, prefill=prefill, src=src, error=None,
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
    if url and not text:
        source = "url"
    mode = form.get("mode") if form.get("mode") in ("PRODUCT_PAGE", "EDITORIAL_BLOG") else "AUTO"
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
        prefill = {"text": text, "url": url, "mode": mode, "product_name": product_name, "composition": composition}
        return render(request, "app", 400, title="Analyser", examples=EXAMPLES, prefill=prefill, src=source, error=error,
                      substances=sorted(SUBSTANCES.values(), key=lambda s: s["name_fr"]), units=UNITS, recent=[])
    title = (result.get("product_name") or (url if source == "url" else text[:60].replace("\n", " "))) or "Analyse"
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
                     "origin": "Import officiel" if s.get("origin") == "IMPORT_OFFICIEL" else "Base rédigée",
                     "claims": len(claims), "variants": sum(1 for v in SEED["claim_variants"] if v["claim"] in codes),
                     "risks": sum(1 for e in SEED["lexical_entries"] if e.get("substance") == s["code"]),
                     "reviewed": sum(1 for k in rv if k.split(":", 1)[1] in codes or k.split(":", 1)[1] == s["code"])})
    rows.sort(key=lambda r: norm(r["name"]))
    return render(request, "base_index", title="Base réglementaire", rows=rows, release=KB.release,
                  official=SEED.get("official_import"), n_claims=len(SEED["regulatory_claims"]),
                  n_rejected=len(SEED.get("rejected_claims") or []),
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
    from engine.ai import model_for
    diag = {"db": db_backend(), "db_host": ENGINE.url.host or "", "db_detail": DB_STATUS.get("detail"), "ai": AI_ENABLED, "model": model_for("extracteur"),
            "last_ai_error": LAST_AI_ERROR.get("msg")}
    return render(request, "admin", title="Administration", rows=rows, diag=diag)


@app.get("/admin/avis.csv")
def admin_csv(request: Request):
    user = current_user(request)
    if user is None or not access(user)["admin"]:
        return redirect("/app")
    return _csv(_reviews_rows(None))


from webapp import features  # noqa: E402,F401  (routes supplémentaires : réécriture, décisions, rapport, tarifs, banc)
from webapp import kb_admin  # noqa: E402,F401  (import officiel de la base, demandes refusées)
from webapp import audit  # noqa: E402,F401  (audit de site : lot d'URL, sitemap, fichiers)
