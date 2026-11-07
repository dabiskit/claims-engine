"""
engine/dom_extractor.py — Extraction DOM exhaustive et patch par nœuds texte.

1. render_page()   : rendu Chromium (Playwright) puis annotation de visibilité en page.
2. extract_page()  : TOUS les nœuds texte (masqués compris : accordéons, onglets, badges,
                     puces, tableaux, micro-copies), numérotés N1…Nn, avec zone, visibilité,
                     section Shopify, liens, et balises inline converties en marqueurs
                     protégés ⟦M1⟧…⟦/M1⟧ (⟦M2/⟧ pour les éléments vides : br, img).
3. mark_global_nodes() : dédoublonnage inter-pages (bandeau, menu, footer, widgets de
                     gabarit). Rien n'est filtré : le global est analysé une fois par site.
4. patch_fragment(): réinjection dans un fragment HTML (description produit) avec
                     garanties vérifiées : même séquence de marqueurs, même squelette
                     (balises + attributs + commentaires), aucun autre nœud modifié,
                     stabilité au reparsing. Sinon : exception, pas d'export HTML.

Dépendances : lxml (obligatoire), playwright (rendu uniquement).
"""
from __future__ import annotations

import hashlib
import html as html_lib
import itertools
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from lxml import etree
from lxml import html as lhtml

from .textnorm import norm

INLINE_TAGS = frozenset({"a", "abbr", "b", "bdi", "bdo", "br", "cite", "code", "data", "dfn", "em", "font",
                         "i", "img", "kbd", "mark", "q", "s", "samp", "small", "span", "strong", "sub",
                         "sup", "time", "u", "var", "wbr"})
VOID_INLINE_TAGS = frozenset({"br", "wbr", "img"})
NONEMPTY_TAGS = frozenset({"a", "strong", "b", "em", "i", "u", "mark", "span"})
SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "object", "canvas",
                       "head", "meta", "link", "input", "textarea"})
HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

HINTS = {
    "BANDEAU_COOKIES": ("onetrust", "axeptio", "didomi", "cookiebot", "tarteaucitron", "cookie-banner",
                        "cookieconsent", "cc-window", "gdpr-banner"),
    "AVIS_CLIENTS": ("jdgm", "judgeme", "yotpo", "stamped", "loox", "okendo", "trustpilot", "netreviews",
                     "avis-verifies", "product-reviews", "reviews-widget", "ryviu"),
    "BANDEAU_ANNONCE": ("announcement", "annonce", "promo-bar", "topbar", "top-bar"),
    "ACCORDEON": ("accordion", "accordeon", "collapsible", "collapse", "tab-content", "tabs__panel",
                  "tabpanel", "faq", "toggle-content"),
    "CTA_MICROCOPIE": ("add-to-cart", "addtocart", "product-form", "buy-buttons", "shopify-payment",
                       "single_add_to_cart", "reassurance"),
    "BADGE": ("badge", "pill", "sticker", "ribbon", "chip", "product-label", "product-tag"),
    "DESCRIPTION": ("product__description", "product-single__description", "product-description",
                    "woocommerce-product-details__short-description", "woocommerce-tabs", "tab-description"),
}
PRODUCT_CARD_HINTS = ("product-card", "card-product", "product-item", "grid-product", "product-grid-item",
                      "featured-product", "product-block")
EXCLUDED_ZONES = frozenset({"BANDEAU_COOKIES"})
NOT_ANALYZED_ZONES = frozenset({"AVIS_CLIENTS"})       # extraits et comptés, non analysés en MVP
LANDMARK_ZONES = frozenset({"EN_TETE", "NAVIGATION", "PIED_DE_PAGE", "BANDEAU_ANNONCE"})

_TOKEN_RE = re.compile(r"⟦(/?)(M\d+)(/?)⟧")

# Exécuté dans la page après rendu : annote chaque élément (V visible, H masqué,
# C replié dans un <details> fermé) puis renvoie le DOM sérialisé.
ANNOTATE_JS = r"""
() => {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','IFRAME']);
  for (const el of document.querySelectorAll('body *')) {
    if (SKIP.has(el.tagName)) continue;
    let vis = 'V';
    const closedDetails = el.closest('details:not([open])');
    if (closedDetails && closedDetails !== el && !el.closest('summary')) vis = 'C';
    else {
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      if (cs.display === 'none' || cs.visibility === 'hidden' || el.hidden ||
          el.closest('[aria-hidden="true"]') || (r.width === 0 && r.height === 0)) vis = 'H';
    }
    el.setAttribute('data-cx-vis', vis);
  }
  return '<!DOCTYPE html>' + document.documentElement.outerHTML;
}
"""


