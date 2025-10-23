#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_cz_append_idx_overlay.py
=============================

Účel
----
Pro každý překlad v csCZ.json (podle map.json: idx -> GUID) přidá na **konec textu**
identifikátor ve tvaru " (IDX)". To je vhodné pro betatestery, aby mohli v hlášce
snadno nahlásit konkrétní index k opravě.

Vlastnosti
----------
- Idempotentní: pokud text již končí " (12345)", nepřidá znovu; pokud je jiný index,
  a je zapnuto --force, nahradí jej za správný; jinak ponechá.
- Umí i reverz (odstranění) přes --strip.
- Omezit na subset indexů: --only-idxs "12,34,56" nebo --only-idxs-file.
- Bezpečný zápis (dočasný .tmp soubor).
- Zachová strukturu JSONu, zapisuje s UTF-8 a ensure_ascii=False.

Vstupy
------
- --cs:       cesta k vstupnímu csCZ.json (obsahuje "strings": {GUID: "text", ...})
- --map:      cesta k map.json (obsahuje mapování "idx" -> "GUID")
- --out:      výstupní JSON s overlayem (nepřepisuje vstup, pokud neuvedeš stejné jméno)

Použití – příklady
------------------
1) Přidat indexy za všechny řádky:
   python wotr_cz_append_idx_overlay.py ^
     --cs out_wotr\\csCZ.json ^
     --map out_wotr\\map.json ^
     --out out_wotr\\csCZ.with_idx.json

2) Přidat indexy jen pro konkrétní seznam:
   python wotr_cz_append_idx_overlay.py ^
     --cs out_wotr\\csCZ.json ^
     --map out_wotr\\map.json ^
     --out out_wotr\\csCZ.with_idx.some.json ^
     --only-idxs "8527,8532,27034"

3) Přidat indexy jen pro seznam z textového souboru (čárky/nové řádky):
   python wotr_cz_append_idx_overlay.py ^
     --cs out_wotr\\csCZ.json ^
     --map out_wotr\\map.json ^
     --out out_wotr\\csCZ.with_idx.list.json ^
     --only-idxs-file idx_list.txt

4) Odstranit indexy (reverz) pro celý soubor:
   python wotr_cz_append_idx_overlay.py ^
     --cs out_wotr\\csCZ.with_idx.json ^
     --map out_wotr\\map.json ^
     --out out_wotr\\csCZ.clean.json ^
     --strip

Poznámky
--------
- Pokud řetězec přirozeně končí nějakými závorkami s číslem (vzácné), detekce může kolidovat –
  v takovém případě lze použít --force, které přepíše koncové "(čísla)" na "(IDX)" dle mapy.
- Žádné úpravy uvnitř {g|...} tagů – overlay se připojí až na úplný konec řetězce.
"""

from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from typing import Dict, Any, Optional, Set

IDX_SUFFIX_RX = re.compile(r"\s*\((\d{1,9})\)\s*$")  # match trailing " (12345)"

def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def write_json_safely(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)

def load_idx_whitelist(only_idxs: Optional[str], only_idxs_file: Optional[str]) -> Optional[Set[str]]:
    ids: Set[str] = set()
    if only_idxs:
        for part in only_idxs.replace(" ", "").split(","):
            if part:
                ids.add(str(int(part)))  # normalize to digits
    if only_idxs_file:
        p = Path(only_idxs_file)
        text = p.read_text(encoding="utf-8")
        for token in re.split(r"[,\s]+", text):
            token = token.strip()
            if not token:
                continue
            ids.add(str(int(token)))
    return ids if ids else None

def overlay_one(text: str, idx: str, force: bool) -> str:
    """Append ' (idx)' if not present; if present with different digits and force=True, replace."""
    if text is None:
        text = ""
    m = IDX_SUFFIX_RX.search(text)
    if m:
        current = m.group(1)
        if current == idx:
            return text  # already correct
        if force:
            # replace the trailing "(digits)" with "(idx)"
            return IDX_SUFFIX_RX.sub(f" ({idx})", text)
        else:
            # leave as-is if not forcing
            return text
    # Not present → append
    return f"{text} ({idx})"

def strip_one(text: str) -> str:
    """Remove trailing ' (digits)' if present."""
    if text is None:
        return ""
    return IDX_SUFFIX_RX.sub("", text)

def main():
    ap = argparse.ArgumentParser(description="Append or strip trailing '(IDX)' overlay in csCZ.json by idx->GUID map.")
    ap.add_argument("--cs", required=True, help="Vstupní csCZ.json (obsahuje 'strings')")
    ap.add_argument("--map", required=True, help="map.json (idx -> GUID)")
    ap.add_argument("--out", required=True, help="Výstupní JSON")
    ap.add_argument("--only-idxs", default=None, help="Čárkami oddělený seznam idx (volitelné)")
    ap.add_argument("--only-idxs-file", default=None, help="Soubor se seznamem idx (čárky/nové řádky)")
    ap.add_argument("--strip", action="store_true", help="Místo přidání indexu je odstraní (reverz overlaye)")
    ap.add_argument("--force", action="store_true", help="Při rozdílném '(digits)' na konci řetězce přepiš na '(IDX)' dle mapy")
    args = ap.parse_args()

    cs_path = Path(args.cs)
    map_path = Path(args.map)
    out_path = Path(args.out)

    data = read_json(cs_path)
    if not isinstance(data, dict) or "strings" not in data or not isinstance(data["strings"], dict):
        raise ValueError("Soubor --cs neobsahuje objekt 'strings'.")

    strings: Dict[str, str] = data["strings"]
    id_map: Dict[str, str] = read_json(map_path)

    only: Optional[Set[str]] = load_idx_whitelist(args.only_idxs, args.only_idxs_file)

    total = 0
    changed = 0
    missing_guid = 0
    missing_text = 0

    # map.json: idx -> GUID
    for idx, guid in id_map.items():
        if only is not None and str(int(idx)) not in only:
            continue

        total += 1
        if guid not in strings:
            missing_guid += 1
            continue

        old = strings.get(guid, "")
        if old is None:
            old = ""
            missing_text += 1

        new = strip_one(old) if args.strip else overlay_one(old, str(int(idx)), force=args.force)

        if new != old:
            strings[guid] = new
            changed += 1

    write_json_safely(out_path, data)

    mode = "STRIP" if args.strip else "APPEND"
    print(f"[{mode}] total_mapped={total} | changed={changed} | missing_guid={missing_guid} | missing_text={missing_text}")
    print(f"[OUT] {out_path}")

if __name__ == "__main__":
    main()
