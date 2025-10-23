#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wotr_apply_female_by_speaker.py
================================

Co dělá
-------
1) Načte TSV `wotr_dialog_speakers.tsv` (sloupce: key, type, speaker_gender, speaker_name, ...).
2) Vyfiltruje řádky se `speaker_gender == Female` (case-insensitive).
3) Načte český překlad `csCZ.json` (očekává mapu GUID→text buď v rootu, nebo pod klíčem "strings").
4) Podle GUID (sloupec `key`) najde odpovídající český text a přepíše jej do feminina
   pomocí služby `wotr_gender_service.CzechGenderService`.
5) Vytvoří TSV pro kontrolu: guid, speaker_name, orig_cs, fem_cs, changed.
6) (Volitelně) zapíše patchnutý JSON `csCZ-patched.json`.

Použití – příklady
------------------
# A) Jen kontrolní vzorek prvních 200 replik (bez patchování JSON)
python wotr_apply_female_by_speaker.py ^
  --speakers audit\\wotr_dialog_speakers.tsv ^
  --cs-json out_wotr\\csCZ.json ^
  --out-tsv out_wotr\\audit\\female_preview.tsv ^
  --limit 200

# B) Náhodných 300 replik (seed 42), vygenerovat TSV; JSON zatím nepatchovat
python wotr_apply_female_by_speaker.py ^
  --speakers audit\\wotr_dialog_speakers.tsv ^
  --cs-json out_wotr\\csCZ.json ^
  --out-tsv out_wotr\\audit\\female_preview_sample.tsv ^
  --limit 300 --random --seed 42

# C) Zpracovat všechna Female, vyrobit TSV a patchnout JSON
python wotr_apply_female_by_speaker.py ^
  --speakers audit\\wotr_dialog_speakers.tsv ^
  --cs-json out_wotr\\csCZ.json ^
  --out-tsv out_wotr\\audit\\female_full_preview.tsv ^
  --out-json out_wotr\\csCZ-patched.json ^
  --apply --apply-all

