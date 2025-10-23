#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_idx_overlay_and_tsv.py
---------------------------
1) Vytvoří speciální czCZ-idx.json: místo překladu vloží "<idx> <2 slova EN>".
   - formát: {"$id":"<id>","strings":{ "<GUID>": "<IDX two-words>", ... }}
   - $id se vezme z --cz (pokud je), jinak z --en, jinak "1".

2) Umí vygenerovat korekční TSV pro zadané indexy (oddělené čárkou/whitespace nebo ze souboru),
   Formát TSV: idx<TAB>Source<TAB>Translation<TAB>reason
   - Source = EN text
   - Translation = CZ (pokud je k dispozici z --cz), jinak prázdné
   - TAB/NEWLINE ve zdrojích jsou escapované na \t a \n

Vstupy:
- --map: out_dir/map.json (idx -> GUID) [povinné]
- --en:  enGB.json (obsahuje "$id" a "strings": { GUID: EN text }) [povinné]
- --cz:  csCZ.json (volitelné; pro TSV předvyplní "Translation" a pro overlay dodá $id)

Použití:
- Overlay JSON:
  python wotr_idx_overlay_and_tsv.py ^
    --make overlay ^
    --map out_wotr\\map.json ^
    --en enGB.json ^
    --cz out_wotr\\csCZ.json ^
    --out-json out_wotr\\czCZ-idx.json

- Korekční TSV (seznam indexů přímo):
  python wotr_idx_overlay_and_tsv.py ^
    --make tsv ^
    --map out_wotr\\map.json ^
    --en enGB.json ^
    --cz out_wotr\\csCZ.json ^
    --idx-list "2200,2282,5158,5160" ^
    --out-tsv out_wotr\\audit\\corrections.tsv

- Korekční TSV (seznam indexů ze souboru):
  python wotr_idx_overlay_and_tsv.py ^
    --make tsv ^
    --map out_wotr\\map.json ^
    --en enGB.json ^
    --cz out_wotr\\csCZ.json ^
    --idx-file fix_me.txt ^
    --out-tsv out_wotr\\audit\\corrections.tsv ^
    --reason "manual-check"
