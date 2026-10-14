import { useEffect, useReducer, useRef, useState } from "react";
import { findQuote } from "./highlight";
import { type Call, type Claim, type Config, type RunState, type Source, idle, investigate, reduce } from "./stream";

export const DEMO = {
  question:
    "The payment API is returning connection-refused errors. What likely happened, what evidence supports it, and what should I check next?",
  asOf: "2026-10-06T14:52",
};

const panel = "rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900";
const h2 = "mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400";
const focus = "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600";

function Spinner({ label }: { label: string }) {
  return (
    <span
      role="status"
      aria-label={label}
      className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent align-middle"
    />
  );
}

// datetime-local gives "YYYY-MM-DDTHH:mm[:ss]"; the field is labelled UTC.
const toUtc = (v: string) => (v ? `${v.length === 16 ? `${v}:00` : v}Z` : null);

export default function App() {
  const [run, dispatch] = useReducer(reduce, idle);
  const [question, setQuestion] = useState("");
  const [asOf, setAsOf] = useState("");
  const [open, setOpen] = useState<{ id: string; quote: string } | null>(null);
  const abort = useRef<AbortController | null>(null);
  const running = run.status === "running";

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (running || question.trim().length < 5) return;
    abort.current = new AbortController();
    void investigate(question.trim(), toUtc(asOf), dispatch, abort.current.signal);
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <main className="mx-auto max-w-7xl space-y-4 px-4 py-6">
        <header>
          <h1 className="text-2xl font-bold">OpsPilot</h1>
          <p className="text-sm text-slate-600 dark:text-slate-400">
            Describe an incident. The agent searches runbooks, past incidents, logs and service status, then
            answers with claims you can check against the evidence they cite.
          </p>
        </header>

        <form onSubmit={submit} className={`${panel} space-y-3`}>
          <div>
            <label htmlFor="question" className="mb-1 block text-sm font-medium">
              Incident
            </label>
            <textarea
              id="question"
              rows={3}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(e);
              }}
              placeholder="e.g. The payment API is refusing connections. What happened?"
              className={`w-full rounded-md border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800 ${focus}`}
              minLength={5}
              maxLength={2000}
              required
            />
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label htmlFor="as-of" className="mb-1 block text-sm font-medium">
                As of (UTC, optional)
              </label>
              <input
                id="as-of"
                type="datetime-local"
                step={1}
                value={asOf}
                onChange={(e) => setAsOf(e.target.value)}
                aria-describedby="as-of-help"
                className={`rounded-md border border-slate-300 bg-white p-2 dark:border-slate-600 dark:bg-slate-800 ${focus}`}
              />
              <p id="as-of-help" className="mt-1 text-xs text-slate-500">
                When the question is asked; log windows end here. Empty means now.
              </p>
            </div>
            <div className="flex flex-wrap gap-2 pb-5">
              <button
                type="submit"
                disabled={running || question.trim().length < 5}
                className={`rounded-md bg-blue-700 px-4 py-2 font-medium text-white hover:bg-blue-800 disabled:cursor-not-allowed disabled:opacity-50 ${focus}`}
              >
                {running ? "Investigating…" : "Investigate"}
              </button>
              {running ? (
                <button
                  type="button"
                  onClick={() => abort.current?.abort()}
                  className={`rounded-md border border-slate-300 px-4 py-2 dark:border-slate-600 ${focus}`}
                >
                  Stop
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setQuestion(DEMO.question);
                    setAsOf(DEMO.asOf);
                  }}
                  className={`rounded-md border border-slate-300 px-4 py-2 dark:border-slate-600 ${focus}`}
                >
                  Load demo incident
                </button>
              )}
            </div>
          </div>
        </form>

        {run.status === "error" && (
          <div role="alert" className="rounded-lg border border-red-300 bg-red-50 p-3 text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100">
            <strong>Investigation failed.</strong> {run.error}
          </div>
        )}

        <Stats run={run} />

        <div className="grid gap-4 lg:grid-cols-12">
          <div className="space-y-4 lg:col-span-5">
            <Stream run={run} />
            <Sources run={run} onOpen={(id) => setOpen({ id, quote: "" })} />
          </div>
          <div className="lg:col-span-7">
            <DiagnosisPanel run={run} onCite={(id, quote) => setOpen({ id, quote })} />
          </div>
        </div>
      </main>
      <EvidenceDialog open={open} sources={run.sources} onClose={() => setOpen(null)} />
    </div>
  );
}

function defensesLabel(c?: Config): string {
  if (!c) return "unknown";
  const on = [c.wrap && "wrap", c.flag && "flag", c.guard && c.guard !== "off" && `guard ${c.guard}`].filter(Boolean);
  return on.length ? `on: ${on.join(", ")}` : "off";
}

