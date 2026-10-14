"""llm/client.py against httpx.MockTransport: no network, no keys used."""

import ast
import json
from pathlib import Path

import httpx
import pytest

from config import get_settings
from llm import client as client_module
from llm.client import BudgetExceeded, Client, LLMError, estimate_tokens, usage

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
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
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


def test_usage_counts_cost_of_calls_made_only():
    rows = [
        {"prompt_tokens": 100, "completion_tokens": 10, "cost_usd": 0.002, "cached": False},
        {"prompt_tokens": 50, "completion_tokens": 5, "cost_usd": 0.001, "cached": True},
    ]
    assert usage(rows) == {
        "llm_calls": 2,
        "cached_calls": 1,
        "prompt_tokens": 150,
        "completion_tokens": 15,
        "cost_usd": 0.002,  # the cache hit cost nothing this time
    }
    assert usage([])["cost_usd"] == 0


def test_usage_and_cache_flag_reach_langfuse(monkeypatch):
    seen = []

    class Obs:
        def update(self, **k):
            seen.append(("update", k))

        def end(self):
            pass

    class FakeLangfuse:
        def start_observation(self, **k):
            seen.append(("generation", k))
            return Obs()

        def create_event(self, **k):
            seen.append(("event", k))

    c = client("ollama", lambda req: httpx.Response(200, json={"message": {"content": "hi"}}))
    monkeypatch.setattr(c, "tracer", lambda: FakeLangfuse())
    c.chat(MSGS, trace_id="ab" * 16)
    c.trace_usage("ab" * 16, {"cost_usd": 0.5})
    gen = next(k for kind, k in seen if kind == "generation")
    assert gen["metadata"]["cached"] is False
    ev = next(k for kind, k in seen if kind == "event")
    assert ev == {
        "trace_context": {"trace_id": "ab" * 16},
        "name": "usage",
        "metadata": {"cost_usd": 0.5},
    }


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
            for u in (
                "api.groq.com",
                "generativelanguage.googleapis.com",
                "/api/chat",
                "openrouter.ai",
            )
            if u in text and not (u == "openrouter.ai" and rel == Path("config.py"))
        ]
    assert bad == []


# ---------------------------------------------------------------------- openrouter


def openrouter(handler, model="openai/gpt-oss-120b", **kw):
    return Client(
        "openrouter",
        model,
        transport=httpx.MockTransport(handler),
        sleep=kw.pop("sleep", None) or (lambda s: None),
        **kw,
    )


def test_openrouter_pins_one_upstream_with_no_fallback(keys, monkeypatch):
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://router.example/api/v1/")
    get_settings.cache_clear()
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=openai_reply())

    openrouter(handler).chat(MSGS, TOOLS, json_schema={"type": "object"})
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://router.example/api/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert body["model"] == "openai/gpt-oss-120b" and body["tools"] == TOOLS
    assert body["provider"] == {
        "order": ["groq"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert body["response_format"]["type"] == "json_schema" and body["temperature"] == 0.0


def test_other_providers_send_no_routing_field(keys):
    seen = []
    client("groq", lambda req: seen.append(req) or httpx.Response(200, json=openai_reply())).chat(
        MSGS
    )
    assert "provider" not in json.loads(seen[0].content)


def test_openrouter_cost_is_read_from_usage(keys):
    reply = openai_reply()
    reply["usage"]["cost"] = 0.00042
    r = openrouter(lambda req: httpx.Response(200, json=reply)).chat(MSGS)
    assert (r.cost_usd, r.cost_source) == (0.00042, "reported")
    assert r.log_row()["cost_usd"] == 0.00042


def test_openrouter_cost_is_computed_when_not_reported(keys):
    reply = openai_reply()
    reply["usage"] = {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}
    r = openrouter(lambda req: httpx.Response(200, json=reply)).chat(MSGS)
    assert r.cost_source == "computed" and r.cost_usd == pytest.approx(0.15 + 0.06)


def test_free_providers_cost_nothing(keys):
    r = client("gemini", lambda req: httpx.Response(200, json=openai_reply())).chat(MSGS)
    assert (r.cost_usd, r.cost_source) == (0.0, "free")


def test_openrouter_cache_hit_keeps_cost_but_is_marked_cached(keys, tmp_path):
    reply, n = openai_reply(), []
    reply["usage"]["cost"] = 0.001
    c = openrouter(lambda req: n.append(1) or httpx.Response(200, json=reply), cache_dir=tmp_path)
    first, second = c.chat(MSGS), c.chat(MSGS)
    assert len(n) == 1 and second.cached and not first.cached
    assert second.cost_usd == 0.001  # the eval counts only uncached calls as new spend


def test_openrouter_retries_429_then_succeeds(keys):
    replies = [
        httpx.Response(429, headers={"retry-after": "3"}),
        httpx.Response(200, json=openai_reply()),
    ]
    slept = []
    r = openrouter(lambda req: replies.pop(0), sleep=slept.append).chat(MSGS)
    assert r.content == "ok" and slept == [3.0]


def test_openrouter_out_of_credit_is_not_retried(keys):
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})

    with pytest.raises(LLMError, match="402"):
        openrouter(handler).chat(MSGS)
    assert len(n) == 1


def test_openrouter_error_body_with_200_raises(keys):
    body = {"error": {"message": "Provider returned error", "code": 502}}
    with pytest.raises(LLMError, match="no choices"):
        openrouter(lambda req: httpx.Response(200, json=body)).chat(MSGS)


def test_openrouter_missing_key_raises(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        openrouter(lambda req: httpx.Response(200, json=openai_reply())).chat(MSGS)


@pytest.mark.parametrize(
    "provider, field",
    [
        ("openrouter", ("reasoning", {"effort": "low"})),
        ("groq", ("reasoning_effort", "low")),
        ("gemini", None),
        ("ollama", None),
    ],
)
def test_reasoning_effort_reaches_reasoning_providers_only(keys, provider, field):
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        if provider == "ollama":
            return httpx.Response(200, json={"message": {"content": "x"}})
        return httpx.Response(200, json=openai_reply())

    client(provider, handler).chat(MSGS, reasoning_effort="low")
    body = sent[0]
    for k in ("reasoning", "reasoning_effort"):
        assert (k in body) == (field is not None and field[0] == k)
    if field:
        assert body[field[0]] == field[1]


def test_reasoning_effort_is_part_of_the_cache_key_only_when_set(keys, tmp_path):
    n = []
    c = openrouter(
        lambda r: n.append(1) or httpx.Response(200, json=openai_reply()), cache_dir=tmp_path
    )
    c.chat(MSGS)
    c.chat(MSGS, reasoning_effort=None)  # same key as before: earlier caches stay valid
    c.chat(MSGS, reasoning_effort="low")
    assert len(n) == 2


def test_a_reply_that_trickles_past_the_deadline_is_retried_then_fails(keys, monkeypatch):
    """httpx's timeout is per read: a server sending a byte at a time never trips it. The
    total deadline does, and counts as a transport error (retried, then raised)."""
    monkeypatch.setattr(client_module, "REQUEST_DEADLINE_S", 0.0)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, content=iter([b" ", b" ", json.dumps(openai_reply()).encode()]))

    with pytest.raises(LLMError, match="no complete reply"):
        client("groq", handler).chat(MSGS)
    assert len(calls) == 5


def test_a_gzipped_reply_still_parses(keys):
    import gzip

    def handler(req):
        body = gzip.compress(json.dumps(openai_reply("zipped")).encode())
        return httpx.Response(200, content=body, headers={"content-encoding": "gzip"})

    assert client("groq", handler).chat(MSGS).content == "zipped"
