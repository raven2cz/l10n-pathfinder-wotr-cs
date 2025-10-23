#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_patch_mf_pronouns.py
-------------------------
Nahrazuje v csCZ.json vybrané {mf|…|…} placeholdery na české tvary:

  {mf|he|she}        -> {mf|on|ona}        (case-preserve: on/On/ON, ona/Ona/ONA)
  {mf|his|her}       -> {mf|jeho|její}     (case-preserve)
  {mf|Master|Mistress} -> {mf|pane|paní}   (case-preserve: pane/Pane/PANE, paní/Paní/PANÍ)

Vlastnosti
- Match je case-insensitive a toleruje mezery: {mf| He | She }, {mf| MASTER | MISTRESS } apod.
- Zachovává „styl“ psaní podle anglického vzoru (lower/Title/UPPER) zvlášť pro obě varianty.
- Bezpečný zápis (tmp + replace), volitelná záloha .bak, dry-run.

Použití (PowerShell):
  # náhled (jen počty), bez zápisu
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

# --- pomocné: zachování stylu psaní (lower / Title / UPPER) ---
def apply_case_like(sample: str, target: str) -> str:
    s = sample or ""
    if s.isupper():
        return target.upper()
    if s.islower():
        return target.lower()
    # Title-case (první písmeno velké, zbytek malé)
    if s[:1].isupper() and s[1:].islower():
        return target[:1].upper() + target[1:]
    # fallback – nic speciálního
    return target

# --- regexy (case-insensitive, povolí mezery kolem slov) ---
PAT_HE_SHE = re.compile(r"\{mf\|\s*(he)\s*\|\s*(she)\s*\}", re.IGNORECASE)
PAT_HIS_HER = re.compile(r"\{mf\|\s*(his)\s*\|\s*(her)\s*\}", re.IGNORECASE)
PAT_MASTER_MISTRESS = re.compile(r"\{mf\|\s*(master)\s*\|\s*(mistress)\s*\}", re.IGNORECASE)

def repl_he_she(m: re.Match) -> str:
    he = m.group(1)
    she = m.group(2)
    cz_m = apply_case_like(he, "on")
    cz_f = apply_case_like(she, "ona")
    return f"{{mf|{cz_m}|{cz_f}}}"

def repl_his_her(m: re.Match) -> str:
    his = m.group(1)
    her = m.group(2)
    cz_m = apply_case_like(his, "jeho")
    cz_f = apply_case_like(her, "její")
    return f"{{mf|{cz_m}|{cz_f}}}"

def repl_master_mistress(m: re.Match) -> str:
    master = m.group(1)
    mistress = m.group(2)
    cz_m = apply_case_like(master, "pane")
    cz_f = apply_case_like(mistress, "paní")
    return f"{{mf|{cz_m}|{cz_f}}}"

def write_safely(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

def main():
    ap = argparse.ArgumentParser(description="Replace selected {mf|…|…} placeholders in csCZ.json with Czech forms (case-preserving).")
    ap.add_argument("--cs", dest="cs_path", required=True, help="Vstupní csCZ.json")
    ap.add_argument("--out", dest="out_path", default=None, help="Výstupní JSON (pokud nepoužiješ --in-place)")
    ap.add_argument("--in-place", action="store_true", help="Přepsat přímo vstupní soubor")
    ap.add_argument("--backup", action="store_true", help="Před přepisem uložit .bak")
    ap.add_argument("--dry-run", action="store_true", help="Jen spočítat a vypsat počty, nic nezapisovat")
    args = ap.parse_args()

    cs_path = Path(args.cs_path)
    if not cs_path.exists():
        raise SystemExit(f"Soubor nenalezen: {cs_path}")

    text = cs_path.read_text(encoding="utf-8")

    # Náhrady (vždy spočítáme a aplikujeme)
    new_text, n_he_she = PAT_HE_SHE.subn(repl_he_she, text)
    new_text, n_his_her = PAT_HIS_HER.subn(repl_his_her, new_text)
    new_text, n_master_mistress = PAT_MASTER_MISTRESS.subn(repl_master_mistress, new_text)

    total = n_he_she + n_his_her + n_master_mistress
    log(f"[SCAN] he/she: {n_he_she} | his/her: {n_his_her} | Master/Mistress: {n_master_mistress} | total: {total}")

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

    write_safely(out_path, new_text)
    log(f"[DONE] zapsáno → {out_path} | nahrazeno celkem {total} výskytů")

if __name__ == "__main__":
    main()
