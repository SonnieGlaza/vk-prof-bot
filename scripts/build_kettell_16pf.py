#!/usr/bin/env python3
"""Собирает kettell_questions.json из _16pf_raw.txt (187 пунктов 16PF для взрослых)."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "_16pf_raw.txt"
OUT = ROOT / "kettell_questions.json"

# 16 первичных факторов Cattell (порядок блоков пунктов формы A)
FACTOR_BLOCK_ENDS = [
    (12, "A"),
    (24, "B"),
    (36, "C"),
    (48, "E"),
    (60, "F"),
    (72, "G"),
    (84, "H"),
    (96, "I"),
    (108, "L"),
    (120, "M"),
    (132, "N"),
    (143, "O"),
    (154, "Q1"),
    (165, "Q2"),
    (176, "Q3"),
    (187, "Q4"),
]


def factor_for_item(num: int) -> str:
    for end, code in FACTOR_BLOCK_ENDS:
        if num <= end:
            return code
    return "Q4"


def parse_raw(text: str) -> list[dict]:
    lines = text.splitlines()
    items: list[dict] = []
    cur_lines: list[str] = []
    qnum = 0

    def flush():
        nonlocal cur_lines, qnum
        if not cur_lines:
            return
        m = re.match(r"^(\d+)\.\s*(.*)$", cur_lines[0].strip())
        if not m:
            raise ValueError(f"Bad question line: {cur_lines[0]!r}")
        n = int(m.group(1))
        qtext = m.group(2).strip()
        opts: dict[int, str] = {}
        for line in cur_lines[1:]:
            line = line.strip()
            om = re.match(r"^[аa]\)\s*(.*)$", line, re.I)
            if om:
                opts[1] = om.group(1).strip()
            elif re.match(r"^[бb]\)\s*", line, re.I):
                opts[2] = re.sub(r"^[бb]\)\s*", "", line, flags=re.I).strip()
            elif re.match(r"^[вv]\)\s*", line, re.I):
                opts[3] = re.sub(r"^[вv]\)\s*", "", line, flags=re.I).strip()
        if set(opts.keys()) != {1, 2, 3}:
            raise ValueError(f"Item {n}: need 3 options, got {opts}")
        items.append({"n": n, "q": f"{n}. {qtext}", "opts": opts})
        cur_lines = []

    for line in lines:
        if re.match(r"^\d+\.\s+", line.strip()):
            flush()
        cur_lines.append(line.rstrip())
    flush()
    items.sort(key=lambda x: x["n"])
    if len(items) != 187:
        raise ValueError(f"Expected 187 items, got {len(items)}")
    if items[0]["n"] != 1 or items[-1]["n"] != 187:
        raise ValueError("Item numbering gap")
    return items


def build_questions(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        n = it["n"]
        fac = factor_for_item(n)
        out.append(
            {
                "q": it["q"],
                "options": {
                    "1": [it["opts"][1], {fac: 1.0}],
                    "2": [it["opts"][2], {fac: 0.5}],
                    "3": [it["opts"][3], {}],
                },
            }
        )
    return out


def main():
    items = parse_raw(RAW.read_text(encoding="utf-8"))
    qs = build_questions(items)
    OUT.write_text(json.dumps(qs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(qs)} items to {OUT}")


if __name__ == "__main__":
    main()
