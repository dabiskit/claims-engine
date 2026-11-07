"""
engine/mode_router.py — Aiguillage « Fiche produit » / « Article éditorial ».

Fondement : un contenu publié par une marque sur son propre site est une communication
commerciale ; la DGCCRF apprécie la présentation d'ensemble. Une pathologie citée dans
un article purement informatif n'est donc pas traitée comme dans une fiche produit,
mais tout PONT COMMERCIAL (lien vers une fiche, encart produit, CTA, code promo, nom de
produit, ingrédient vendu associé à une pathologie) rapproche l'article du régime produit
et du risque de présentation médicamenteuse (CSP L5111-1).

Le module ne « blanchit » jamais un contenu : chaque option de remédiation porte un
risque résiduel affiché, jamais un « conforme ».
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

from .dom_extractor import NOT_ANALYZED_ZONES, PageExtraction
from .frozen_zones import FrozenKind, scan_frozen
from .lexicon import SEV_RANK, LexEntry, LexiconIndex, max_severity
from .textnorm import norm


class Mode(str, Enum):
    PRODUIT = "PRODUIT"
    ARTICLE = "ARTICLE"


class Proximity(str, Enum):
    MEME_SEGMENT = "MEME_SEGMENT"
    MEME_SECTION = "MEME_SECTION"
    MEME_ARTICLE = "MEME_ARTICLE"
    GABARIT_SEULEMENT = "GABARIT_SEULEMENT"   # widget produit du gabarit de blog uniquement
    AUCUNE = "AUCUNE"


class BridgeKind(str, Enum):
    LIEN_PRODUIT = "LIEN_PRODUIT"
    MENTION_PRODUIT = "MENTION_PRODUIT"
    CTA_COMMERCIAL = "CTA_COMMERCIAL"
    CODE_PROMO = "CODE_PROMO"
    ENCART_PRODUIT = "ENCART_PRODUIT"


PRODUCT_PATH = re.compile(r"/(?:products|produits?|product|shop|boutique|collections)/", re.I)
ARTICLE_PATH = re.compile(r"/(?:blogs?|articles?|journal|conseils|magazine|actualites)/", re.I)
CTA_LEXICON = ("acheter", "commander", "ajouter au panier", "decouvrir notre", "decouvrez notre",
               "decouvrez nos", "notre complement", "nos complements", "notre gamme", "en savoir plus sur notre",
               "profitez de", "livraison offerte")
DISCOUNT_CODE = re.compile(r"(?i:code\s*(?:promo)?)\s*:?\s*[A-Z0-9]{4,15}(?![A-Za-z0-9])")
PAGE_LEVEL_MEDICINAL_THRESHOLD = {"ROUGE": 4, "ORANGE": 2}   # à calibrer sur le golden set

# Dans un avertissement obligatoire (« Déconseillé aux… », « Demander l'avis d'un médecin en cas de… »),
# citer une pathologie ou un traitement est attendu : ces catégories y sont tolérées. Les verbes
# thérapeutiques (soigne, guérit…) restent détectés partout.
WARNING_TOLERATED_CATEGORIES = frozenset({"PATHOLOGIE", "TERME_MEDICAL", "VOCABULAIRE_PHARMACEUTIQUE", "VERBE_THERAPEUTIQUE"})
WARNING_TOLERATED_CODES = frozenset({"LEX-TRAITER"})


def lexical_hits(lexicon: LexiconIndex, text: str, *, exclude_control: bool = True) -> list:
    """Point d'entrée unique de la détection lexicale (pipeline, seed, golden set)."""
    warnings = [(z.start, z.end) for z in scan_frozen(text).zones
                if z.kind is FrozenKind.AVERTISSEMENT_OBLIGATOIRE]
    out = []
    for h in lexicon.scan(text, exclude_control=exclude_control):
        tolerated = h.entry.category in WARNING_TOLERATED_CATEGORIES or h.entry.code in WARNING_TOLERATED_CODES
        if tolerated and any(a <= h.start < b for a, b in warnings):
            continue
        out.append(h)
    return out


