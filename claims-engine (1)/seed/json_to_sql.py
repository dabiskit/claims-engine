#!/usr/bin/env python3
"""
seed/json_to_sql.py — Convertit un seed JSON (format 1.0) en SQL d'import.

Usage : python3 seed/json_to_sql.py seed/out/lot1.json > seed/out/lot1.sql
        psql -v ON_ERROR_STOP=1 -d <base> -f seed/out/lot1.sql

* UUID déterministes (uuid5) : réimporter le même seed produit les mêmes identifiants,
  et les lots suivants référencent les lignes du lot 1 sans requête.
* Tout est inséré en DRAFT ; une release non publiée est créée si besoin.
* Les règles d'intégrité restent celles de la base (contraintes, déclencheurs) : le
  convertisseur ne contourne rien.
"""
from __future__ import annotations

import json
import sys
import uuid

NS = uuid.UUID("6f1c3a52-6d0e-4c5b-9a57-3f0b8c0d2a11")


def uid(kind: str, code: str) -> str:
    return str(uuid.uuid5(NS, f"{kind}:{code}"))


def q(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def arr(values, cast="text[]") -> str:
    return f"'{{}}'::{cast}" if not values else "ARRAY[" + ", ".join(q(x) for x in values) + f"]::{cast}"


def jb(v) -> str:
    return q(json.dumps(v, ensure_ascii=False)) + "::jsonb"


def insert(table: str, row: dict) -> str:
    return f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join(row.values())});"


