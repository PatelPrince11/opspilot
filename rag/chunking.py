"""Split a markdown document into token-bounded chunks.

The body becomes units: headings, whole code blocks, table lines and sentences. Units are
packed greedily up to `size` tokens. With `by_heading` (runbooks) a heading starts a new
chunk (consecutive headings stay together). The next chunk starts with the previous chunk's
trailing sentences, up to `overlap` tokens (never a code block or heading). A code block is
cut only if it alone exceeds the model's input limit.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
# A sentence ends after a word, not a number: "**3. Look at" is one list item.
SENTENCE = re.compile(r"(?<=[A-Za-z)\]`'\"%*][.!?])\s+(?=[A-Z0-9`*(\"])")
FENCE = "```"
MODEL_LIMIT = 510  # bge's 512 minus [CLS] and [SEP]


@dataclass
class Unit:
    text: str
    sep: str  # joins this unit to the one before it
    path: tuple[str, ...]
    kind: str = "text"  # text | code | heading
    tokens: int = 0


def units(body: str) -> list[Unit]:
    out: list[Unit] = []
    path: tuple[str, ...] = ()
    lines = body.split("\n")
    i, blank = 0, False

    def add(text: str, kind: str = "text", sep: str | None = None) -> None:
        nonlocal blank
        if sep is None:
            sep = "" if not out else "\n\n" if blank else "\n"
        out.append(Unit(text, sep, path, kind))
        blank = False

    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            blank, i = True, i + 1
        elif line.lstrip().startswith(FENCE):
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith(FENCE):
                j += 1
            add("\n".join(lines[i : j + 1]), "code")
            i = j + 1
        elif m := HEADING.match(line):
            path = path[: len(m[1]) - 1] + (m[2].strip(),)
            add(line, "heading")
            i += 1
        else:
            parts = [line] if line.lstrip().startswith("|") else SENTENCE.split(line)
            add(parts[0])
            for p in parts[1:]:
                add(p, sep=" ")
            i += 1
    return out


def split_big(u: Unit, size: int, count: Callable[[list[str]], list[int]]) -> list[Unit]:
    """A unit over `size` is split by lines (code) or words (prose); code that fits the
    model limit is kept whole."""
    if u.tokens <= size or (u.kind == "code" and u.tokens <= MODEL_LIMIT):
        return [u]
    joiner = "\n" if u.kind == "code" else " "
    pieces = u.text.split(joiner)
    out, cur, n = [], [], 0
    for piece, t in zip(pieces, count(pieces), strict=True):
        if cur and n + t > size:
            out.append(joiner.join(cur))
            cur, n = [], 0
        cur.append(piece)
        n += t
    out.append(joiner.join(cur))
    return [
        Unit(text, u.sep if k == 0 else joiner, u.path, u.kind, t)
        for k, (text, t) in enumerate(zip(out, count(out), strict=True))
    ]


def tail(cur: list[Unit], overlap: int) -> list[Unit]:
    keep, n = [], 0
    for u in reversed(cur):
        if u.kind != "text" or n + u.tokens > overlap:
            break
        keep.insert(0, u)
        n += u.tokens
    return keep


def chunk(
    body: str,
    count: Callable[[list[str]], list[int]],
    size: int,
    overlap: int,
    by_heading: bool,
) -> list[dict]:
    """[{content, token_count, heading_path}] in order."""
    us = units(body)
    for u, t in zip(us, count([u.text for u in us]), strict=True):
        u.tokens = t
    sections: list[list[Unit]] = []
    for u in us:
        # a heading opens a section unless the current one is headings only ("# T" + "## A")
        opens = by_heading and u.kind == "heading" and sections
        if not sections or (opens and any(x.kind != "heading" for x in sections[-1])):
            sections.append([])
        sections[-1].append(u)

    packed: list[tuple[list[Unit], int]] = []  # (units, how many lead units are overlap)
    for sec in sections:
        cur, lead = [], 0
        for u in (piece for v in sec for piece in split_big(v, size, count)):
            if len(cur) > lead and sum(x.tokens for x in cur) + u.tokens > size:
                carry = []
                while cur[-1].kind == "heading" and len(cur) - 1 > lead:
                    carry.insert(0, cur.pop())  # a heading never ends a chunk
                packed.append((cur, lead))
                cur = tail(cur, overlap) if not carry else []
                if sum(x.tokens for x in cur + carry) + u.tokens > size:
                    cur = []
                lead = len(cur)
                cur += carry
            cur.append(u)
        if len(cur) > lead:
            packed.append((cur, lead))

    out = []
    for us_, lead in packed:
        text = us_[0].text + "".join(u.sep + u.text for u in us_[1:])
        body_units = [u for u in us_[lead:] if u.kind != "heading"] or us_[-1:]
        out.append({"content": text, "heading_path": list(body_units[0].path)})
    for c, t in zip(out, count([c["content"] for c in out]), strict=True):
        c["token_count"] = t
    return out
