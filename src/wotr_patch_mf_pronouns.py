#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_patch_mf_pronouns.py
-------------------------
Nahrazuje v csCZ.json výskyty {mf|he|she} -> {mf|On|Ona}.

Vlastnosti
- Match je case-insensitive a toleruje mezery: {mf| he | she } atd.
- Nemění nic jiného než přesný 2-argumentový mf placeholder he/she.
- Bezpečný zápis (tmp + replace), volitelná záloha .bak, dry-run.

Použití (PowerShell):
  # náhled (kolik výskytů by se změnilo), bez zápisu
  python wotr_patch_mf_pronouns.py --cs .\out_wotr\csCZ.json --dry-run

  # zápis do nového souboru
  python wotr_patch_mf_pronouns.py --cs .\out_wotr\csCZ.json --out .\out_wotr\csCZ-pronouns.json

  # in-place přepis s .bak zálohou
  python wotr_patch_mf_pronouns.py --cs .\out_wotr\csCZ.json --in-place --backup
"""

from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

def log(msg: str) -> None:
    print(msg, flush=True)

# Matchuje: {mf|he|she} s libovolnými mezerami a velikostí písmen
PAT = re.compile(r"\{mf\|\s*he\s*\|\s*she\s*\}", re.IGNORECASE)
REPL = "{mf|On|Ona}"

def write_safely(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

def main():
    ap = argparse.ArgumentParser(description="Replace {mf|he|she} -> {mf|On|Ona} in csCZ.json.")
    ap.add_argument("--cs", dest="cs_path", required=True, help="Vstupní csCZ.json")
    ap.add_argument("--out", dest="out_path", default=None, help="Výstupní JSON (pokud nepoužiješ --in-place)")
    ap.add_argument("--in-place", action="store_true", help="Přepsat přímo vstupní soubor")
    ap.add_argument("--backup", action="store_true", help="Před přepisem uložit .bak")
    ap.add_argument("--dry-run", action="store_true", help="Jen vypiš počty, nic nezapisuj")
    args = ap.parse_args()

    cs_path = Path(args.cs_path)
    if not cs_path.exists():
        raise SystemExit(f"Soubor nenalezen: {cs_path}")

    text = cs_path.read_text(encoding="utf-8")

    # Spočti výskyty + proveď náhradu v paměti
    matches = list(PAT.finditer(text))
    replaced_text = PAT.sub(REPL, text)
    n = len(matches)

    log(f"[SCAN] nalezeno k nahrazení: {n}")

    if args.dry_run:
        log("[DRY-RUN] Žádný zápis neproběhne.")
        return

    # Urči cílovou cestu
    if args.in_place:
        out_path = cs_path
    else:
        out_path = Path(args.out_path) if args.out_path else cs_path.with_name(cs_path.stem + "-pronouns.json")

    if args.backup and out_path.exists():
        bak = out_path.with_suffix(out_path.suffix + ".bak")
        bak.write_text(out_path.read_text(encoding="utf-8"), encoding="utf-8")
        log(f"[BAK] záloha → {bak}")

    write_safely(out_path, replaced_text)
    log(f"[DONE] zapsáno → {out_path} | nahrazeno {n} výskytů")

if __name__ == "__main__":
    main()
