import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App, { DEMO } from "./App";

const enc = new TextEncoder();
const sse = (event: string, data: object) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const ok = { success: true, error: null, note: "", output_tokens: 120, latency_ms: 42.4 };
const LOG = "2026-10-06T14:40:01Z ERROR payments-api connection refused by db-primary:5432";

const EVENTS = [
  sse("start", { investigation_id: "i-1", as_of: "2026-10-06T14:52:00Z", config: { wrap: true, flag: true, guard: "withhold", first_look: true } }),
  sse("tool_call", { name: "get_service_status", arguments: { service: "payments-api" } }),
  sse("tool_result", { name: "get_service_status", ids: ["status:payments-api"], ...ok }),
  sse("tool_call", { name: "query_logs", arguments: { service: "payments-api", level: "WARN" } }),
  sse("tool_result", { name: "query_logs", ids: ["log:1"], ...ok }),
  sse("sources", {
    items: [
      { id: "log:1", source: "log", text: LOG, metadata: {}, cited: true },
      { id: "RB-007#2", source: "runbook", text: "Check the pool.", metadata: { score: 0.03279 }, cited: false },
    ],
  }),
  sse("diagnosis", {
    diagnosis: {
      summary: "Payments lost its database.",
      likely_cause: "db-primary refused connections",
      confidence: "high",
      claims: [{ text: "The DB refused connections.", evidence_ids: ["log:1"], quote: "connection refused by db-primary" }],
      next_checks: ["Check db-primary health"],
      citations_verified: true,
      dropped_claims: [],
      note: "",
    },
    loops: 1,
    usage: { llm_calls: 6, cached_calls: 0, prompt_tokens: 12345, completion_tokens: 678, cost_usd: 0.0042 },
  }),
];

// A response whose body is released chunk by chunk, so the test can look between events.
function controlledStream() {
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start: (c) => void (ctrl = c) });
  return { body, push: (s: string) => ctrl.enqueue(enc.encode(s)), close: () => ctrl.close() };
}

afterEach(() => vi.unstubAllGlobals());

async function start() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "Load demo incident" }));
  await user.click(screen.getByRole("button", { name: "Investigate" }));
  return user;
}

describe("App", () => {
  it("shows empty states before a run", () => {
    render(<App />);
    expect(screen.getByText(/Tool calls appear here/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Investigate" })).toBeDisabled();
    expect(screen.queryByLabelText("Run stats")).not.toBeInTheDocument();
  });

  it("streams tool calls as they arrive, then the diagnosis and stats", async () => {
    const s = controlledStream();
    const fetchMock = vi.fn().mockResolvedValue(new Response(s.body, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await start();

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/investigate");
    expect(JSON.parse(init.body)).toEqual({ question: DEMO.question, as_of: "2026-10-06T14:52:00Z" });
    expect(init.headers["X-API-Key"]).toBeUndefined(); // no key typed: no header

    s.push(EVENTS[0] + EVENTS[1]);
    const stream = screen.getByRole("region", { name: /Live tool calls/ });
    await within(stream).findByText("get_service_status");
    expect(within(stream).getByRole("status", { name: "get_service_status running" })).toBeInTheDocument();
    expect(screen.getByText("Defenses on: wrap, flag, guard withhold")).toBeInTheDocument();

    s.push(EVENTS[2] + EVENTS[3]);
    await within(stream).findByText("query_logs");
    expect(within(stream).getByText(/1 items · 120 tok · 42 ms/)).toBeInTheDocument();
    expect(within(stream).getByRole("status", { name: "query_logs running" })).toBeInTheDocument();
    expect(screen.getByText(/Gathering evidence/)).toBeInTheDocument();

    s.push(EVENTS.slice(4).join(""));
    s.close();
    await screen.findByText("Payments lost its database.");
    expect(within(stream).queryByRole("status")).not.toBeInTheDocument();
    const stats = screen.getByLabelText("Run stats");
    expect(within(stats).getByText("12,345 / 678")).toBeInTheDocument();
    expect(within(stats).getByText("$0.0042")).toBeInTheDocument();
    expect(within(stats).getByText("2")).toBeInTheDocument(); // tool calls
    expect(within(stats).getByText("1")).toBeInTheDocument(); // verify loops
    const sources = screen.getByRole("region", { name: /Sources/ });
    expect(within(sources).getByText("0.0328")).toBeInTheDocument();
    expect(within(sources).getByText("runbook")).toBeInTheDocument();
  });

  it("opens a citation's evidence with the quote highlighted", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(EVENTS.join(""), { status: 200 })));
    const user = await start();
    await user.click(await screen.findByRole("button", { name: "Open evidence log:1" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("open");
    expect(dialog.querySelector("mark")).toHaveTextContent("connection refused by db-primary");
    await user.click(within(dialog).getByRole("button", { name: "Close evidence" }));
    expect(dialog).not.toHaveAttribute("open");
  });

  it("shows the API's validation error", async () => {
    const detail = [{ msg: "String should have at least 5 characters" }];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ detail }, { status: 422 })));
    await start();
    expect(await screen.findByRole("alert")).toHaveTextContent("422: String should have at least 5 characters");
  });

  it("sends the typed API key as X-API-Key, keeps it out of storage, and shows a 401", async () => {
    const detail = "missing or invalid X-API-Key";
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ detail }, { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText("API key"), " k-123 ");
    await user.click(screen.getByRole("button", { name: "Load demo incident" }));
    await user.click(screen.getByRole("button", { name: "Investigate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(`401: ${detail}`);
    expect(fetchMock.mock.calls[0][1].headers["X-API-Key"]).toBe("k-123");
    expect(localStorage.length + sessionStorage.length).toBe(0);
    await user.clear(screen.getByLabelText("API key")); // module memory: reset for later tests
  });

  it("reports a run that ended without a diagnosis", async () => {
    const text = EVENTS[0] + sse("error", { message: "LLMError: upstream 503" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(text, { status: 200 })));
    await start();
    expect(await screen.findByRole("alert")).toHaveTextContent("LLMError: upstream 503");
    expect(screen.getByText("No diagnosis: the run did not finish.")).toBeInTheDocument();
  });

  it("switches theme from the header, in memory only", async () => {
    const user = userEvent.setup();
    render(<App />);
    const root = document.documentElement;
    const group = screen.getByRole("group", { name: "Theme" });
    expect(within(group).getByRole("button", { name: "light" })).toHaveAttribute("aria-pressed", "true");
    expect(root.dataset.theme).toBe("light"); // light by default

    await user.click(within(group).getByRole("button", { name: "dark" }));
    expect(root.dataset.theme).toBe("dark");
    expect(within(group).getByRole("button", { name: "dark" })).toHaveAttribute("aria-pressed", "true");
    await user.click(within(group).getByRole("button", { name: "system" }));
    expect(root.dataset.theme).toBeUndefined(); // follows prefers-color-scheme
    await user.click(within(group).getByRole("button", { name: "light" }));
    expect(root.dataset.theme).toBe("light");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("reports a network failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await start();
    expect(await screen.findByRole("alert")).toHaveTextContent("Network error: TypeError: Failed to fetch");
  });
});
