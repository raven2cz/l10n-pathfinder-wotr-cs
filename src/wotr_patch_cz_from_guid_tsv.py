#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_patch_cz_from_guid_tsv.py
------------------------------
Patchne finální csCZ.json podle TSV, kde:
- první (nebo zvolený) sloupec obsahuje GUID (klíč do csCZ.json["strings"])
- vybraný sloupec (default: cs_text_female) obsahuje nový český text

Vlastnosti:
- Nemění strukturu JSON; očekává objekt s "strings": { GUID: "text", ... }.
- Bezpečný zápis (temp + replace), volitelná .bak záloha.
- Dry-run režim (počítá změny, ale nezapisuje).
- Report TSV se změnami/skipy (pro audit).
- Volitelná konverze doslovných escape sekvencí "\n", "\t" na skutečné znaky.

Použití (PowerShell):
  python wotr_patch_cz_from_guid_tsv.py `
    --in .\audit\female_heroes_only_fixed.tsv `
    --cs .\out_wotr\csCZ.json `
    --out .\out_wotr\csCZ-patched.json `
    --guid-col key `
    --text-col cs_text_female `
    --report .\audit\female_patch_report.tsv

Volby:
  --in              vstupní TSV s GUIDy a novými texty
  --cs              vstupní csCZ.json (s "strings" mapou)
  --out             výstupní JSON; nezadáš-li, vytvoří se <cs>.patched.json
  --guid-col        název sloupce s GUID (default: key)
  --text-col        název sloupce s novým textem (default: cs_text_female)
  --skip-empty      přeskočí řádky s prázdným novým textem (default: zapnuto)
  --only-different  patchuje jen pokud je text jiný než stávající (default: zapnuto)
  --unescape        převádí doslovné "\n", "\t", "\r" na skutečné znaky (default: vypnuto)
  --backup          uloží <out>.bak s původním csCZ.json (před přepsáním out) (default: vypnuto)
  --dry-run         nic nezapisuje, jen reportuje
  --report          cesta k auditnímu TSV reportu (volitelné)