class RenderError(Exception):
    pass


class AccessBlocked(RenderError):
    """Boutique protégée par mot de passe, barrière d'âge ou anti-bot : repli sur le
    copier-coller du texte, ou lecture OAuth via l'app Shopify (V2)."""


class MarkerViolation(Exception):
    pass


class StructuralViolation(Exception):
    pass


async def render_page(url: str, *, timeout_ms: int = 30000, settle_ms: int = 1500,
                      user_agent: str = "ClaimsPreAnalysisBot/1.0 (+https://example.invalid/bot)") -> str:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(user_agent=user_agent, viewport={"width": 1366, "height": 900})
            response = await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            if response is None or response.status >= 400:
                raise RenderError(f"HTTP {response.status if response else 'sans réponse'} sur {url}")
            final_path = urlparse(page.url).path
            if final_path.startswith("/password") or await page.locator("form[action*='password']").count():
                raise AccessBlocked(f"Boutique protégée par mot de passe : {url}")
            if await page.locator("#challenge-form, .cf-challenge, #cf-challenge-running").count():
                raise AccessBlocked(f"Protection anti-bot : {url}")
            await page.wait_for_timeout(settle_ms)
            return await page.evaluate(ANNOTATE_JS)
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MarkerSpec:
    ref: str
    tag: str                                  # nom de balise, ou '#comment'
    attrs: tuple[tuple[str, str], ...]
    void: bool


@dataclass
class TextNode:
    key: str
    host_xpath: str
    slot: str                                 # 'inline' | 'text' | 'tail:<i>'
    tag: str
    zone: str
    in_description: bool
    heading_level: int | None
    visibility: str
    shopify_section: str | None
    text_marked: str
    text_plain: str
    markers: dict[str, MarkerSpec]
    links: list[tuple[str, str]]              # (URL absolue, texte d'ancre)
    patchable: bool = True
    is_global: bool = False

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text_marked.encode("utf-8")).hexdigest()


@dataclass
class CommercialBlock:
    kind: str                                 # 'ENCART_PRODUIT' | 'FORMULAIRE_PANIER'
    after_node_index: int                     # index (0-based) du dernier nœud qui précède le bloc
    evidence: str


@dataclass
class SeoFields:
    title: str | None = None
    meta_description: str | None = None
    og_title: str | None = None
    og_description: str | None = None
    canonical_url: str | None = None
    slug: str | None = None
    image_alts: list[tuple[str, str]] = field(default_factory=list)
    jsonld_types: set[str] = field(default_factory=set)
    jsonld_product_name: str | None = None
    jsonld_product_description: str | None = None
    jsonld_faq: list[tuple[str, str]] = field(default_factory=list)
    jsonld_article_headline: str | None = None
    jsonld_article_description: str | None = None
    article_published_time: str | None = None

    def analyzable_texts(self) -> list[tuple[str, str]]:
        """(champ, texte) à passer dans le même pipeline que les nœuds de page."""
        out = [(k, v) for k, v in (("title", self.title), ("meta_description", self.meta_description),
                                    ("og_title", self.og_title), ("og_description", self.og_description),
                                    ("jsonld_product_description", self.jsonld_product_description),
                                    ("jsonld_article_description", self.jsonld_article_description)) if v]
        if self.slug:
            out.append(("slug", self.slug.replace("-", " ")))
        out += [(f"img_alt:{src}", alt) for src, alt in self.image_alts if alt.strip()]
        for i, (q, a) in enumerate(self.jsonld_faq, start=1):
            out += [(f"jsonld_faq_q:{i}", q), (f"jsonld_faq_a:{i}", a)]
        return out


@dataclass
class PageExtraction:
    url: str
    nodes: list[TextNode]
    seo: SeoFields
    commercial_blocks: list[CommercialBlock]
    has_add_to_cart: bool
    dom_sha256: str


# ---------------------------------------------------------------------------
# Hôtes de texte et marqueurs
# ---------------------------------------------------------------------------
def _tag(el) -> str | None:
    return el.tag.lower() if isinstance(el.tag, str) else None


def _is_pure_inline(el) -> bool:
    if not isinstance(el.tag, str):
        return True                           # commentaire : traité comme marqueur vide
    if _tag(el) not in INLINE_TAGS:
        return False
    return all(_is_pure_inline(c) for c in el)


