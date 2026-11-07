"""
engine/page_scope.py — Périmètre d'analyse d'une page récupérée par URL.

Une page produit réelle contient bien plus que le produit : méga-menu, tiroir de recherche et
panier, produits suggérés, avis clients, pied de page. Les analyser tous noie les constats utiles
(le menu « Détox · Coupe-faim · Ventre plat » n'est pas une allégation du produit analysé).

isolate() renvoie :
  * un document HTML réduit au contenu éditorial de la page (le <main>, sans les blocs hors
    périmètre), qui est seul passé au moteur ;
  * les informations produit (données structurées schema.org Product, H1, image, marque) ;
  * les avis clients, extraits à part (ils sont signalés séparément, pas mélangés aux constats) ;
  * un décompte de ce qui a été écarté, affiché à l'utilisateur.

detect_composition() repère les quantités déclarées dans la page (« 60 mg de vitamine C »,
tableau nutritionnel) pour les substances connues de la base. Le résultat est une proposition
affichée comme telle : l'utilisateur la corrige si besoin.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from lxml import etree
from lxml import html as lhtml

from .textnorm import norm, norm_with_offsets, original_span

# Blocs hors périmètre, repérés par balise, rôle ou signature (classe, id, type de section).
DROP_TAGS = {"header", "nav", "footer", "aside", "dialog", "form"}
DROP_ROLES = {"banner", "navigation", "contentinfo", "search", "dialog", "complementary"}
DROP_HINTS = (   # tiroirs, fenêtres et bandeaux (les modales produit, qui portent souvent la composition, restent)
    "search-drawer", "predictive-search", "cart-drawer", "mini-cart", "newsletter", "cookie",
    "announcement", "trust-icons", "trust_icons", "back-to-top", "social-share",
)
REVIEW_HINTS = (
    "customer-reviews", "customer_reviews", "product-reviews", "product_reviews", "reviews-widget",
    "testimonial", "avis-clients", "avis_clients", "jdgm", "judgeme", "yotpo", "stamped", "loox",
    "okendo", "trustpilot", "netreviews", "avis-verifies", "ryviu", "society-of-reviews",
)
PRODUCT_CARD_HINTS = ("product-card", "card-product", "product-item", "grid-product", "product-grid-item",
                      "productitem", "product-tile")
# Bloc vendable par la forme « 60 mg de vitamine C », « vitamine C : 60 mg », « Vitamine C ... 60 mg ».
_QTY = r"(\d+(?:[.,]\d+)?)\s*(mg|µg|μg|mcg|ug|g)\b"
_UNIT = {"mg": "mg", "µg": "µg", "μg": "µg", "mcg": "µg", "ug": "µg", "g": "g"}


@dataclass
class ProductInfo:
    name: str | None = None
    brand: str | None = None
    description: str | None = None
    category: str | None = None
    image: str | None = None
    gtin: str | None = None
    h1: str | None = None
    kind: str = "INCONNU"                      # PRODUIT | ARTICLE | INCONNU

    @property
    def title(self) -> str | None:
        return self.name or self.h1


@dataclass
class Scope:
    html: str                                  # document réduit, passé au moteur
    product: ProductInfo
    reviews: list[str] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)   # catégorie -> nombre de blocs écartés
    used_main: bool = False

    def dropped_summary(self) -> list[str]:
        labels = {"navigation": "menus et navigation", "autres_produits": "produits suggérés",
                  "pied_de_page": "en-tête et pied de page", "fenetres": "tiroirs, fenêtres et bandeaux",
                  "hors_main": "blocs hors du contenu principal"}
        return [f"{labels.get(k, k)} ({v})" for k, v in self.dropped.items() if v]


def _sig(el) -> str:
    return " ".join(filter(None, [el.get("class"), el.get("id"), el.get("data-section-type")])).lower()


def _jsonld(root):
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


def _first_str(v) -> str | None:
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list) and v:
        return _first_str(v[0])
    if isinstance(v, dict):
        return _first_str(v.get("name") or v.get("url") or v.get("contentUrl"))
    return None


def product_info(root, url: str = "") -> ProductInfo:
    info = ProductInfo()
    for o in _jsonld(root):
        types = _types(o)
        if "Product" in types:
            info.kind = "PRODUIT"
            info.name = info.name or _first_str(o.get("name"))
            info.brand = info.brand or _first_str(o.get("brand"))
            info.description = info.description or _first_str(o.get("description"))
            info.category = info.category or _first_str(o.get("category"))
            info.image = info.image or _first_str(o.get("image"))
            info.gtin = info.gtin or _first_str(o.get("gtin") or o.get("gtin13") or o.get("sku"))
        elif types & {"Article", "BlogPosting", "NewsArticle"} and info.kind == "INCONNU":
            info.kind = "ARTICLE"
            info.name = info.name or _first_str(o.get("headline"))
    h1 = root.xpath("//h1")
    if h1:
        info.h1 = re.sub(r"\s+", " ", h1[0].text_content()).strip() or None
    if not info.image:
        og = root.xpath('//meta[@property="og:image"]/@content')
        info.image = og[0] if og else None
    if info.kind == "INCONNU":
        path = urlparse(url).path.lower()
        if re.search(r"/(products?|produits?|p)/", path) or root.xpath('//form[contains(@action, "/cart/add")]'):
            info.kind = "PRODUIT"
        elif re.search(r"/(blogs?|articles?|actualites|conseils|magazine)/", path):
            info.kind = "ARTICLE"
    return info


def _classify_drop(el) -> str | None:
    tag = el.tag.lower() if isinstance(el.tag, str) else ""
    role = (el.get("role") or "").lower()
    sig = _sig(el)
    if tag in ("header", "footer") or role in ("banner", "contentinfo") or "footer" in sig:
        return "pied_de_page"
    if tag == "nav" or role in ("navigation",) or any(h in sig for h in ("mega-menu", "megamenu", "menu-drawer",
                                                                          "nav-drawer", "site-nav", "breadcrumb",
                                                                          "product-nav", "product_nav", "sommaire",
                                                                          "table-of-contents")):
        return "navigation"
    if any(h in sig for h in ("cross-sell", "cross_sell", "crosssell", "upsell", "up-sell", "related", "recommend",
                              "recently-viewed", "recently_viewed", "complementary", "you-may-also", "also-like")):
        return "autres_produits"
    if tag in ("dialog", "aside", "form") or role in ("dialog", "search", "complementary") or any(h in sig for h in DROP_HINTS):
        if tag == "form" and not el.xpath('.//*[contains(@class, "product") or contains(@id, "product")]') \
                and "cart" not in (el.get("action") or ""):
            return None                          # un formulaire de contenu (FAQ, filtre) reste analysé
        return "fenetres"
    return None


def _is_review(el) -> bool:
    sig = _sig(el)
    return any(h in sig for h in REVIEW_HINTS)


def _is_other_product_card(el, product_name: str | None, current_path: str) -> bool:
    sig = _sig(el)
    if not any(h in sig for h in PRODUCT_CARD_HINTS):
        return False
    if el.xpath(".//h1"):
        return False
    for href in el.xpath(".//a/@href"):
        p = urlparse(href).path.rstrip("/")
        if "/products/" in p and current_path and p != current_path:
            return True
    return bool(product_name) and norm(product_name) not in norm(el.text_content())


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.text_content()).strip()


def isolate(document_html: str, url: str) -> Scope:
    """Réduit une page complète à son contenu principal. Sans <main>, garde le <body> moins les blocs
    hors périmètre. N'invente rien : en cas de doute, un bloc est conservé."""
    root = lhtml.document_fromstring(document_html)
    info = product_info(root, url)
    current_path = urlparse(url).path.rstrip("/")
    dropped: dict[str, int] = {}
    reviews: list[str] = []

    body = root.find("body")
    if body is None:
        body = root
    mains = [m for m in root.xpath("//main | //*[@role='main']") if len(_text(m)) > 200]
    container = max(mains, key=lambda m: len(_text(m))) if mains else body
    used_main = container is not body
    if used_main:
        dropped["hors_main"] = sum(1 for c in body.iterdescendants() if isinstance(c.tag, str) and c.getparent() is body
                                   and c is not container and c.tag not in ("script", "style", "template", "noscript")
                                   and _text(c))

    # Parcours descendant : on retire les blocs entiers (le premier ancêtre qualifié l'emporte).
    for el in list(container.iterdescendants()):
        if not isinstance(el.tag, str) or el.getparent() is None:
            continue
        if el.tag in ("script", "style", "template", "noscript", "svg"):
            continue
        if _is_review(el):
            for item in el.xpath('.//*[contains(@class, "review") or contains(@class, "avis") '
                                 'or contains(@class, "testimonial") or self::blockquote]'):
                t = _text(item)
                if 20 <= len(t) <= 2000 and not any(t in r or r in t for r in reviews):
                    reviews.append(t)
            if not reviews and _text(el):
                reviews.extend(p for p in (_text(x) for x in el.xpath(".//p")) if len(p) >= 20)
            el.getparent().remove(el)
            continue
        kind = _classify_drop(el)
        if kind is None and _is_other_product_card(el, info.title, current_path):
            kind = "autres_produits"
        if kind:
            if el.xpath(".//h1") and kind != "pied_de_page":
                continue                          # ne jamais retirer le bloc qui porte le titre de la page
            dropped[kind] = dropped.get(kind, 0) + 1
            el.getparent().remove(el)

    head = root.find("head")
    head_html = etree.tostring(head, encoding="unicode", method="html") if head is not None else "<head><title></title></head>"
    inner = etree.tostring(container, encoding="unicode", method="html")
    if not used_main:
        inner = "".join(etree.tostring(c, encoding="unicode", method="html") for c in container)
        inner = f"<main>{inner}</main>"
    doc = f"<!DOCTYPE html><html>{head_html}<body>{inner}</body></html>"
    return Scope(doc, info, reviews[:200], dropped, used_main)


