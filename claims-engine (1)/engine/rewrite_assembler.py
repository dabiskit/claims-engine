"""
engine/rewrite_assembler.py — Construction du payload du réécrivain, assemblage et
validation de sa sortie.

Pourquoi le LLM ne peut ni inventer une allégation ni altérer une dose :
  * il ne renvoie pas de texte final mais une suite de SPANS typés ;
  * une allégation est un span ALLEGATION portant un identifiant de variante choisi dans
    une énumération fermée (JSON Schema strict) : le texte vient de kb.claim_variants ;
  * une valeur gelée est un span ZONE_GELEE portant un identifiant F1…Fn : le texte vient
    de la source ; le texte libre ne peut contenir AUCUN chiffre ;
  * une balise inline est un span BALISE_* portant un identifiant M1…Mn.
Le seul canal libre est TEXTE_LIBRE. Il est contrôlé ici de façon déterministe (verbes
d'effet, thèmes physiologiques non ancrés, lexique, termes interdits, chiffres, substances
absentes du produit, attributs factuels inventés), puis le pipeline repasse l'extracteur
LLM sur le texte assemblé. Toute erreur rend la proposition REJETEE : rien n'est publié.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .dom_extractor import MarkerSpec, MarkerViolation, assert_marker_sequence, tokenize_marked
from .frozen_zones import FrozenScan, FrozenZoneViolation, assert_frozen_preserved
from .lexicon import SEV_RANK, LexiconIndex
from .textnorm import norm

SENTENCE_END = re.compile(r"(?:^|[.!?…]\s*)$")


class UnsupportedSegment(Exception):
    """Zone gelée à cheval sur une balise inline : réécriture manuelle uniquement."""


@dataclass
class AssemblyResult:
    status: str                                   # VALIDEE | REJETEE_VALIDATEUR | IMPOSSIBLE
    marked: str | None = None
    plain: str | None = None
    used_variant_refs: list[str] = field(default_factory=list)
    used_claim_refs: list[str] = field(default_factory=list)
    appended_notices: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    report: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------
def build_reference_text(marked: str, markers: dict[str, MarkerSpec], scan: FrozenScan) -> str:
    """Remplace chaque zone gelée par ⟦Fk⟧ dans le texte marqué (offsets du texte brut)."""
    out: list[str] = []
    plain_pos = 0
    zones = sorted(scan.zones, key=lambda z: z.start)
    for kind, val in tokenize_marked(marked):
        if kind != "T":
            out.append(f"⟦/{val}⟧" if kind == "C" else f"⟦{val}/⟧" if kind == "V" else f"⟦{val}⟧")
            if kind == "V" and markers[val].tag == "br":
                plain_pos += 1
            continue
        seg_start, seg_end = plain_pos, plain_pos + len(val)
        cursor = 0
        for z in zones:
            if z.end <= seg_start or z.start >= seg_end:
                continue
            if z.start < seg_start or z.end > seg_end:
                raise UnsupportedSegment(f"Zone gelée {z.ref} à cheval sur une balise")
            out.append(val[cursor:z.start - seg_start])
            out.append(f"⟦{z.ref}⟧")
            cursor = z.end - seg_start
        out.append(val[cursor:])
        plain_pos = seg_end
    return "".join(out)


# ---------------------------------------------------------------------------
# Assemblage
# ---------------------------------------------------------------------------
def _adjust_case(text: str, at_sentence_start: bool) -> str:
    if not text:
        return text
    first = text[0].upper() if at_sentence_start else text[0].lower()
    return first + text[1:]


def _plain_tail(parts: list[str]) -> str:
    return re.sub(r"⟦/?M\d+/?⟧", "", "".join(parts)).rstrip()


def _bounded(needle: str, hay: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", hay) is not None


def assemble_and_validate(llm_output: dict, payload: dict, *, original_marked: str,
                          markers: dict[str, MarkerSpec], source_scan: FrozenScan,
                          lexicon: LexiconIndex, compiled_rules: dict,
                          notices_by_claim: dict[str, list[str]]) -> AssemblyResult:
    if llm_output.get("status") == "IMPOSSIBLE_SANS_INVENTER":
        return AssemblyResult("IMPOSSIBLE", report={"notes_fr": llm_output.get("notes_fr", "")})

    errors: list[str] = []
    variants = {v["variant_ref"]: (c["claim_ref"], v) for c in payload["allowed_claims"] for v in c["variants"]}
    frozen = {z["ref"]: z for z in payload["frozen_zones"]}
    product_names = payload.get("product_names", [])

    parts: list[str] = []
    free_texts: list[str] = []
    used_variants: list[str] = []
    used_claims: list[str] = []
    frozen_uses: dict[str, int] = {}
    claim_end_positions: dict[str, int] = {}

    for i, span in enumerate(llm_output.get("spans", [])):
        kind, ref = span.get("kind"), span.get("ref")
        if kind == "TEXTE_LIBRE":
            text = span.get("text") or ""
            if "⟦" in text or "⟧" in text:
                errors.append(f"span {i} : marqueur dans le texte libre")
            parts.append(text)
            free_texts.append(text)
        elif kind == "ALLEGATION":
            if ref not in variants:
                errors.append(f"span {i} : variante inconnue {ref}")
                continue
            claim_ref, v = variants[ref]
            start = not _plain_tail(parts) or SENTENCE_END.search(_plain_tail(parts)) is not None
            parts.append(_adjust_case(v["text"], start))
            used_variants.append(ref)
            used_claims.append(claim_ref)
            claim_end_positions[claim_ref] = len(parts)
        elif kind == "ZONE_GELEE":
            if ref not in frozen:
                errors.append(f"span {i} : zone gelée inconnue {ref}")
                continue
            parts.append(frozen[ref]["text"])
            frozen_uses[ref] = frozen_uses.get(ref, 0) + 1
        elif kind in ("BALISE_OUVRANTE", "BALISE_FERMANTE", "BALISE_VIDE"):
            if ref not in markers:
                errors.append(f"span {i} : balise inconnue {ref}")
                continue
            parts.append({"BALISE_OUVRANTE": f"⟦{ref}⟧", "BALISE_FERMANTE": f"⟦/{ref}⟧",
                          "BALISE_VIDE": f"⟦{ref}/⟧"}[kind])
        else:
            errors.append(f"span {i} : type inconnu {kind}")

    # 1. Unicité des allégations
    if len(set(used_variants)) != len(used_variants) or len(set(used_claims)) != len(used_claims):
        errors.append("Allégation répétée dans le segment")

    # 2. Zones gelées : usage déclaratif
    for ref, z in frozen.items():
        n = frozen_uses.get(ref, 0)
        if not z["removable"] and n != 1:
            errors.append(f"Zone protégée {ref} utilisée {n} fois (attendu : 1)")
        if z["removable"] and n > 1:
            errors.append(f"Zone supprimable {ref} dupliquée")

    # 3. Mentions obligatoires : astérisque après l'allégation, mention en fin de segment
    appended: list[str] = []
    for claim_ref in used_claims:
        for notice in notices_by_claim.get(claim_ref, []):
            stars = "*" * (len(appended) + 1)
            pos = claim_end_positions[claim_ref]
            parts.insert(pos, stars)
            claim_end_positions = {k: (p + 1 if p >= pos else p) for k, p in claim_end_positions.items()}
            appended.append(f"{stars} {notice}.")
    body_marked = "".join(parts)
    marked = body_marked + ("".join(" " + a for a in appended) if appended else "")

    # 4. Balises inline : même séquence que l'original
    try:
        assert_marker_sequence(original_marked, body_marked, markers)
    except MarkerViolation as e:
        errors.append(f"Balises : {e}")

    body_plain = re.sub(r"⟦/?M\d+/?⟧", "", body_marked).replace("*", "")
    digit_aliases = [a["norm"] for a in compiled_rules.get("substance_aliases", []) if re.search(r"\d", a["norm"])]

    # 5. Zones gelées : assertion par re-extraction (avant ajout des mentions, issues de la base)
    try:
        assert_frozen_preserved(source_scan, body_plain, product_names=product_names, extra_identifiers=digit_aliases)
    except FrozenZoneViolation as e:
        errors.append(f"Zones gelées : {e}")

    # 6. Texte libre
    allowed_substances = {norm(n) for s in payload.get("substances_in_product", []) for n in s["names"]}
    source_attrs = {h.entry.code + ":" + h.surface for h in lexicon.scan(payload["segment"]["original_plain"],
                                                                         categories={"ATTRIBUT_FACTUEL"})}
    forbidden = [norm(t) for t in payload.get("forbidden_terms", [])]
    # Art. 10§3 : un thème général doit être accompagné d'une allégation spécifique, dans le segment ou
    # dans un segment voisin déjà validé (ex. un intertitre accompagné par le paragraphe qu'il annonce).
    anchoring = set(used_claims) | set(payload.get("accompanying_claim_refs", []))
    topic_ok = {t["topic_key"] for t in payload.get("allowed_topics", []) if set(t["anchor_claim_refs"]) & anchoring}
    free_view = " ¤ ".join(free_texts)
    free_norm = norm(free_view)
    free_wo_idents = free_norm
    for alias in sorted(digit_aliases, key=len, reverse=True):
        free_wo_idents = re.sub(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", " ", free_wo_idents)
    if re.search(r"\d", free_wo_idents):
        errors.append("Chiffre dans le texte libre (seules les zones gelées peuvent porter des chiffres)")
    for h in lexicon.scan(free_view):
        cat = h.entry.category
        if cat == "VERBE_EFFET":
            errors.append(f"Verbe d'effet dans le texte libre : « {h.surface} » (un effet ne s'exprime que par une allégation)")
        elif cat == "THEME_PHYSIOLOGIQUE" and h.entry.topic not in topic_ok:
            errors.append(f"Thème « {h.surface} » sans allégation d'ancrage dans le segment")
        elif cat == "ATTRIBUT_FACTUEL" and (h.entry.code + ":" + h.surface) not in source_attrs:
            errors.append(f"Attribut factuel absent de la source : « {h.surface} »")
        elif not h.entry.is_control and SEV_RANK[h.entry.severity[payload["mode"] if payload["mode"] == "PRODUIT" else "ARTICLE_AVEC_PONT"]] >= SEV_RANK["ORANGE"]:
            errors.append(f"Terme à risque dans le texte libre : « {h.surface} » ({cat})")
    for t in forbidden:
        if t and _bounded(t, free_norm):
            errors.append(f"Terme interdit réintroduit : « {t} »")
    for a in compiled_rules.get("substance_aliases", []):
        if _bounded(a["norm"], free_norm) and a["norm"] not in allowed_substances:
            if not any(_bounded(a["norm"], s) for s in allowed_substances):
                errors.append(f"Substance absente du produit citée : « {a['norm']} »")

    status = "VALIDEE" if not errors else "REJETEE_VALIDATEUR"
    return AssemblyResult(status, marked if not errors else None,
                          re.sub(r"⟦/?M\d+/?⟧", "", marked) if not errors else None,
                          used_variants, used_claims, appended, errors,
                          {"checks": ["references", "unicite_allegations", "zones_gelees_declaratif",
                                      "sequence_balises", "zones_gelees_reextraction", "texte_libre"],
                           "free_text": free_view})
