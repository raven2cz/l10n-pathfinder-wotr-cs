#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_patch_cz_from_tsv.py  (SAFE PATCHER, enhanced)
===================================================

Přepíše csCZ.json podle corrections TSV.

Vstupní TSV:
- klíč (idx nebo GUID) může být v libovolném sloupci (viz --key-col),
- nový text je ve sloupci 'Translation' (lze změnit --patch-col nebo alias --col).

Hlavní přepínače:
  --tsv <path>           vstupní TSV
  --map <path>           map.json (idx -> GUID) (nutné, pokud key-type=idx)
  --cs  <path>           vstupní csCZ.json  (obsahuje 'strings')
  --out <path>           výstupní csCZ.json (patched)

  --key-col <name>       jméno sloupce s klíčem (default: automaticky z hlavičky:
                         'idx' pokud existuje, jinak 'guid' pokud existuje, jinak 1. sloupec)
  --key-type auto|idx|guid   jak interpretovat klíč (default auto)
  --patch-col <name>     sloupec s novým textem (default: Translation)
  --col <name>           alias k --patch-col (zpětná kompatibilita)

  --no-unescape          NEpřevádět \\n/\\t na reálné znaky (default: převádí)
  --allow-empty          povolit patch i prázdným textem (jinak se prázdné přeskočí)
  --backup               uloží <cs>.bak před zápisem
  --dry-run              pouze report, nezapisuje výstup
  --report-tsv <path>    diff report (guid\\told\\tnew)
  --report-json <path>   JSON souhrn
  --fail-on-missing      na chybějící mapování/GUID vrací chybový kód 3

Volitelné ochrany (guards):
  --verify-guards        porovná výskyty a pořadí {g|...}{/g} a obecných {...}
  --on-guard-fail skip|patch|fail   co dělat při porušení (default: patch)

Příklad:
  python wotr_patch_cz_from_tsv.py ^
    --tsv .\\out_wotr\\fix\\short_texts_translated.tsv ^
    --map .\\out_wotr\\map.json ^
    --cs  .\\out_wotr\\csCZ.json ^
    --out .\\out_wotr\\csCZ-patched.json ^
    --col translation ^
    --backup ^
    --report-tsv .\\out_wotr\\reports\\short_texts_diff.tsv ^
    --report-json .\\out_wotr\\reports\\short_texts_diff.json
