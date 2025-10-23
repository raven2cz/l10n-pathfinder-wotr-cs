#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_debug_batch_analyzer.py
============================

Co dělá
-------
- Projde adresář s debugy (např. out_wotr\fix\debug_sync).
- Načte pro každou dávku:
    * batch_XXX.req.txt  → ID požadovaných řádků (první sloupec před TAB).
    * batch_XXX.resp.txt → text odpovědi (nebo když chybí, pokusí se vytáhnout text z batch_XXX.resp.json).
- Pokusí se **vyparsovat "idx<TAB>translation"**. Kromě striktního TAB parseru zkusí i tolerantní varianty:
    * "idx : translation" (dvojtečka),
    * "idx - translation" (pomlčka),
    * "idx    translation" (více mezer),
    * "idx | translation" (svislítko),
  a automaticky **překlopí na TAB-y**.
- U každé dávky uloží:
    * batch_XXX.parsed_strict.tsv (co striktně prošlo jen přes TAB),
    * batch_XXX.parsed_fixed.tsv  (nejlepší nalezený formát převedený na TAB),
    * batch_XXX.parse_diag.txt    (diagnostika – počty shod pro jednotlivé vzory),
    * batch_XXX.idx_overlap.txt   (jaká ID se protnula s požadavkem),
- Na konci vygeneruje globální:
    * _analyze_summary.txt (souhrn),
    * ALL_fixed.tsv (sloučená mapa idx→translation přes všechny dávky; poslední výskyt vyhrává).

Použití
-------
python wotr_debug_batch_analyzer.py ^
  --debug-dir .\out_wotr\fix\debug_sync ^
  --write-fixed

Poté můžeš ALL_fixed.tsv rovnou aplikovat na svůj TSV zdroj bez volání API:
python wotr_tsv_gpt_sync_apply.py ^
  --in .\out_wotr\fix\multiline_src.tsv ^
  --out .\out_wotr\fix\multiline_src_translated.tsv ^
  --apply-from-resp .\out_wotr\fix\debug_sync\ALL_fixed.tsv ^
  --source-col source_escaped ^
  --output-col translation_escaped ^
  --debug-dir .\out_wotr\fix\debug_sync