def _inner_text(el) -> str:
    parts = [el.text or ""] if isinstance(el.tag, str) else []
    for c in el:
        if isinstance(c.tag, str) and _tag(c) not in SKIP_TAGS:
            parts.append(_inner_text(c))
        parts.append(c.tail or "")
    return "".join(parts)


def _iter_hosts(el, out: list) -> None:
    """Hôte 'inline' : élément ne contenant que du texte et de l'inline pur.
    Contenu mixte (texte + blocs) : textes orphelins en hôtes 'text' ou 'tail:<i>'."""
    tag = _tag(el)
    if tag is None or tag in SKIP_TAGS:
        return
    kids = list(el)
    if all(_is_pure_inline(k) for k in kids):
        if _inner_text(el).strip():
            out.append((el, "inline"))
        return
    if el.text and el.text.strip():
        out.append((el, "text"))
    for i, k in enumerate(kids):
        if isinstance(k.tag, str):
            if _is_pure_inline(k):
                if _inner_text(k).strip():
                    out.append((k, "inline"))
            else:
                _iter_hosts(k, out)
        if k.tail and k.tail.strip():
            out.append((el, f"tail:{i}"))


def _to_marked(host, slot: str) -> tuple[str, dict[str, MarkerSpec], bool]:
    """Retourne (texte marqué, marqueurs, patchable)."""
    if slot == "text":
        raw = host.text or ""
        return raw, {}, "⟦" not in raw and "⟧" not in raw
    if slot.startswith("tail:"):
        raw = list(host)[int(slot[5:])].tail or ""
        return raw, {}, "⟦" not in raw and "⟧" not in raw
    markers: dict[str, MarkerSpec] = {}
    counter = itertools.count(1)
    parts: list[str] = []
    patchable = True

    def add_raw(t: str | None) -> None:
        nonlocal patchable
        if t:
            if "⟦" in t or "⟧" in t:
                patchable = False
                t = t.replace("⟦", "[").replace("⟧", "]")
            parts.append(t)

    def walk(el) -> None:
        if isinstance(el.tag, str):
            add_raw(el.text)
        for c in el:
            ref = f"M{next(counter)}"
            if not isinstance(c.tag, str):
                markers[ref] = MarkerSpec(ref, "#comment", (("text", c.text or ""),), True)
                parts.append(f"⟦{ref}/⟧")
            elif _tag(c) in VOID_INLINE_TAGS:
                markers[ref] = MarkerSpec(ref, _tag(c), tuple(c.attrib.items()), True)
                parts.append(f"⟦{ref}/⟧")
            else:
                markers[ref] = MarkerSpec(ref, _tag(c), tuple(c.attrib.items()), False)
                parts.append(f"⟦{ref}⟧")
                walk(c)
                parts.append(f"⟦/{ref}⟧")
            add_raw(c.tail)

    walk(host)
    return "".join(parts), markers, patchable


def plain_from_marked(marked: str, markers: dict[str, MarkerSpec]) -> str:
    def repl(m: re.Match) -> str:
        spec = markers.get(m.group(2))
        return "\n" if spec and spec.tag == "br" else ""
    return _TOKEN_RE.sub(repl, marked)