function Stats({ run }: { run: RunState }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (run.status !== "running") return;
    const t = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(t);
  }, [run.status]);
  if (run.status === "idle") return null;
  const end = run.finishedAt ?? now;
  const wall = run.startedAt ? Math.max(0, (end - run.startedAt) / 1000) : 0;
  const u = run.usage;
  const failed = run.calls.filter((c) => c.result && !c.result.success).length;
  const defended = run.config && defensesLabel(run.config) !== "off";
  const items: [string, string][] = [
    ["Tokens in / out", u ? `${u.prompt_tokens.toLocaleString()} / ${u.completion_tokens.toLocaleString()}` : "…"],
    ["Cost", u ? `$${u.cost_usd.toFixed(4)}` : "…"],
    ["Wall time", `${wall.toFixed(1)} s`],
    ["Verify loops", run.loops === undefined ? "…" : String(run.loops)],
    ["Tool calls", `${run.calls.length}${failed ? ` (${failed} failed)` : ""}`],
  ];
  return (
    <section aria-label="Run stats" className={`${panel} flex flex-wrap items-center gap-x-6 gap-y-2`}>
      {items.map(([k, v]) => (
        <div key={k}>
          <div className="text-xs text-slate-500">{k}</div>
          <div className="font-mono text-sm tabular-nums">{v}</div>
        </div>
      ))}
      <span
        className={`rounded-full px-3 py-1 text-xs font-medium ${
          defended ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900 dark:text-emerald-100" : "bg-amber-100 text-amber-900"
        }`}
      >
        Defenses {defensesLabel(run.config)}
      </span>
    </section>
  );
}

function argsText(a: Record<string, unknown>): string {
  return Object.entries(a)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(" ");
}

function CallRow({ call }: { call: Call }) {
  const r = call.result;
  return (
    <li className="border-b border-slate-100 py-2 last:border-0 dark:border-slate-800">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-sm font-semibold">{call.name}</span>
        {!r ? (
          <span className="text-blue-700 dark:text-blue-400">
            <Spinner label={`${call.name} running`} />
          </span>
        ) : (
          <span className="font-mono text-xs tabular-nums text-slate-600 dark:text-slate-400">
            <span className={r.success ? "text-emerald-700 dark:text-emerald-400" : "text-red-700 dark:text-red-400"}>
              {r.success ? "ok" : "failed"}
            </span>{" "}
            · {r.ids.length} items · {r.output_tokens.toLocaleString()} tok · {Math.round(r.latency_ms)} ms
          </span>
        )}
      </div>
      <div className="break-all font-mono text-xs text-slate-500">{argsText(call.arguments)}</div>
      {r?.error && <div className="mt-1 text-xs text-red-700 dark:text-red-400">{r.error}</div>}
    </li>
  );
}

