#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_verify_shortlabel_anomalies.py
-----------------------------------
Porovnává enGB.json (EN) a csCZ.json (CZ) přes GUID klíče a generuje auditní TSV
ve formátu: idx/Guid <TAB> Source <TAB> Translation <TAB> reason.

Režimy:
  --mode longer    : EN má <= N slov (default 3) a CZ má >= M slov (default 7; tj. >6)
  --mode identical : EN má <= N slov a CZ == EN (pro ruční průchod)

Volitelné:
  - map.json (idx -> GUID). Je-li zadán, první sloupec bude `idx`, jinak GUID.
"""

from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

TSV_SEP = "\t"
WORD_RE = re.compile(r"[0-9A-Za-zÀ-ž]+", re.UNICODE)

def tokens(s: str) -> List[str]:
    return [m.group(0) for m in WORD_RE.finditer(s or "")]

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

def load_strings(json_path: Path) -> Dict[str, str]:
    j = read_json(json_path)
    if "strings" not in j or not isinstance(j["strings"], dict):
        raise ValueError(f"{json_path} neobsahuje objekt 'strings'.")
    out: Dict[str, str] = {}
    for k, v in j["strings"].items():
        out[str(k)] = v if isinstance(v, str) else ""
    return out  # GUID -> text

def load_guid2idx(map_path: Optional[Path]) -> Dict[str, str]:
    if not map_path:
        return {}
    m = read_json(map_path)
    if not isinstance(m, dict):
        raise ValueError("map.json má nečekaný formát (očekáván objekt idx->GUID).")
    guid2idx: Dict[str, str] = {}
    for idx, guid in m.items():
        guid2idx[str(guid)] = str(idx)
    return guid2idx

def main():
    ap = argparse.ArgumentParser(description="Audit krátkých EN labelů vůči finálním CZ překladům.")
    ap.add_argument("-e", "--en", required=True, help="enGB.json (obsahuje 'strings')")
    ap.add_argument("-c", "--cs", required=True, help="csCZ.json (obsahuje 'strings')")
    ap.add_argument("-o", "--output", required=True, help="Výstupní TSV (např. out_wotr/audit/error.tsv)")
    ap.add_argument("-m", "--map", help="Volitelné: out_dir/map.json (idx -> GUID) pro 1. sloupec jako idx")
    ap.add_argument("--mode", choices=["longer","identical"], default="longer",
                    help="Typ auditu: 'longer' (EN≤N a CZ≥M) nebo 'identical' (EN≤N a CZ==EN).")
    ap.add_argument("--max-src-words", type=int, default=3, help="N: max slov v EN pro zahrnutí (default 3)")
    ap.add_argument("--min-tr-words", type=int, default=7, help="M: min slov v CZ pro režim 'longer' (default 7 => >6)")
    ap.add_argument("--case-insensitive", action="store_true",
                    help="U 'identical' porovnávat bez rozlišení velikosti písmen")
    args = ap.parse_args()

    en_path = Path(args.en)
    cs_path = Path(args.cs)
    out_path = Path(args.output)
    map_path = Path(args.map) if args.map else None

    en = load_strings(en_path)   # GUID -> EN
    cs = load_strings(cs_path)   # GUID -> CZ
    guid2idx = load_guid2idx(map_path)

    rows_out: List[str] = []
    flagged = 0
    missing_cs = 0

    for guid, src in en.items():
        tr = cs.get(guid, "")
        if not tr:
            missing_cs += 1
            continue

        src_words = len(tokens(src))
        if src_words <= args.max_src_words:
            if args.mode == "longer":
                tr_words = len(tokens(tr))
                if tr_words >= args.min_tr_words:
                    # doplň i pár metrik pro snazší optickou kontrolu
                    punct = sum(tr.count(ch) for ch in ".!?:;")
                    commas = tr.count(",")
                    ratio = (len(tr) / max(1, len(src))) if src else 0.0
                    reason = f"short_src_long_tr: src_words={src_words}, tr_words={tr_words}, len_ratio={ratio:.2f}"
                    extras = []
                    if punct > 0: extras.append(f"sent_punct={punct}")
                    if commas >= 2: extras.append(f"commas={commas}")
                    if extras: reason += ";" + ",".join(extras)
                    key = guid2idx.get(guid, guid)
                    rows_out.append(TSV_SEP.join([key, src, tr, reason]))
                    flagged += 1
            else:  # identical
                s = src.strip()
                t = tr.strip()
                cond = s.casefold() == t.casefold() if args.case_insensitive else (s == t)
                if cond:
                    key = guid2idx.get(guid, guid)
                    rows_out.append(TSV_SEP.join([key, src, tr, "identical_short_label"]))
                    flagged += 1

    write_safely(out_path, ("\n".join(rows_out) + ("\n" if rows_out else "")))
    mode_note = ("EN≤{} & CZ≥{}".format(args.max_src_words, args.min_tr_words)
                 if args.mode=="longer"
                 else "EN≤{} & CZ==EN{}".format(args.max_src_words,
                     " (ci)" if args.case_insensitive else ""))
    print(f"[VERIFY] mode={args.mode} [{mode_note}] | flagged={flagged} | missing_cs={missing_cs} | → {out_path}")

if __name__ == "__main__":
    main()
