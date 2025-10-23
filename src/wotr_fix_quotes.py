#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_fix_quotes.py
------------------
Upraví uvozovky v češtině podle anglického originálu:

Pravidla
- Pokud anglický EN řetězec začíná i končí uvozovkami ( "..." nebo “…” ),
  a český CZ je na začátku/konci nemá, CZ se obalí rovnými uvozovkami: "…".
- Pokud CZ používá české uvozovky „…“, převede se na "…".
- Pokud CZ už má "…" (nebo “…”), ponechá se tak (nepřidávají se další uvozovky).
- Pracuje po GUID; map.json je nepovinný pro report (idx).

Použití (PowerShell):
  python wotr_fix_quotes.py `
    --en .\enGB.json `
    --cz .\out_wotr\csCZ.json `
    --out .\out_wotr\csCZ-quotes.json `
    --report-tsv .\out_wotr\reports\quotes_fix.tsv `
    --map .\out_wotr\map.json

Přepínače:
  --en           enGB.json (obsahuje "strings")
  --cz           vstupní csCZ.json (obsahuje "strings")
  --out          výstupní csCZ.json (patched)
  --map          (volitelné) map.json (idx->GUID) pro hezčí report
  --only-when-en-quoted   Upravovat jen tehdy, když EN je v uvozovkách (default: on)
  --allow-smart           Ponechat “chytré” EN uvozovky v CZ (default: off → vždy rovné ")
  --dry-run               Nezapisovat, jen vypsat statistiky
  --backup                Uložit .bak staré CZ před zápisem
"""

from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from typing import Dict, Tuple

# páry uvozovek (otevírací, zavírací)
STRAIGHT = ('"', '"')
EN_SMART = ('\u201C', '\u201D')  # “ ”
CZ_PAIR = ('\u201E', '\u201C')   # „ “

def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))

def write_json_atomic(p: Path, obj: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)

def has_wrapping(s: str, pair: Tuple[str,str]) -> bool:
    if not s: return False
    s = s.strip()
    return s.startswith(pair[0]) and s.endswith(pair[1]) and len(s) >= 2

def has_any_quotes(s: str) -> bool:
    t = (s or "").strip()
    return (
        has_wrapping(t, STRAIGHT) or
        has_wrapping(t, EN_SMART) or
        has_wrapping(t, CZ_PAIR)
    )

def wrap_straight(s: str) -> str:
    # obalí celý řetězec rovnými uvozovkami
    return f"\"{s}\""

def convert_cz_to_straight(s: str) -> str:
    t = s.strip()
    if has_wrapping(t, CZ_PAIR):
        inner = t[1:-1]
        return f"\"{inner}\""
    return s

def convert_smart_to_straight(s: str) -> str:
    t = s.strip()
    if has_wrapping(t, EN_SMART):
        inner = t[1:-1]
        return f"\"{inner}\""
    return s

def log(msg: str) -> None:
    print(msg, flush=True)

def main():
    ap = argparse.ArgumentParser(description="Fix CZ quotes based on EN; convert „…“ to \"…\"; optionally add quotes if EN has them.")
    ap.add_argument("--en", required=True, help="enGB.json")
    ap.add_argument("--cz", required=True, help="input csCZ.json")
    ap.add_argument("--out", required=True, help="output csCZ.json (patched)")
    ap.add_argument("--map", default=None, help="(optional) map.json (idx->GUID) for report")
    ap.add_argument("--report-tsv", default=None, help="(optional) TSV report of changes")
    ap.add_argument("--only-when-en-quoted", action="store_true", default=True,
                    help="Modify CZ only if EN has wrapping quotes (default: True)")
    ap.add_argument("--allow-smart", action="store_true", default=False,
                    help="If set, keep smart quotes “…” in CZ; otherwise convert to straight \"…\"")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backup", action="store_true")
    args = ap.parse_args()

    en = read_json(Path(args.en))
    cz = read_json(Path(args.cz))
    if "strings" not in en or "strings" not in cz:
        raise SystemExit("ERROR: EN/CZ JSON must contain 'strings' object.")
    en_str: Dict[str,str] = en["strings"]
    cz_str: Dict[str,str] = cz["strings"]

    idx2guid: Dict[str,str] = {}
    guid2idx: Dict[str,str] = {}
    if args.map:
        try:
            idx2guid = read_json(Path(args.map))
            guid2idx = {v:k for k,v in idx2guid.items()}
        except Exception:
            guid2idx = {}

    changes = []
    stats = {
        "total_checked": 0,
        "converted_cz_to_straight": 0,
        "added_quotes_because_en_had": 0,
        "kept_existing_quotes": 0,
        "kept_unquoted": 0,
        "converted_smart_to_straight": 0
    }

    for guid, cz_text in cz_str.items():
        en_text = en_str.get(guid, "")
        stats["total_checked"] += 1

        cz_new = cz_text
        changed = False
        reason = ""

        # 1) české „…“ → rovné "…"
        if has_wrapping(cz_text.strip(), CZ_PAIR):
            cz_new = convert_cz_to_straight(cz_new)
            changed = True
            reason = "cz_czech_quotes_to_straight"
            stats["converted_cz_to_straight"] += 1

        # 2) případně i “…” → "…", pokud není povoleno ponechat smart
        if not args.allow_smart and has_wrapping(cz_new.strip(), EN_SMART):
            cz_new = convert_smart_to_straight(cz_new)
            if not changed:
                reason = "cz_smart_quotes_to_straight"
            else:
                reason += "+smart_to_straight"
            changed = True
            stats["converted_smart_to_straight"] += 1

        # 3) když EN má uvozovky, CZ je nemá → obal CZ rovnými
        en_quoted = has_wrapping(en_text.strip(), STRAIGHT) or has_wrapping(en_text.strip(), EN_SMART)
        cz_has_any = has_any_quotes(cz_new)

        if (not cz_has_any) and (en_quoted or not args.only_when_en_quoted):
            # buď EN má uvozovky (default), nebo jsme výslovně řekli "obaluj i bez EN uvozovek"
            cz_new = wrap_straight(cz_new)
            if not changed:
                reason = "added_quotes_because_en_had" if en_quoted else "added_quotes_unconditional"
            else:
                reason += "+added_quotes"
            changed = True
            stats["added_quotes_because_en_had"] += 1

        if not changed:
            # metrika "pro informaci"
            if cz_has_any:
                stats["kept_existing_quotes"] += 1
            else:
                stats["kept_unquoted"] += 1
            continue

        if cz_new != cz_text:
            cz_str[guid] = cz_new
            idx = guid2idx.get(guid, "")
            changes.append((idx, guid, cz_text, cz_new, reason))

    # report
    if args.report_tsv:
        lines = ["idx\tguid\treason\told\tnew"]
        for idx, guid, old, new, reason in changes:
            # v reportu escapuj taby/nové řádky
            def esc(x: str) -> str:
                return (x or "").replace("\t","\\t").replace("\n","\\n")
            lines.append(f"{idx}\t{guid}\t{reason}\t{esc(old)}\t{esc(new)}")
        Path(args.report_tsv).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_tsv).write_text("\n".join(lines) + "\n", encoding="utf-8")

    log(f"[QUOTES] checked={stats['total_checked']} | "
        f"cz->straight={stats['converted_cz_to_straight']} | "
        f"smart->straight={stats['converted_smart_to_straight']} | "
        f"added={stats['added_quotes_because_en_had']} | "
        f"kept_existing={stats['kept_existing_quotes']} | kept_unquoted={stats['kept_unquoted']} | "
        f"changes={len(changes)}")

    if args.dry_run:
        log("[DRY-RUN] No file written.")
        return

    out_path = Path(args.out)
    if args.backup and Path(args.cz).exists() and Path(args.cz) == out_path:
        bak = out_path.with_suffix(out_path.suffix + ".bak")
        bak.write_text(Path(args.cz).read_text(encoding="utf-8"), encoding="utf-8")
        log(f"[BAK] {bak}")

    write_json_atomic(out_path, cz)
    log(f"[OUT] {out_path} (changes: {len(changes)})")

if __name__ == "__main__":
    main()
