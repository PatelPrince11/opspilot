// POST /investigate answers with server-sent events. EventSource only does GET, so the stream is
// read with fetch: the body arrives in chunks, and an event ends at a blank line ("\n\n").

export type Claim = { text: string; evidence_ids: string[]; quote: string };
export type Diagnosis = {
  summary: string;
  likely_cause: string;
  confidence: "low" | "medium" | "high";
  claims: Claim[];
  next_checks: string[];
  citations_verified: boolean | null;
  dropped_claims: Claim[];
  note: string;
};
export type Source = {
  id: string;
  source: string; // log, runbook, incident, status
  text: string;
  metadata: Record<string, unknown>;
  cited: boolean;
};
export type Usage = {
  llm_calls: number;
  cached_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
};
export type ToolResult = {
  success: boolean;
  error: string | null;
  ids: string[];
  note: string;
  output_tokens: number;
  latency_ms: number;
};
export type Call = { name: string; arguments: Record<string, unknown>; result?: ToolResult };
export type Config = { wrap?: boolean; flag?: boolean; guard?: string; first_look?: boolean };

export type AgentEvent =
  | { event: "start"; investigation_id: string; as_of: string; config: Config }
  | { event: "tool_call"; name: string; arguments: Record<string, unknown> }
  | ({ event: "tool_result"; name: string } & ToolResult)
  | { event: "sources"; items: Source[] }
  | { event: "diagnosis"; diagnosis: Diagnosis; loops: number; usage: Usage }
  | { event: "error"; message: string };

export type RunState = {
  status: "idle" | "running" | "done" | "error";
  id?: string;
  config?: Config;
  calls: Call[];
  sources: Source[];
  diagnosis?: Diagnosis;
  loops?: number;
  usage?: Usage;
  warnings: string[]; // error events: a skipped call, a failed draft, or the fatal one
  error?: string; // why the run failed (HTTP, network, or a stream with no diagnosis)
  startedAt?: number;
  finishedAt?: number;
};

export const idle: RunState = { status: "idle", calls: [], sources: [], warnings: [] };

export type Action =
  | { type: "begin"; at: number }
  | { type: "event"; ev: AgentEvent }
  | { type: "end"; at: number; error?: string };

export function reduce(s: RunState, a: Action): RunState {
  if (a.type === "begin") return { ...idle, status: "running", startedAt: a.at };
  if (a.type === "end") {
    const error = a.error ?? (s.diagnosis ? undefined : (s.warnings.at(-1) ?? "The stream ended without a diagnosis."));
    return { ...s, status: error ? "error" : "done", error, finishedAt: a.at };
  }
  const ev = a.ev;
  switch (ev.event) {
    case "start":
      return { ...s, id: ev.investigation_id, config: ev.config };
    case "tool_call":
      return { ...s, calls: [...s.calls, { name: ev.name, arguments: ev.arguments }] };
    case "tool_result": {
      // Results arrive in call order: fill the oldest call still waiting.
      const i = s.calls.findIndex((c) => !c.result);
      if (i < 0) return s;
      const { event: _event, name: _name, ...result } = ev;
      const calls = s.calls.map((c, j) => (j === i ? { ...c, result } : c));
      return { ...s, calls };
    }
    case "sources":
      return { ...s, sources: ev.items };
    case "diagnosis":
      return { ...s, diagnosis: ev.diagnosis, loops: ev.loops, usage: ev.usage };
    case "error":
      return { ...s, warnings: [...s.warnings, ev.message] };
  }
}

export function* parseBlocks(buffer: string): Generator<AgentEvent> {
  for (const block of buffer.split("\n\n")) {
    const data = block
      .split("\n")
      .filter((l) => l.startsWith("data:"))
      .map((l) => l.slice(5).trimStart())
      .join("\n");
    const name = block.split("\n").find((l) => l.startsWith("event:"))?.slice(6).trim();
    if (name && data) yield { event: name, ...JSON.parse(data) } as AgentEvent;
  }
}

export async function* readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<AgentEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder(); // stream: true keeps a UTF-8 character split across chunks
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const end = buf.lastIndexOf("\n\n");
    if (end < 0) continue;
    yield* parseBlocks(buf.slice(0, end));
    buf = buf.slice(end + 2);
  }
  if (buf.trim()) yield* parseBlocks(buf);
}

// The API key the user typed into the dashboard. Held in this module's memory only: never
// localStorage or a cookie, never built into the bundle; a reload forgets it.
let apiKey = "";
export function setApiKey(key: string): void {
  apiKey = key.trim();
}

export function authHeaders(): Record<string, string> {
  return apiKey ? { "X-API-Key": apiKey } : {};
}

export async function investigate(
  question: string,
  asOf: string | null,
  dispatch: (a: Action) => void,
  signal?: AbortSignal,
): Promise<void> {
  dispatch({ type: "begin", at: Date.now() });
  let error: string | undefined;
  try {
    const r = await fetch("/investigate", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...authHeaders() },
      body: JSON.stringify(asOf ? { question, as_of: asOf } : { question }),
      signal,
    });
    if (!r.ok || !r.body) {
      error = await httpError(r);
    } else {
      for await (const ev of readEvents(r.body)) dispatch({ type: "event", ev });
    }
  } catch (e) {
    error = e instanceof DOMException && e.name === "AbortError" ? "Stopped." : `Network error: ${String(e)}`;
  }
  dispatch({ type: "end", at: Date.now(), error });
}

async function httpError(r: Response): Promise<string> {
  let detail = r.statusText;
  try {
    const body = await r.json();
    detail = Array.isArray(body.detail)
      ? body.detail.map((d: { msg: string }) => d.msg).join("; ")
      : String(body.detail ?? detail);
  } catch {
    /* not JSON: keep the status text */
  }
  return `The API answered ${r.status}: ${detail}`;
}
