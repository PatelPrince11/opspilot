"""A scripted LLM: the real llm.client.Client with only the HTTP post replaced, so the token
budget check, response parsing and token logging all run as in production."""

import json

from llm.client import Client


class StubLLM(Client):
    def __init__(self, script: list, budget: int = 6000):
        super().__init__("ollama", "stub", budget=budget, cache_dir=None)
        self.script = list(script)
        self.bodies: list[dict] = []
        self.checked: list[int] = []  # estimate + max_tokens of every call

    def check_budget(self, messages, tools, json_schema, max_tokens, name):
        estimate = super().check_budget(messages, tools, json_schema, max_tokens, name)
        self.checked.append(estimate + max_tokens)
        return estimate

    def post(self, body: dict) -> dict:
        self.bodies.append(body)
        if not self.script:
            raise AssertionError("the stub's script ran out")
        step = self.script.pop(0)
        return step(body) if callable(step) else step


def say(obj) -> dict:
    content = obj if isinstance(obj, str) else json.dumps(obj)
    return {"message": {"content": content}, "prompt_eval_count": 100, "eval_count": 20}


def calls(*pairs) -> dict:
    tc = [{"function": {"name": n, "arguments": a}} for n, a in pairs]
    return {
        "message": {"content": "", "tool_calls": tc},
        "prompt_eval_count": 100,
        "eval_count": 20,
    }


TRIAGE = say(
    {
        "services": ["payments-api"],
        "symptoms": "connection refused",
        "window_start": "2026-10-06T13:52:00Z",
        "window_end": "2026-10-06T14:52:00Z",
    }
)


def draft(*claims, confidence="high") -> dict:
    return say(
        {
            "summary": "s",
            "likely_cause": "c",
            "confidence": confidence,
            "claims": [{"text": t, "evidence_ids": ids} for t, ids in claims],
            "next_checks": ["check x"],
        }
    )