"""

from __future__ import annotations
import argparse, csv, json, sys, re
from pathlib import Path
from typing import Dict, List, Tuple, Optional

TSV = "\t"

# ---------- Guards (volitelné) ----------

BRACED_ANY = re.compile(r"\{[^{}]*\}")
GLINK = re.compile(r"\{g\|[^{}]*\}.*?\{\/g\}", re.DOTALL | re.IGNORECASE)

def _extract_all_braced(s: str) -> List[str]:
    return BRACED_ANY.findall(s or "")

def _extract_glinks(s: str) -> List[str]:
    return GLINK.findall(s or "")

def guards_ok(old: str, new: str) -> bool:
    """Zachováno stejné pořadí {g|…}{/g} i celkových {...} bloků?"""
    return _extract_glinks(old) == _extract_glinks(new) and _extract_all_braced(old) == _extract_all_braced(new)

# ---------- IO helpers ----------

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")

def read_json(p: Path):
    return json.loads(read_text(p))

def write_text_atomic(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)

def backup_file(src: Path) -> Path:
    bak = src.with_suffix(src.suffix + ".bak")
    bak.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return bak

def unescape_literal(s: str) -> str:
    return (s or "").replace("\\n", "\n").replace("\\t", "\t")

# ---------- TSV ----------

def read_tsv(path: Path) -> Tuple[List[str], List[Dict[str,str]]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=TSV)
        if not r.fieldnames:
            raise ValueError("TSV nemá hlavičku.")
        fields = [c for c in r.fieldnames if c is not None]
        rows: List[Dict[str,str]] = []
        for row in r:
            if None in row:  # případné „navíc“ sloupce odfiltruj
                row.pop(None, None)
            rows.append({k: (v if v is not None else "") for k, v in row.items() if k in fields})
        return fields, rows

def resolve_col(fieldnames: List[str], wanted: str) -> Optional[str]:
    """Najdi sloupec case-insensitive. Vrátí skutečné jméno z hlavičky nebo None."""
    if wanted in fieldnames:
        return wanted
    lowmap = {c.lower(): c for c in fieldnames}
    return lowmap.get(wanted.lower())

# ---------- logic ----------

def load_idx_to_guid(map_path: Optional[Path]) -> Dict[str, str]:
    if not map_path:
        return {}
    m = read_json(map_path)
    if not isinstance(m, dict):
        raise ValueError("map.json musí být objekt { idx: GUID, ... }")
    return {str(k): str(v) for k, v in m.items()}

def detect_key_type(sample_value: str, idx2guid: Dict[str,str], forced: Optional[str]) -> str:
    if forced: 
        return forced
    s = (sample_value or "").strip()
    if not s:
        return "guid" if not idx2guid else "idx"
    if idx2guid and s in idx2guid:
        return "idx"
    if not s.isdigit():
        return "guid"
    if idx2guid and s in set(idx2guid.values()):
        return "guid"
    return "idx" if idx2guid else "guid"

def main():
    ap = argparse.ArgumentParser(description="Patch csCZ.json z corrections TSV (idx/guid → Translation).")
    ap.add_argument("--tsv", required=True, help="Vstupní TSV (s hlavičkou)")
    ap.add_argument("--map", help="map.json (idx->GUID), nutné pro key-type=idx/auto")
    ap.add_argument("--cs",  required=True, help="Vstupní csCZ.json")
    ap.add_argument("--out", required=True, help="Výstupní csCZ.json (patched)")

    # Klíč + nový text
    ap.add_argument("--key-col", default=None, help="Jméno sloupce s klíčem (idx/guid). Default: idx|guid|1. sloupec.")
    ap.add_argument("--key-type", choices=["auto","idx","guid"], default="auto", help="Interpretace klíče (default auto)")
    ap.add_argument("--patch-col", default="Translation", help="Sloupec s novým textem (default: Translation)")
    ap.add_argument("--col", dest="patch_col_alias", default=None, help="Alias k --patch-col (zpětná kompatibilita)")

    # Chování
    ap.add_argument("--no-unescape", action="store_true", help="Nevykonat unescape \\n/\\t")
    ap.add_argument("--allow-empty", action="store_true", help="Patchovat i prázdný text (default: přeskočit)")
    ap.add_argument("--dry-run", action="store_true", help="Nezapisovat, pouze report")
    ap.add_argument("--backup", action="store_true", help="Před zápisem uložit <cs>.bak")
    ap.add_argument("--report-tsv", help="Kam zapsat diff (guid\\told\\tnew)")
    ap.add_argument("--report-json", help="Kam zapsat JSON report")
    ap.add_argument("--fail-on-missing", action="store_true", help="Na chybějící mapování/GUID vrátit kód 3")

    # Guards
    ap.add_argument("--verify-guards", action="store_true", help="Ověřit tagy {g|…}{/g} a {...} bloky (pořadí i počty)")
    ap.add_argument("--on-guard-fail", choices=["skip","patch","fail"], default="patch",
                    help="Co dělat při porušení guardů (default: patch)")

    args = ap.parse_args()

    # alias --col → --patch-col
    if args.patch_col_alias:
        args.patch_col = args.patch_col_alias

    tsv_path = Path(args.tsv)
    cs_in    = Path(args.cs)
    cs_out   = Path(args.out)
    map_path = Path(args.map) if args.map else None

    # ---- načtení vstupů
    fieldnames, rows = read_tsv(tsv_path)
    if len(fieldnames) < 1:
        print("ERROR: TSV nemá žádné sloupce.", file=sys.stderr)
        sys.exit(2)

    # detekce/resolve sloupců
    # key col
    key_col: Optional[str] = None
    if args.key_col:
        key_col = resolve_col(fieldnames, args.key_col)
        if not key_col:
            print(f"ERROR: Sloupec '{args.key_col}' (key) v TSV nenalezen. Dostupné: {fieldnames}", file=sys.stderr)
            sys.exit(2)
    else:
        # heuristika: preferuj 'idx', pak 'guid', jinak první sloupec
        key_col = resolve_col(fieldnames, "idx") or resolve_col(fieldnames, "guid") or fieldnames[0]

    # patch col
    patch_col: Optional[str] = resolve_col(fieldnames, args.patch_col)
    if not patch_col:
        print(f"ERROR: Sloupec '{args.patch_col}' (patch) v TSV nenalezen. Dostupné: {fieldnames}", file=sys.stderr)
        sys.exit(2)

    idx2guid = load_idx_to_guid(map_path)

    cz = read_json(cs_in)
    if "strings" not in cz or not isinstance(cz["strings"], dict):
        print("ERROR: csCZ.json neobsahuje objekt 'strings'.", file=sys.stderr)
        sys.exit(2)
    strings: Dict[str,str] = cz["strings"]

    # key-type
    sample_key = (rows[0].get(key_col) or "").strip() if rows else ""
    forced_type = None if args.key_type == "auto" else args.key_type
    key_type = detect_key_type(sample_key, idx2guid, forced_type)
    if key_type == "idx" and not idx2guid:
        print("ERROR: key-type=idx ale chybí --map.", file=sys.stderr)
        sys.exit(2)

    # ---- statistiky
    patched = 0
    unchanged = 0
    skipped_empty = 0
    skipped_no_map = 0
    skipped_missing_guid = 0
    guard_fail = 0

    changes_tsv: List[str] = ["guid\told\tnew"]

    # ---- hlavní smyčka
    for r in rows:
        key_raw = (r.get(key_col) or "").strip()
        new_text = r.get(patch_col) or ""

        if not key_raw:
            continue
        if not args.no_unescape:
            new_text = unescape_literal(new_text)

        # převést klíč na GUID
        if key_type == "idx":
            guid = idx2guid.get(key_raw)
            if not guid:
                skipped_no_map += 1
                continue
        else:
            guid = key_raw

        old = strings.get(guid)
        if old is None:
            skipped_missing_guid += 1
            continue

        if not args.allow_empty and new_text == "":
            skipped_empty += 1
            continue

        if old == new_text:
            unchanged += 1
            continue

        # Guards?
        if args.verify_guards and not guards_ok(old, new_text):
            guard_fail += 1
            if args.on_guard_fail == "skip":
                continue
            elif args.on_guard_fail == "fail":
                print(f"ERROR: Guard mismatch for GUID {guid}", file=sys.stderr)
                sys.exit(4)
            # else "patch" – propadni na patch

        # zaznamenej diff a aplikuj
        old_cell = (old or "").replace("\t","\\t").replace("\n","\\n")
        new_cell = (new_text or "").replace("\t","\\t").replace("\n","\\n")
        changes_tsv.append(f"{guid}\t{old_cell}\t{new_cell}")
        strings[guid] = new_text
        patched += 1

    # ---- report
    report = {
        "key_col": key_col,
        "key_type": key_type,
        "patch_col": patch_col,
        "rows_in_tsv": len(rows),
        "patched": patched,
        "unchanged": unchanged,
        "skipped_empty": skipped_empty,
        "skipped_no_map": skipped_no_map,
        "skipped_missing_guid": skipped_missing_guid,
        "guard_fail": guard_fail,
        "verify_guards": bool(args.verify_guards),
        "on_guard_fail": args.on_guard_fail,
        "tsv": str(tsv_path),
        "cs_in": str(cs_in),
        "cs_out": str(cs_out),
        "map": str(map_path) if map_path else None,
    }

    if args.report_tsv:
        Path(args.report_tsv).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_tsv).write_text("\n".join(changes_tsv) + "\n", encoding="utf-8")

    if args.report_json:
        Path(args.report_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- zápis
    if args.dry_run:
        print("[DRY-RUN]", json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if args.backup:
            bak = backup_file(cs_in)
            print(f"[BACKUP] {bak}")
        write_text_atomic(cs_out, json.dumps(cz, ensure_ascii=False, indent=2))
        print(f"[PATCH] patched={patched} | unchanged={unchanged} | "
              f"skipped_empty={skipped_empty} | skipped_no_map={skipped_no_map} | "
              f"skipped_missing_guid={skipped_missing_guid} | guard_fail={guard_fail} → {cs_out}")

    # návratové kódy
    if args.fail_on_missing and (skipped_no_map > 0 or skipped_missing_guid > 0):
        sys.exit(3)

if __name__ == "__main__":
    main()