function Stream({ run }: { run: RunState }) {
  return (
    <section aria-labelledby="stream-h" className={panel}>
      <h2 id="stream-h" className={h2}>
        Live tool calls {run.status === "running" && <Spinner label="investigation running" />}
      </h2>
      {run.calls.length === 0 ? (
        <p className="text-sm text-slate-500">
          {run.status === "running" ? "Triage first: reading the question…" : "Tool calls appear here as the agent makes them."}
        </p>
      ) : (
        <ol aria-live="polite" aria-busy={run.status === "running"}>
          {run.calls.map((c, i) => (
            <CallRow key={i} call={c} />
          ))}
        </ol>
      )}
      {run.warnings.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs text-amber-800 dark:text-amber-300">
          {run.warnings.map((w, i) => (
            <li key={i}>⚠ {w}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

const score = (s: Source) => (typeof s.metadata.score === "number" ? s.metadata.score.toFixed(4) : "—");

function Sources({ run, onOpen }: { run: RunState; onOpen: (id: string) => void }) {
  return (
    <section aria-labelledby="sources-h" className={panel}>
      <h2 id="sources-h" className={h2}>
        Sources {run.sources.length > 0 && `(${run.sources.length})`}
      </h2>
      {run.sources.length === 0 ? (
        <p className="text-sm text-slate-500">Retrieved evidence is listed when the agent finishes.</p>
      ) : (
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-slate-500">
            <tr>
              <th className="py-1 font-medium">Evidence</th>
              <th className="py-1 font-medium">Type</th>
              <th className="py-1 text-right font-medium">Score</th>
            </tr>
          </thead>
          <tbody>
            {run.sources.map((s) => (
              <tr key={s.id} className="border-t border-slate-100 dark:border-slate-800">
                <td className="py-1 pr-2">
                  <button
                    type="button"
                    onClick={() => onOpen(s.id)}
                    className={`break-all text-left font-mono text-xs text-blue-700 underline-offset-2 hover:underline dark:text-blue-400 ${focus}`}
                  >
                    {s.id}
                  </button>
                  {s.cited && <span className="ml-1 rounded bg-blue-100 px-1 text-[10px] text-blue-900">cited</span>}
                </td>
                <td className="py-1 pr-2 text-xs">{s.source}</td>
                <td className="py-1 text-right font-mono text-xs tabular-nums">{score(s)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function ClaimItem({ claim, onCite }: { claim: Claim; onCite: (id: string, quote: string) => void }) {
  return (
    <li className="rounded-md border border-slate-200 p-3 dark:border-slate-700">
      <p>{claim.text}</p>
      {claim.quote && <p className="mt-1 text-sm italic text-slate-600 dark:text-slate-400">“{claim.quote}”</p>}
      <div className="mt-2 flex flex-wrap gap-2">
        {claim.evidence_ids.map((id) => (
          <button
            key={id}
            type="button"
            onClick={() => onCite(id, claim.quote)}
            aria-label={`Open evidence ${id}`}
            className={`rounded bg-slate-100 px-2 py-0.5 font-mono text-xs text-blue-800 hover:bg-blue-100 dark:bg-slate-800 dark:text-blue-300 ${focus}`}
          >
            [{id}]
          </button>
        ))}
      </div>
    </li>
  );
}

function DiagnosisPanel({ run, onCite }: { run: RunState; onCite: (id: string, quote: string) => void }) {
  const d = run.diagnosis;
  return (
    <section aria-labelledby="diag-h" className={panel}>
      <h2 id="diag-h" className={h2}>
        Diagnosis
      </h2>
      {!d ? (
        <p className="text-sm text-slate-500">
          {run.status === "running"
            ? "Gathering evidence, then drafting and verifying citations…"
            : run.status === "error"
              ? "No diagnosis: the run did not finish."
              : "The diagnosis appears here, with every claim linked to the evidence it cites."}
        </p>
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="rounded-full bg-slate-100 px-2 py-0.5 dark:bg-slate-800">confidence: {d.confidence}</span>
            <span className="rounded-full bg-slate-100 px-2 py-0.5 dark:bg-slate-800">
              citations{" "}
              {d.citations_verified === null ? "not checked" : d.citations_verified ? "verified" : "partly dropped"}
            </span>
          </div>
          <p>{d.summary}</p>
          <div>
            <h3 className="text-sm font-semibold">Likely cause</h3>
            <p>{d.likely_cause}</p>
          </div>
          <div>
            <h3 className="mb-2 text-sm font-semibold">Claims</h3>
            {d.claims.length ? (
              <ol className="space-y-2">
                {d.claims.map((c, i) => (
                  <ClaimItem key={i} claim={c} onCite={onCite} />
                ))}
              </ol>
            ) : (
              <p className="text-sm text-slate-500">No claim survived citation checks.</p>
            )}
          </div>
          {d.dropped_claims.length > 0 && (
            <details className="text-sm">
              <summary className={`cursor-pointer text-amber-800 dark:text-amber-300 ${focus}`}>
                {d.dropped_claims.length} claim(s) dropped by verify
              </summary>
              <p className="mt-1 text-slate-600 dark:text-slate-400">{d.note}</p>
              <ul className="mt-2 list-disc pl-5">
                {d.dropped_claims.map((c, i) => (
                  <li key={i}>{c.text}</li>
                ))}
              </ul>
            </details>
          )}
          <div>
            <h3 className="text-sm font-semibold">Next checks</h3>
            <ul className="list-disc pl-5">
              {d.next_checks.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </section>
  );
}

function EvidenceDialog({
  open,
  sources,
  onClose,
}: {
  open: { id: string; quote: string } | null;
  sources: Source[];
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal(); // native modal: focus moves in, Escape closes
    if (!open && d.open) d.close();
    d.querySelector("mark")?.scrollIntoView?.({ block: "center" });
  }, [open]);
  const s = open && sources.find((x) => x.id === open.id);
  const at = s && open.quote ? findQuote(s.text, open.quote) : null;
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-labelledby="evidence-h"
      className="m-auto w-[min(48rem,calc(100vw-2rem))] rounded-lg p-0 backdrop:bg-black/40 dark:bg-slate-900 dark:text-slate-100"
    >
      {open && (
        <div className="space-y-3 p-4">
          <div className="flex items-start justify-between gap-3">
            <h2 id="evidence-h" className="break-all font-mono text-sm font-semibold">
              {open.id}
            </h2>
            <button type="button" onClick={onClose} className={`rounded px-2 text-sm ${focus}`} aria-label="Close evidence">
              ✕
            </button>
          </div>
          {!s ? (
            <p className="text-sm text-red-700">This id is not in the gathered evidence.</p>
          ) : (
            <>
              <p className="text-xs text-slate-500">
                {s.source} · score {score(s)}
                {open.quote && !at && " · quote not located verbatim in this item"}
              </p>
              <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-3 text-sm dark:bg-slate-800">
                {at ? (
                  <>
                    {s.text.slice(0, at[0])}
                    <mark className="bg-yellow-200 text-slate-900">{s.text.slice(at[0], at[1])}</mark>
                    {s.text.slice(at[1])}
                  </>
                ) : (
                  s.text
                )}
              </pre>
            </>
          )}
        </div>
      )}
    </dialog>
  );
}