"""

from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# --- util ---

def log(msg: str) -> None:
    from time import strftime
    print(f"[{strftime('%H:%M:%S')}] {msg}", flush=True)

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

def write_text(p: Path, txt: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(txt, encoding="utf-8", newline="\n")

def write_json(p: Path, obj) -> None:
    write_text(p, json.dumps(obj, ensure_ascii=False, indent=2))

# --- extrakce textu z resp.json, kdyby chyběl resp.txt ---

def extract_text_from_response_json(js: dict) -> str:
    # responses-like:
    if isinstance(js, dict) and "output_text" in js and isinstance(js["output_text"], str):
        return js["output_text"]
    if isinstance(js, dict) and "output" in js and isinstance(js["output"], list) and js["output"]:
        first = js["output"][0]
        if isinstance(first, dict) and "content" in first and isinstance(first["content"], list):
            for piece in first["content"]:
                if isinstance(piece, dict) and isinstance(piece.get("text"), str):
                    return piece["text"]
    # chat-like:
    if isinstance(js, dict) and "choices" in js and isinstance(js["choices"], list) and js["choices"]:
        ch = js["choices"][0]
        try:
            return ch["message"]["content"]
        except Exception:
            pass
    # fallback:
    if isinstance(js, dict) and isinstance(js.get("content"), str):
        return js["content"]
    return ""

# --- parsování ---

FENCE_RX = re.compile(r"^\s*```.*?$|^\s*```$", re.MULTILINE)

# striktní TSV (TAB)
RX_TAB      = re.compile(r"(?m)^\s*(\d+)\t(.*)$")
# tolerantní alternativy (překlopíme na TAB)
RX_COLON    = re.compile(r"(?m)^\s*(\d+)\s*:\s*(.+)$")
RX_MINUS    = re.compile(r"(?m)^\s*(\d+)\s*-\s*(.+)$")
RX_SPACES   = re.compile(r"(?m)^\s*(\d+)\s{2,}(.+)$")
RX_PIPE     = re.compile(r"(?m)^\s*(\d+)\s*\|\s*(.+)$")

ALT_PATTERNS = [
    ("colon", RX_COLON),
    ("minus", RX_MINUS),
    ("spaces", RX_SPACES),
    ("pipe",  RX_PIPE),
]

def parse_pairs_best(resp_text: str) -> Tuple[Dict[str, str], Dict[str, int], str]:
    """
    Vrátí:
      - pairs: dict idx->translation (normalizované s TAB)
      - stats: kolik řádků matchlo pro který vzor
      - chosen: název použitého vzoru ("tab"/"colon"/"minus"/"spaces"/"pipe"/"none")
    """
    pairs: Dict[str, str] = {}
    stats = {"tab": 0, "colon": 0, "minus": 0, "spaces": 0, "pipe": 0}
    chosen = "none"

    if not resp_text:
        return pairs, stats, chosen

    txt = FENCE_RX.sub("", resp_text)

    # 1) striktní TAB
    for m in RX_TAB.finditer(txt):
        idx, tr = m.group(1).strip(), m.group(2).rstrip()
        pairs[idx] = tr
    stats["tab"] = len(pairs)
    if stats["tab"] > 0:
        chosen = "tab"
        return pairs, stats, chosen

    # 2) zkus alternativy a vyber nejlepší
    best_name = None
    best_count = 0
    best_items: List[Tuple[str, str]] = []

    for name, rx in ALT_PATTERNS:
        items = [(m.group(1).strip(), m.group(2).rstrip()) for m in rx.finditer(txt)]
        count = len(items)
        stats[name] = count
        if count > best_count:
            best_count = count
            best_name = name
            best_items = items

    if best_count > 0 and best_name:
        chosen = best_name
        pairs = {idx: tr for idx, tr in best_items}
        return pairs, stats, chosen

    # 3) nic
    return {}, stats, "none"

# --- čtení požadovaných ID z req.txt ---

def parse_req_ids(req_text: str) -> List[str]:
    ids: List[str] = []
    for line in req_text.splitlines():
        if "\t" not in line:
            continue
        idx = line.split("\t", 1)[0].strip()
        if idx.isdigit():
            ids.append(idx)
    return ids

# --- main ---

def main():
    ap = argparse.ArgumentParser(description="Analyze GPT debug batches, find why applied=0, and optionally write fixed TSVs.")
    ap.add_argument("--debug-dir", required=True, help="Adresář s batch_XXX.* soubory.")
    ap.add_argument("--write-fixed", action="store_true", help="Zapiš batch_XXX.parsed_fixed.tsv a ALL_fixed.tsv.")
    ap.add_argument("--limit", type=int, default=0, help="Omez počet analyzovaných dávek (0=bez limitu).")
    args = ap.parse_args()

    dbg = Path(args.debug_dir)
    if not dbg.exists():
        raise SystemExit(f"Neexistuje: {dbg}")

    # najdi dávky podle req.txt
    req_files = sorted(dbg.glob("batch_*.req.txt"))
    if not req_files:
        raise SystemExit("Nenašel jsem žádné batch_*.req.txt soubory.")

    summary_lines = []
    all_fixed_pairs: Dict[str, str] = {}

    total_req = 0
    total_parsed = 0
    total_overlap = 0
    zero_overlap_batches = 0

    for i, req_path in enumerate(req_files, 1):
        if args.limit > 0 and i > args.limit: break

        stem = req_path.stem  # batch_XXX.req
        batch_id = stem.replace(".req", "")
        resp_txt_path = dbg / f"{batch_id}.resp.txt"
        resp_json_path = dbg / f"{batch_id}.resp.json"
        parsed_strict_path = dbg / f"{batch_id}.parsed_strict.tsv"
        parsed_fixed_path  = dbg / f"{batch_id}.parsed_fixed.tsv"
        parse_diag_path    = dbg / f"{batch_id}.parse_diag.txt"
        overlap_path       = dbg / f"{batch_id}.idx_overlap.txt"

        req_text = read_text(req_path)
        ids_in_req = parse_req_ids(req_text)
        total_req += len(ids_in_req)

        resp_text = read_text(resp_txt_path)
        if not resp_text and resp_json_path.exists():
            # zkus vytáhnout z json
            try:
                js = json.loads(read_text(resp_json_path))
            except Exception:
                js = {}
            resp_text = extract_text_from_response_json(js)

        pairs, stats, chosen = parse_pairs_best(resp_text)
        parsed_ids = list(pairs.keys())
        overlap = [x for x in ids_in_req if x in pairs]

        total_parsed += len(parsed_ids)
        total_overlap += len(overlap)
        if len(overlap) == 0:
            zero_overlap_batches += 1

        # uložit diagnostiku
        diag = []
        diag.append(f"batch: {batch_id}")
        diag.append(f"req_lines: {len(ids_in_req)}")
        diag.append(f"resp_present: {bool(resp_text)}")
        diag.append(f"parsed_tab: {stats['tab']}")
        diag.append(f"parsed_colon: {stats['colon']}")
        diag.append(f"parsed_minus: {stats['minus']}")
        diag.append(f"parsed_spaces: {stats['spaces']}")
        diag.append(f"parsed_pipe: {stats['pipe']}")
        diag.append(f"chosen: {chosen}")
        diag.append(f"overlap: {len(overlap)}")
        diag.append("")
        diag.append("sample_parsed (max 5):")
        for k in parsed_ids[:5]:
            prev = pairs[k]
            show = prev if len(prev) <= 100 else (prev[:97] + "…")
            diag.append(f"{k}\t{show}")
        write_text(parse_diag_path, "\n".join(diag))

        write_text(overlap_path, "\n".join(overlap))

        # ulož parsed_strict.tsv (jen co prošlo přes TAB)
        if stats["tab"] > 0:
            # striktní znovu projedu, abych v souboru měl opravdu jen TAB varianty
            strict_pairs = {}
            for m in RX_TAB.finditer(FENCE_RX.sub("", resp_text or "")):
                strict_pairs[m.group(1).strip()] = m.group(2).rstrip()
            if strict_pairs:
                write_text(parsed_strict_path, "\n".join(f"{k}\t{v}" for k, v in strict_pairs.items()))
        else:
            # prázdný soubor pro přehled
            write_text(parsed_strict_path, "")

        # ulož parsed_fixed.tsv – normalizovaný výsledek (cokoli nejlepšího převedené na TAB)
        if pairs:
            write_text(parsed_fixed_path, "\n".join(f"{k}\t{v}" for k, v in pairs.items()))
            # agreguj globální
            for k, v in pairs.items():
                all_fixed_pairs[k] = v
        else:
            write_text(parsed_fixed_path, "")

        summary_lines.append(
            f"{batch_id}\treq={len(ids_in_req)}\tparsed_total={len(parsed_ids)}\toverlap={len(overlap)}\tchosen={chosen}"
        )
        log(f"[{batch_id}] req={len(ids_in_req)} parsed={len(parsed_ids)} overlap={len(overlap)} chosen={chosen}")

    # globální souhrn
    summary_txt = []
    summary_txt.append("=== BATCH SUMMARY ===")
    summary_txt.extend(summary_lines)
    summary_txt.append("")
    summary_txt.append("=== TOTALS ===")
    summary_txt.append(f"total_req_lines: {total_req}")
    summary_txt.append(f"total_parsed_lines: {total_parsed}")
    summary_txt.append(f"total_overlap: {total_overlap}")
    summary_txt.append(f"zero_overlap_batches: {zero_overlap_batches}")
    write_text(Path(args.debug_dir) / "_analyze_summary.txt", "\n".join(summary_txt))
    log(f"[SUMMARY] → {Path(args.debug_dir) / '_analyze_summary.txt'}")

    if args.write_fixed:
        all_path = Path(args.debug_dir) / "ALL_fixed.tsv"
        if all_fixed_pairs:
            write_text(all_path, "\n".join(f"{k}\t{v}" for k, v in all_fixed_pairs.items()))
            log(f"[FIXED] merged {len(all_fixed_pairs)} line(s) → {all_path}")
        else:
            log("[FIXED] nic k zápisu – nepodařilo se nic parse-nout.")

if __name__ == "__main__":
    main()
