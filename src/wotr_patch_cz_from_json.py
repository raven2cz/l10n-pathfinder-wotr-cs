#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_patch_cz_from_json.py
--------------------------
Patchuje finální csCZ.json na základě JSON výstupů z nového pipeline.

Podporované vstupy:
  1) final_translations.json  -> objekt { "<idx>": "<translation_escaped>", ... }
  2) translations.ndjson      -> každý řádek JSON: { "idx": "...", "translation_escaped": "..." }
  3) obecný JSON array        -> [ { "idx": "...", "translation_escaped": "..." }, ... ]

Klíč a mapování:
  - 1. sloupec (klíč) je obvykle `idx` -> pro patch je třeba --map (idx->GUID).
  - Lze patchovat i přímo přes GUID (pak --map není potřeba) a přepnout --key-type guid.
  - --key-type=auto (default) se pokusí odhadnout (pokud máme map.json a klíč existuje v mapě → idx).

Unescape:
  - Ve výstupech držíme odstavce jako LITERALY "\\n" a TABy jako "\\t".
  - Přepínač --unescape (default ON) je převede na skutečné '\n' a '\t' pro zápis do csCZ.json.

Idempotentní: opakované spuštění s týmiž daty nic nemění.
"""

from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple, Optional

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")

def read_json(p: Path):
    return json.loads(read_text(p))

def write_safely(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)

def unescape_literal(s: str) -> str:
    # převede LITERALY "\\n" -> "\n", "\\t" -> "\t"
    # pozor: NEprovádíme plný unicode escape decode, jen \n a \t
    return s.replace("\\n", "\n").replace("\\t", "\t")

def load_idx_to_guid(map_path: Optional[Path]) -> Dict[str, str]:
    if not map_path:
        return {}
    m = read_json(map_path)
    if not isinstance(m, dict):
        raise ValueError("map.json má nečekaný formát (očekáván objekt idx->GUID).")
    return {str(k): str(v) for k, v in m.items()}

def detect_key_type(sample_key: str, idx2guid: Dict[str, str]) -> str:
    """
    Vrací 'idx' nebo 'guid'.
    - Pokud máme mapu a sample_key je v mapě -> 'idx'
    - Pokud sample_key nevypadá jako čistě numerický string -> 'guid'
    - Pokud sample_key je v hodnotách mapy -> 'guid'
    - Jinak: 'idx' pokud máme mapu, jinak 'guid'
    """
    if idx2guid and sample_key in idx2guid:
        return "idx"
    if not sample_key.isdigit():
        return "guid"
    if idx2guid and sample_key in set(idx2guid.values()):
        return "guid"
    return "idx" if idx2guid else "guid"

def iter_pairs_from_json(input_path: Path,
                         idx_field: str = "idx",
                         tr_field: str = "translation_escaped"
                         ) -> Iterable[Tuple[str, str]]:
    """
    Vrátí (key, value) dvojice ze vstupního JSON/NDJSON:
      - final_translations.json: dict { idx: translation_escaped }
      - translations.ndjson:     každá řádka objekt s poli
      - array objektů:           [{"idx":...,"translation_escaped":...}, ...]
    """
    text = read_text(input_path)
    if not text:
        return

    # odstraň případný BOM
    text = text.lstrip("\ufeff").strip()

    # 1) Pokus: načíst celý obsah jako standardní JSON (dict nebo list)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for k, v in data.items():
                yield (str(k).strip(), str(v))
            return
        elif isinstance(data, list):
            for obj in data:
                if not isinstance(obj, dict):
                    continue
                k = str(obj[idx_field]).strip()
                v = str(obj[tr_field])
                yield (k, v)
            return
    except Exception:
        pass  # nevadí, zkusíme NDJSON

    # 2) Fallback: NDJSON (každý neprázdný řádek je samostatný JSON objekt)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # pro robustnost: přeskoč „řádky“ co zjevně nejsou JSON objekty
        if not (line.startswith("{") and line.endswith("}")):
            continue
        obj = json.loads(line)
        k = str(obj[idx_field]).strip()
        v = str(obj[tr_field])
        yield (k, v)

def main():
    ap = argparse.ArgumentParser(description="Patch csCZ.json z JSON výsledků (final_translations.json / translations.ndjson).")
    ap.add_argument("--in", dest="in_path", required=True, help="Vstupní JSON (final_translations.json nebo translations.ndjson)")
    ap.add_argument("--cs-in",  required=True, help="Vstupní csCZ.json (obsahuje 'strings')")
    ap.add_argument("--cs-out", required=True, help="Výstupní csCZ.json (patched)")

    ap.add_argument("--map", help="map.json (idx -> GUID). Povinné, pokud --key-type=idx nebo auto+detekce idx.")
    ap.add_argument("--key-type", choices=["auto","idx","guid"], default="auto",
                    help="Jak interpretovat klíč ve vstupu (default: auto).")
    ap.add_argument("--idx-field", default="idx", help="Název pole s klíčem v NDJSON/array (default: idx)")
    ap.add_argument("--tr-field",  default="translation_escaped", help="Název pole s překladem (default: translation_escaped)")
    ap.add_argument("--no-unescape", action="store_true",
                    help="Nevykonávej unescape \\n/\\t (defaultně se unescape provádí).")
    args = ap.parse_args()

    in_path = Path(args.in_path)
    cs_in   = Path(args.cs_in)
    cs_out  = Path(args.cs_out)
    map_path = Path(args.map) if args.map else None

    # načti mapping idx->GUID (může být prázdný)
    idx2guid = load_idx_to_guid(map_path)

    # načti csCZ.json
    cz = read_json(cs_in)
    if "strings" not in cz or not isinstance(cz["strings"], dict):
        raise ValueError("csCZ.json neobsahuje objekt 'strings'.")
    strings: Dict[str, str] = cz["strings"]  # GUID -> text

    # nashromáždi (key, value) dvojice
    pairs = list(iter_pairs_from_json(in_path, idx_field=args.idx_field, tr_field=args.tr_field))
    if not pairs:
        print("[PATCH] prázdný vstup, není co patchovat.")
        write_safely(cs_out, json.dumps(cz, ensure_ascii=False, indent=2))
        return

    # detekce/volba key typu
    sample_key = pairs[0][0]
    key_type = args.key_type
    if key_type == "auto":
        key_type = detect_key_type(sample_key, idx2guid)
    if key_type == "idx" and not idx2guid:
        raise ValueError("KEY=idx ale nebyl dodán --map (idx->GUID).")

    # statistiky
    patched = 0
    unchanged = 0
    skipped_empty = 0
    skipped_no_map = 0
    skipped_missing_guid = 0

    # patchování
    for key_raw, tr_escaped in pairs:
        tr = tr_escaped
        if not args.no_unescape:  # default je unescape ON
            tr = unescape_literal(tr)

        if not tr:
            skipped_empty += 1
            continue

        if key_type == "idx":
            guid = idx2guid.get(key_raw)
            if not guid:
                skipped_no_map += 1
                continue
        else:
            guid = key_raw

        if guid not in strings:
            skipped_missing_guid += 1
            continue

        if strings[guid] == tr:
            unchanged += 1
        else:
            strings[guid] = tr
            patched += 1

    # zápis
    write_safely(cs_out, json.dumps(cz, ensure_ascii=False, indent=2))

    print(f"[PATCH] key_type={key_type} | items={len(pairs)} | patched={patched} | unchanged={unchanged} | "
          f"skipped_empty={skipped_empty} | skipped_no_map={skipped_no_map} | skipped_missing_guid={skipped_missing_guid} | "
          f"→ {cs_out}")

if __name__ == "__main__":
    main()
