#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_filter_female_heroes_tsv.py
--------------------------------
Z input TSV vyfiltruje řádky, kde sloupec speaker_name (lze změnit) obsahuje
jméno některé z hlavních ženských postav z Pathfinder: Wrath of the Righteous.

• Defaultní seznam (case-insensitive substring match):
  Seelah, Camellia, Wenduag, Ember, Nenio, Arueshalae, Queen Galfrey (Galfrey),
  Areelu Vorlesh (Areelu), Nocticula, Minagho, Anevia (Anevia Tirabade),
  Irabeth (Irabeth Tirabade), Terendelev, Yaniel, Nurah Dendiwhar (Nurah),
  Aivu, Hepzamirah, Shamira, Vellexia

• Lze přebít:
  --names "Seelah,Camellia,Arueshalae"  (čárkami oddělený seznam)
  --names-file path\to\names.txt        (jedno jméno na řádek)

Pozn.: Porovnání je substring přes malá písmena; pro „Queen Galfrey“ se tedy chytí i jen „Galfrey“.

Použití:
  python wotr_filter_female_heroes_tsv.py ^
    --in .\audit\female_needs_feminine.tsv ^
    --out .\audit\female_heroes_only.tsv ^
    --speaker-col speaker_name

Volitelné:
  --names "Seelah,Camellia,Arueshalae"
  --names-file .\my_female_names.txt
  --sep "\t"     (default tab)
"""

from __future__ import annotations
import argparse
import csv
from pathlib import Path
from typing import List, Set

DEFAULT_NAMES: List[str] = [
    # Parta – ženské companionship postavy
    "Seelah",
    "Camellia",
    "Wenduag",
    "Ember",
    "Nenio",
    "Arueshalae",
    "Aivu",          # (azata dráček – často mluví)
    # Klíčové NPC / antagonistky / vládkyně
    "Galfrey",       # Queen Galfrey (měj i “Queen Galfrey” ve zdrojích)
    "Areelu",        # Areelu Vorlesh
    "Areelu Vorlesh",
    "Nocticula",
    "Minagho",
    "Anevia",        # Anevia Tirabade
    "Irabeth",       # Irabeth Tirabade
    "Terendelev",
    "Yaniel",
    "Nurah",         # Nurah Dendiwhar
    "Nurah Dendiwhar",
    "Hepzamirah",
    "Shamira",
    "Vellexia",
]

def load_names(cli_csv: str | None, file_path: Path | None) -> List[str]:
    names: List[str] = []
    if cli_csv:
        names.extend([x.strip() for x in cli_csv.split(",") if x.strip()])
    if file_path:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)
    if not names:
        names = DEFAULT_NAMES[:]
    # normalizace: lower + ořez
    # (vnitřně budeme porovnávat přes lower().find())
    return sorted(set(n.strip() for n in names if n.strip()), key=str.casefold)

def row_matches(row: dict, speaker_col: str, needles_lower: List[str]) -> bool:
    val = (row.get(speaker_col) or "").strip()
    low = val.casefold()
    for n in needles_lower:
        if n in low:
            return True
    return False

def main():
    ap = argparse.ArgumentParser(description="Vyfiltruj ženské hrdinky podle speaker_name a ulož do nového TSV.")
    ap.add_argument("--in", dest="in_path", required=True, help="Vstupní TSV (např. female_needs_feminine.tsv)")
    ap.add_argument("--out", dest="out_path", required=True, help="Výstupní TSV s vyfiltrovanými řádky")
    ap.add_argument("--speaker-col", default="speaker_name", help="Název sloupce se jménem mluvčí (default: speaker_name)")
    ap.add_argument("--sep", default="\\t", help="Oddělovač sloupců; default TAB (\\t)")
    ap.add_argument("--names", default=None, help="Čárkami oddělený seznam jmen pro filtr")
    ap.add_argument("--names-file", type=str, default=None, help="Soubor se seznamem jmen (1 řádek = 1 jméno)")
    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)
    sep = ("\t" if args.sep == "\\t" else args.sep)

    names = load_names(args.names, Path(args.names_file) if args.names_file else None)
    needles = [n.casefold() for n in names]

    # Načti TSV
    with in_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=sep)
        fieldnames = reader.fieldnames or []
        if not fieldnames:
            raise SystemExit("Input TSV nemá header.")
        if args.speaker_col not in fieldnames:
            raise SystemExit(f"Ve vstupu chybí sloupec '{args.speaker_col}'. K dispozici: {fieldnames}")

        rows = list(reader)

    # Filtrování
    kept = [r for r in rows if row_matches(r, args.speaker_col, needles)]

    # Zápis
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter=sep, lineterminator="\n")
        w.writeheader()
        w.writerows(kept)

    print(f"[FILTER] names={len(names)} | input_rows={len(rows)} | kept={len(kept)}")
    print(f"[FILTER] → {out_path}")

if __name__ == "__main__":
    main()
