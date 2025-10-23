#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wotr_gender_split_female_rows.py
--------------------------------
Z “wotr_dialog_speakers.tsv” (sloupce mj. `key`, `speaker_gender`, `speaker_name`) a csCZ.json
vybere **jen řádky se speaker_gender=Female**, porovná originální český text s bezpečně
feminizovanou variantou (pomocí wotr_gender_service.CzechGenderService) a rozdělí je do dvou TSV:

  1) --out-ok    (default: audit/female_feminine_ok.tsv)
     Řádky, kde NENÍ třeba nic měnit (už ženský rod nebo gender-neutrální).
  2) --out-need  (default: audit/female_needs_feminine.tsv)
     Řádky, kde by došlo ke ZMĚNĚ (tzn. kandidat != původní).

Oba TSV mají sloupce:
  key, speaker_gender, speaker_name, cs_text, fem_preview, changed

Použití:
  python wotr_gender_split_female_rows.py ^
    --speakers out_wotr/audit/wotr_dialog_speakers.tsv ^
    --cs out_wotr/csCZ.json ^
    --out-ok out_wotr/audit/female_feminine_ok.tsv ^
    --out-need out_wotr/audit/female_needs_feminine.tsv ^
    --limit 500

Pozn:
- Pokud zadáš --limit, vezmeme prvních N řádků (pro náhled). Přidej --shuffle pro náhodný vzorek.
- Vyžaduje `stanza` a stažený model `cs`:  pip install stanza  ;  python -c "import stanza; stanza.download('cs')"
"""

from __future__ import annotations
from pathlib import Path
import argparse, csv, json, random, sys
from datetime import datetime

# import služby (soubor musí být po boku)
from wotr_gender_service import CzechGenderService

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def read_speakers_tsv(path: Path):
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter="\t")
        rows = list(r)
        return rows

def read_cs_json(path: Path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "strings" not in data or not isinstance(data["strings"], dict):
        raise ValueError("csCZ.json nemá očekávanou strukturu { 'strings': { GUID: 'text', ... } }")
    return data["strings"]

def write_tsv(path: Path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, delimiter="\t", fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speakers", required=True, help="wotr_dialog_speakers.tsv")
    ap.add_argument("--cs", required=True, help="csCZ.json")
    ap.add_argument("--out-ok", default="audit/female_feminine_ok.tsv")
    ap.add_argument("--out-need", default="audit/female_needs_feminine.tsv")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--shuffle", action="store_true")
    args = ap.parse_args()

    speakers = read_speakers_tsv(Path(args.speakers))
    strings = read_cs_json(Path(args.cs))

    females = [r for r in speakers if (r.get("speaker_gender","").strip().lower() == "female")]
    if args.shuffle:
        random.shuffle(females)
    if args.limit is not None:
        females = females[:max(0, int(args.limit))]
    log(f"Female rows considered: {len(females)}")

    svc = CzechGenderService(cpu=True)

    ok_rows, need_rows = [], []
    for r in females:
        guid = r.get("key","").strip()
        cs = strings.get(guid, "")
        if not guid or not cs:
            continue
        fem = svc.rewrite_to_feminine(cs)
        changed = (fem != cs)
        row_out = {
            "key": guid,
            "speaker_gender": r.get("speaker_gender",""),
            "speaker_name": r.get("speaker_name",""),
            "cs_text": cs,
            "fem_preview": fem,
            "changed": "yes" if changed else "no"
        }
        (need_rows if changed else ok_rows).append(row_out)

    fieldnames = ["key","speaker_gender","speaker_name","cs_text","fem_preview","changed"]
    write_tsv(Path(args.out_ok), ok_rows, fieldnames)
    write_tsv(Path(args.out_need), need_rows, fieldnames)
    log(f"Wrote OK:   {args.out_ok} ({len(ok_rows)})")
    log(f"Wrote NEED: {args.out_need} ({len(need_rows)})")

if __name__ == "__main__":
    main()
