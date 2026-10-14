import { useEffect, useReducer, useRef, useState } from "react";
import { findQuote } from "./highlight";
import { type Call, type Claim, type Config, type RunState, type Source, idle, investigate, reduce, setApiKey } from "./stream";

export const DEMO = {
  question:
    "The payment API is returning connection-refused errors. What likely happened, what evidence supports it, and what should I check next?",
  asOf: "2026-10-06T14:52",
};

// Colours are design tokens (src/index.css); the two themes differ only there.
const panel = "rounded-xl border border-line bg-surface p-4";
const h2 = "mb-3 flex items-center gap-2 text-sm font-semibold text-muted";
const focus = "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";
const press = "transition-[background-color,border-color,color,transform] duration-150 active:scale-[0.98] motion-reduce:transition-none motion-reduce:active:scale-100";
const field = `rounded-lg border border-field-line bg-surface p-2 text-ink ${focus}`;
const btn = `rounded-lg px-4 py-2 font-medium ${press} ${focus}`;
const btnSecondary = `${btn} border border-line bg-raised text-ink hover:border-field-line`;
const chip = "inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs font-medium";

const ICONS = {
  check: "M3.5 8.5l3 3 6-7",
  alert: "M8 2.5l6 11H2zM8 7v3M8 12.2v.1",
  close: "M4 4l8 8M12 4l-8 8",
  lock: "M4.5 7.5h7v6h-7zM6 7.5V5.5a2 2 0 014 0v2",
  dash: "M4 8h8",
};

function Icon({ name, className = "" }: { name: keyof typeof ICONS; className?: string }) {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className={`h-3.5 w-3.5 shrink-0 ${className}`} fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round">
      <path d={ICONS[name]} />
    </svg>
  );
}

function Spinner({ label }: { label: string }) {
  return (
    <span
      role="status"
      aria-label={label}
      className="inline-block h-3.5 w-3.5 rounded-full border-2 border-current border-t-transparent align-middle motion-safe:animate-spin"
    />
  );
}

type Theme = "system" | "light" | "dark";

