#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_feminize_apply_tsv.py
--------------------------
Vezme vstupní TSV s ženskými mluvčími (např. z předchozího filtru), pro každý
řádek zavolá feminizační službu (gpt-5-mini, bez temperature), a do výstupního
TSV zapíše POUZE ty řádky, kde se text skutečně změnil a zároveň nebyly porušeny
odkazy {g|…}…{/g} ani jiné složené závorky.

Vstup – očekávané sloupce:
- speaker_name  (lze změnit parametrem)
- cs_text       (lze změnit parametrem)
- ostatní sloupce zachováme beze změn

Výstup:
- stejné sloupce jako vstup + nový sloupec 'cs_text_female'
- jsou zapsány jen změněné řádky (identické se přeskočí)

Použití:
  python wotr_feminize_apply_tsv.py ^
    --in .\audit\female_heroes_only_filtered.tsv ^
    --out .\audit\female_feminized_changed.tsv ^
    --api-key sk-... ^
    --speaker-col speaker_name ^
    --text-col cs_text

Volitelně:
  --base-url https://api.openai.com
  --model gpt-5-mini
  --limit 200             # procesuj jen prvních N řádků (rychlá zkouška)
  --skip-identical        # (default ON) – identické řádky neukládej
  --debug-dir .\debug     # uloží JSONy request/response pro diagnostiku
"""

from __future__ import annotations
import os
import re
import csv
import json
import time
import argparse
from pathlib import Path
from typing import List, Dict, Tuple

from wotr_feminize_service import FeminizeService, DEFAULT_MODEL

BRACED_ANY = re.compile(r"\{[^{}]*\}")
GLINK = re.compile(r"\{g\|[^{}]*\}.*?\{\/g\}", re.DOTALL)

def extract_braced_chunks(s: str) -> List[str]:
    """Hrubý výčet všech {...} sekvencí (neřeší vnořování – v našich datech to nevadí)."""
    return BRACED_ANY.findall(s)

def extract_glink_chunks(s: str) -> List[str]:
    """Specificky {g|...}...{/g} bloky."""
    return GLINK.findall(s)

def links_preserved(src: str, dst: str) -> bool:
    """Základní jistota: všechny {g|…}…{/g} bloky musí být 100% shodné a ve stejném pořadí."""
    a = extract_glink_chunks(src)
    b = extract_glink_chunks(dst)
    return a == b

def braced_balance_ok(src: str, dst: str) -> bool:
    """Počet a pořadí obecných {...} bloků je stejný."""
    a = extract_braced_chunks(src)
    b = extract_braced_chunks(dst)
    return a == b

def write_debug(debug_dir: Path, stem: str, kind: str, payload: dict | str) -> None:
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        p = debug_dir / f"{stem}.{kind}.json"
        if isinstance(payload, str):
            data = {"data": payload}
        else:
            data = payload
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def main():
    ap = argparse.ArgumentParser(description="Feminizační průchod přes TSV – zapisuje pouze změněné řádky.")
    ap.add_argument("--in", dest="in_path", required=True, help="Vstupní TSV")
    ap.add_argument("--out", dest="out_path", required=True, help="Výstupní TSV pouze se změněnými řádky")
    ap.add_argument("--speaker-col", default="speaker_name", help="Sloupec se jménem mluvčí (default: speaker_name)")
    ap.add_argument("--text-col", default="cs_text", help="Sloupec s českým textem k úpravě (default: cs_text)")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=0, help="Zpracuj max N řádků (0=bez limitu)")
    ap.add_argument("--skip-identical", action="store_true", default=True, help="Identické výsledky neukládej")
    ap.add_argument("--debug-dir", default=None, help="Adresář pro debug výpisy request/response")
    ap.add_argument("--timeout-s", type=int, default=120)
    ap.add_argument("--retries", type=int, default=5)
    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)
    debug_dir = Path(args.debug_dir) if args.debug_dir else None

    with in_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = reader.fieldnames or []
        if not fieldnames:
            raise SystemExit("Input TSV nemá header.")
        for col in (args.speaker_col, args.text_col):
            if col not in fieldnames:
                raise SystemExit(f"Chybí sloupec '{col}'. K dispozici: {fieldnames}")
        rows = list(reader)

    # Připrav writer – přidáme nový sloupec
    out_fieldnames = list(fieldnames)
    new_col = "cs_text_female"
    if new_col not in out_fieldnames:
        out_fieldnames.append(new_col)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    w = csv.DictWriter(out_path.open("w", encoding="utf-8", newline=""), fieldnames=out_fieldnames, delimiter="\t", lineterminator="\n")
    w.writeheader()

    svc = FeminizeService(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        timeout_s=args.timeout_s,
        retries=args.retries,
    )

    total = 0
    kept = 0
    start = time.time()
    for i, row in enumerate(rows, start=1):
        if args.limit and total >= args.limit:
            break
        total += 1

        speaker = (row.get(args.speaker_col) or "").strip()
        src_text = (row.get(args.text_col) or "").rstrip("\n")
        if not src_text:
            continue

        # zavolej službu
        try:
            out_text = svc.feminize(src_text, speaker=speaker)
        except Exception as e:
            if debug_dir:
                write_debug(debug_dir, f"row_{i:06d}", "error", {"error": str(e), "speaker": speaker, "src": src_text})
            continue

        # identické? (přesný match)
        if args.skip_identical and out_text == src_text:
            continue

        # bezpečnost: ověř zachování závorek/odkazů
        if not links_preserved(src_text, out_text) or not braced_balance_ok(src_text, out_text):
            # nedůvěryhodný výsledek – přeskočit, lognout
            if debug_dir:
                write_debug(debug_dir, f"row_{i:06d}", "violation", {
                    "speaker": speaker, "src": src_text, "out": out_text,
                    "links_ok": links_preserved(src_text, out_text),
                    "braces_ok": braced_balance_ok(src_text, out_text),
                })
            continue

        # napiš jen změněné
        row[new_col] = out_text
        w.writerow(row)
        kept += 1

        if i % 50 == 0:
            elapsed = time.time() - start
            print(f"[{i}/{len(rows)}] kept={kept} elapsed={int(elapsed)}s", flush=True)

    svc.close()
    print(f"[DONE] processed={total} | written_changed={kept} → {out_path}")

if __name__ == "__main__":
    main()