# ---------------------------------------------------------------------------
# Composition déclarée
# ---------------------------------------------------------------------------
@dataclass
class DetectedIngredient:
    substance: str
    label: str
    amount: float
    unit: str
    evidence: str


def detect_composition(texts: list[str], aliases: dict[str, list[str]], labels: dict[str, str],
                       max_items: int = 12) -> list[DetectedIngredient]:
    """aliases : code substance -> noms (normalisés ou non). Cherche, pour chaque mention d'une
    substance, la quantité la plus proche dans la même phrase ou ligne de tableau (fenêtre de 60
    caractères). Une substance retient la première quantité trouvée, en privilégiant les lignes de
    tableau et les formulations « X mg de <substance> »."""
    alias_list = sorted(((norm(a), code) for code, names in aliases.items() for a in names if len(norm(a)) >= 3),
                        key=lambda x: -len(x[0]))
    found: dict[str, DetectedIngredient] = {}
    qty_re = re.compile(_QTY, re.IGNORECASE)
    for raw in texts:
        if not raw or not re.search(r"\d", raw):
            continue
        t, offs = norm_with_offsets(raw)
        taken: list[tuple[int, int]] = []
        for a, code in alias_list:
            if code in found:
                continue
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", t):
                s, e = m.span()
                if any(not (e <= x or s >= y) for x, y in taken):
                    continue
                window_start, window_end = max(0, s - 60), min(len(t), e + 60)
                best = None
                for q in qty_re.finditer(t, window_start, window_end):
                    # « 60 mg de vitamine c » : quantité avant ; « vitamine c : 60 mg » : après
                    gap = s - q.end() if q.end() <= s else q.start() - e
                    if gap < 0:
                        continue
                    between = t[q.end():s] if q.end() <= s else t[e:q.start()]
                    if q.end() <= s:
                        # « 60 mg de vitamine C » : seule une préposition sépare la dose de la substance.
                        # « 6 g, arôme framboise, acide ascorbique » : la dose est celle de la portion, pas de la substance.
                        if not re.fullmatch(r"\s*(?:de |d'|d )?\s*(?:la |l')?\s*", between):
                            continue
                    elif re.search(r"[.;!?,]", between) or len(between) > 30:
                        # « vitamine C : 60 mg », « vitamine C (60 mg) », ligne de tableau « Vitamine C 60 mg 75 % »
                        continue
                    if any(a2 != a and a2 in between for a2, _ in alias_list[:400] if len(a2) >= 5):
                        continue                  # une autre substance s'intercale : ce n'est pas sa dose
                    if best is None or gap < best[0]:
                        best = (gap, q)
                if best is None:
                    continue
                q = best[1]
                amount = float(q.group(1).replace(",", "."))
                unit = _UNIT.get(q.group(2).lower(), q.group(2).lower())
                if amount <= 0 or (unit == "g" and amount > 100):
                    continue
                os_, oe = original_span(offs, min(s, q.start()), max(e, q.end()))
                found[code] = DetectedIngredient(code, labels.get(code, code), amount, unit, raw[os_:oe].strip()[:160])
                taken.append((s, e))
                break
            if len(found) >= max_items:
                break
    return list(found.values())
