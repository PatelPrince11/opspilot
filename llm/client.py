"""The one LLM entry point. Every model call in OpsPilot goes through Client.chat or Client.stream.

    llm = Client.for_role("agent")            # AGENT_LLM, e.g. "groq:openai/gpt-oss-120b"
    r = llm.chat([{"role": "user", "content": "hi"}], max_tokens=64)
    r.content, r.tool_calls, r.prompt_tokens, r.completion_tokens, r.estimated

- Providers over plain HTTP: groq and gemini through their OpenAI-compatible chat endpoints,
  ollama through its native /api/chat, because only that one accepts options.num_ctx (Ollama's
  default context is small and it silently drops the start of longer prompts).
- Temperature 0 by default.
- Token budget per call (LLM_TOKEN_BUDGET, 6,000): estimated prompt + max_tokens must fit, or
  BudgetExceeded is raised before any request. Groq's free tier allows 8K tokens per minute, so
  one call near that would stall a run. Callers trim; this module never truncates silently.
- Token counts come from the provider's usage fields. When a provider omits them the estimate
  is used and Response.estimated is True.
- Retries on 429, 5xx and transport errors: the retry-after header (or Gemini's retryDelay in
  the body) when present, else
  exponential backoff with jitter, both capped. A retry-after longer than the cap (a daily limit)
  raises at once instead of sleeping for hours.
- Disk cache keyed by sha256(provider, model, messages, tools, params). Cached responses keep
  their original token counts and are marked cached.
- Langfuse tracing (generation per call, grouped by trace_id) only when TRACING_ENABLED and both
  keys are set; otherwise the SDK is never constructed.
"""

import hashlib
import json
import logging
import math
import random
import re
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from config import get_settings

log = logging.getLogger(__name__)

OPENAI_COMPAT = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}
PROVIDERS = (*OPENAI_COMPAT, "ollama")
CHARS_PER_TOKEN = 3  # conservative: English and JSON run nearer 4, so this over-estimates
MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
MAX_RETRY_AFTER_S = 60.0


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Response:
    content: str
    tool_calls: list[ToolCall]
    prompt_tokens: int
    completion_tokens: int
    estimated: bool  # True when the provider gave no usage counts
    prompt_estimate: int  # the pre-call estimate the budget was checked against
    provider: str
    model: str
    cached: bool = False
    latency_ms: float = 0.0
    name: str = "chat"
    wait_s: float = 0.0  # time slept on 429/5xx retries, included in latency_ms

    def log_row(self) -> dict:
        d = asdict(self)
        for k in ("content", "tool_calls"):
            d.pop(k)
        return d


def estimate_tokens(*parts) -> int:
    return math.ceil(sum(len(json.dumps(p, default=str)) for p in parts if p) / CHARS_PER_TOKEN)


def retry_after_seconds(headers: httpx.Headers, body: str = "") -> float | None:
    """The retry-after header, else Gemini's delay in the body: RetryInfo ("retryDelay": "50s")
    or the message ("Please retry in 50.8s")."""
    v = headers.get("retry-after")
    if not v:
        m = re.search(r'"retryDelay":\s*"([\d.]+)s"|retry in ([\d.]+)s', body)
        return float(m.group(1) or m.group(2)) if m else None
    try:
        return max(0.0, float(v))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


