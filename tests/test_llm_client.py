"""llm/client.py against httpx.MockTransport: no network, no keys used."""

import ast
import json
from pathlib import Path

import httpx
import pytest

from config import get_settings
from llm.client import BudgetExceeded, Client, LLMError, estimate_tokens

ROOT = Path(__file__).resolve().parents[1]
MSGS = [{"role": "user", "content": "hello"}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_service_status",
            "parameters": {"type": "object", "properties": {"service": {"type": "string"}}},
        },
    }
]


def openai_reply(content="ok", tool_calls=None, usage=True):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    body = {"choices": [{"message": msg}]}
    if usage:
        body["usage"] = {"prompt_tokens": 11, "completion_tokens": 3}
    return body


def client(provider, handler, **kw):
    return Client(
        provider,
        "m",
        transport=httpx.MockTransport(handler),
        sleep=kw.pop("sleep", None) or (lambda s: None),
        **kw,
    )


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()


def test_ollama_sends_num_ctx_and_temperature_zero():
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(
            200, json={"message": {"content": "hi"}, "prompt_eval_count": 7, "eval_count": 2}
        )

    r = client("ollama", handler).chat(MSGS, max_tokens=50)
    opts = sent[0]["options"]
    assert opts["num_ctx"] >= 8192 and opts["num_ctx"] == get_settings().ollama_num_ctx
    assert opts["temperature"] == 0.0 and opts["num_predict"] == 50
    assert (r.content, r.prompt_tokens, r.completion_tokens, r.estimated) == ("hi", 7, 2, False)


def test_ollama_tool_calls_and_schema(keys):
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        call = {"function": {"name": "get_service_status", "arguments": {"service": "x"}}}
        return httpx.Response(200, json={"message": {"content": "", "tool_calls": [call]}})

    r = client("ollama", handler).chat(MSGS, TOOLS, json_schema={"type": "object"})
    assert sent[0]["tools"] == TOOLS and sent[0]["format"] == {"type": "object"}
    assert [(c.name, c.arguments) for c in r.tool_calls] == [
        ("get_service_status", {"service": "x"})
    ]
    assert r.estimated  # no usage fields: estimate used and marked


def test_groq_tool_call_arguments_are_parsed(keys):
    sent = []

    def handler(req):
        sent.append(req)
        call = {
            "id": "c1",
            "type": "function",
            "function": {"name": "get_service_status", "arguments": '{"service": "x"}'},
        }
        return httpx.Response(200, json=openai_reply("", [call]))

    r = client("groq", handler).chat(MSGS, TOOLS)
    assert sent[0].url == "https://api.groq.com/openai/v1/chat/completions"
    assert sent[0].headers["authorization"] == "Bearer test-key"
    assert r.tool_calls[0].arguments == {"service": "x"} and r.prompt_tokens == 11


def test_budget_raises_before_any_request(keys):
    def handler(req):
        raise AssertionError("no request may be sent over budget")

    c = client("groq", handler, budget=100)
    with pytest.raises(BudgetExceeded, match="trim"):
        c.chat([{"role": "user", "content": "x" * 300}], max_tokens=10)
    with pytest.raises(BudgetExceeded):
        c.chat(MSGS, max_tokens=100)  # max output counts toward the budget too


def test_default_budget_is_6000():
    assert Client("ollama", "m").budget == 6000
    assert estimate_tokens("x" * 30) == 11  # json quotes count: ceil(32 chars / 3)


def test_retry_honours_retry_after_then_succeeds(keys):
    replies = [
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(503),
        httpx.Response(200, json=openai_reply()),
    ]
    slept = []
    r = client("groq", lambda req: replies.pop(0), sleep=slept.append).chat(MSGS)
    assert r.content == "ok"
    assert slept[0] == 7.0 and 0 < slept[1] <= 30  # header, then capped backoff with jitter
    assert r.wait_s == sum(slept) and r.log_row()["wait_s"] == r.wait_s


@pytest.mark.parametrize(
    "body",
    [
        '[{"error": {"details": [{"retryDelay": "50s"}]}}]',
        '[{"error": {"message": "Quota exceeded... Please retry in 50.84s."}}]',
    ],
)
def test_gemini_retry_delay_in_the_body_is_honoured(keys, body):
    replies = [httpx.Response(429, text=body), httpx.Response(200, json=openai_reply())]
    slept = []
    r = client("gemini", lambda req: replies.pop(0), sleep=slept.append).chat(MSGS)
    assert r.content == "ok" and slept[0] in (50.0, 50.84)


def test_long_retry_after_raises_instead_of_sleeping(keys):
    slept = []
    c = client(
        "groq", lambda req: httpx.Response(429, headers={"retry-after": "3600"}), sleep=slept.append
    )
    with pytest.raises(LLMError, match="429"):
        c.chat(MSGS)
    assert slept == []


def test_retries_are_capped(keys):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500)

    with pytest.raises(LLMError):
        client("groq", handler).chat(MSGS)
    assert len(calls) == 5


def test_client_errors_are_not_retried(keys):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": "bad"})

    with pytest.raises(LLMError, match="400"):
        client("groq", handler).chat(MSGS)
    assert len(calls) == 1


def test_disk_cache_hit_skips_the_request(keys, tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=openai_reply("cached answer"))

    c = client("gemini", handler, cache_dir=tmp_path)
    first, second = c.chat(MSGS), c.chat(MSGS)
    assert len(calls) == 1 and second.cached and not first.cached
    assert (second.content, second.prompt_tokens) == ("cached answer", 11)
    c.chat(MSGS, temperature=0.5)  # different params, different key
    assert len(calls) == 2


def test_stream_yields_deltas_and_usage(keys):
    sse = (
        "".join(
            f"data: {json.dumps(e)}\n\n"
            for e in [
                {"choices": [{"delta": {"content": "he"}}]},
                {"choices": [{"delta": {"content": "llo"}}]},
                {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
            ]
        )
        + "data: [DONE]\n\n"
    )
    c = client("groq", lambda req: httpx.Response(200, text=sse))
    assert list(c.stream(MSGS)) == ["he", "llo"]
    assert (c.last.content, c.last.prompt_tokens, c.last.estimated) == ("hello", 5, False)


def test_tracing_disabled_never_builds_langfuse(monkeypatch):
    import langfuse

    def boom(*a, **k):
        raise AssertionError("Langfuse must not be constructed")

    monkeypatch.setattr(langfuse, "Langfuse", boom)
    reply = {"message": {"content": "hi"}}
    client("ollama", lambda req: httpx.Response(200, json=reply)).chat(MSGS)
    # Enabled but keys missing: still a no-op.
    monkeypatch.setenv("TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    get_settings.cache_clear()
    client("ollama", lambda req: httpx.Response(200, json=reply)).chat(MSGS)


PROVIDER_SDKS = {
    "groq",
    "openai",
    "anthropic",
    "ollama",
    "langfuse",
    "google.genai",
    "google.generativeai",
    "litellm",
    "langchain_openai",
    "langchain_groq",
}


def test_no_provider_sdk_or_endpoint_outside_llm():
    bad = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in {".venv", "llm", "tests"} or "site-packages" in rel.parts:
            continue
        text = path.read_text()
        for node in ast.walk(ast.parse(text)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            bad += [
                f"{rel}: import {n}"
                for n in names
                if any(n == s or n.startswith(s + ".") for s in PROVIDER_SDKS)
            ]
        bad += [
            f"{rel}: {u}"
            for u in ("api.groq.com", "generativelanguage.googleapis.com", "/api/chat")
            if u in text
        ]
    assert bad == []
