#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wotr_gender_apply_female.py
---------------------------
Aplikuje bezpečný převod do feminina na **Female** repliky v csCZ.json.

Dva režimy:
  A) Z ohodnocení (doporučeno):  --from-tsv audit/female_needs_feminine.tsv
     -> upraví jen GUIDy z tohoto TSV (sloupce: key, fem_preview/cs_text).

  B) Bez TSV:  --speakers wotr_dialog_speakers.tsv
     -> sám najde Female řádky, z nich vybere ty, kde by došlo ke změně,
        a ty upraví.

Vždy vytvoří:
  --out-json csCZ-patched.json             (přepsané texty)
Volitelně:
  --review-tsv audit/female_applied.tsv    (před/po + changed=yes)

Použití (z TSV z předchozího kroku):
  python wotr_gender_apply_female.py ^
    --cs out_wotr/csCZ.json ^
    --from-tsv out_wotr/audit/female_needs_feminine.tsv ^
    --out-json out_wotr/csCZ-patched.json ^
    --review-tsv out_wotr/audit/female_applied.tsv

Použití (automaticky přes speakers):
  python wotr_gender_apply_female.py ^
    --cs out_wotr/csCZ.json ^
    --speakers out_wotr/audit/wotr_dialog_speakers.tsv ^
    --out-json out_wotr/csCZ-patched.json ^
    --review-tsv out_wotr/audit/female_applied.tsv
"""

from __future__ import annotations
from pathlib import Path
import argparse, csv, json
from datetime import datetime

from wotr_gender_service import CzechGenderService

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def read_cs_json(path: Path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "strings" not in data or not isinstance(data["strings"], dict):
        raise ValueError("csCZ.json nemá očekávanou strukturu { 'strings': { GUID: 'text', ... } }")
    return data

def read_tsv(path: Path):
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter="\t")
        return list(r)

def write_tsv(path: Path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, delimiter="\t", fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

def read_speakers_tsv(path: Path):
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter="\t")
        return list(r)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cs", required=True, help="csCZ.json (input)")
    ap.add_argument("--out-json", required=True, help="kam zapsat csCZ-patched.json")
    ap.add_argument("--from-tsv", help="TSV z wotr_gender_split_female_rows.py (female_needs_feminine.tsv)")
    ap.add_argument("--speakers", help="wotr_dialog_speakers.tsv (alternativně k --from-tsv)")
    ap.add_argument("--review-tsv", help="volitelně TSV s přehledem změn")
    args = ap.parse_args()

    if not args.from_tsv and not args.speakers:
        ap.error("Zadej buď --from-tsv (doporučeno), nebo --speakers (automatický výběr).")

    cs_all = read_cs_json(Path(args.cs))   # celé JSON {strings: {...}}
    strings = cs_all["strings"]

    svc = CzechGenderService(cpu=True)
    to_apply = []  # list of dict: {"key","orig","new","speaker_gender","speaker_name"}

    if args.from_tsv:
        rows = read_tsv(Path(args.from_tsv))
        for r in rows:
            guid = (r.get("key") or "").strip()
            if not guid:
                continue
            orig = strings.get(guid, "")
            if not orig:
                continue
            # použijeme lokální službu nad orig (ne fem_preview – aby to bylo znovu deterministické)
            new = svc.rewrite_to_feminine(orig)
            if new != orig:
                to_apply.append({
                    "key": guid,
                    "orig": orig,
                    "new": new,
                    "speaker_gender": r.get("speaker_gender",""),
                    "speaker_name": r.get("speaker_name","")
                })
    else:
        # automat: vezmeme Female ze speakers a z nich jen ty, kde by došlo ke změně
        speakers = read_speakers_tsv(Path(args.speakers))
        females = [r for r in speakers if (r.get("speaker_gender","").strip().lower() == "female")]
        for r in females:
            guid = (r.get("key") or "").strip()
            if not guid:
                continue
            orig = strings.get(guid, "")
            if not orig:
                continue
            new = svc.rewrite_to_feminine(orig)
            if new != orig:
                to_apply.append({
                    "key": guid,
                    "orig": orig,
                    "new": new,
                    "speaker_gender": r.get("speaker_gender",""),
                    "speaker_name": r.get("speaker_name","")
                })

    log(f"Planned changes: {len(to_apply)}")
    # aplikace do JSON
    for item in to_apply:
        strings[item["key"]] = item["new"]

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cs_all, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Wrote patched JSON: {out_path}")

    # review TSV
    if args.review_tsv:
        fieldnames = ["key","speaker_gender","speaker_name","orig","new","changed"]
        rows = []
        for it in to_apply:
            rows.append({
                "key": it["key"],
                "speaker_gender": it["speaker_gender"],
                "speaker_name": it["speaker_name"],
                "orig": it["orig"],
                "new": it["new"],
                "changed": "yes"
            })
        write_tsv(Path(args.review_tsv), rows, fieldnames)
        log(f"Wrote review TSV: {args.review_tsv}")

if __name__ == "__main__":
    main()