// Dark by default (also set on <html> in index.html, so the first paint is dark). The choice
// lives in React state only (no storage): a reload goes back to dark. "system" follows the OS.
function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("dark");
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
  }, [theme]);
  return (
    <div role="group" aria-label="Theme" className="inline-flex rounded-lg border border-line bg-surface p-0.5 text-xs">
      {(["system", "light", "dark"] as const).map((t) => (
        <button
          key={t}
          type="button"
          aria-pressed={theme === t}
          onClick={() => setTheme(t)}
          className={`rounded-md px-2.5 py-1 font-medium capitalize ${press} ${focus} ${
            theme === t ? "bg-raised text-ink" : "text-muted hover:text-ink"
          }`}
        >
          {t}
        </button>
      ))}
    </div>
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
    <div className="min-h-dvh bg-canvas text-ink">
      <main className="mx-auto max-w-7xl space-y-4 px-4 py-6 sm:px-6">
        <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
              <svg viewBox="0 0 24 24" aria-hidden="true" className="h-6 w-6 text-accent" fill="none" stroke="currentColor" strokeWidth={2.25} strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 12h4l2.5-6 5 12L17 12h4" />
              </svg>
              OpsPilot
            </h1>
            <p className="mt-1 max-w-[68ch] text-sm text-pretty text-muted">
              Describe an incident. The agent searches runbooks, past incidents, logs and service status, then
              answers with claims you can check against the evidence they cite.
            </p>
          </div>
          <ThemeToggle />
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
              className={`w-full placeholder:text-muted ${field}`}
              minLength={5}
              maxLength={2000}
              required
            />
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label htmlFor="api-key" className="mb-1 flex items-center gap-1.5 text-sm font-medium">
                <Icon name="lock" className="text-muted" />
                API key
              </label>
              <input
                id="api-key"
                type="password"
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setApiKey(e.target.value)}
                aria-describedby="api-key-help"
                className={field}
              />
              <p id="api-key-help" className="mt-1 text-xs text-muted">
                Sent as X-API-Key. Kept in memory only.
              </p>
            </div>
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
                className={field}
              />
              <p id="as-of-help" className="mt-1 text-xs text-muted">
                When the question is asked; log windows end here. Empty means now.
              </p>
            </div>
            <div className="flex flex-wrap gap-2 pb-5">
              <button
                type="submit"
                disabled={running || question.trim().length < 5}
                className={`${btn} bg-accent text-accent-ink hover:bg-accent-hover disabled:cursor-not-allowed disabled:bg-raised disabled:text-muted disabled:active:scale-100`}
              >
                {running ? "Investigating…" : "Investigate"}
              </button>
              {running ? (
                <button type="button" onClick={() => abort.current?.abort()} className={btnSecondary}>
                  Stop
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setQuestion(DEMO.question);
                    setAsOf(DEMO.asOf);
                  }}
                  className={btnSecondary}
                >
                  Load demo incident
                </button>
              )}
            </div>
          </div>
        </form>

        {run.status === "error" && (
          <div role="alert" className="flex gap-2 rounded-xl border border-bad bg-bad-soft p-3 text-ink motion-safe:animate-rise">
            <Icon name="alert" className="mt-1 text-bad" />
            <p>
              <strong>Investigation failed.</strong> {run.error}
            </p>
          </div>
        )}

        <Stats run={run} />

        {/* Diagnosis first in the DOM, so on a phone the answer comes before the evidence trail;
            from lg up it takes the right-hand columns as before. */}
        <div className="grid gap-4 lg:grid-cols-12">
          <div className="lg:col-span-7 lg:col-start-6 lg:row-start-1">
            <DiagnosisPanel run={run} onCite={(id, quote) => setOpen({ id, quote })} />
          </div>
          <div className="space-y-4 lg:col-span-5 lg:col-start-1 lg:row-start-1">
            <Stream run={run} />
            <Sources run={run} onOpen={(id) => setOpen({ id, quote: "" })} />
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
    <section
      aria-label="Run stats"
      className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl border border-line bg-surface px-4 py-2.5"
    >
      {items.map(([k, v]) => (
        <div key={k} className="flex items-baseline gap-2">
          <div className="text-xs text-muted">{k}</div>
          <div className="text-sm font-semibold tabular-nums">{v}</div>
        </div>
      ))}
      <span className={`${chip} sm:ml-auto ${defended ? "bg-ok-soft text-ok" : "bg-warn-soft text-warn"}`}>
        <Icon name={defended ? "check" : "alert"} />
        <span>Defenses {defensesLabel(run.config)}</span>
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
  const dot = !r ? "bg-accent" : r.success ? "bg-ok" : "bg-bad";
  return (
    <li className="relative pb-3 pl-5 last:pb-0 motion-safe:animate-rise">
      <span aria-hidden="true" className="absolute top-0 bottom-0 left-[4px] w-0.5 bg-line" />
      <span aria-hidden="true" className={`absolute top-1.5 left-0 h-2.5 w-2.5 rounded-full ring-4 ring-surface ${dot}`} />
      <div className="flex flex-wrap items-center justify-between gap-x-2">
        <span className="font-mono text-sm font-semibold">{call.name}</span>
        {!r ? (
          <span className="text-accent">
            <Spinner label={`${call.name} running`} />
          </span>
        ) : (
          <span className={`${chip} ${r.success ? "bg-ok-soft text-ok" : "bg-bad-soft text-bad"}`}>{r.success ? "ok" : "failed"}</span>
        )}
      </div>
      <div className="font-mono text-xs break-all text-muted">{argsText(call.arguments)}</div>
      {r && (
        <div className="mt-0.5 font-mono text-xs text-muted tabular-nums">
          {r.ids.length} items · {r.output_tokens.toLocaleString()} tok · {Math.round(r.latency_ms)} ms
        </div>
      )}
      {r?.error && <div className="mt-1 text-xs text-bad">{r.error}</div>}
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
        <p className="text-sm text-muted">
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
        <ul className="mt-3 space-y-1 text-xs text-warn">
          {run.warnings.map((w, i) => (
            <li key={i} className="flex gap-1.5">
              <Icon name="alert" className="mt-px" />
              {w}
            </li>
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
        <p className="text-sm text-muted">Retrieved evidence is listed when the agent finishes.</p>
      ) : (
        <table className="w-full text-left text-sm motion-safe:animate-rise">
          <thead className="text-xs text-muted">
            <tr>
              <th className="pb-1 font-medium">Evidence</th>
              <th className="pb-1 font-medium">Type</th>
              <th className="pb-1 text-right font-medium">Score</th>
            </tr>
          </thead>
          <tbody>
            {run.sources.map((s) => (
              <tr key={s.id} className="border-t border-line">
                <td className="py-1.5 pr-2">
                  <button
                    type="button"
                    onClick={() => onOpen(s.id)}
                    className={`rounded-sm text-left font-mono text-xs break-all text-accent underline-offset-2 hover:underline ${focus}`}
                  >
                    {s.id}
                  </button>
                  {s.cited && <span className="ml-1.5 rounded-sm bg-accent-soft px-1 text-[11px] font-medium text-accent">cited</span>}
                </td>
                <td className="py-1.5 pr-2 text-xs text-muted">{s.source}</td>
                <td className="py-1.5 text-right font-mono text-xs tabular-nums">{score(s)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

const LEVEL = { low: 1, medium: 2, high: 3 } as const;
const LEVEL_TONE = { low: "text-bad", medium: "text-warn", high: "text-ok" } as const;
const BAR_H = ["h-[7px]", "h-[10px]", "h-[13px]"];

// Three bars plus the word, so the level reads without relying on colour alone.
function Confidence({ level }: { level: keyof typeof LEVEL }) {
  return (
    <span className={`${chip} bg-raised text-ink`}>
      <span aria-hidden="true" className={`flex items-end gap-0.5 ${LEVEL_TONE[level]}`}>
        {BAR_H.map((h, i) => (
          <span key={h} className={`w-1 rounded-[1px] ${h} ${i < LEVEL[level] ? "bg-current" : "bg-line"}`} />
        ))}
      </span>
      <span>Confidence: {level}</span>
    </span>
  );
}

function ClaimItem({ claim, onCite }: { claim: Claim; onCite: (id: string, quote: string) => void }) {
  return (
    <li className="rounded-lg bg-raised p-3">
      <p>{claim.text}</p>
      {claim.quote && (
        <blockquote className="mt-2 border-l-2 border-accent pl-3 font-mono text-xs text-muted">“{claim.quote}”</blockquote>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        {claim.evidence_ids.map((id) => (
          <button
            key={id}
            type="button"
            onClick={() => onCite(id, claim.quote)}
            aria-label={`Open evidence ${id}`}
            className={`rounded-md border border-line bg-surface px-2 py-0.5 font-mono text-xs text-accent hover:border-accent ${press} ${focus}`}
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
  const verified = d?.citations_verified;
  return (
    <section aria-labelledby="diag-h" className="rounded-xl border border-line border-t-4 border-t-accent bg-surface p-5 sm:p-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 id="diag-h" className="text-base font-semibold">
          Diagnosis
        </h2>
        {d && (
          <div className="flex flex-wrap items-center gap-2">
            <Confidence level={d.confidence} />
            <span className={`${chip} ${verified === null ? "bg-raised text-muted" : verified ? "bg-ok-soft text-ok" : "bg-warn-soft text-warn"}`}>
              <Icon name={verified === null ? "dash" : verified ? "check" : "alert"} />
              <span>Citations {verified === null ? "not checked" : verified ? "verified" : "partly dropped"}</span>
            </span>
          </div>
        )}
      </div>
      {!d ? (
        <div>
          <p className="text-sm text-muted">
            {run.status === "running"
              ? "Gathering evidence, then drafting and verifying citations…"
              : run.status === "error"
                ? "No diagnosis: the run did not finish."
                : "The diagnosis appears here, with every claim linked to the evidence it cites."}
          </p>
          {run.status === "running" && (
            <div aria-hidden="true" className="mt-5 space-y-3 motion-safe:animate-breathe">
              <div className="h-5 w-4/5 rounded bg-line" />
              <div className="h-5 w-3/5 rounded bg-line" />
              <div className="mt-6 h-16 rounded-lg bg-line" />
              <div className="h-16 rounded-lg bg-line" />
            </div>
          )}
        </div>
      ) : (
        <div className="space-y-5 motion-safe:animate-rise">
          <p className="text-xl leading-snug font-semibold text-balance sm:text-2xl">{d.summary}</p>
          <div className="rounded-lg bg-accent-soft p-3">
            <h3 className="text-xs font-semibold text-accent">Likely cause</h3>
            <p className="mt-0.5">{d.likely_cause}</p>
          </div>
          <div>
            <h3 className="mb-2 text-sm font-semibold text-muted">Claims</h3>
            {d.claims.length ? (
              <ol className="space-y-2">
                {d.claims.map((c, i) => (
                  <ClaimItem key={i} claim={c} onCite={onCite} />
                ))}
              </ol>
            ) : (
              <p className="text-sm text-muted">No claim survived citation checks.</p>
            )}
          </div>
          {d.dropped_claims.length > 0 && (
            <details className="text-sm">
              <summary className={`cursor-pointer rounded-sm text-warn ${focus}`}>{d.dropped_claims.length} claim(s) dropped by verify</summary>
              <p className="mt-1 text-muted">{d.note}</p>
              <ul className="mt-2 list-disc pl-5">
                {d.dropped_claims.map((c, i) => (
                  <li key={i}>{c.text}</li>
                ))}
              </ul>
            </details>
          )}
          <div>
            <h3 className="mb-1 text-sm font-semibold text-muted">Next checks</h3>
            <ul className="list-disc space-y-1 pl-5 marker:text-accent">
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
      className="m-auto w-[min(48rem,calc(100vw-2rem))] rounded-xl border border-line bg-surface p-0 text-ink shadow-2xl backdrop:bg-black/50 open:motion-safe:animate-rise"
    >
      {open && (
        <div className="space-y-3 p-4">
          <div className="flex items-start justify-between gap-3">
            <h2 id="evidence-h" className="font-mono text-sm font-semibold break-all">
              {open.id}
            </h2>
            <button
              type="button"
              onClick={onClose}
              className={`rounded-md p-1.5 text-muted hover:bg-raised hover:text-ink ${focus}`}
              aria-label="Close evidence"
            >
              <Icon name="close" className="h-4 w-4" />
            </button>
          </div>
          {!s ? (
            <p className="text-sm text-bad">This id is not in the gathered evidence.</p>
          ) : (
            <>
              <p className="text-xs text-muted">
                {s.source} · score {score(s)}
                {open.quote && !at && " · quote not located verbatim in this item"}
              </p>
              <pre className="max-h-[60vh] overflow-auto rounded-lg bg-raised p-3 text-sm break-words whitespace-pre-wrap">
                {at ? (
                  <>
                    {s.text.slice(0, at[0])}
                    <mark className="rounded-sm bg-mark text-ink">{s.text.slice(at[0], at[1])}</mark>
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
