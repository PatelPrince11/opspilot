"""The generation judge: one LLM call per answer, strict JSON, through llm/client.py.

    verdict = judge(llm, question, diagnosis, evidence)   # a Verdict, or None if invalid twice

The judge sees the question, its expected key facts, the diagnosis (summary, likely cause,
claims) and the full text of each cited evidence item, nothing else: no gold documents and no
other gathered evidence, so "supported" means supported by what the claim cites. Temperature 0;
the client caches by prompt hash, and PROMPT_VERSION is part of the prompt, so changing the
prompt can never reuse an old verdict.

Rate limits (Gemini free tier): RateLimiter spaces real requests (cache hits never wait) to
JUDGE_RPM per minute and refuses past JUDGE_RPD per Pacific day (Gemini's reset), counted in a
file so a resumed run keeps the count. A daily-limit 429 (retry-after over the client's cap)
raises LLMError; the eval leaves that answer unjudged and a rerun picks it up.
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from agent.graph import json_content
from llm.client import Client, LLMError, estimate_tokens

PROMPT_VERSION = "judge-v1"
JUDGE_MAX_TOKENS = 1500
JUDGE_RPM = 15
JUDGE_RPD = 500
# Gemini's daily quota resets at midnight Pacific; it also counts Gemini calls made elsewhere
# (e.g. Gemini as the agent), which this counter cannot see.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")
EVIDENCE_CHARS = 1200  # per cited item; shrunk if the prompt would pass the budget
NOT_GATHERED = "(this id was not in the gathered evidence: nothing supports the claim)"

SYSTEM = (
    f"[{PROMPT_VERSION}] You grade an incident diagnosis written by another model. Judge only "
    "from the texts given: the question, the expected key facts, the diagnosis and the evidence "
    "each claim cites. Do not use outside knowledge. Reply with JSON only."
)

RUBRIC = """For each claim, supported is true only if the evidence it cites states what the claim
says (all of it; a claim about a different incident, date or host than its evidence is not
supported).
For each expected key fact, present is true only if the diagnosis (summary, likely cause or a
claim) states that fact; a vaguer or partial statement is false. A key fact "not recorded: X" is
present if the diagnosis says X is unknown or not recorded.
faithfulness: 1 to 5, how fully the whole diagnosis (summary and likely cause too) sticks to the
cited evidence: 5 = nothing beyond it, 1 = mostly unsupported.
premise_corrected: {premise}
unrecorded_respected: {unrecorded}"""


class ClaimVerdict(BaseModel):
    claim: int = Field(description="Claim number as listed, from 1")
    supported: bool


class FactVerdict(BaseModel):
    fact: int = Field(description="Key fact number as listed, from 1")
    present: bool


class Verdict(BaseModel):
    claims: list[ClaimVerdict]
    key_facts: list[FactVerdict]
    faithfulness: int = Field(ge=1, le=5)
    premise_corrected: bool | None
    unrecorded_respected: bool | None


class RateLimiter:
    """At most rpm requests per minute (evenly spaced) and rpd per Pacific day, across threads."""

    def __init__(self, rpm: int, rpd: int, state: Path | None, clock=time.time, sleep=time.sleep):
        self.gap, self.rpd, self.state = 60.0 / rpm, rpd, state
        self.clock, self.sleep = clock, sleep
        self.lock, self.next_at = threading.Lock(), 0.0

    def counts(self) -> dict:
        if self.state and self.state.exists():
            return json.loads(self.state.read_text())
        return {}

    def acquire(self) -> None:
        with self.lock:
            day = datetime.fromtimestamp(self.clock(), QUOTA_TZ).date().isoformat()
            counts = self.counts()
            if counts.get(day, 0) >= self.rpd:
                raise LLMError(f"judge: {self.rpd} requests already made on {day} (Pacific)")
            wait = self.next_at - self.clock()
            if wait > 0:
                self.sleep(wait)
            self.next_at = max(self.clock(), self.next_at) + self.gap
            if self.state:
                self.state.parent.mkdir(parents=True, exist_ok=True)
                self.state.write_text(json.dumps(counts | {day: counts.get(day, 0) + 1}))


class JudgeClient(Client):
    """The configured judge model; every real request (not a cache hit) passes the limiter."""

    limiter: RateLimiter | None = None

    def post(self, body: dict) -> dict:
        if self.limiter:
            self.limiter.acquire()
        return super().post(body)


def prompt(
    question: dict, diagnosis: dict, evidence: dict, chars: int = EVIDENCE_CHARS
) -> list[dict]:
    fp, unrec = bool(question.get("false_premise")), bool(question.get("unrecorded"))
    rubric = RUBRIC.format(
        premise="the question rests on a false premise (the key facts state the correction): "
        "true if the diagnosis says the premise is wrong, else false"
        if fp
        else "null (no false premise)",
        unrecorded="the corpus does not record some asked part (key facts starting 'not "
        "recorded'): true if the diagnosis does not invent an answer for it, else false"
        if unrec
        else "null (nothing unrecorded)",
    )
    claims, cited = [], []
    for i, c in enumerate(diagnosis["claims"], 1):
        claims.append(f"{i}. {c['text']} (cites {', '.join(c['evidence_ids']) or 'nothing'})")
        cited += [x for x in c["evidence_ids"] if x not in cited]
    texts = [
        f"[{x}] {' '.join(evidence[x]['text'].split())[:chars]}"
        if x in evidence
        else f"[{x}] {NOT_GATHERED}"
        for x in cited
    ]
    facts = [f"{i}. {k}" for i, k in enumerate(question["expected_key_facts"], 1)]
    user = (
        f"Question: {question['question']}\n\nExpected key facts:\n{chr(10).join(facts)}\n\n"
        f"Diagnosis\nSummary: {diagnosis['summary']}\nLikely cause: {diagnosis['likely_cause']}"
        f"\nClaims:\n{chr(10).join(claims) or '(none)'}\n\n"
        f"Cited evidence:\n{chr(10).join(texts) or '(none)'}\n\n{rubric}\n\nReply with JSON: "
        "claims (one {claim, supported} per claim), key_facts (one {fact, present} per key "
        "fact), faithfulness, premise_corrected, unrecorded_respected."
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def fits(llm: Client, question, diagnosis, evidence) -> list[dict]:
    """The prompt, with evidence texts cut shorter until it fits the per-call budget."""
    chars = EVIDENCE_CHARS
    while True:
        msgs = prompt(question, diagnosis, evidence, chars)
        size = estimate_tokens(msgs, Verdict.model_json_schema()) + JUDGE_MAX_TOKENS
        if size <= llm.budget or chars <= 200:
            return msgs
        chars -= 200


def complete(v: Verdict, n_claims: int, n_facts: int) -> bool:
    return sorted(c.claim for c in v.claims) == list(range(1, n_claims + 1)) and sorted(
        f.fact for f in v.key_facts
    ) == list(range(1, n_facts + 1))


def judge(llm: Client, question: dict, diagnosis: dict, evidence: dict) -> tuple:
    """(Verdict or None, the call log rows). One retry when the reply is invalid or incomplete."""
    msgs, rows = fits(llm, question, diagnosis, evidence), []
    n_claims, n_facts = len(diagnosis["claims"]), len(question["expected_key_facts"])
    schema = Verdict.model_json_schema()
    for attempt in range(2):
        r = llm.chat(
            msgs,
            json_schema=schema,
            max_tokens=JUDGE_MAX_TOKENS,
            name="judge" if attempt == 0 else "judge_retry",
        )
        rows.append(r.log_row())
        try:
            v = Verdict.model_validate_json(json_content(r.content))
            if complete(v, n_claims, n_facts):
                return v, rows
            err = f"give exactly {n_claims} claim verdicts and {n_facts} key fact verdicts"
        except ValidationError as exc:
            err = "; ".join(e["msg"] for e in exc.errors()[:3])
        msgs = [
            *msgs,
            {"role": "user", "content": f"Your reply was not valid ({err}). JSON only."},
        ]
    return None, rows