"""

from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional

TSV_SEP = "\t"

# -------- I/O helpers --------

def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def write_safely(path: Path, data: str | bytes, binary: bool = False) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        with open(tmp, "wb") as f:
            f.write(data)  # type: ignore[arg-type]
    else:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(data))
    tmp.replace(path)

def sanitize_tsv_cell(s: str) -> str:
    """Make embedded tabs/newlines visible to avoid breaking TSV structure."""
    return (s or "").replace("\t", "\\t").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")

# -------- Text utils --------

G_TAG_PAIR_RE = re.compile(r"\{g\|[^}]*\}(.*?)\{\/g\}", re.DOTALL | re.IGNORECASE)
CURLY_SINGLE_RE = re.compile(r"\{[^}]*\}")     # ostatní { ... } nahradíme prázdnem
HTML_TAG_RE = re.compile(r"<[^>]+>")           # <...> pryč
WORD_RE = re.compile(r"[0-9A-Za-zÀ-ž'’\-]+")

def strip_game_tags(s: str) -> str:
    if not s:
        return s
    s = G_TAG_PAIR_RE.sub(lambda m: m.group(1), s)  # {g|...}...{/g} -> uchovej vnitřek
    s = CURLY_SINGLE_RE.sub("", s)                  # ostatní {...} odstraň
    s = HTML_TAG_RE.sub("", s)                      # <...> odstraň
    return s

def first_n_words(s: str, n: int = 2) -> str:
    s = strip_game_tags(s)
    tokens = WORD_RE.findall(s)
    return " ".join(tokens[:n])

# -------- Core --------

def resolve_id_value(cz_path: Optional[Path], en_path: Path) -> str:
    """Pick $id from CZ if available, else from EN, else '1'."""
    if cz_path and cz_path.exists():
        cz = read_json(cz_path)
        if isinstance(cz, dict) and "$id" in cz:
            return str(cz["$id"])
    en = read_json(en_path)
    if isinstance(en, dict) and "$id" in en:
        return str(en["$id"])
    return "1"

def build_overlay(map_path: Path, en_path: Path, out_json: Path, cz_path: Optional[Path]) -> Tuple[int,int]:
    """
    Vytvoří czCZ-idx.json:
      {"$id":"<id>","strings":{ GUID: f"{idx} {two_words}" }}
    """
    idx2guid: Dict[str, str] = read_json(map_path)
    en = read_json(en_path)
    if "strings" not in en or not isinstance(en["strings"], dict):
        raise ValueError("--en neobsahuje objekt 'strings'.")

    en_strings: Dict[str, str] = en["strings"]
    out_strings: Dict[str, str] = {}
    missing_sources = 0
    total = 0

    # projdi idx vzestupně, aby bylo deterministické
    for idx in sorted(idx2guid.keys(), key=lambda s: int(s) if s.isdigit() else s):
        guid = idx2guid[idx]
        src = en_strings.get(guid, "")
        if not src:
            missing_sources += 1
            two = ""
        else:
            two = first_n_words(src, 2)
        val = f"{idx}" + (f" {two}" if two else "")
        out_strings[guid] = val
        total += 1

    payload = {"$id": resolve_id_value(cz_path, en_path), "strings": out_strings}
    write_safely(out_json, json.dumps(payload, ensure_ascii=False, indent=2))
    return total, missing_sources

def parse_idx_list(idx_list: Optional[str], idx_file: Optional[Path]) -> List[str]:
    raw = ""
    if idx_list:
        raw += idx_list
    if idx_file and idx_file.exists():
        raw += (" " if raw else "") + idx_file.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    # rozděl podle čárek, středníků, whitespace
    parts = re.split(r"[,\s;]+", raw.strip())
    out = []
    for p in parts:
        q = p.strip()
        if q:
            out.append(q)
    # deduplikace se zachováním pořadí
    seen = set()
    uniq: List[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq

def build_corrections_tsv(map_path: Path,
                          en_path: Path,
                          out_tsv: Path,
                          idxs: List[str],
                          cz_path: Optional[Path] = None,
                          reason_label: str = "manual") -> Tuple[int,int]:
    """
    Vytvoří TSV (idx, Source, Translation, reason) pro vybrané indexy.
    - Source z EN (přes GUID dle mapy)
    - Translation z CZ (--cz), je-li k dispozici; jinak prázdné
    """
    # založ prázdný s hlavičkou, i když nic nenajdeme
    lines: List[str] = ["idx\tSource\tTranslation\treason"]

    if not idxs:
        write_safely(out_tsv, "\n".join(lines) + "\n")
        return 0, 0

    idx2guid: Dict[str, str] = read_json(map_path)

    en = read_json(en_path)
    if "strings" not in en or not isinstance(en["strings"], dict):
        raise ValueError("--en neobsahuje objekt 'strings'.")
    en_strings: Dict[str, str] = en["strings"]

    cz_strings: Dict[str, str] = {}
    if cz_path and Path(cz_path).exists():
        cz = read_json(Path(cz_path))
        if "strings" in cz and isinstance(cz["strings"], dict):
            cz_strings = cz["strings"]

    ok = 0
    missing = 0
    for idx in idxs:
        guid = idx2guid.get(idx)
        if not guid:
            missing += 1
            continue
        src = sanitize_tsv_cell(en_strings.get(guid, ""))
        tr  = sanitize_tsv_cell(cz_strings.get(guid, ""))
        line = f"{idx}{TSV_SEP}{src}{TSV_SEP}{tr}{TSV_SEP}{reason_label}"
        lines.append(line)
        ok += 1

    write_safely(out_tsv, "\n".join(lines) + "\n")
    return ok, missing

# -------- CLI --------

def main():
    ap = argparse.ArgumentParser(description="Vytvoř czCZ-idx overlay a/nebo korekční TSV pro vybrané indexy.")
    ap.add_argument("--map", required=True, help="out_dir/map.json (idx -> GUID)")
    ap.add_argument("--en",  required=True, help="enGB.json s objektem '$id' a 'strings' (GUID -> EN)")
    ap.add_argument("--cz",  help="csCZ.json (volitelné; pro $id v overlayi a předvyplnění 'Translation' v TSV)")

    ap.add_argument("--make", choices=["overlay","tsv","both"], default="overlay",
                    help="Co vytvořit (overlay JSON, korekční TSV, nebo obojí). Default overlay.")

    # Overlay výstup
    ap.add_argument("--out-json", help="Kam zapsat czCZ-idx.json (např. out_wotr/czCZ-idx.json)")

    # TSV vstupy/výstupy
    ap.add_argument("--idx-list", help="Seznam indexů oddělených čárkou/whitespace/; pro TSV")
    ap.add_argument("--idx-file", help="Soubor s indexy (oddělené čárkou/whitespace/;), pro TSV")
    ap.add_argument("--out-tsv",  help="Kam zapsat korekční TSV (např. out_wotr/audit/corrections.tsv)")
    ap.add_argument("--reason",   default="manual", help="Hodnota sloupce 'reason' v TSV (default 'manual')")

    args = ap.parse_args()

    map_path = Path(args.map)
    en_path  = Path(args.en)
    cz_path  = Path(args.cz) if args.cz else None

    if args.make in ("overlay","both"):
        if not args.out_json:
            raise SystemExit("--out-json je povinné pro --make overlay/both.")
        out_json = Path(args.out_json)
        total, miss = build_overlay(map_path, en_path, out_json, cz_path)
        print(f"[OVERLAY] strings={total} | en_missing={miss} → {out_json}")

    if args.make in ("tsv","both"):
        if not args.out_tsv:
            raise SystemExit("--out-tsv je povinné pro --make tsv/both.")
        idxs = parse_idx_list(args.idx_list, Path(args.idx_file) if args.idx_file else None)
        ok, miss = build_corrections_tsv(
            map_path=map_path,
            en_path=en_path,
            out_tsv=Path(args.out_tsv),
            idxs=idxs,
            cz_path=cz_path,
            reason_label=args.reason
        )
        print(f"[TSV] rows_written={ok} | idx_not_in_map={miss} → {args.out_tsv}")

if __name__ == "__main__":
    main()
