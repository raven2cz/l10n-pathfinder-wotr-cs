#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_prepare_json_requests.py
-----------------------------

Preprocess TSV -> JSON requests, one JSON per row, plus NDJSON and manifest.
- Input TSV must contain 'idx' and a source column (default: 'source_escaped').
- Cleans stray extra CSV columns (DictReader's None keys).
- Sanitizes and validates minimal shape.
- Writes:
    <out-dir>/
      idx_<ID>.json               # one JSON request per row
      requests.ndjson             # all requests as NDJSON (one JSON per line)
      manifest.json               # counts, fields, sample, etc.

Usage (PowerShell):
  python .\\wotr_prepare_json_requests.py `
    --in .\\out_wotr\\fix\\multiline_src.tsv `
    --out-dir .\\out_wotr\\fix\\requests_json `
    --source-col source_escaped `
    --only-preprocess

You can restrict rows:
  --only-ids 101,202,303
  --limit 200 --offset 0
"""

from __future__ import annotations
import argparse, csv, json, sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set

TSV_SEP = "\t"

def log(msg: str) -> None:
    print(msg, flush=True)

def read_tsv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    """Read TSV safely; remove stray None-keyed columns; keep only known fields."""
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=TSV_SEP)
        fields = [c for c in (r.fieldnames or []) if c is not None]
        rows: List[Dict[str, str]] = []
        for row in r:
            if None in row:
                row.pop(None, None)
            # keep only known columns
            row = {k: v for k, v in row.items() if k in fields}
            rows.append(row)
    return fields, rows

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def parse_id_list(s: Optional[str]) -> Optional[Set[str]]:
    if not s:
        return None
    return {x.strip() for x in s.split(",") if x.strip()}

def filter_rows(rows: List[Dict[str, str]],
                only_ids: Optional[Set[str]],
                limit: Optional[int],
                offset: int) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if only_ids:
        rows = [r for r in rows if (r.get("idx") or "").strip() in only_ids]
    if offset:
        rows = rows[offset:]
    if limit is not None and limit >= 0:
        rows = rows[:limit]
    out = rows
    return out

def zero_pad_id(idx: str, pad: int) -> str:
    """If idx is an integer-like string, pad with zeros for filenames; otherwise return as-is."""
    s = idx.strip()
    if s.isdigit():
        return s.zfill(pad)
    return s

def build_request(idx: str, source_escaped: str) -> Dict[str, str]:
    """
    Minimal request JSON we will feed into the translation step later.
    Keeping the structure simple and explicit.
    """
    return {
        "idx": idx,
        "source_escaped": source_escaped
    }

def write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def append_ndjson_line(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def main():
    ap = argparse.ArgumentParser(description="Preprocess TSV -> JSON requests (one file per row, plus NDJSON & manifest).")
    ap.add_argument("--in", dest="in_path", required=True, help="Input TSV path")
    ap.add_argument("--out-dir", required=True, help="Output directory for JSON requests")
    ap.add_argument("--source-col", default="source_escaped", help="Source column name in TSV (default: source_escaped)")
    ap.add_argument("--id-pad", type=int, default=6, help="Zero-padding for numeric idx in filenames (default: 6)")
    ap.add_argument("--only-ids", default=None, help="Comma-separated list of idx to include")
    ap.add_argument("--limit", type=int, default=None, help="Max number of rows to export")
    ap.add_argument("--offset", type=int, default=0, help="Skip first N rows before exporting")
    ap.add_argument("--only-preprocess", action="store_true", help="Run only TSV->JSON preparation and exit")
    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_dir = Path(args.out_dir)
    ensure_dir(out_dir)

    fields, rows = read_tsv(in_path)
    if "idx" not in fields:
        log("ERROR: Input TSV must contain 'idx' column.")
        sys.exit(2)
    if args.source_col not in fields:
        log(f"ERROR: Input TSV must contain '{args.source_col}' column.")
        sys.exit(2)

    only_ids = parse_id_list(args.only_ids)
    rows = filter_rows(rows, only_ids, args.limit, args.offset)

    # Prepare outputs
    ndjson_path   = out_dir / "requests.ndjson"
    manifest_path = out_dir / "manifest.json"

    # Truncate NDJSON if exists (fresh run)
    if ndjson_path.exists():
        ndjson_path.unlink()

    exported = 0
    samples: List[dict] = []

    for r in rows:
        idx = (r.get("idx") or "").strip()
        src = r.get(args.source_col) or ""
        if not idx or not src:
            # skip incomplete rows rather than fail
            continue

        req = build_request(idx, src)

        # Per-row JSON file, zero-padded numeric idx for filename consistency
        file_idx = zero_pad_id(idx, args.id_pad)
        out_file = out_dir / f"idx_{file_idx}.json"
        write_json(out_file, req)

        # NDJSON append
        append_ndjson_line(ndjson_path, req)

        # sample few examples for manifest
        if exported < 5:
            samples.append(req)

        exported += 1

    manifest = {
        "input_tsv": str(in_path),
        "output_dir": str(out_dir),
        "fields": fields,
        "source_col": args.source_col,
        "count_exported": exported,
        "ndjson": str(ndjson_path),
        "examples": samples,
    }
    write_json(manifest_path, manifest)

    log(f"[OK] exported={exported} → {out_dir}")
    log(f"      ndjson:  {ndjson_path}")
    log(f"      manifest:{manifest_path}")

    if args.only_preprocess:
        # The caller only wants JSON preparation.
        return

    # If you later want to chain translation in the same script, you can do it here.
    # For now, we exit after preprocessing to keep responsibilities separated.
    log("[INFO] only-preprocess not set, but this script currently implements only the preprocessing step.")
    log("[INFO] Add your translation step here if you want to extend this script in the future.")

if __name__ == "__main__":
    main()
