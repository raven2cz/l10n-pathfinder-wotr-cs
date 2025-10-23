#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_extract_and_fix_arrows.py
--------------------------------
Scan csCZ.json for bilingual segments with an "arrow" and produce:
  1) bad_segments.json  : [{"guid": "...", "original": "...", "left": "...", "right": "..."}...]
  2) fixed_segments.json: {"<GUID>": "<right-side only>", ...}

Optionally write a patched csCZ.json with fixes applied (--write-patched).

Arrow detection (by default):
  - ASCII arrow:  \s*->\s*
  - Unicode:      \s*→\s* and \s*⇒\s*
  ( \s* covers spaces and TABs, so " -> " i "\t->\t" oboje projde )

Behavior:
  - If multiple arrows occur, take the **rightmost** match and use its right-hand side.
  - Right side is lstrip()'d (leading whitespace removed), trailing whitespace kept as-is.
"""

from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from typing import Dict, List, Tuple

ARROW_PATTERNS = [
    r"\s*->\s*",
    r"\s*→\s*",
    r"\s*⇒\s*",
]

def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))

def write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def compile_arrow_regex(include_unicode: bool = True) -> re.Pattern:
    pats = ARROW_PATTERNS if include_unicode else ARROW_PATTERNS[:1]  # only '->'
    # join as alternatives, capture nothing
    rx = "(?:" + "|".join(pats) + ")"
    return re.compile(rx)

def split_on_last_arrow(text: str, arrow_re: re.Pattern) -> Tuple[str, str] | None:
    """
    Find the **last** arrow occurrence and split into (left, right).
    Returns None if no arrow is found.
    """
    last = None
    for m in arrow_re.finditer(text):
        last = m
    if not last:
        return None
    left = text[:last.start()]
    right = text[last.end():]
    return left, right

def main():
    ap = argparse.ArgumentParser(description="Extract and fix bilingual arrow segments from csCZ.json.")
    ap.add_argument("--cs-in", required=True, help="Path to csCZ.json (with 'strings' object)")
    ap.add_argument("--bad-out", required=True, help="Path to write bad_segments.json")
    ap.add_argument("--fixed-out", required=True, help="Path to write fixed_segments.json (GUID -> fixed text)")
    ap.add_argument("--write-patched", action="store_true", help="Also write csCZ.patched.json in the same folder as cs-in")
    ap.add_argument("--ascii-only", action="store_true", help="Detect only '->' (ignore Unicode arrows)")
    args = ap.parse_args()

    cs_in = Path(args.cs_in)
    bad_out = Path(args.bad_out)
    fixed_out = Path(args.fixed_out)

    data = read_json(cs_in)
    if "strings" not in data or not isinstance(data["strings"], dict):
        print("ERROR: csCZ.json must contain object 'strings'.", file=sys.stderr)
        sys.exit(2)

    strings: Dict[str, str] = data["strings"]
    arrow_re = compile_arrow_regex(include_unicode=(not args.ascii_only))

    bad_list: List[dict] = []
    fixed_map: Dict[str, str] = {}

    total = 0
    found = 0
    for guid, txt in strings.items():
        total += 1
        if not isinstance(txt, str) or not txt:
            continue
        res = split_on_last_arrow(txt, arrow_re)
        if not res:
            continue
        left, right = res
        found += 1

        # build outputs
        bad_list.append({
            "guid": guid,
            "original": txt,
            "left": left,
            "right": right,
        })

        fixed_map[guid] = right.lstrip()  # keep exact content on the right, but trim leading ws

    write_json(bad_out, bad_list)
    write_json(fixed_out, fixed_map)

    print(f"[ARROWS] scanned={total} found={found} → bad:{bad_out} fixed:{fixed_out}")

    if args.write_patched:
        patched = dict(data)  # shallow copy
        patched_strings = dict(strings)
        for guid, fx in fixed_map.items():
            patched_strings[guid] = fx
        patched["strings"] = patched_strings
        patched_path = cs_in.with_suffix(".patched.json")
        write_json(patched_path, patched)
        print(f"[PATCHED] written → {patched_path}")

if __name__ == "__main__":
    main()
