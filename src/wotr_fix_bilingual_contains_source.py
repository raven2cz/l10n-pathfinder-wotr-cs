#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
wotr_fix_bilingual_contains_source.py
-------------------------------------

Fixes bilingual/contains_source/arrow issues and writes ONLY changed rows.

NEW:
  --only-reasons <list>   Filter input rows to process only selected reasons.
                          Works even if input has no 'reason' column:
                            - 'bilingual_arrow' → rows with arrow detected in text
                            - 'contains_source' → rows with strong EN overlap heuristic
                            - 'bilingual'       → rows with leading EN block heuristic
  (Comma-separated or repeatable: --only-reasons bilingual_arrow,contains_source)

Header handling:
- Autodetect headerless TSV (common for suspect.tsv):
  * 2 columns  -> idx + Translation
  * 3 columns  -> idx + Source + Translation
- Force with --no-header and/or --assume-cols idx-translation | idx-source-translation

CLI (compatible):
  -i/--in, -o/--out, --min-src-words (alias for --min-en-run)

Output columns: idx<TAB>Source<TAB>Translation<TAB>reason
"""

from __future__ import annotations
import argparse, csv, re, sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set

TSV = "\t"

def log(s: str) -> None:
    print(s, flush=True)

# --------- header autodetect ---------

def _peek_first_row(path: Path) -> List[str]:
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.reader(f, delimiter=TSV)
        try:
            row = next(r)
        except StopIteration:
            return []
        return row

def _looks_like_header(cells: List[str]) -> bool:
    if not cells:
        return False
    first = (cells[0] or "").strip()
    if first.lower() == "idx":
        return True
    return not first.isdigit()

def _read_rows_with_fieldnames(path: Path, fieldnames: List[str]) -> Tuple[List[str], List[Dict[str,str]]]:
    rows: List[Dict[str,str]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.reader(f, delimiter=TSV)
        for row in r:
            if not row:
                continue
            d = {}
            for i, name in enumerate(fieldnames):
                d[name] = row[i] if i < len(row) else ""
            rows.append(d)
    # pokud první řádek vypadal jako hlavička 'idx', odstraň jej
    if rows and (rows[0].get("idx","").lower() == "idx"):
        rows = rows[1:]
    return fieldnames, rows

def read_tsv_autodetect(path: Path, no_header: bool, assume_cols: Optional[str]) -> Tuple[List[str], List[Dict[str,str]]]:
    first = _peek_first_row(path)
    if assume_cols:
        if assume_cols == "idx-translation":
            fns = ["idx", "Translation"]
        elif assume_cols == "idx-source-translation":
            fns = ["idx", "Source", "Translation"]
        else:
            raise ValueError("Unknown --assume-cols")
        return _read_rows_with_fieldnames(path, fns)

    if no_header:
        if len(first) >= 3:
            fns = ["idx", "Source", "Translation"]
        elif len(first) == 2:
            fns = ["idx", "Translation"]
        else:
            fns = ["idx"]
        return _read_rows_with_fieldnames(path, fns)

    if _looks_like_header(first):
        with path.open("r", encoding="utf-8", newline="") as f:
            r = csv.DictReader(f, delimiter=TSV)
            fields = [c for c in (r.fieldnames or []) if c is not None]
            rows = []
            for row in r:
                if None in row:
                    row.pop(None, None)
                rows.append({k: v for k, v in row.items() if k in fields})
        return fields, rows
    else:
        if len(first) >= 3:
            fns = ["idx", "Source", "Translation"]
        elif len(first) == 2:
            fns = ["idx", "Translation"]
        else:
            fns = ["idx"]
        return _read_rows_with_fieldnames(path, fns)

def write_tsv(path: Path, rows: List[Dict[str,str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["idx", "Source", "Translation", "reason"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=TSV, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k,"") for k in fields})

# ---------- heuristics ----------

_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_ARROW = re.compile(r"\s->\s|→|⇒")

def tokenize_en(s: str) -> List[str]:
    return _WORD.findall(s or "")

def contains_arrow(s: str) -> bool:
    return bool(_ARROW.search(s or ""))

def strip_bilingual_arrow(s: str) -> Tuple[str, bool]:
    if not s:
        return s, False
    m = _ARROW.search(s)
    if not m:
        return s, False
    pos = m.end()
    new = s[pos:].lstrip()
    return (new, new != s)

def sanitize_for_tsv(s: str) -> str:
    return (s or "").replace("\t", "\\t").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")

def strong_contains_source(src_en_tokens: List[str], tr_text: str, min_overlap: int) -> bool:
    if not src_en_tokens or not tr_text:
        return False
    tr_tokens = tokenize_en(tr_text)
    if not tr_tokens:
        return False
    src_set = set(t.lower() for t in src_en_tokens if len(t) >= 2)
    overlap = sum(1 for t in tr_tokens if t.lower() in src_set)
    return overlap >= min_overlap

def drop_leading_english_lines(tr_text: str, min_en_run: int = 3) -> Tuple[str, bool]:
    if not tr_text:
        return tr_text, False
    logical = tr_text.replace("\r\n", "\n")
    parts = re.split(r"(?:\\n|\n)", logical)
    changed = False
    kept: List[str] = []
    drop = True
    for p in parts:
        en_count = len(tokenize_en(p))
        if drop and en_count >= min_en_run:
            changed = True
            continue
        drop = False
        kept.append(p)
    if not changed:
        return tr_text, False
    sep = "\\n" if "\\n" in tr_text else "\n"
    new = sep.join(kept).lstrip()
    return new, True

# ---------- fixer + reason tagging ----------

def fix_row(idx: str, src_text: str, tr_text: str, min_en_run: int, min_overlap: int) -> Tuple[str, bool, List[str]]:
    reasons: List[str] = []
    changed = False
    new = tr_text or ""

    # tag: bilingual_arrow (detected)
    arrow_present = contains_arrow(new)
    if arrow_present:
        new2, ch = strip_bilingual_arrow(new)
        if ch:
            new = new2
            changed = True
        reasons.append("bilingual_arrow")

    # tag: bilingual (leading EN drop)
    new2, ch = drop_leading_english_lines(new, min_en_run=min_en_run)
    if ch:
        new = new2
        changed = True
        reasons.append("bilingual")

    # tag: contains_source (heuristic overlap)
    src_tokens = tokenize_en(src_text)
    if strong_contains_source(src_tokens, new, min_overlap=min_overlap):
        reasons.append("contains_source")

    return new, changed, reasons

def should_process_row(reason_field_value: str,
                       detected_reasons: List[str],
                       only_reasons: Optional[Set[str]]) -> bool:
    if not only_reasons:
        return True
    # prefer explicit 'reason' column if provided
    if reason_field_value:
        rv = reason_field_value.lower()
        return any(token in rv for token in only_reasons)
    # otherwise fall back to detected reasons
    det = set(r.lower() for r in detected_reasons)
    return bool(det & only_reasons)

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Fix bilingual/contains_source and emit ONLY changed rows.")
    ap.add_argument("-i", "--in", dest="in_path", required=True)
    ap.add_argument("-o", "--out", dest="out_path", required=True)
    ap.add_argument("--no-header", action="store_true", help="Treat input as headerless (first row is data).")
    ap.add_argument("--assume-cols", choices=["idx-translation","idx-source-translation"], help="Force column meaning for headerless input.")
    ap.add_argument("--idx-col", default=None)
    ap.add_argument("--src-col", default=None)
    ap.add_argument("--tr-col",  default=None)
    ap.add_argument("--reason-col", default="reason", help="Name of input reason column if present (default: reason)")
    ap.add_argument("--reason", default="auto_bilingual_fix", help="Reason suffix written to output")
    ap.add_argument("--emit-unchanged", action="store_true")
    ap.add_argument("--min-en-run", type=int, default=3)
    ap.add_argument("--min-overlap", type=int, default=7)
    ap.add_argument("--min-src-words", type=int, default=None, help="Alias for --min-en-run")
    ap.add_argument("--only-reasons", nargs="+", help="Process ONLY rows whose reason matches these tokens (comma or space separated)."
                                                     " Supported aliases without input 'reason' column: bilingual_arrow, contains_source, bilingual")
    args = ap.parse_args()

    # normalize only-reasons set
    only_reasons: Optional[Set[str]] = None
    if args.only_reasons:
        tokens: List[str] = []
        for it in args.only_reasons:
            tokens.extend([t.strip().lower() for t in it.split(",") if t.strip()])
        only_reasons = set(tokens) if tokens else None

    if args.min_src_words is not None:
        args.min_en_run = args.min_src_words

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)

    fields, rows = read_tsv_autodetect(in_path, no_header=args.no_header, assume_cols=args.assume_cols)

    # resolve input columns
    idx_col = args.idx_col or ("idx" if "idx" in fields else None)
    # prefer explicit Source; else try 'source_escaped'; else empty
    src_col = args.src_col or ("Source" if "Source" in fields else ("source_escaped" if "source_escaped" in fields else None))
    tr_col  = args.tr_col  or ("Translation" if "Translation" in fields else ("translation_escaped" if "translation_escaped" in fields else None))
    in_reason_col = args.reason_col if args.reason_col in fields else None

    if idx_col is None or tr_col is None:
        log(f"ERROR: Cannot determine columns. Detected fields: {fields}")
        log("Hint: use --no-header and/or --assume-cols idx-translation")
        sys.exit(2)

    total = len(rows)
    changed_rows: List[Dict[str,str]] = []
    kept_unchanged = 0
    changed_count = 0
    skipped_incomplete = 0
    skipped_filtered = 0

    for r in rows:
        idx = (r.get(idx_col) or "").strip()
        if not idx:
            skipped_incomplete += 1
            continue
        tr  = r.get(tr_col) or ""
        src = r.get(src_col) or ""
        in_reason_val = (r.get(in_reason_col) or "") if in_reason_col else ""

        new_tr, changed, detected = fix_row(idx, src, tr, args.min_en_run, args.min_overlap)

        # filter by only-reasons (either from input or detected)
        if not should_process_row(in_reason_val, detected, only_reasons):
            skipped_filtered += 1
            continue

        if changed:
            changed_count += 1
            changed_rows.append({
                "idx": idx,
                "Source": sanitize_for_tsv(tr),          # původní hodnota překladu pro diff
                "Translation": sanitize_for_tsv(new_tr), # opravená
                "reason": f"{args.reason}+{'/'.join(detected) if detected else 'unknown'}"
            })
        else:
            kept_unchanged += 1
            if args.emit_unchanged:
                changed_rows.append({
                    "idx": idx,
                    "Source": sanitize_for_tsv(tr),
                    "Translation": sanitize_for_tsv(tr),
                    "reason": "unchanged"
                })

    write_tsv(out_path, changed_rows)
    log(f"[FIX] total={total} changed={changed_count} unchanged={kept_unchanged} "
        f"skipped_filtered={skipped_filtered} skipped_incomplete={skipped_incomplete} → {out_path}")

if __name__ == "__main__":
    main()
