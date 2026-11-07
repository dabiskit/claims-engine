"""
engine/ai.py — Appels au modèle pour l'extraction d'allégations et la réécriture.

Modèle par défaut : claude-fable-5-1 (variables MODEL_EXTRACTEUR, MODEL_REECRIVAIN, MODEL_DEFAULT).
Les sorties sont obtenues par appel d'outil forcé avec un JSON Schema strict, puis revalidées
localement : une sortie non conforme est une erreur, jamais un résultat.

Ce module ne décide de rien : il transporte des listes fermées vers le modèle et des choix
structurés vers le moteur de règles (eval/pipeline_adapter.py, engine/rewrite_assembler.py).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jsonschema

ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-fable-5-1"
RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504, 529}

EXTRACTOR_PROMPT = (ROOT / "prompts" / "extracteur.md").read_text(encoding="utf-8")
REWRITER_PROMPT = (ROOT / "prompts" / "reecrivain.md").read_text(encoding="utf-8")
EXTRACTOR_SCHEMA = json.loads((ROOT / "schemas" / "extracteur.schema.json").read_text(encoding="utf-8"))
REWRITER_SCHEMA = json.loads((ROOT / "schemas" / "reecrivain.schema.json").read_text(encoding="utf-8"))


@dataclass
class LLMUsage:
    stage: str
    model: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    latency_s: float


class LLMError(Exception):
    def __init__(self, message: str, usage: list[LLMUsage] | None = None):
        super().__init__(message)
        self.usage = usage or []


def model_for(stage: str) -> str:
    return os.environ.get(f"MODEL_{stage.upper()}") or os.environ.get("MODEL_DEFAULT") or DEFAULT_MODEL


class AnthropicClient:
    """Client minimal (httpx) : appel d'outil forcé, nouvelles tentatives sur erreurs transitoires."""

    def __init__(self, api_key: str | None = None, transport=None, timeout: float = 180.0, max_retries: int = 3):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY manquante")
        self.http = httpx.Client(timeout=timeout, transport=transport)
        self.max_retries = max_retries

    def call_tool(self, *, stage: str, model: str, system: str, user_content: str, tool_name: str,
                  tool_description: str, input_schema: dict, max_tokens: int = 4096) -> tuple[dict, LLMUsage]:
        body = {
            "model": model, "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user_content}],
            "tools": [{"name": tool_name, "description": tool_description, "input_schema": input_schema}],
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        headers = {"x-api-key": self.api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        delay, last = 2.0, "aucune réponse"
        for _ in range(self.max_retries):
            t0 = time.perf_counter()
            try:
                r = self.http.post(API_URL, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last = f"réseau : {exc}"
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code in RETRY_STATUSES:
                last = f"HTTP {r.status_code}"
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code >= 400:
                raise LLMError(f"HTTP {r.status_code} : {r.text[:300]}")
            data = r.json()
            u = data.get("usage", {}) or {}
            cached = int(u.get("cache_read_input_tokens") or 0)
            usage = LLMUsage(stage, data.get("model", model),
                             int(u.get("input_tokens") or 0) + cached + int(u.get("cache_creation_input_tokens") or 0),
                             cached, int(u.get("output_tokens") or 0), time.perf_counter() - t0)
            block = next((b for b in data.get("content", []) if b.get("type") == "tool_use" and b.get("name") == tool_name), None)
            if block is None or not isinstance(block.get("input"), dict):
                raise LLMError("Réponse sans appel d'outil exploitable", [usage])
            return block["input"], usage
        raise LLMError(f"Échec après {self.max_retries} tentatives ({last})")


def _validated(out: dict, schema: dict, stage: str, usage: LLMUsage) -> dict:
    try:
        jsonschema.validate(out, schema)
    except jsonschema.ValidationError as exc:
        raise LLMError(f"Sortie de l'étape « {stage} » non conforme au schéma : {exc.message[:200]}", [usage])
    return out


def extractor_schema(node_keys: list[str], claim_codes: list[str], substance_codes: list[str]) -> dict:
    """Listes fermées injectées dans le schéma : le modèle ne peut citer qu'un segment existant,
    qu'un code d'allégation fourni (ou AUCUNE) et qu'une substance déclarée (ou null)."""
    schema = json.loads(json.dumps(EXTRACTOR_SCHEMA))
    props = schema["properties"]["claims"]["items"]["properties"]
    props["node_key"]["enum"] = node_keys
    props["matched_claim_code"]["enum"] = sorted(claim_codes) + ["AUCUNE"]
    props["substance_code"]["enum"] = sorted(substance_codes) + [None]
    return schema


def extract_claims(client: AnthropicClient, *, mode: str, nodes: list[dict], substances: list[dict],
                   other_ingredients: list[str], claims: list[dict], model: str | None = None) -> tuple[list[dict], list[LLMUsage]]:
    if not nodes:
        return [], []
    schema = extractor_schema([n["key"] for n in nodes], [c["code"] for c in claims], [s["code"] for s in substances])
    user = json.dumps({"mode": mode, "nodes": nodes, "substances": substances, "other_ingredients": other_ingredients,
                       "authorized_claims": claims}, ensure_ascii=False)
    out, usage = client.call_tool(
        stage="extracteur", model=model or model_for("extracteur"), system=EXTRACTOR_PROMPT, user_content=user,
        tool_name="enregistrer_allegations",
        tool_description="Enregistre toutes les allégations, explicites ou implicites, trouvées dans les segments.",
        input_schema=schema, max_tokens=8000)
    return _validated(out, schema, "extracteur", usage)["claims"], [usage]


def rewrite_spans(client: AnthropicClient, payload: dict, model: str | None = None) -> tuple[dict, LLMUsage]:
    out, usage = client.call_tool(
        stage="reecrivain", model=model or model_for("reecrivain"), system=REWRITER_PROMPT,
        user_content=json.dumps(payload, ensure_ascii=False), tool_name="proposer_reecriture",
        tool_description="Propose la réécriture d'un segment sous forme de spans contrôlés.",
        input_schema=REWRITER_SCHEMA, max_tokens=3000)
    return _validated(out, REWRITER_SCHEMA, "réécrivain", usage), usage
