#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_extract_multiline_suspects.py
----------------------------------
Z `audit/suspect.tsv` robustně vytáhne záznamy, kde EN Source obsahuje nový řádek.
Zapíše bezpečný TSV pro GPT sync apply: sloupce
  idx<TAB>source_escaped
kde jsou v EN zdroji nahrazeny:
  \t → \\t   a   \n → \\n
Tak se každý záznam vejde do JEDNOHO řádku a nic se „nerozbije“.
"""

from __future__ import annotations
import re
import argparse
from pathlib import Path

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")

def write_text(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")

REC_START_RX = re.compile(r"(?m)^(\d+)\t")  # začátek záznamu (idx na začátku řádku)

def parse_records(raw: str):
    """Rozřeže suspect.tsv na záznamy i s víceradkovým polem Source."""
    recs = []
    starts = list(REC_START_RX.finditer(raw))
    for i, m in enumerate(starts):
        beg = m.start()
        end = starts[i+1].start() if i+1 < len(starts) else len(raw)
        chunk = raw[beg:end].rstrip("\n")
        # Očekávané 4 pole: idx \t src \t tr \t reason
        # poslední 2 pole (tr, reason) nepoužívají \t → můžeme kotvit zprava
        mm = re.match(r"^(\d+)\t(.*)\t([^\t]*)\t([^\t]*)\s*$", chunk, flags=re.DOTALL)
        if not mm:
            # nedekódovatelný záznam – přeskočit
            continue
        idx, src, tr, why = mm.groups()
        recs.append((idx, src, tr, why))
    return recs

def esc(s: str) -> str:
    s = s.replace("\\", "\\\\")         # nejdřív backslash
    s = s.replace("\t", "\\t")
    s = s.replace("\r\n", "\n")
    s = s.replace("\n", "\\n")
    return s

def main():
    ap = argparse.ArgumentParser(description="Vyextrahuje multiline Source ze suspect.tsv do bezpečného TSV pro GPT apply.")
    ap.add_argument("-i", "--input", required=True, help="cesta k audit/suspect.tsv")
    ap.add_argument("-o", "--output", required=True, help="výstupní TSV (např. out_wotr/fix/multiline_src.tsv)")
    ap.add_argument("--with-translation", action="store_true", help="přidej i sloupec tr_escaped (informativně)")
    args = ap.parse_args()

    raw = read_text(Path(args.input))
    recs = parse_records(raw)
    pick = [(idx, src, tr) for idx, src, tr, _ in recs if "\n" in src or "\r\n" in src]

    lines = []
    header = "idx\tsource_escaped" + ("\ttr_escaped" if args.with_translation else "")
    lines.append(header)
    for idx, src, tr in pick:
        se = esc(src)
        if args.with_translation:
            te = esc(tr or "")
            lines.append(f"{idx}\t{se}\t{te}")
        else:
            lines.append(f"{idx}\t{se}")

    write_text(Path(args.output), "\n".join(lines) + ("\n" if lines else ""))
    print(f"[OK] Zapsáno {len(pick)} řádků → {args.output}")

if __name__ == "__main__":
    main()