@dataclass
class Catalog:
    domain: str
    product_paths: set[str]                 # chemins des fiches du catalogue (sans '/' final)
    product_names: set[str]                 # noms normalisés (norm)
    ingredient_aliases: dict[str, str]      # alias normalisé -> code substance vendue par la marque


@dataclass(frozen=True)
class ModeDecision:
    mode: Mode
    score: int
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Bridge:
    kind: BridgeKind
    node_key: str | None
    section: int
    is_template: bool
    evidence: str


@dataclass(frozen=True)
class RemediationOption:
    code: str
    residual_level: str
    explanation_fr: str


@dataclass
class RouterFinding:
    code: str                    # LEXIQUE | ASSOCIATION_INGREDIENT_PATHOLOGIE | ARTICLE_CIBLE_PATHOLOGIE_AVEC_PONT | PRESENTATION_MEDICAMENTEUSE
    level: str
    node_keys: list[str]
    quote: str
    category: str
    legal_basis: str
    lexical_code: str | None
    proximity: Proximity | None
    remediations: list[RemediationOption] = field(default_factory=list)


def detect_mode(page: PageExtraction) -> ModeDecision:
    """Score > 0 : produit ; < 0 : article ; égalité : PRODUIT (règles les plus strictes)."""
    score, ev = 0, []
    types = page.seo.jsonld_types
    if "Product" in types:
        score += 3; ev.append("JSON-LD Product")
    if types & {"Article", "BlogPosting", "NewsArticle"}:
        score -= 3; ev.append("JSON-LD Article")
    path = urlparse(page.url).path
    if PRODUCT_PATH.search(path):
        score += 2; ev.append(f"chemin produit {path}")
    if ARTICLE_PATH.search(path):
        score -= 2; ev.append(f"chemin éditorial {path}")
    if page.has_add_to_cart:
        score += 2; ev.append("formulaire d'ajout au panier")
    if page.seo.article_published_time:
        score -= 1; ev.append("date de publication d'article")
    return ModeDecision(Mode.PRODUIT if score >= 0 else Mode.ARTICLE, score, tuple(ev))


def assign_sections(page: PageExtraction) -> dict[str, int]:
    """Section = bloc entre deux intertitres H2/H3 ; -1 pour le gabarit global."""
    sections, current = {}, 0
    for n in page.nodes:
        if n.is_global:
            sections[n.key] = -1
            continue
        if n.zone == "TITRE" and n.heading_level in (2, 3):
            current += 1
        sections[n.key] = current
    return sections


