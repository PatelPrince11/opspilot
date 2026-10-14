import { describe, expect, it } from "vitest";
import { findQuote } from "./highlight";
import { type AgentEvent, idle, readEvents, reduce } from "./stream";

const enc = new TextEncoder();
function body(chunks: string[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(c) {
      chunks.forEach((x) => c.enqueue(enc.encode(x)));
      c.close();
    },
  });
}

async function collect(chunks: string[]) {
  const out: AgentEvent[] = [];
  for await (const ev of readEvents(body(chunks))) out.push(ev);
  return out;
}

describe("readEvents", () => {
  it("parses events split across chunks at any byte", async () => {
    const text =
      'event: tool_call\ndata: {"name": "query_logs", "arguments": {"service": "payments-api"}}\n\n' +
      'event: error\ndata: {"message": "skipped x: tool-call cap"}\n\n';
    for (const cut of [1, 7, 30, text.length - 3]) {
      const evs = await collect([text.slice(0, cut), text.slice(cut)]);
      expect(evs.map((e) => e.event)).toEqual(["tool_call", "error"]);
      expect(evs[0]).toMatchObject({ name: "query_logs", arguments: { service: "payments-api" } });
    }
  });

  it("keeps a last event that has no trailing blank line", async () => {
    const evs = await collect(['event: error\ndata: {"message": "m"}']);
    expect(evs).toEqual([{ event: "error", message: "m" }]);
  });
});

describe("reduce", () => {
  const result = { success: true, error: null, ids: ["a"], note: "", output_tokens: 5, latency_ms: 3 };

  it("pairs each tool_result with the oldest waiting call", () => {
    let s = reduce(idle, { type: "begin", at: 0 });
    s = reduce(s, { type: "event", ev: { event: "tool_call", name: "a", arguments: {} } });
    s = reduce(s, { type: "event", ev: { event: "tool_call", name: "b", arguments: {} } });
    s = reduce(s, { type: "event", ev: { event: "tool_result", name: "a", ...result } });
    expect(s.calls.map((c) => !!c.result)).toEqual([true, false]);
    expect(s.calls[0].result).toEqual(result);
  });

  it("ends in error when no diagnosis arrived, with the stream's last error", () => {
    let s = reduce(idle, { type: "begin", at: 0 });
    s = reduce(s, { type: "event", ev: { event: "error", message: "RuntimeError: boom" } });
    s = reduce(s, { type: "end", at: 1 });
    expect(s).toMatchObject({ status: "error", error: "RuntimeError: boom" });
  });
});

describe("findQuote", () => {
  it("matches like verify: case, whitespace, curly quotes and defense markers ignored", () => {
    const text = "WARN pool  Exhausted ⟦flagged: x⟧ after 30s ‘retry’";
    const q = '"pool exhausted after 30s \'retry\'"';
    const [a, b] = findQuote(text, q)!;
    // verify drops quote marks around a quote, so the closing one is not part of the match
    expect(text.slice(a, b)).toBe("pool  Exhausted ⟦flagged: x⟧ after 30s ‘retry");
    expect(findQuote(text, "not there")).toBeNull();
  });
});
