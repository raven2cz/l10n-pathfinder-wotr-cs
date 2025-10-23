#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_extract_short_texts_tsv.py
--------------------------------
Vybere z enGB/csCZ všechny položky, jejichž anglický text má
po odfiltrování tagů max N slov (default 2), a uloží je do TSV:

  idx<TAB>source<TAB>translation<TAB>reason

Volby:
  --max-words N            (default 2)
  --min-letters N          (default 1)
  --exclude-identical      vynechat řádky, kde CZ == EN (po trim)
  --only-missing           vynechat řádky s neprázdnou CZ
  --reason TEXT            hodnota do sloupce 'reason' (default "short<=N")
"""

from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from typing import Dict, List

TSV = "\t"

def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))

def write_text(p: Path, data: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8")

def sanitize_tsv_cell(s: str) -> str:
    # Krátké labely obvykle nemají \t ani \n, ale pro jistotu escapujeme.
    return (s or "").replace("\t","\\t").replace("\r\n","\n").replace("\r","\n").replace("\n","\\n")

# tag stripping & tokenization
G_TAG_PAIR_RE = re.compile(r"\{g\|[^}]*\}(.*?)\{\/g\}", re.IGNORECASE | re.DOTALL)
CURLY_SINGLE_RE = re.compile(r"\{[^}]*\}")
HTML_TAG_RE    = re.compile(r"<[^>]+>")
WORD_RE        = re.compile(r"[0-9A-Za-zÀ-ž'’\-]+")

def strip_game_tags(s: str) -> str:
    if not s:
        return ""
    # {g|...}...{/g} -> necháme jen vnitřek
    s = G_TAG_PAIR_RE.sub(lambda m: m.group(1), s)
    # ostatní {...} pryč
    s = CURLY_SINGLE_RE.sub("", s)
    # HTML tagy pryč
    s = HTML_TAG_RE.sub("", s)
    return s.strip()

def count_words(s: str) -> int:
    return len(WORD_RE.findall(s or ""))

def main():
    ap = argparse.ArgumentParser(description="Extract short (<=N words) EN strings into TSV for per-line translation.")
    ap.add_argument("--map", required=True, help="map.json (idx -> GUID)")
    ap.add_argument("--en",  required=True, help="enGB.json (obsahuje 'strings')")
    ap.add_argument("--cz",  required=False, help="csCZ.json (pro předvyplnění sloupce 'translation')")
    ap.add_argument("--out-tsv", required=True, help="Výstupní TSV")

    ap.add_argument("--max-words", type=int, default=2)
    ap.add_argument("--min-letters", type=int, default=1)
    ap.add_argument("--exclude-identical", action="store_true")
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--reason", default=None)
    args = ap.parse_args()

    map_path = Path(args.map)
    en_path  = Path(args.en)
    cz_path  = Path(args.cz) if args.cz else None
    out_path = Path(args.out_tsv)

    idx2guid: Dict[str,str] = read_json(map_path)

    en = read_json(en_path)
    if "strings" not in en or not isinstance(en["strings"], dict):
        print("ERROR: enGB.json neobsahuje objekt 'strings'.", file=sys.stderr)
        sys.exit(2)
    en_strings: Dict[str,str] = en["strings"]

    cz_strings: Dict[str,str] = {}
    if cz_path and cz_path.exists():
        cz = read_json(cz_path)
        if "strings" in cz and isinstance(cz["strings"], dict):
            cz_strings = cz["strings"]

    # DŮLEŽITÉ: lowercase hlavička, aby to sedělo na wotr_tsv_gpt_sync_apply.py
    lines: List[str] = ["idx\tsource\ttranslation\treason"]
    reason_label = args.reason or f"short<={args.max_words}"

    total = 0
    picked = 0
    for idx in sorted(idx2guid.keys(), key=lambda s: int(s) if s.isdigit() else s):
        total += 1
        guid = idx2guid[idx]
        src_en = en_strings.get(guid, "")
        if not src_en:
            continue

        stripped = strip_game_tags(src_en)
        if args.min_letters > 0 and len(re.sub(r"\s+","", stripped)) < args.min_letters:
            continue
        if count_words(stripped) > args.max_words:
            continue

        cz_text = cz_strings.get(guid, "")

        if args.only_missing and cz_text.strip():
            continue
        if args.exclude_identical and cz_text.strip() == src_en.strip():
            continue

        src_cell = sanitize_tsv_cell(src_en)
        cz_cell  = sanitize_tsv_cell(cz_text)
        lines.append(f"{idx}{TSV}{src_cell}{TSV}{cz_cell}{TSV}{reason_label}")
        picked += 1

    write_text(out_path, "\n".join(lines) + "\n")
    print(f"[SHORT] scanned={total} picked={picked} max_words<={args.max_words} → {out_path}")

if __name__ == "__main__":
    main()
