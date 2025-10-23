#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_unescape_tsv_column.py
---------------------------
Účel:
- Vezme vstupní TSV a v jednom vybraném sloupci nahradí textové sekvence "\n", "\r\n", "\t"
  za skutečné znaky nového řádku a tabulátoru.
- Výsledek uloží do nového sloupce (default: translate_real), případně přepíše původní.
- Idempotentní: pokud skript pustíš opakovaně, cílový sloupec jen přegeneruje (nepřidává další kopie).

Použití (příklady):
1) Z `translate` udělej `translate_real` do nového souboru:
   python wotr_unescape_tsv_column.py -i in.tsv -o out.tsv --col-in translate --col-out translate_real

2) Přepiš sloupec `translate` na místě:
   python wotr_unescape_tsv_column.py -i in.tsv --inplace --col-in translate --col-out translate

Poznámka:
- Čte a zapisuje UTF-8 (bez BOM). Zachová pořadí sloupců.
- Unescape je uměrně konzervativní: nahrazuje jen \\r\\n, \\n, \\t (v tomto pořadí).
"""

import argparse, csv, sys
from pathlib import Path

def unescape_basic(s: str) -> str:
    if not s:
        return s
    # pořadí důležité: nejdřív \r\n → \n, pak samostatné \n, a nakonec \t
    s = s.replace("\\r\\n", "\n")
    s = s.replace("\\n", "\n")
    s = s.replace("\\t", "\t")
    return s

def main():
    ap = argparse.ArgumentParser(description="Unescape \\n/\\r\\n/\\t v zadaném sloupci TSV.")
    ap.add_argument("-i", "--in", dest="in_path", required=True, help="Vstupní TSV")
    ap.add_argument("-o", "--out", dest="out_path", default=None, help="Výstupní TSV (pokud ne --inplace)")
    ap.add_argument("--col-in", default="translate", help="Zdrojový sloupec k unescape (default: translate)")
    ap.add_argument("--col-out", default="translate_real", help="Cílový sloupec (default: translate_real; může být stejné jako --col-in)")
    ap.add_argument("--inplace", action="store_true", help="Zapis do stejného souboru (bez -o)")
    args = ap.parse_args()

    in_path = Path(args.in_path)
    if not in_path.exists():
        print(f"ERROR: Nenalezen vstup: {in_path}", file=sys.stderr)
        sys.exit(1)

    if args.inplace and args.out_path:
        print("ERROR: Nelze použít zároveň --inplace a --out.", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.out_path) if args.out_path else (
        in_path if args.inplace else in_path.with_suffix(".unescaped.tsv")
    )

    # Načti TSV
    with open(in_path, "r", encoding="utf-8", newline="") as fin:
        rdr = csv.DictReader(fin, delimiter="\t")
        fieldnames = list(rdr.fieldnames) if rdr.fieldnames else []
        if not fieldnames:
            print("ERROR: Prázdný nebo neplatný TSV (bez hlavičky).", file=sys.stderr)
            sys.exit(1)
        if args.col_in not in fieldnames:
            print(f"ERROR: Vstupní sloupec '{args.col_in}' není v TSV. K dispozici: {fieldnames}", file=sys.stderr)
            sys.exit(1)

        # Přidej cílový sloupec, pokud chybí (a není to overwrite stejného)
        if args.col_out not in fieldnames:
            fieldnames.append(args.col_out)

        rows = []
        for row in rdr:
            src = row.get(args.col_in, "")
            row[args.col_out] = unescape_basic(src)
            rows.append(row)

    # Zapiš TSV
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as fout:
        w = csv.DictWriter(fout, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    print(f"[OK] Zapsáno: {out_path} | řádků: {len(rows)} | sloupec '{args.col_in}' → '{args.col_out}'")

if __name__ == "__main__":
    main()
