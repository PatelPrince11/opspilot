# Dashboard audit (before the redesign)

Baseline: the build on `origin/main` (a1e47ad), driven by the stub-LLM e2e server
(`tests/e2e_server.py`), screenshots in [`img/before/`](img/before/) (light and dark at 1440 px:
running, done, citation open; light at 375 px). Method: the audit list of the
`redesign-existing-projects` skill, plus `design-taste-frontend`'s `lint.mjs` (12 FAIL, 1 WARN:
`font-mono` ×8, `border-b`/`border-t` ×2, `⚠`/`✕` glyphs ×2, `min-h-screen`). The stub's
diagnosis text ("s", "c", "check x") is test data, not a design problem.

In priority order:

1. **The diagnosis has no more weight than anything else.** It sits in the same white bordered
   card, under the same small grey heading, as the stats, the tool calls and the sources. The
   summary is body text. At 375 px the grid stacks in source order, so the diagnosis comes
   last, below 13 source rows: on a phone the answer is a long scroll away.
2. **Confidence and citation status are not glanceable.** Both are identical grey pills
   (`confidence: high`, `citations verified`) in 12 px text; "low" and "high", "verified" and
   "partly dropped" look the same. Nothing uses colour or shape to carry the meaning.
3. **Every panel is the same template card.** `rounded-lg border bg-white p-4` five times, with
   the same uppercase tracked heading. No surface hierarchy: primary, secondary and chrome
   regions are indistinguishable, which is what makes it read as a Tailwind starter.
4. **Colour is ad hoc and the dark theme has gaps.** Raw palette classes (`blue-700`,
   `emerald-100`, `amber-100`, `slate-*`) with no tokens. Defenses-off (`bg-amber-100
   text-amber-900`), the `cited` chip (`bg-blue-100`) and the quote `<mark>` have no dark
   variants; the disabled "Investigating…" button at 50 % opacity on dark is hard to read.
   Theme follows the OS only; there is no way to switch for a demo.
5. **States are thin.** Running is a 14 px spinner next to a heading and a one-line grey
   sentence; the diagnosis panel stays a short empty box until the end, so the page jumps when
   it arrives. Stats show `…` placeholders. Buttons have colour change on hover only: no press
   feedback, no transitions, nothing honours `prefers-reduced-motion` (the spinner always spins).
6. **Type has no scale.** The page uses the browser's default sans at three sizes; the page
   title is 24 px bold and section headings are 14 px uppercase tracked grey. Monospace is used
   for ids, args, metadata and every stat value, so the stats strip reads as a log line rather
   than numbers to read at a glance.
7. **Stream rows are dense and wrap badly.** Name, status and metadata share one flex row; at
   375 px the metadata wraps under the name mid-phrase ("309 / tok · 0 ms") and long args
   break mid-token. Rows are separated by hairlines; failures are only a word in red.
8. **Spacing is uniform.** `p-4` and `space-y-4` everywhere: the form, the stats strip and the
   diagnosis get the same padding, so the compact things are not compact and the important
   thing is not roomy. The stats strip takes a full card for five numbers.
9. **Small polish.** `✕` and `⚠` glyphs instead of drawn icons; the citation dialog has no
   border or elevation in dark mode; no favicon; the page `<title>` is just "OpsPilot".

Kept as is (works, and the tests depend on it): the layout structure (form, stats, two columns
with stream and sources on the left and diagnosis on the right), every element id and
accessible label, the native `<dialog>` for evidence, the password-type API-key field held in
module memory only.