Poznámky
--------
- `--apply` bez `--apply-all` aplikuje jen na vybraný vzorek (podle --limit / --random).
- Pokud `--limit` neudáš, vezme se všechno (ekvivalent `--apply-all` pro seznam female GUID).
"""

from __future__ import annotations
import argparse
import csv
import json
import random
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# import služby
from wotr_gender_service import CzechGenderService

def log(msg: str) -> None:
    from datetime import datetime
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def read_speakers_tsv(path: Path) -> List[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f, delimiter="\t")
        rows = list(rd)
    # kontrola sloupců
    need = {"key", "speaker_gender"}
    miss = [c for c in need if c not in (rd.fieldnames or [])]
    if miss:
        raise SystemExit(f"[ERR] {path} chybí sloupce: {miss}. Máš: {rd.fieldnames}")
    return rows

def load_cs_json(cs_path: Path) -> Tuple[dict, Dict[str, str], str]:
    """
    Vrátí: (root_json_obj, strings_map, mode)
      - mode = "root"  (root je rovnou GUID→text)
      - mode = "strings" (root["strings"] je GUID→text)
    """
    root = json.loads(cs_path.read_text(encoding="utf-8"))
    if isinstance(root, dict) and "strings" in root and isinstance(root["strings"], dict):
        return root, root["strings"], "strings"
    if isinstance(root, dict):
        # zkusit, že root sám je GUID→text
        sample_vals = list(root.values())[:3]
        if all(isinstance(v, str) for v in sample_vals):
            return root, root, "root"
    raise SystemExit("[ERR] csCZ.json nemá rozpoznatelný formát (ani 'strings', ani plochá mapa GUID→text).")

def write_json_safely(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def main():
    ap = argparse.ArgumentParser(description="Přepiš repliky Female mluvčích do feminina + náhled TSV, volitelně patch JSON.")
    ap.add_argument("--speakers", required=True, help="TSV s kolonkami: key, speaker_gender, ...")
    ap.add_argument("--cs-json", required=True, help="Původní csCZ.json")
    ap.add_argument("--out-tsv", required=True, help="Výstupní kontrolní TSV (orig + fem)")
    ap.add_argument("--out-json", default=None, help="Cílový JSON s patchem (např. out_wotr/csCZ-patched.json)")
    ap.add_argument("--limit", type=int, default=0, help="Kolik řádků z Female vzít (0 = všechno)")
    ap.add_argument("--random", action="store_true", help="Místo prvních N vybrat náhodných N")
    ap.add_argument("--seed", type=int, default=42, help="Seed pro --random")
    ap.add_argument("--apply", action="store_true", help="Zapsat patchnutý JSON (viz --out-json)")
    ap.add_argument("--apply-all", action="store_true", help="Při --apply aplikovat na všechny Female GUID bez ohledu na --limit")
    ap.add_argument("--cpu", action="store_true", help="Vynutit CPU (default). GPU by šlo vypnout tím, že tento přepínač nedáš.")
    args = ap.parse_args()

    speakers_path = Path(args.speakers)
    cs_path = Path(args.cs_json)
    out_tsv = Path(args.out_tsv)
    out_json = Path(args.out_json) if args.out_json else None

    log("Načítám speakers TSV…")
    rows = read_speakers_tsv(speakers_path)

    # Female filtrace (case-insensitive)
    female_rows = [r for r in rows if str(r.get("speaker_gender", "")).strip().lower() == "female"]
    log(f"Řádků Female v TSV: {len(female_rows):,}")

    # Deduplikace GUIDů (key)
    guid_to_row = {}
    for r in female_rows:
        k = str(r.get("key", "")).strip()
        if k and k not in guid_to_row:
            guid_to_row[k] = r
    female_guids = list(guid_to_row.keys())
    log(f"Unikátních Female GUID: {len(female_guids):,}")

    if args.limit and args.limit > 0 and args.limit < len(female_guids):
        if args.random:
            random.seed(args.seed)
            sampled = random.sample(female_guids, args.limit)
        else:
            sampled = female_guids[:args.limit]
        selected_guids = sampled
        log(f"Vybráno {len(selected_guids)} GUID (limit={args.limit}, random={bool(args.random)})")
    else:
        selected_guids = female_guids
        log(f"Vybráno {len(selected_guids)} GUID (vše)")

    log("Načítám csCZ.json…")
    root_obj, strings_map, mode = load_cs_json(cs_path)
    log(f"Formát csCZ.json: {mode}")

    # Připrav službu
    log("Inicializuji gender službu… (Stanza cs)")
    svc = CzechGenderService(cpu=True if args.cpu else True)

    # Build preview TSV
    out_rows: List[dict] = []
    changed_count = 0
    missing_in_json = 0

    for guid in selected_guids:
        speaker_name = guid_to_row[guid].get("speaker_name", "")
        orig = strings_map.get(guid, None)
        if orig is None:
            # GUID není v JSONu
            missing_in_json += 1
            continue
        fem = svc.rewrite_to_feminine(orig) if orig else orig
        changed = (fem != orig)
        if changed:
            changed_count += 1
        out_rows.append({
            "guid": guid,
            "speaker_name": speaker_name,
            "orig_cs": orig,
            "fem_cs": fem,
            "changed": "yes" if changed else "no",
        })

    out_tsv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_tsv, "w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, delimiter="\t",
                            fieldnames=["guid", "speaker_name", "orig_cs", "fem_cs", "changed"],
                            lineterminator="\n")
        wr.writeheader()
        wr.writerows(out_rows)

    log(f"TSV uloženo: {out_tsv} | řádků: {len(out_rows):,} | changed={changed_count:,} | missing_in_json={missing_in_json:,}")

    # Patch JSON?
    if args.apply:
        if not out_json:
            raise SystemExit("[ERR] Při --apply musíš dát --out-json.")
        # Rozsah aplikace:
        apply_guids = female_guids if (args.apply_all or args.limit == 0) else set(selected_guids)
        if not isinstance(apply_guids, list):
            apply_guids = list(apply_guids)

        updated = 0
        for guid in apply_guids:
            orig = strings_map.get(guid, None)
            if orig is None:
                continue
            fem = svc.rewrite_to_feminine(orig) if orig else orig
            if fem != orig:
                strings_map[guid] = fem
                updated += 1

        # Zapiš JSON
        if mode == "strings":
            root_obj["strings"] = strings_map
        else:
            # mode == "root"
            root_obj = strings_map
        out_json.parent.mkdir(parents=True, exist_ok=True)
        write_json_safely(out_json, root_obj)
        log(f"Patched JSON uložen: {out_json} | updated={updated:,}")

if __name__ == "__main__":
    main()
