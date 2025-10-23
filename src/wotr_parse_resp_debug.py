#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import sys
from pathlib import Path

def _extract_candidate_blocks(full: str) -> list[str]:
    blocks = []

    # ```...``` code-fence (volitelně s "tsv")
    for m in re.finditer(r"```(?:tsv|text|markdown)?\s*(.*?)```", full, re.DOTALL | re.IGNORECASE):
        blocks.append(m.group(1))

    # BEGIN TSV ... END TSV
    m = re.search(r"BEGIN\s*TSV.*?\n(.*?)\nEND\s*TSV", full, re.DOTALL | re.IGNORECASE)
    if m:
        blocks.append(m.group(1))

    # <tsv>...</tsv>
    for m in re.finditer(r"<tsv>(.*?)</tsv>", full, re.DOTALL | re.IGNORECASE):
        blocks.append(m.group(1))

    # fallback: celý text
    blocks.append(full)
    return blocks

def parse_pairs(text: str) -> dict[str, str]:
    # normalize newlines + strip BOM
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text and text[0] == "\ufeff":
        text = text[1:]

    pairs = {}

    for blk in _extract_candidate_blocks(text):
        # multiline match: ^<spaces><digits>\t<translation>
        for m in re.finditer(r"(?m)^\s*(\d{1,10})\t(.*)$", blk):
            idx = m.group(1)
            tr  = m.group(2)
            pairs[idx] = tr
        if pairs:
            break

    return pairs

def main():
    if len(sys.argv) != 2:
        print("Použití: python wotr_parse_resp_debug.py <response.txt>")
        sys.exit(2)

    f = Path(sys.argv[1])
    if not f.exists():
        print(f"Soubor nenalezen: {f}")
        sys.exit(1)

    text = f.read_text(encoding="utf-8", errors="replace")
    pairs = parse_pairs(text)

    print(f"[DEBUG] Found {len(pairs)} TSV line(s)")
    # ukaž prvních ~15
    n = 0
    for k, v in pairs.items():
        print(f"{k}\t{v[:120].replace('\\n','\\\\n').replace('\\t','\\\\t')}{'...' if len(v)>120 else ''}")
        n += 1
        if n >= 15: 
            break

    if not pairs:
        print("\n[HINT] V odpovědi se nenašly řádky `idx<TAB>text`.")
        print("Zkontroluj, jestli model nevrací TSV v code fence (```), v blocích BEGIN/END TSV, nebo s nečekanými prefixy.")

if __name__ == "__main__":
    main()