def convert(seed: dict) -> str:
    rel = seed["release"]["id"]
    out = ["-- Généré par seed/json_to_sql.py — ne pas éditer à la main.",
           f"-- Release {seed['release']['label']} — statut du contenu : {seed['content_status']}",
           "\\set ON_ERROR_STOP on", "BEGIN;",
           insert("kb.releases", {"id": str(rel), "label": q(seed["release"]["label"]),
                                  "changelog": q(seed["release"]["changelog"])}) [:-1] + " ON CONFLICT (id) DO NOTHING;"]

    for n in seed["notices"]:
        out.append(insert("kb.mandatory_notices", {
            "id": q(uid("notice", n["code"])), "code": q(n["code"]), "text_fr": q(n["text_fr"]),
            "text_status": q(n["text_status"]), "source_ref": q(n["source_ref"]), "introduced_in": str(rel)}))

    for s in seed["substances"]:
        out.append(insert("kb.substances", {
            "id": q(uid("substance", s["code"])), "code": q(s["code"]), "kind": q(s["kind"]),
            "name_fr": q(s["name_fr"]), "scientific_name": q(s["scientific_name"]),
            "botanical_family": q(s["botanical_family"]), "nrv_value": q(s["nrv_value"]),
            "nrv_unit": q(s["nrv_unit"]), "source_ref": q(s["source_ref"]), "introduced_in": str(rel)}))
        for a in s["aliases"]:
            out.append(insert("kb.substance_aliases", {
                "id": q(uid("alias", f"{s['code']}:{a['alias']}")), "substance_id": q(uid("substance", s["code"])),
                "alias": q(a["alias"]), "alias_kind": q(a["alias_kind"]), "is_ambiguous": q(a["is_ambiguous"]),
                "note": q(a["note"]), "introduced_in": str(rel)}))

    for p in seed["plant_part_authorizations"]:
        out.append(insert("kb.plant_part_authorizations", {
            "id": q(uid("plant_part", f"{p['substance']}:{p['plant_part']}")),
            "substance_id": q(uid("substance", p["substance"])), "plant_part": q(p["plant_part"]),
            "authorized_fr": q(p["authorized_fr"]), "source_ref": q(p["source_ref"]), "introduced_in": str(rel)}))

    for r in seed["population_restrictions"]:
        out.append(insert("kb.population_restrictions", {
            "id": q(uid("restriction", f"{r['substance']}:{r['notice']}")),
            "substance_id": q(uid("substance", r["substance"])), "plant_part": q(r["plant_part"]),
            "populations": arr(r["populations"], "kb.population[]"), "restriction_kind": q(r["kind"]),
            "notice_id": q(uid("notice", r["notice"])), "source_ref": q(r["source_ref"]),
            "detail_fr": q(r["detail"]), "introduced_in": str(rel)}))

    for c in seed["regulatory_claims"]:
        out.append(insert("kb.regulatory_claims", {
            "id": q(uid("claim", c["code"])), "code": q(c["code"]), "regime": q(c["regime"]),
            "substance_id": q(uid("substance", c["substance"])), "plant_part": q(c.get("plant_part")),
            "efsa_ids": arr(c.get("efsa_ids", [])), "text_en_source": q(c.get("text_en_source")),
            "text_fr": q(c["text_fr"]), "text_fr_status": q(c["text_fr_status"]),
            "text_fr_source_ref": q(c["text_fr_source_ref"]), "verb_fr": q(c["verb_fr"]),
            "function_fr": q(c["function_fr"]), "function_label_fr": q(c["function_label_fr"]),
            "introduced_in": str(rel)}))

    for c in seed["claim_conditions"]:
        out.append(insert("kb.claim_conditions", {
            "id": q(uid("condition", c["code"])), "claim_id": q(uid("claim", c["claim"])),
            "condition_type": q(c["type"]), "params": jb(c["params"]),
            "notice_id": q(uid("notice", c["notice_code"])) if c.get("notice_code") else "NULL",
            "source_ref": q(c["source_ref"]), "introduced_in": str(rel)}))

    for v in seed["claim_variants"]:
        out.append(insert("kb.claim_variants", {
            "id": q(uid("variant", v["code"])), "code": q(v["code"]), "claim_id": q(uid("claim", v["claim"])),
            "text_fr": q(v["text_fr"]), "syntactic_form": q(v["syntactic_form"]),
            "is_official_wording": q(v["is_official_wording"]), "equivalence_note": q(v["equivalence_note"]),
            "introduced_in": str(rel)}))

    for e in seed["lexical_entries"]:
        out.append(insert("kb.lexical_entries", {
            "id": q(uid("lexical", e["code"])), "code": q(e["code"]), "lemma": q(e["lemma"]),
            "surface_forms": arr(e["surface_forms"]), "pattern_regex": q(e["pattern_regex"]),
            "category": q(e["category"]), "legal_basis": q(e["legal_basis"]),
            "severity_product": q(e["severity_product"]),
            "severity_article_with_bridge": q(e["severity_article_with_bridge"]),
            "severity_article_without_bridge": q(e["severity_article_without_bridge"]),
            "icd10_code": q(e["icd10_code"]), "topic_key": q(e["topic_key"]),
            "neutralizing_left_context": arr(e["neutralizing_left_context"]), "note": q(e["note"]),
            "substance_id": q(uid("substance", e["substance"])) if e.get("substance") else "NULL",
            "introduced_in": str(rel)}))

    for b in seed["seo_semantic_bridges"]:
        out.append(insert("kb.seo_semantic_bridges", {
            "id": q(uid("bridge", b["code"])), "code": q(b["code"]), "illegal_intent_label": q(b["illegal_intent_label"]),
            "illegal_query_terms": arr(b["illegal_query_terms"]), "legal_term": q(b["legal_term"]),
            "legal_term_variants": arr(b["legal_term_variants"]), "topic_key": q(b["topic_key"]),
            "search_volume_fr": q(b["search_volume_fr"]), "volume_source": q(b["volume_source"]),
            "usage_scope": q(b["usage_scope"]), "introduced_in": str(rel)}))
        for a in b["anchor_claims"]:
            out.append(insert("kb.seo_bridge_anchors", {"bridge_id": q(uid("bridge", b["code"])),
                                                        "claim_id": q(uid("claim", a))}))
        for p in b["pending_anchors"]:
            out.append(insert("kb.bridge_pending_anchors", {"bridge_id": q(uid("bridge", b["code"])),
                                                            "substance_id": q(uid("substance", p["substance"])),
                                                            "theme_fr": q(p["theme_fr"])}))

    for w in seed["botanical_import_worklist"]:
        out.append(insert("kb.botanical_import_worklist", {
            "id": q(uid("worklist", w["code"])), "code": q(w["code"]), "release_id": str(rel),
            "substance_id": q(uid("substance", w["substance"])), "plant_part": q(w["plant_part"]),
            "theme_fr": q(w["theme_fr"]), "theme_confidence": q(w["theme_confidence"]),
            "efsa_ids": arr(w["efsa_ids"]), "efsa_id_status": q(w["efsa_id_status"]),
            "market_phrasings_detection_only": arr(w["market_phrasings_detection_only"]), "note": q(w["note"])}))

    for i, r in enumerate(seed["review_items"], start=1):
        out.append(insert("kb.review_items", {
            "id": q(uid("review", f"{rel}:{i}:{r['target_code']}")), "release_id": str(rel),
            "target_table": q(r["target_table"]), "target_code": q(r["target_code"]),
            "confidence": q(r["confidence"]), "task_fr": q(r["task_fr"]), "author": q(r["author"])}))

    out.append("COMMIT;")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        sys.stdout.write(convert(json.load(f)))
