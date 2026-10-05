#!/usr/bin/env python3
"""Собирает kettell_16pf_c_youth.json из _16pf_c_youth_raw.txt (105 пунктов 16PF/C для молодёжи)."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "_16pf_c_youth_raw.txt"
OUT = ROOT / "kettell_16pf_c_youth.json"

# В боте — тот же порядок факторов; форма C (молодёжь): 15 блоков по 7 пунктов (105), фактор Q4 в этой форме не представлен.
FACTOR_BLOCK_ENDS = [
    (7, "A"),
    (14, "B"),
    (21, "C"),
    (28, "E"),
    (35, "F"),
    (42, "G"),
    (49, "H"),
    (56, "I"),
    (63, "L"),
    (70, "M"),
    (77, "N"),
    (84, "O"),
    (91, "Q1"),
    (98, "Q2"),
    (105, "Q3"),
]


def factor_for_item(num: int) -> str:
    for end, code in FACTOR_BLOCK_ENDS:
        if num <= end:
            return code
    return "Q3"


def parse_raw(text: str) -> list[dict]:
    lines = text.splitlines()
    items: list[dict] = []
    cur_lines: list[str] = []

    def flush():
        nonlocal cur_lines
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
            if re.match(r"^[аa]\)\s*", line, re.I):
                opts[1] = re.sub(r"^[аa]\)\s*", "", line, flags=re.I).strip()
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
    if len(items) != 105:
        raise ValueError(f"Expected 105 items, got {len(items)}")
    if items[0]["n"] != 1 or items[-1]["n"] != 105:
        raise ValueError("Item numbering gap")
    return items


def build_questions(items: list[dict]) -> list[dict]:
    return [
        {
            "q": it["q"],
            "options": {
                "1": [it["opts"][1], {factor_for_item(it["n"]): 1.0}],
                "2": [it["opts"][2], {factor_for_item(it["n"]): 0.5}],
                "3": [it["opts"][3], {}],
            },
        }
        for it in items
    ]


def main():
    items = parse_raw(RAW.read_text(encoding="utf-8"))
    qs = build_questions(items)
    OUT.write_text(json.dumps(qs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(qs)} items to {OUT}")


if __name__ == "__main__":
    main()
