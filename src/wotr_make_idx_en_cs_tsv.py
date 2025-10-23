#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_make_idx_en_cs_tsv.py
--------------------------
Z `map.json` (idx -> GUID), `enGB.json` (strings) a `csCZ.json` (strings) vytvoří
TSV s hlavičkou: guid<TAB>idx<TAB>en_text<TAB>cs_text

- Výstup je "bezpečný" pro TSV: znaky TAB/NEWLINE se escapují jako \t, \n, \r (lze vypnout --no-escape).
- Pokud některý text chybí (není v en/cs JSONu), sloupec zůstane prázdný.
- Umí omezit na vybrané indexy přes --idxs 12,34,56 a řadit číselně/lexikálně.

Použití:
  python wotr_make_idx_en_cs_tsv.py ^
    --map out_wotr/map.json ^
    --en enGB.json ^
    --cs out_wotr/csCZ.json ^
    --out out_wotr/audit/beta_review.tsv
"""

from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Dict, Any, List

TSV_SEP = "\t"

def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def escape_tsv(s: str) -> str:
    # Bezpečné zobrazení: nevkládat skutečné taby a nové řádky do TSV
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")

def load_strings(json_path: Path) -> Dict[str, str]:
    data = read_json(json_path)
    if not isinstance(data, dict) or "strings" not in data or not isinstance(data["strings"], dict):
        raise SystemExit(f"{json_path} neobsahuje objekt 'strings'.")
    out: Dict[str, str] = {}
    for k, v in data["strings"].items():
        out[str(k)] = "" if v is None else str(v)
    return out

def main():
    ap = argparse.ArgumentParser(description="Vytvoří TSV: guid, idx, en_text, cs_text (pro betatestery).")
    ap.add_argument("--map", required=True, help="out_dir/map.json (idx -> GUID)")
    ap.add_argument("--en",  required=True, help="enGB.json (originální angličtina)")
    ap.add_argument("--cs",  required=True, help="csCZ.json (aktuální překlad)")
    ap.add_argument("--out", required=True, help="Cílový TSV soubor")

    ap.add_argument("--idxs", default=None, help="Volitelně: čárkami oddělený seznam idx (např. 12,34,56). Pokud nezadáno, bere vše.")
    ap.add_argument("--no-escape", action="store_true", help="Nevyměňuj \\t/\\n/\\r za escape sekvence (pozor na TSV editory).")
    ap.add_argument("--sort", choices=["numeric","lex"], default="numeric", help="Pořadí idx ve výstupu (default numeric).")

    args = ap.parse_args()

    map_path = Path(args.map)
    en_path  = Path(args.en)
    cs_path  = Path(args.cs)
    out_path = Path(args.out)

    # Načtení mapy idx->GUID
    idx2guid = read_json(map_path)
    if not isinstance(idx2guid, dict):
        raise SystemExit("map.json nemá očekávaný formát (dict idx->GUID).")

    # Načtení EN a CS strings
    en_strings = load_strings(en_path)
    cs_strings = load_strings(cs_path)

    # Jaké idx exportovat
    if args.idxs:
        wanted = {x.strip() for x in args.idxs.split(",") if x.strip()}
        idx_list = [i for i in idx2guid.keys() if i in wanted]
    else:
        idx_list = list(idx2guid.keys())

    # Třídění
    if args.sort == "numeric":
        try:
            idx_list.sort(key=lambda s: int(s))
        except Exception:
            idx_list.sort()
    else:
        idx_list.sort()

    # Výstup
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        f.write("guid\tidx\ten_text\tcs_text\n")
        total = 0
        miss_en = 0
        miss_cs = 0
        miss_guid = 0
        for idx in idx_list:
            guid = idx2guid.get(idx, "")
            if not guid:
                miss_guid += 1
            en = en_strings.get(guid, "")
            cs = cs_strings.get(guid, "")
            if en == "": miss_en += 1
            if cs == "": miss_cs += 1
            if not args.no_escape:
                en_out = escape_tsv(en)
                cs_out = escape_tsv(cs)
            else:
                en_out = en
                cs_out = cs
            line = f"{guid}{TSV_SEP}{idx}{TSV_SEP}{en_out}{TSV_SEP}{cs_out}\n"
            f.write(line)
            total += 1

    print(f"[OK] napsáno: {out_path} | řádků: {total} | missing_guid: {miss_guid} | missing_en: {miss_en} | missing_cs: {miss_cs}")

if __name__ == "__main__":
    main()