def tokenize_marked(marked: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    for m in _TOKEN_RE.finditer(marked):
        if m.start() > pos:
            tokens.append(("T", marked[pos:m.start()]))
        closing, ref, void = m.group(1), m.group(2), m.group(3)
        if closing and void:
            raise MarkerViolation(f"Marqueur mal formé : {m.group(0)}")
        tokens.append(("C" if closing else "V" if void else "O", ref))
        pos = m.end()
    if pos < len(marked):
        tokens.append(("T", marked[pos:]))
    for kind, val in tokens:
        if kind == "T" and ("⟦" in val or "⟧" in val):
            raise MarkerViolation("Marqueur inconnu ou altéré dans le texte")
    return tokens


def assert_marker_sequence(original_marked: str, new_marked: str, markers: dict[str, MarkerSpec]) -> None:
    """Même séquence ordonnée d'ouvertures, fermetures et marqueurs vides ; liens et
    emphases jamais vidés."""
    original = [t for t in tokenize_marked(original_marked) if t[0] != "T"]
    tokens = tokenize_marked(new_marked)
    structure = [t for t in tokens if t[0] != "T"]
    if structure != original:
        raise MarkerViolation(f"Séquence de balises inline modifiée : attendu {original}, obtenu {structure}")
    for i, (kind, ref) in enumerate(tokens):
        if kind == "O" and markers[ref].tag in NONEMPTY_TAGS:
            j = tokens.index(("C", ref), i)
            inner = tokens[i + 1:j]
            if not any((k == "T" and v.strip()) or k == "V" for k, v in inner):
                raise MarkerViolation(f"Contenu vidé pour <{markers[ref].tag}> ({ref})")


def _visibility(el) -> str:
    seen = set()
    for a in [el, *el.iterancestors()]:
        v = a.get("data-cx-vis") if isinstance(a.tag, str) else None
        if v:
            seen.add(v)
    return "C" if "C" in seen else "H" if "H" in seen else "V"


def _signature(el) -> str:
    return " ".join(filter(None, [el.get("class"), el.get("id"), el.get("data-section-type")])).lower()


def _classify_zone(host) -> tuple[str, bool]:
    chain = [host, *host.iterancestors()]
    in_description = any(any(h in _signature(a) for h in HINTS["DESCRIPTION"]) or a.get("itemprop") == "description"
                         for a in chain)
    for a in chain:
        sig = _signature(a)
        for zone in ("BANDEAU_COOKIES", "AVIS_CLIENTS", "BANDEAU_ANNONCE"):
            if any(h in sig for h in HINTS[zone]):
                return zone, in_description
    for a in chain:
        tag, role = _tag(a), (a.get("role") or "").lower()
        if tag == "header" or role == "banner":
            return "EN_TETE", in_description
        if tag == "nav" or role == "navigation":
            return "NAVIGATION", in_description
        if tag == "footer" or role == "contentinfo":
            return "PIED_DE_PAGE", in_description
    for a in chain:                             # le plus proche l'emporte
        tag, sig, role = _tag(a), _signature(a), (a.get("role") or "").lower()
        if tag in HEADINGS:
            return "TITRE", in_description
        if tag in ("details", "summary") or any(h in sig for h in HINTS["ACCORDEON"]):
            return "ACCORDEON", in_description
        if tag == "button" or role == "button" or any(h in sig for h in HINTS["CTA_MICROCOPIE"]):
            return "CTA_MICROCOPIE", in_description
        if any(h in sig for h in HINTS["BADGE"]):
            return "BADGE", in_description
        if tag == "li":
            return "PUCE", in_description
        if tag in ("td", "th", "caption"):
            return "TABLEAU", in_description
        if any(h in sig for h in HINTS["DESCRIPTION"]) or a.get("itemprop") == "description":
            return "DESCRIPTION", in_description
    return "CORPS", in_description


def _heading_level(host) -> int | None:
    for a in [host, *host.iterancestors()]:
        if _tag(a) in HEADINGS:
            return int(_tag(a)[1])
    return None


def _shopify_section(host) -> str | None:
    for a in [host, *host.iterancestors()]:
        i = a.get("id") or ""
        if i.startswith("shopify-section-"):
            return i
    return None


def _links(marked: str, markers: dict[str, MarkerSpec], base_url: str) -> list[tuple[str, str]]:
    tokens = tokenize_marked(marked)
    out = []
    for i, (kind, ref) in enumerate(tokens):
        if kind == "O" and markers[ref].tag == "a":
            href = dict(markers[ref].attrs).get("href")
            if href:
                j = tokens.index(("C", ref), i)
                anchor = "".join(v for k, v in tokens[i + 1:j] if k == "T")
                out.append((urljoin(base_url, href), anchor.strip()))
    return out


# ---------------------------------------------------------------------------
# Extraction de page
# ---------------------------------------------------------------------------
def _jsonld_objects(root):
    for s in root.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(s.text or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack.extend(o)
            elif isinstance(o, dict):
                yield o
                g = o.get("@graph")
                if g:
                    stack.extend(g if isinstance(g, list) else [g])


def _types(o: dict) -> set[str]:
    t = o.get("@type")
    return set(t) if isinstance(t, list) else {t} if isinstance(t, str) else set()


def _extract_seo(root, url: str) -> SeoFields:
    seo = SeoFields()
    t = root.find(".//title")
    seo.title = (t.text_content().strip() if t is not None else None) or None

    def meta(xpath: str) -> str | None:
        r = root.xpath(xpath)
        return (r[0].get("content") or "").strip() or None if r else None

    seo.meta_description = meta('//meta[@name="description"]')
    seo.og_title = meta('//meta[@property="og:title"]')
    seo.og_description = meta('//meta[@property="og:description"]')
    seo.article_published_time = meta('//meta[@property="article:published_time"]')
    canon = root.xpath('//link[@rel="canonical"]/@href')
    seo.canonical_url = urljoin(url, canon[0]) if canon else url
    seo.slug = urlparse(seo.canonical_url).path.rstrip("/").rsplit("/", 1)[-1] or None
    seo.image_alts = [(img.get("src") or "", img.get("alt") or "") for img in root.iter("img") if img.get("alt")]
    for o in _jsonld_objects(root):
        types = _types(o)
        seo.jsonld_types |= types
        if "Product" in types:
            seo.jsonld_product_name = seo.jsonld_product_name or o.get("name")
            seo.jsonld_product_description = seo.jsonld_product_description or o.get("description")
        if types & {"Article", "BlogPosting", "NewsArticle"}:
            seo.jsonld_article_headline = seo.jsonld_article_headline or o.get("headline")
            seo.jsonld_article_description = seo.jsonld_article_description or o.get("description")
        if "FAQPage" in types:
            entities = o.get("mainEntity") or []
            for q in entities if isinstance(entities, list) else [entities]:
                if isinstance(q, dict):
                    ans = q.get("acceptedAnswer") or {}
                    seo.jsonld_faq.append((str(q.get("name") or ""),
                                           str(ans.get("text") or "") if isinstance(ans, dict) else ""))
    return seo


def extract_page(rendered_html: str, url: str) -> PageExtraction:
    root = lhtml.document_fromstring(rendered_html)
    body = root.find("body")
    if body is None:
        body = root
    hosts: list = []
    _iter_hosts(body, hosts)
    order = {el: i for i, el in enumerate(root.iter())}
    tree = root.getroottree()
    nodes: list[TextNode] = []
    host_orders: list[int] = []
    for el, slot in hosts:
        zone, in_desc = _classify_zone(el)
        if zone in EXCLUDED_ZONES:
            continue
        marked, markers, patchable = _to_marked(el, slot)
        plain = plain_from_marked(marked, markers)
        if not plain.strip():
            continue
        nodes.append(TextNode(
            key=f"N{len(nodes) + 1}", host_xpath=tree.getpath(el), slot=slot, tag=_tag(el) or "",
            zone=zone, in_description=in_desc, heading_level=_heading_level(el),
            visibility=_visibility(el), shopify_section=_shopify_section(el), text_marked=marked,
            text_plain=plain, markers=markers, links=_links(marked, markers, url), patchable=patchable,
            is_global=zone in LANDMARK_ZONES))
        host_orders.append(order[el])

    blocks: list[CommercialBlock] = []
    card_xpath = " | ".join(f'//*[contains(@class, "{h}")]' for h in PRODUCT_CARD_HINTS)
    for el in root.xpath(card_xpath + ' | //*[@data-product-id] | //*[@data-product-handle]'):
        blocks.append(CommercialBlock("ENCART_PRODUIT", sum(o < order[el] for o in host_orders) - 1,
                                      (el.get("class") or el.get("data-product-handle") or "")[:80]))
    cart_forms = root.xpath('//form[contains(@action, "/cart/add")] | //*[@name="add-to-cart"]'
                            ' | //*[contains(@class, "single_add_to_cart_button")]')
    for el in cart_forms:
        blocks.append(CommercialBlock("FORMULAIRE_PANIER", sum(o < order[el] for o in host_orders) - 1,
                                      el.get("action") or el.get("class") or ""))
    return PageExtraction(url=url, nodes=nodes, seo=_extract_seo(root, url), commercial_blocks=blocks,
                          has_add_to_cart=bool(cart_forms),
                          dom_sha256=hashlib.sha256(rendered_html.encode("utf-8")).hexdigest())


def mark_global_nodes(pages: list[PageExtraction], min_pages: int = 3) -> None:
    """Un texte présent à l'identique sur toutes les pages échantillonnées (au moins
    min_pages) est un élément de gabarit : analysé une fois, au niveau du site."""
    if len(pages) < 2:
        return
    counts: dict[str, int] = {}
    for p in pages:
        for t in {norm(n.text_plain) for n in p.nodes}:
            counts[t] = counts.get(t, 0) + 1
    threshold = min(min_pages, len(pages))
    for p in pages:
        for n in p.nodes:
            if counts.get(norm(n.text_plain), 0) >= threshold:
                n.is_global = True


# ---------------------------------------------------------------------------
# Patch d'un fragment HTML (description produit)
# ---------------------------------------------------------------------------
def parse_fragment(fragment_html: str):
    return lhtml.fragment_fromstring(fragment_html, create_parent="div")


def serialize_fragment(wrapper) -> str:
    return html_lib.escape(wrapper.text or "", quote=False) + "".join(
        lhtml.tostring(c, encoding="unicode", with_tail=True) for c in wrapper)


def skeleton(wrapper) -> list[tuple]:
    out = []
    for el in wrapper.iter():
        if el is wrapper:
            continue
        if isinstance(el.tag, str):
            out.append(("E", el.tag.lower(), tuple(sorted(el.attrib.items()))))
        else:
            out.append(("C", el.text or ""))
    return out


def fragment_hosts(wrapper) -> list[tuple[str, object, str]]:
    hosts: list = []
    _iter_hosts(wrapper, hosts)
    return [(f"N{i}", el, slot) for i, (el, slot) in enumerate(hosts, start=1)]


def fragment_nodes(fragment_html: str) -> list[tuple[str, str, dict[str, MarkerSpec]]]:
    """(clé, texte marqué, marqueurs) des nœuds d'un fragment, dans l'ordre."""
    wrapper = parse_fragment(fragment_html)
    out = []
    for key, el, slot in fragment_hosts(wrapper):
        marked, markers, _ = _to_marked(el, slot)
        out.append((key, marked, markers))
    return out


def is_roundtrip_stable(fragment_html: str) -> bool:
    w1 = parse_fragment(fragment_html)
    w2 = parse_fragment(serialize_fragment(w1))
    texts = lambda w: [_to_marked(e, s)[0] for _, e, s in fragment_hosts(w)]  # noqa: E731
    return skeleton(w1) == skeleton(w2) and texts(w1) == texts(w2)


def _rebuild_inline(host, new_marked: str, markers: dict[str, MarkerSpec]) -> None:
    tail = host.tail
    for c in list(host):
        host.remove(c)
    host.text = None
    stack = [host]
    for kind, val in tokenize_marked(new_marked):
        if kind == "T":
            cur = stack[-1]
            if len(cur):
                cur[-1].tail = (cur[-1].tail or "") + val
            else:
                cur.text = (cur.text or "") + val
            continue
        if kind == "C":
            stack.pop()
            continue
        spec = markers[val]
        node = (etree.Comment(dict(spec.attrs).get("text", "")) if spec.tag == "#comment"
                else host.makeelement(spec.tag, dict(spec.attrs)))
        stack[-1].append(node)
        if kind == "O":
            stack.append(node)
    host.tail = tail


def patch_fragment(fragment_html: str, node_key: str, new_marked: str) -> str:
    """Remplace le texte d'UN nœud. Lève StructuralViolation ou MarkerViolation si la
    moindre balise, le moindre attribut ou le texte d'un autre nœud change."""
    if not new_marked.strip():
        raise StructuralViolation("Remplacement vide refusé")
    if not is_roundtrip_stable(fragment_html):
        raise StructuralViolation("HTML source instable au reparsing : export HTML refusé, export texte uniquement")
    wrapper = parse_fragment(fragment_html)
    hosts = fragment_hosts(wrapper)
    by_key = {k: (el, slot) for k, el, slot in hosts}
    if node_key not in by_key:
        raise KeyError(node_key)
    el, slot = by_key[node_key]
    original_marked, markers, patchable = _to_marked(el, slot)
    if not patchable:
        raise StructuralViolation("Nœud contenant des caractères réservés : export texte uniquement")
    others_before = {k: _to_marked(e, s)[0] for k, e, s in hosts if k != node_key}
    skeleton_before = skeleton(wrapper)

    if slot == "inline":
        assert_marker_sequence(original_marked, new_marked, markers)
        _rebuild_inline(el, new_marked, markers)
    else:
        if "⟦" in new_marked or "⟧" in new_marked:
            raise MarkerViolation("Aucun marqueur admis dans ce nœud")
        if slot == "text":
            el.text = new_marked
        else:
            list(el)[int(slot[5:])].tail = new_marked

    if skeleton(wrapper) != skeleton_before:
        raise StructuralViolation("Squelette HTML modifié")
    others_after = {k: _to_marked(e, s)[0] for k, e, s in fragment_hosts(wrapper) if k != node_key}
    if others_after != others_before:
        raise StructuralViolation("Un autre nœud a été modifié")
    out = serialize_fragment(wrapper)
    if skeleton(parse_fragment(out)) != skeleton_before:
        raise StructuralViolation("Squelette modifié au reparsing")
    return out
