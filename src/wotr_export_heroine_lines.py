#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_export_heroine_lines.py
----------------------------
Z `wotr_dialog_speakers.tsv` vybere repliky mluvčích, jejichž `speaker_name`
obsahuje některé z (case-insensitive) jmen: seelah, camellia, arueshalae, nenio,
ember, wenduag, galfrey, delamere, aivu, irabeth, anevia, terendelev, yaniel,
areelu, minagho, hepzamirah, jerribeth, nocticula, shamira, vellexia, zanedra,
jeslyn, nurah.

`key` je GUID (NENÍ to idx). K GUID dohledá `cs_text` v `csCZ.json` (pole "strings").

Výstup: TSV se sloupci:
    key<TAB>speaker_gender<TAB>speaker_name<TAB>cs_text

Poznámka:
- Řádky bez textu (`cs_text` neexistuje nebo je prázdný/jen whitespace) se přeskočí.

Použití:
    python wotr_export_heroine_lines.py ^
      --speakers .\audit\wotr_dialog_speakers.tsv ^
      --cs .\out_wotr\csCZ.json ^
      --out .\audit\female_heroes_only_filtered.tsv

Volitelné:
    --names-file heroes.txt   # 1 jméno na řádek, přepíše vestavěný seznam
    --dedupe-by-key           # nechá jen první výskyt daného GUID
"""

from __future__ import annotations
import argparse, csv, json, sys
from pathlib import Path
from typing import List, Set, Dict

TSV = "\t"

DEFAULT_NAMES = [
    "seelah", "camellia", "arueshalae", "nenio", "ember", "wenduag", "galfrey",
    "delamere", "aivu", "irabeth", "anevia", "terendelev", "yaniel", "areelu",
    "minagho", "hepzamirah", "jerribeth", "nocticula", "shamira", "vellexia",
    "zanedra", "jeslyn", "nurah",
]

def load_names(names_file: Path | None) -> List[str]:
    if not names_file:
        return DEFAULT_NAMES[:]
    txt = names_file.read_text(encoding="utf-8")
    out = []
    for line in txt.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s.lower())
    return out or DEFAULT_NAMES[:]

def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: reading JSON {p}: {e}", file=sys.stderr)
        sys.exit(2)

def sanitize_tsv_cell(s: str) -> str:
    s = s or ""
    return s.replace("\t", "\\t").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")

def main():
    ap = argparse.ArgumentParser(description="Export vybraných hrdinek do TSV (GUID → cs_text).")
    ap.add_argument("--speakers", required=True, help="wotr_dialog_speakers.tsv")
    ap.add_argument("--cs", required=True, help="csCZ.json (obsahuje 'strings')")
    ap.add_argument("--out", required=True, help="výstupní TSV")
    ap.add_argument("--names-file", default=None, help="volitelně soubor se jmény (1/řádek), přepíše default seznam")
    ap.add_argument("--dedupe-by-key", action="store_true", help="ponechat jen první výskyt GUID (key)")
    args = ap.parse_args()

    speakers_path = Path(args.speakers)
    cs_path = Path(args.cs)
    out_path = Path(args.out)

    # Načti CZ strings
    cs = read_json(cs_path)
    if "strings" not in cs or not isinstance(cs["strings"], dict):
        print("ERROR: csCZ.json postrádá objekt 'strings'.", file=sys.stderr)
        sys.exit(2)
    cz_strings: Dict[str, str] = cs["strings"]

    # Jména (case-insensitive substring match)
    names_file = Path(args.names_file) if args.names_file else None
    needles = [n.lower() for n in load_names(names_file)]

    # Čti speakers TSV
    with speakers_path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=TSV)
        if not r.fieldnames:
            print("ERROR: speakers TSV nemá hlavičku.", file=sys.stderr)
            sys.exit(2)

        required = {"key", "speaker_gender", "speaker_name"}
        missing = [c for c in required if c not in r.fieldnames]
        if missing:
            print(f"ERROR: speakers TSV postrádá sloupce: {missing}", file=sys.stderr)
            sys.exit(2)

        rows = list(r)

    # Filtr
    out_rows = []
    seen: Set[str] = set()
    scanned = 0
    matched = 0
    skipped_empty = 0

    for row in rows:
        scanned += 1
        key = (row.get("key") or "").strip()
        speaker = (row.get("speaker_name") or "").strip()
        gender = (row.get("speaker_gender") or "").strip()
        if not key or not speaker:
            continue

        sp_low = speaker.lower()
        if not any(n in sp_low for n in needles):
            continue

        if args.dedupe_by_key and key in seen:
            continue
        seen.add(key)

        cs_text_raw = cz_strings.get(key, "")
        if (cs_text_raw or "").strip() == "":
            skipped_empty += 1
            continue

        matched += 1
        out_rows.append({
            "key": key,
            "speaker_gender": gender,
            "speaker_name": speaker,
            "cs_text": cs_text_raw,  # zachováme původní (nejen ořezaný) text
        })

    # Zápis TSV
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as g:
        fieldnames = ["key", "speaker_gender", "speaker_name", "cs_text"]
        w = csv.DictWriter(g, fieldnames=fieldnames, delimiter=TSV, lineterminator="\n")
        w.writeheader()
        for r in out_rows:
            r2 = dict(r)
            r2["cs_text"] = sanitize_tsv_cell(r2.get("cs_text", ""))
            r2["speaker_name"] = sanitize_tsv_cell(r2.get("speaker_name", ""))
            r2["speaker_gender"] = sanitize_tsv_cell(r2.get("speaker_gender", ""))
            w.writerow(r2)

    print(f"[OK] scanned={scanned} | matched_names={matched + skipped_empty} | "
          f"empty_or_missing_cs_text={skipped_empty} | exported={len(out_rows)} → {out_path}")

if __name__ == "__main__":
    main()
