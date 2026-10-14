// Where a claim's quote sits in its cited evidence text. Verify (agent/graph.py normalize) ignores
// case, runs of whitespace, curly quotes, ⟦...⟧ defense markers, and quote marks or ellipses
// around the quote; the match here allows the same, and returns offsets into the original text.

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// Same length out as in, so offsets still index the original text.
const straighten = (s: string) => s.replace(/[“”]/g, '"').replace(/[‘’]/g, "'");

export function findQuote(text: string, quote: string): [number, number] | null {
  const words = straighten(quote)
    .trim()
    .replace(/^["'.…\s]+|["'.…\s]+$/g, "")
    .split(/\s+/)
    .filter(Boolean);
  if (!words.length) return null;
  const gap = String.raw`(?:\s|⟦[^⟧]*⟧)+`;
  const m = new RegExp(words.map(escape).join(gap), "i").exec(straighten(text));
  return m ? [m.index, m.index + m[0].length] : null;
}