"""

from __future__ import annotations
import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

def log(msg: str) -> None:
    print(msg, flush=True)

def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def write_safely(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

def ensure_strings_map(obj) -> Dict[str, str]:
    if isinstance(obj, dict) and "strings" in obj and isinstance(obj["strings"], dict):
        return obj["strings"]
    if isinstance(obj, dict):
        # fallback: možná je to rovnou mapa guid->text
        return obj
    raise SystemExit("csCZ.json nemá očekávaný formát (objekt s 'strings').")

def unescape_literals(s: str) -> str:
    # pouze nejběžnější sekvence
    return (
        s.replace("\\n", "\n")
         .replace("\\t", "\t")
         .replace("\\r", "\r")
    )

def main():
    ap = argparse.ArgumentParser(description="Patch csCZ.json podle TSV s GUID->cs_text_female.")
    ap.add_argument("--in", dest="in_path", required=True, help="Vstupní TSV s GUID a novým textem.")
    ap.add_argument("--cs", dest="cs_path", required=True, help="csCZ.json (s 'strings').")
    ap.add_argument("--out", dest="out_path", default=None, help="Výstupní JSON (default: <cs>.patched.json).")
    ap.add_argument("--guid-col", default="key", help="Sloupec s GUID (default: key).")
    ap.add_argument("--text-col", default="cs_text_female", help="Sloupec s novým textem (default: cs_text_female).")
    ap.add_argument("--skip-empty", action="store_true", default=True, help="Přeskočit prázdné nové texty (default ON).")
    ap.add_argument("--no-skip-empty", dest="skip_empty", action="store_false", help="Patchovat i prázdné texty.")
    ap.add_argument("--only-different", action="store_true", default=True, help="Měnit jen pokud je jiný text (default ON).")
    ap.add_argument("--no-only-different", dest="only_different", action="store_false", help="Přepsat i shodné texty.")
    ap.add_argument("--unescape", action="store_true", help="Převést doslovné \\n/\\t/\\r na skutečné znaky.")
    ap.add_argument("--backup", action="store_true", help="Před zápisem vytvořit zálohu out souboru jako .bak.")
    ap.add_argument("--dry-run", action="store_true", help="Jen simulace – nezapisuje out JSON.")
    ap.add_argument("--report", default=None, help="Auditní TSV report změn/skipů.")

    args = ap.parse_args()

    in_path = Path(args.in_path)
    cs_path = Path(args.cs_path)
    out_path = Path(args.out_path) if args.out_path else cs_path.with_name(cs_path.stem + "-patched.json")
    report_path = Path(args.report) if args.report else None

    # Načíst JSON
    try:
        cs_obj = read_json(cs_path)
    except Exception as e:
        raise SystemExit(f"Nešlo načíst csCZ.json: {e}")
    strings = ensure_strings_map(cs_obj)

    # Načíst TSV
    try:
        with in_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            header = reader.fieldnames or []
            if not header:
                raise SystemExit("Vstupní TSV nemá header.")
            for col in (args.guid_col, args.text_col):
                if col not in header:
                    raise SystemExit(f"Chybí sloupec '{col}'. K dispozici: {header}")
            rows = list(reader)
    except Exception as e:
        raise SystemExit(f"Nešlo načíst TSV: {e}")

    total = len(rows)
    changed = 0
    same = 0
    missing = 0
    empty = 0
    errors = 0
    duplicates: Dict[str, int] = {}
    actions: List[Tuple[str, str, str, str]] = []  # guid, action, old, new

    # Pokud v TSV bude duplicitní GUID, rozhoduje poslední výskyt (logujeme počty).
    for r in rows:
        guid = (r.get(args.guid_col) or "").strip()
        if guid:
            duplicates[guid] = duplicates.get(guid, 0) + 1

    # Aplikace (procházíme v pořadí, poslední výskyt vyhrává)
    for idx, r in enumerate(rows, start=1):
        guid = (r.get(args.guid_col) or "").strip()
        new_text = r.get(args.text_col, "")
        if args.unescape:
            new_text = unescape_literals(new_text)

        if not guid:
            errors += 1
            actions.append(("", "invalid_guid", "", new_text))
            continue

        if guid not in strings:
            missing += 1
            actions.append((guid, "missing_guid", "", new_text))
            continue

        if args.skip_empty and (new_text == "" or new_text is None):
            empty += 1
            actions.append((guid, "empty_skip", strings.get(guid, ""), new_text))
            continue

        old_text = strings.get(guid, "")
        if args.only_different and (new_text == old_text):
            same += 1
            actions.append((guid, "same_skip", old_text, new_text))
            continue

        # patch
        strings[guid] = new_text
        changed += 1
        actions.append((guid, "changed", old_text, new_text))

    # Zápis reportu
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w", encoding="utf-8", newline="") as rf:
            w = csv.writer(rf, delimiter="\t", lineterminator="\n")
            w.writerow(["guid", "action", "old_text", "new_text"])
            w.writerows(actions)

    # Zápis JSON (pokud ne-dry-run)
    if args.dry_run:
        log(f"[DRY-RUN] Přeskočen zápis JSON. Náhled: changed={changed}, same_skip={same}, missing_guid={missing}, empty_skip={empty}, errors={errors}")
        log(f"[OUT] report: {report_path}" if report_path else "[OUT] report: (žádný)")
        sys.exit(0)

    # připrav výstupní objekt: pokud původní měl 'strings', zachovej strukturu
    if isinstance(cs_obj, dict) and "strings" in cs_obj:
        out_obj = dict(cs_obj)
        out_obj["strings"] = strings
    else:
        out_obj = strings

    if args.backup and out_path.exists():
        bak = out_path.with_suffix(out_path.suffix + ".bak")
        bak.write_text(out_path.read_text(encoding="utf-8"), encoding="utf-8")

    write_safely(out_path, json.dumps(out_obj, ensure_ascii=False, indent=2))
    log(f"[DONE] rows={total} | changed={changed} | same_skip={same} | missing_guid={missing} | empty_skip={empty} | errors={errors}")
    if duplicates:
        dups = sum(1 for _ in duplicates.values() if _ > 1)
        if dups:
            log(f"[NOTE] duplicitní GUIDy v TSV: {dups} (počítá se poslední výskyt).")
    log(f"[OUT] → {out_path}")
    if report_path:
        log(f"[REPORT] → {report_path}")

if __name__ == "__main__":
    main()
