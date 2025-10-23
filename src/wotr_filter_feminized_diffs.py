#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_filter_feminized_diffs.py
------------------------------
Vezme vstupní TSV (např. female_feminized_changed.tsv), odstraní sloupec `speaker_gender`
a vyhodí všechny řádky, kde jsou `cs_text` a `cs_text_female` shodné (defaultně porovnává přesně,
lze zapnout prosté ořezání okrajových mezer).

Použití (PowerShell):
  python wotr_filter_feminized_diffs.py `
    --in .\audit\female_feminized_changed.tsv `
    --out .\audit\female_feminized_changed.filtered.tsv

Volby:
  --drop-col          název sloupce k odstranění (default: speaker_gender)
  --left-col          původní text (default: cs_text)
  --right-col         upravený text (default: cs_text_female)
  --strip-compare     porovnávat po ořezání okrajových mezer (default: False)
"""

from __future__ import annotations
import csv
import argparse
from pathlib import Path

def log(msg: str) -> None:
    print(msg, flush=True)

def main():
    ap = argparse.ArgumentParser(description="Odstraní shodné řádky a sloupec speaker_gender z TSV.")
    ap.add_argument("--in", dest="in_path", required=True, help="Vstupní TSV (např. female_feminized_changed.tsv)")
    ap.add_argument("--out", dest="out_path", required=True, help="Výstupní TSV (filtrované)")
    ap.add_argument("--drop-col", default="speaker_gender", help="Sloupec k odstranění (default: speaker_gender)")
    ap.add_argument("--left-col", default="cs_text", help="Levá strana porovnání (default: cs_text)")
    ap.add_argument("--right-col", default="cs_text_female", help="Pravá strana porovnání (default: cs_text_female)")
    ap.add_argument("--strip-compare", action="store_true", help="Porovnávat po strip() (okrajové mezery).")

    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)

    with in_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = reader.fieldnames or []
        if not fieldnames:
            raise SystemExit("Vstupní TSV nemá header.")
        for col in (args.left_col, args.right_col):
            if col not in fieldnames:
                raise SystemExit(f"Chybí sloupec '{col}'. K dispozici: {fieldnames}")

        # Připrav výstupní header (odstranit drop-col, pokud existuje)
        out_fields = [c for c in fieldnames if c != args.drop_col]

        rows_in = 0
        rows_out = 0
        dropped_equal = 0

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="") as fo:
            writer = csv.DictWriter(fo, fieldnames=out_fields, delimiter="\t", lineterminator="\n")
            writer.writeheader()

            for row in reader:
                rows_in += 1
                left = row.get(args.left_col, "")
                right = row.get(args.right_col, "")
                if args.strip_compare:
                    left_cmp = left.strip()
                    right_cmp = right.strip()
                else:
                    left_cmp = left
                    right_cmp = right

                # Pokud stejné, řádek vynecháme
                if left_cmp == right_cmp:
                    dropped_equal += 1
                    continue

                # Zapiš bez drop-col
                if args.drop_col in row:
                    row = {k: v for k, v in row.items() if k != args.drop_col}
                writer.writerow(row)
                rows_out += 1

    log(f"[DONE] input_rows={rows_in} | written={rows_out} | dropped_equal={dropped_equal}")
    log(f"[OUT] → {out_path}")

if __name__ == "__main__":
    main()