@dataclass
class Client:
    provider: str
    model: str
    budget: int = field(default_factory=lambda: get_settings().llm_token_budget)
    cache_dir: Path | None = field(default_factory=lambda: get_settings().llm_cache_path)
    transport: httpx.BaseTransport | None = None  # tests pass httpx.MockTransport
    sleep: object = time.sleep

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ValueError(f"provider must be one of {PROVIDERS}, not {self.provider!r}")
        self.http = httpx.Client(timeout=180.0, transport=self.transport)
        self._tracer = None
        self.waited = 0.0  # seconds slept on retries during the last post()

    @classmethod
    def from_spec(cls, spec: str, **kw) -> "Client":
        """ "provider:model"; the model may itself contain colons (qwen2.5:7b)."""
        provider, _, model = spec.partition(":")
        return cls(provider, model, **kw)

    @classmethod
    def for_role(cls, role: str, **kw) -> "Client":
        s = get_settings()
        return cls.from_spec(
            {"agent": s.agent_llm, "judge": s.judge_llm, "dev": s.dev_llm}[role], **kw
        )

    # ------------------------------------------------------------------ public

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        name: str = "chat",
        trace_id: str | None = None,
    ) -> Response:
        estimate = self.check_budget(messages, tools, json_schema, max_tokens, name)
        params = {"temperature": temperature, "max_tokens": max_tokens, "json_schema": json_schema}
        key = self.cache_key(messages, tools, params)
        if (hit := self.cache_get(key)) is not None:
            return Response(**{**hit, "tool_calls": [ToolCall(**t) for t in hit["tool_calls"]]})
        t0 = time.perf_counter()
        raw = self.post(self.payload(messages, tools, json_schema, temperature, max_tokens, False))
        r = self.parse(raw, estimate)
        r.latency_ms = (time.perf_counter() - t0) * 1000
        r.name, r.wait_s = name, self.waited
        self.cache_put(key, r)
        self.trace(name, trace_id, messages, r, params)
        log.info("llm %s %s:%s %s", name, self.provider, self.model, r.log_row())
        return r

    def stream(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        name: str = "stream",
        trace_id: str | None = None,
    ) -> Iterator[str]:
        """Text deltas as they arrive. Not cached. self.last holds the Response afterwards."""
        estimate = self.check_budget(messages, None, None, max_tokens, name)
        body = self.payload(messages, None, None, temperature, max_tokens, True)
        url, headers = self.endpoint()
        text, usage, t0 = [], {}, time.perf_counter()
        with self.http.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise LLMError(f"{self.provider} {resp.status_code}: {resp.text[:500]}")
            for line in resp.iter_lines():
                line = line.removeprefix("data: ").strip()
                if not line or line == "[DONE]":
                    continue
                chunk = json.loads(line)
                if self.provider == "ollama":
                    delta = chunk.get("message", {}).get("content", "")
                    if chunk.get("done"):
                        usage = ollama_usage(chunk)
                else:
                    choices = chunk.get("choices") or [{}]
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    usage = openai_usage(chunk.get("usage")) or usage
                if delta:
                    text.append(delta)
                    yield delta
        self.last = self.response("".join(text), [], usage, estimate)
        self.last.latency_ms, self.last.name = (time.perf_counter() - t0) * 1000, name
        self.trace(name, trace_id, messages, self.last, {"temperature": temperature})

    def check_budget(self, messages, tools, json_schema, max_tokens: int, name: str) -> int:
        estimate = estimate_tokens(messages, tools, json_schema)
        if estimate + max_tokens > self.budget:
            raise BudgetExceeded(
                f"{name}: estimated prompt {estimate} + max_tokens {max_tokens}"
                f" = {estimate + max_tokens} tokens > budget {self.budget}; trim the prompt"
            )
        return estimate

    # ------------------------------------------------------------------ providers

    def endpoint(self) -> tuple[str, dict]:
        s = get_settings()
        if self.provider == "ollama":
            return s.ollama_host.rstrip("/") + "/api/chat", {}
        key = {"groq": s.groq_api_key, "gemini": s.gemini_api_key}[self.provider]
        if key is None:
            raise LLMError(f"{self.provider.upper()}_API_KEY is not set")
        return OPENAI_COMPAT[self.provider], {"Authorization": f"Bearer {key.get_secret_value()}"}

    def payload(self, messages, tools, json_schema, temperature, max_tokens, stream) -> dict:
        body: dict = {"model": self.model, "messages": messages, "stream": stream}
        if tools:
            body["tools"] = tools
        if self.provider == "ollama":
            # num_ctx in every request: without it Ollama uses its small default and truncates.
            opts = {"temperature": temperature, "num_predict": max_tokens}
            body["options"] = opts | {"num_ctx": get_settings().ollama_num_ctx}
            if json_schema:
                body["format"] = json_schema
            if tools:  # Ollama wants tool-call arguments as objects, not JSON strings
                body["messages"] = [ollama_message(m) for m in messages]
            return body
        body |= {"temperature": temperature, "max_tokens": max_tokens}
        if json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema},
            }
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def post(self, body: dict) -> dict:
        url, headers = self.endpoint()
        self.waited = 0.0
        for attempt in range(MAX_ATTEMPTS):
            last = attempt == MAX_ATTEMPTS - 1
            try:
                resp = self.http.post(url, json=body, headers=headers)
            except httpx.TransportError as exc:
                if last:
                    raise LLMError(
                        f"{self.provider}: {exc!r} after {MAX_ATTEMPTS} attempts"
                    ) from exc
                wait = self.backoff(attempt)
                self.waited += wait
                self.sleep(wait)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = retry_after_seconds(resp.headers, resp.text)
                if last or (wait is not None and wait > MAX_RETRY_AFTER_S):
                    raise LLMError(
                        f"{self.provider} {resp.status_code} (retry-after {wait}):"
                        f" {resp.text[:500]}"
                    )
                wait = self.backoff(attempt) if wait is None else wait
                log.warning("%s %s, retrying in %.1fs", self.provider, resp.status_code, wait)
                self.waited += wait
                self.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise LLMError(f"{self.provider} {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        raise AssertionError("unreachable")

    @staticmethod
    def backoff(attempt: int) -> float:
        return min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt) * (0.5 + random.random() / 2)

    def parse(self, raw: dict, estimate: int) -> Response:
        if self.provider == "ollama":
            msg = raw.get("message", {})
            calls = [
                ToolCall(f"call_{i}", c["function"]["name"], c["function"].get("arguments") or {})
                for i, c in enumerate(msg.get("tool_calls") or [])
            ]
            return self.response(msg.get("content") or "", calls, ollama_usage(raw), estimate)
        msg = raw["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            args = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                args = {"_unparsed": args}  # the tool's schema check reports it as invalid
            calls.append(ToolCall(c.get("id", ""), c["function"]["name"], args))
        return self.response(
            msg.get("content") or "", calls, openai_usage(raw.get("usage")), estimate
        )

    def response(self, content, calls, usage: dict, estimate: int) -> Response:
        return Response(
            content=content,
            tool_calls=calls,
            prompt_tokens=usage.get("prompt", estimate),
            completion_tokens=usage.get("completion", estimate_tokens(content) if content else 0),
            estimated=not usage,
            prompt_estimate=estimate,
            provider=self.provider,
            model=self.model,
        )

    # ------------------------------------------------------------------ cache

    def cache_key(self, messages, tools, params) -> str:
        blob = json.dumps(
            [self.provider, self.model, messages, tools, params], sort_keys=True, default=str
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def cache_get(self, key: str) -> dict | None:
        if not self.cache_dir:
            return None
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text()) | {"cached": True, "latency_ms": 0.0, "wait_s": 0.0}

    def cache_put(self, key: str, r: Response) -> None:
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{key}.json").write_text(json.dumps(asdict(r)))

    # ------------------------------------------------------------------ tracing

    def tracer(self):
        """The Langfuse client, or None. Constructed only when enabled and both keys are set."""
        s = get_settings()
        if not (s.tracing_enabled and s.langfuse_public_key and s.langfuse_secret_key):
            return None
        if self._tracer is None:
            from langfuse import Langfuse

            self._tracer = Langfuse(
                public_key=s.langfuse_public_key.get_secret_value(),
                secret_key=s.langfuse_secret_key.get_secret_value(),
                host=s.langfuse_host,
                sample_rate=s.tracing_sample_rate,
            )
        return self._tracer

    def trace(self, name, trace_id, messages, r: Response, params) -> None:
        lf = self.tracer()
        if lf is None:
            return
        gen = lf.start_observation(
            trace_context={"trace_id": trace_id} if trace_id else None,
            name=name,
            as_type="generation",
            model=f"{self.provider}:{self.model}",
            model_parameters={k: v for k, v in params.items() if k != "json_schema"},
            input=messages,
            metadata={"estimated_tokens": r.estimated, "prompt_estimate": r.prompt_estimate},
        )
        gen.update(
            output={"content": r.content, "tool_calls": [asdict(c) for c in r.tool_calls]},
            usage_details={"input": r.prompt_tokens, "output": r.completion_tokens},
        )
        gen.end()

    def flush(self) -> None:
        if self._tracer is not None:
            self._tracer.flush()


def openai_usage(u: dict | None) -> dict:
    if not u or u.get("prompt_tokens") is None:
        return {}
    return {"prompt": u["prompt_tokens"], "completion": u.get("completion_tokens") or 0}


def ollama_usage(raw: dict) -> dict:
    if raw.get("prompt_eval_count") is None:
        return {}
    return {"prompt": raw["prompt_eval_count"], "completion": raw.get("eval_count") or 0}


def ollama_message(m: dict) -> dict:
    if not m.get("tool_calls"):
        return m
    calls = []
    for c in m["tool_calls"]:
        args = c["function"]["arguments"]
        calls.append(
            {
                "function": {
                    "name": c["function"]["name"],
                    "arguments": (json.loads(args) if isinstance(args, str) else args),
                }
            }
        )
    return {**m, "tool_calls": calls}