def _contains(normalized_text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", normalized_text) is not None


def detect_bridges(page: PageExtraction, catalog: Catalog, sections: dict[str, int]) -> list[Bridge]:
    bridges: list[Bridge] = []
    for n in page.nodes:
        sec = sections.get(n.key, 0)
        for href, anchor in n.links:
            u = urlparse(href)
            same_site = not u.netloc or u.netloc.endswith(catalog.domain)
            if same_site and (u.path.rstrip("/") in catalog.product_paths or PRODUCT_PATH.search(u.path)):
                bridges.append(Bridge(BridgeKind.LIEN_PRODUIT, n.key, sec, n.is_global, f"« {anchor} » → {u.path}"))
        t = norm(n.text_plain)
        for name in catalog.product_names:
            if _contains(t, name):
                bridges.append(Bridge(BridgeKind.MENTION_PRODUIT, n.key, sec, n.is_global, name))
        for cta in CTA_LEXICON:
            if _contains(t, cta):
                bridges.append(Bridge(BridgeKind.CTA_COMMERCIAL, n.key, sec, n.is_global, cta))
        if DISCOUNT_CODE.search(n.text_plain):
            bridges.append(Bridge(BridgeKind.CODE_PROMO, n.key, sec, n.is_global, "code promo"))
    for b in page.commercial_blocks:
        anchor = page.nodes[b.after_node_index] if 0 <= b.after_node_index < len(page.nodes) else None
        bridges.append(Bridge(BridgeKind.ENCART_PRODUIT, anchor.key if anchor else None,
                              sections.get(anchor.key, 0) if anchor else 0,
                              anchor.is_global if anchor else False, b.evidence))
    return bridges


def proximity(node_key: str, sections: dict[str, int], bridges: list[Bridge]) -> Proximity:
    content = [b for b in bridges if not b.is_template]
    sec = sections.get(node_key)
    if any(b.node_key == node_key for b in content):
        return Proximity.MEME_SEGMENT
    if sec is not None and sec >= 0 and any(b.section == sec for b in content):
        return Proximity.MEME_SECTION
    if content:
        return Proximity.MEME_ARTICLE
    if any(b.is_template for b in bridges):
        return Proximity.GABARIT_SEULEMENT
    return Proximity.AUCUNE


def resolve_level(entry: LexEntry, mode: Mode, prox: Proximity) -> str:
    sev = entry.severity
    if mode is Mode.PRODUIT:
        return sev["PRODUIT"]
    if prox in (Proximity.MEME_SEGMENT, Proximity.MEME_SECTION):
        return sev["ARTICLE_AVEC_PONT"]
    if prox in (Proximity.MEME_ARTICLE, Proximity.GABARIT_SEULEMENT):
        floor = "ORANGE" if SEV_RANK[sev["ARTICLE_AVEC_PONT"]] >= SEV_RANK["ORANGE"] else "NEUTRE"
        return max_severity(sev["ARTICLE_SANS_PONT"], floor)
    return sev["ARTICLE_SANS_PONT"]


def _remediations(mode: Mode, entry: LexEntry, prox: Proximity | None, has_seo_bridge: bool,
                  ingredient_associated: bool) -> list[RemediationOption]:
    if mode is Mode.PRODUIT:
        opts = [RemediationOption("REECRIRE_ALLEGATION", "CONDITIONNEL_A_LA_REECRITURE",
                                  "Remplacer par une variante d'allégation autorisée applicable au produit.")]
        if entry.category == "VOCABULAIRE_PHARMACEUTIQUE":
            opts.append(RemediationOption("RENOMMER_INTITULE", "AUCUN_RISQUE_DETECTE",
                                          "Renommer l'intitulé (ex. « Posologie » → « Conseils d'utilisation ») ; les valeurs restent gelées."))
        return opts
    residual_without_bridge = entry.severity["ARTICLE_SANS_PONT"]
    if ingredient_associated:
        residual_without_bridge = max_severity(residual_without_bridge, "ORANGE")
    opts: list[RemediationOption] = []
    if prox in (Proximity.MEME_SEGMENT, Proximity.MEME_SECTION, Proximity.MEME_ARTICLE):
        opts.append(RemediationOption("SUPPRIMER_PONT", residual_without_bridge,
                                      "Retirer liens, encarts, CTA et codes promo de l'article ; garder le contenu informatif. "
                                      "La marque reste éditrice : le risque résiduel n'est pas nul."))
        opts.append(RemediationOption("SCINDER_ARTICLE", max_severity(residual_without_bridge, "ORANGE"),
                                      "Article d'information sans élément commercial + article distinct sur la fonction "
                                      "physiologique, qui porte les allégations autorisées et le lien produit."))
    if prox is Proximity.GABARIT_SEULEMENT:
        opts.append(RemediationOption("RETIRER_WIDGET_GABARIT", residual_without_bridge,
                                      "Retirer le widget produit du gabarit des articles de blog."))
    if has_seo_bridge:
        opts.append(RemediationOption("RECADRER_VERS_PONT_SEMANTIQUE", "CONDITIONNEL_A_LA_REECRITURE",
                                      "Recentrer l'article sur l'intention légale (pont sémantique) ancrée sur des "
                                      "allégations autorisées ; le trafic sur la pathologie est en grande partie perdu."))
    return opts


def route_page(page: PageExtraction, decision: ModeDecision, lexicon: LexiconIndex, catalog: Catalog, *,
               seo_bridged_lexical_codes: frozenset[str] = frozenset()) -> list[RouterFinding]:
    mode = decision.mode
    sections = assign_sections(page)
    bridges = detect_bridges(page, catalog, sections) if mode is Mode.ARTICLE else []
    findings: list[RouterFinding] = []
    therapeutic, pathologies, pharma = set(), set(), set()

    for n in page.nodes:
        if n.zone in NOT_ANALYZED_ZONES:
            continue
        hits = lexical_hits(lexicon, n.text_plain, exclude_control=False)
        categories = {h.entry.category for h in hits}
        t = norm(n.text_plain)
        ingredients = [a for a in catalog.ingredient_aliases if _contains(t, a)]
        prox = proximity(n.key, sections, bridges) if mode is Mode.ARTICLE else None
        associated = mode is Mode.ARTICLE and "PATHOLOGIE" in categories and bool(ingredients)
        for h in hits:
            if h.entry.is_control:
                continue
            level = resolve_level(h.entry, mode, prox or Proximity.AUCUNE)
            if SEV_RANK[level] == 0:
                continue
            if h.entry.category == "VERBE_THERAPEUTIQUE":
                therapeutic.add(h.entry.code)
            if h.entry.category in ("PATHOLOGIE", "TERME_MEDICAL") and SEV_RANK[level] >= SEV_RANK["ORANGE"]:
                pathologies.add(h.entry.code)
            if h.entry.category == "VOCABULAIRE_PHARMACEUTIQUE":
                pharma.add(h.entry.code)
            findings.append(RouterFinding(
                "LEXIQUE", level, [n.key], n.text_plain[h.start:h.end], h.entry.category, h.entry.legal_basis,
                h.entry.code, prox,
                _remediations(mode, h.entry, prox, h.entry.code in seo_bridged_lexical_codes, associated)))
        if associated:
            effect = bool(categories & {"VERBE_EFFET", "VERBE_THERAPEUTIQUE"})
            findings.append(RouterFinding(
                "ASSOCIATION_INGREDIENT_PATHOLOGIE", "ROUGE" if effect else "ORANGE", [n.key],
                n.text_plain[:160], "ASSOCIATION", "CSP_L5111_1_PRESENTATION", None, prox,
                [RemediationOption("DISSOCIER_INGREDIENT_PATHOLOGIE", "INFO",
                                   "Ne pas associer, dans un même passage, un ingrédient vendu par la marque à une pathologie.")]))

    if mode is Mode.ARTICLE and bridges:
        title = " ".join(filter(None, [page.seo.title, page.seo.jsonld_article_headline,
                                       *(n.text_plain for n in page.nodes if n.zone == "TITRE" and n.heading_level == 1)]))
        patho_in_title = [h for h in lexicon.scan(title, categories={"PATHOLOGIE"})]
        if patho_in_title:
            findings.append(RouterFinding(
                "ARTICLE_CIBLE_PATHOLOGIE_AVEC_PONT", "ROUGE", [], patho_in_title[0].surface, "PATHOLOGIE",
                "CSP_L5111_1_PRESENTATION", patho_in_title[0].entry.code, None,
                [RemediationOption("SUPPRIMER_PONT", "ORANGE", "Article ciblant une pathologie : retirer tout pont commercial."),
                 RemediationOption("RECADRER_VERS_PONT_SEMANTIQUE", "CONDITIONNEL_A_LA_REECRITURE",
                                   "Retitrer sur l'intention physiologique légale.")]))

    score = 2 * len(therapeutic) + len(pathologies) + len(pharma)
    level = ("ROUGE" if score >= PAGE_LEVEL_MEDICINAL_THRESHOLD["ROUGE"]
             else "ORANGE" if score >= PAGE_LEVEL_MEDICINAL_THRESHOLD["ORANGE"] else None)
    if level:
        findings.append(RouterFinding(
            "PRESENTATION_MEDICAMENTEUSE", level, [], f"score {score}", "AGREGATION", "CSP_L5111_1_PRESENTATION",
            None, None, [RemediationOption("TRAITER_LES_ROUGES_DE_LA_PAGE", "CONDITIONNEL_A_LA_REECRITURE",
                                           "Le score baisse mécaniquement quand les segments rouges sont traités.")]))
    return findings
