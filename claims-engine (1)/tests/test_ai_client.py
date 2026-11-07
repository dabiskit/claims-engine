"""tests/test_ai_client.py — repli automatique quand le modèle refuse l'appel d'outil forcé."""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.ai import AnthropicClient  # noqa: E402

ERR = {"type": "error", "error": {"type": "invalid_request_error",
                                  "message": "tool_choice: type \"tool\" and \"any\" are not supported for this model."}}


def test_falls_back_to_auto_tool_choice_and_remembers():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body["tool_choice"]["type"])
        if body["tool_choice"]["type"] == "tool":
            return httpx.Response(400, json=ERR)
        assert "uniquement en appelant l'outil" in body["system"][0]["text"]
        return httpx.Response(200, json={"model": body["model"], "usage": {"input_tokens": 10, "output_tokens": 5},
                                         "content": [{"type": "thinking", "thinking": "…"},
                                                     {"type": "tool_use", "name": "t", "input": {"claims": []}}]})

    c = AnthropicClient(api_key="k", transport=httpx.MockTransport(handler))
    kw = dict(stage="extracteur", model="m", system="S", user_content="U", tool_name="t", tool_description="d",
              input_schema={"type": "object"})
    out, usage = c.call_tool(**kw)
    assert out == {"claims": []} and seen == ["tool", "auto"]
    c.call_tool(**kw)
    assert seen == ["tool", "auto", "auto"]          # le modèle est mémorisé : plus de tentative forcée


def test_json_in_text_is_accepted_in_auto_mode():
    def handler(request):
        body = json.loads(request.content)
        if body["tool_choice"]["type"] == "tool":
            return httpx.Response(400, json=ERR)
        return httpx.Response(200, json={"usage": {}, "content": [{"type": "text", "text": 'Voici : {"claims": [1]} fin'}]})

    c = AnthropicClient(api_key="k", transport=httpx.MockTransport(handler))
    out, _ = c.call_tool(stage="s", model="m", system="S", user_content="U", tool_name="t", tool_description="d",
                         input_schema={"type": "object"})
    assert out == {"claims": [1]}
