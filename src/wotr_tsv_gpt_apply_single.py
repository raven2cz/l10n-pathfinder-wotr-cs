#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_tsv_gpt_apply_single.py
----------------------------

"1 řádek = 1 request" aplikátor, navržený pro ENTER/multiline texty.
- Žádné 'temperature' (kvůli gpt-5-mini).
- Primárně Chat Completions s function-calling (tools) => vrací striktní JSON.
- Fallback: prostý chat bez tools, ale v promptu vynucení "vrať POUZE JSON".
- Průběžný zápis TSV po každém řádku (safe tmp).
- Detailní logování a těla 400 chyb do logu.

Použití (typické):
  python .\\wotr_tsv_gpt_apply_single.py ^
    --in .\\out_wotr\\fix\\multiline_src.tsv ^
    --out .\\out_wotr\\fix\\multiline_src_translated.tsv ^
    --prompts .\\prompts-multiline.json ^
    --model gpt-5-mini ^
    --source-col source_escaped ^
    --output-col translation_escaped ^
    --retries 8 ^
    --sleep 0.2 ^
    --debug-dir .\\out_wotr\\fix\\debug_single ^
    --resume
"""
from __future__ import annotations
import os, sys, csv, json, time, argparse, random
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import requests

TSV_SEP = "\t"

def log(msg: str) -> None:
    print(msg, flush=True)

# ---------- I/O helpers ----------

def read_json(p: Path):
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)

def read_tsv(path: Path) -> Tuple[List[str], List[Dict[str,str]]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=TSV_SEP)
        fields = [c for c in (r.fieldnames or []) if c is not None]
        rows: List[Dict[str,str]] = []
        for row in r:
            # odstranění "extra sloupců" zachycených pod klíčem None
            if None in row:
                row.pop(None, None)
            # pouze známé fieldy
            row = {k: v for k, v in row.items() if k in fields}
            rows.append(row)
    return fields, rows

def write_tsv(path: Path, fields: List[str], rows: List[Dict[str,str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=fields,
            delimiter=TSV_SEP,
            lineterminator="\n",
            extrasaction="ignore",
        )
        w.writeheader()
        for r in rows:
            if None in r:
                r = {k: v for k in r.items() if k is not None}
            w.writerow(r)
    tmp.replace(path)

# ---------- prompts ----------

def load_prompts(prompts_path: Path) -> Tuple[str,str]:
    j = read_json(prompts_path)
    return j.get("system_rules",""), j.get("user_header","")

def build_user_prompt_single(user_header: str, idx: str, source_escaped: str) -> str:
    """
    Zadání pro 1 řádek → vynucení JSONu.
    Důležité: \\n v source_escaped jsou LITERÁLY, a musí zůstat i v překladu jako doslovné '\\n'.
    """
    return (
        (user_header or "").strip() + "\n\n"
        "INPUT:\n"
        f"idx: {idx}\n"
        f"source_escaped: {source_escaped}\n\n"
        "OUTPUT:\n"
        "Vrať POUZE validní JSON objekt přesně tohoto tvaru (bez kódových bloků a textu navíc):\n"
        "{\n"
        '  "idx": "<stejné číslo jako vstup>",\n'
        '  "translation_escaped": "<překlad do češtiny; zachovej {g|...}...{/g}; ostatní {...} přelož UVNITŘ a ponech závorky; všechny odstavce zachovej jako doslovné \\\\n> > > v textu>"\n'
        "}\n"
    )

# ---------- OpenAI call (Chat Completions + tools) ----------

def _post_json(url: str, headers: Dict[str,str], payload: dict, timeout_s: int) -> dict:
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=(30, timeout_s))
    if r.status_code != 200:
        # Předej celý text chyby – užitečné pro ladění 400
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:4000]}")
    return r.json()

def call_openai_json(base_url: str, api_key: str, model: str,
                     system_rules: str, user_prompt: str, timeout_s: int) -> Dict[str, str]:
    """
    Chat Completions s function-calling (tools) → JSON v 'arguments'.
    ŽÁDNÝ 'temperature' klíč (kvůli gpt-5-mini).
    Fallback: čistý chat bez tools s explicitním 'vrať POUZE JSON' v promptu.
    """
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }

    messages = [
        {"role": "system", "content": system_rules},
        {"role": "user",   "content": user_prompt},
    ]

    tools = [{
        "type": "function",
        "function": {
            "name": "store_translation",
            "description": "Return strict JSON with {idx, translation_escaped}. Keep \\n escaped as \\n.",
            "parameters": {
                "type": "object",
                "properties": {
                    "idx": {"type": "string"},
                    "translation_escaped": {"type": "string"},
                },
                "required": ["idx", "translation_escaped"],
                "additionalProperties": False
            }
        }
    }]

    # A) Přes tools (auto)
    payload_a = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto"
    }
    try:
        data = _post_json(url, headers, payload_a, timeout_s)
        choice = (data.get("choices") or [{}])[0]
        tool_calls = choice.get("message", {}).get("tool_calls") or []
        if tool_calls:
            args = tool_calls[0].get("function", {}).get("arguments", "")
            obj = json.loads(args)
            if isinstance(obj, dict) and "idx" in obj and "translation_escaped" in obj:
                return {"idx": str(obj["idx"]), "translation_escaped": str(obj["translation_escaped"])}
    except Exception as e:
        # spadne do fallbacku
        pass

    # B) Přes tools (explicitní volba funkce)
    payload_b = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": {"type": "function", "function": {"name": "store_translation"}}
    }
    try:
        data = _post_json(url, headers, payload_b, timeout_s)
        choice = (data.get("choices") or [{}])[0]
        tool_calls = choice.get("message", {}).get("tool_calls") or []
        if tool_calls:
            args = tool_calls[0].get("function", {}).get("arguments", "")
            obj = json.loads(args)
            if isinstance(obj, dict) and "idx" in obj and "translation_escaped" in obj:
                return {"idx": str(obj["idx"]), "translation_escaped": str(obj["translation_escaped"])}
    except Exception as e:
        # spadne do fallbacku
        pass

    # C) Nouzově: bez tools, ale v promptu už je vynucení POUZE JSON objektu
    payload_c = {"model": model, "messages": messages}
    data = _post_json(url, headers, payload_c, timeout_s)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    try:
        obj = json.loads(content)
        if isinstance(obj, dict) and "idx" in obj and "translation_escaped" in obj:
            return {"idx": str(obj["idx"]), "translation_escaped": str(obj["translation_escaped"])}
    except Exception as e:
        raise RuntimeError(f"Unexpected non-JSON or wrong shape: {content[:1200]} (err: {e})")

    # Nemělo by nastat, ale pro jistotu:
    raise RuntimeError("No usable JSON found in any path.")

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="LLM single-row apply (strict JSON via tools; no temperature).")
    ap.add_argument("--in",  dest="in_path",  required=True)
    ap.add_argument("--out", dest="out_path", required=True)
    ap.add_argument("--prompts", required=True)
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--base-url", default="https://api.openai.com")
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY",""))

    ap.add_argument("--source-col", default="source_escaped")
    ap.add_argument("--output-col", default="translation_escaped")

    ap.add_argument("--timeout-s", type=int, default=1800)
    ap.add_argument("--retries", type=int, default=8)
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--debug-dir", default=None)
    ap.add_argument("--resume", action="store_true", help="skip rows which already have output_col filled")

    args = ap.parse_args()
    if not args.api_key:
        print("ERROR: add --api-key or set OPENAI_API_KEY", file=sys.stderr)
        sys.exit(2)

    in_path  = Path(args.in_path)
    out_path = Path(args.out_path)
    debug_dir = Path(args.debug_dir) if args.debug_dir else None
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)

    system_rules, user_header = load_prompts(Path(args.prompts))
    fields, rows = read_tsv(in_path)

    # zaruč, že výstupní sloupec existuje
    if args.output_col not in fields:
        fields.append(args.output_col)
        for r in rows:
            r[args.output_col] = ""

    total = len(rows)
    done = 0

    log(f"[RUN] rows={total} | source_col={args.source_col} | output_col={args.output_col} | model={args.model}")
    for i, row in enumerate(rows, 1):
        idx = (row.get("idx") or "").strip()
        src = (row.get(args.source_col) or "")
        if not idx or not src:
            log(f"[SKIP] row {i}/{total} missing idx/source")
            continue
        if args.resume and (row.get(args.output_col) or "").strip():
            done += 1
            if done % 25 == 0:
                log(f"[RESUME] {done}/{total} already filled")
            continue

        user_prompt = build_user_prompt_single(user_header, idx, src)

        # ulož request do debug
        if debug_dir:
            (debug_dir / f"idx_{idx}.req.json").write_text(
                json.dumps({"system": system_rules, "user": user_prompt}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

        attempt = 0
        while True:
            attempt += 1
            try:
                obj = call_openai_json(args.base_url, args.api_key, args.model,
                                       system_rules, user_prompt, args.timeout_s)
                if debug_dir:
                    (debug_dir / f"idx_{idx}.resp.json").write_text(
                        json.dumps(obj, ensure_ascii=False, indent=2),
                        encoding="utf-8"
                    )

                if str(obj["idx"]).strip() != idx:
                    raise RuntimeError(f"idx mismatch: expected {idx}, got {obj['idx']}")

                tr = obj["translation_escaped"]
                if not isinstance(tr, str) or not tr.strip():
                    raise RuntimeError("empty translation_escaped")

                row[args.output_col] = tr
                done += 1
                log(f"[OK] {done}/{total} idx={idx} len={len(tr)}")
                write_tsv(out_path, fields, rows)  # průběžný safe zápis
                if args.sleep > 0:
                    time.sleep(args.sleep)
                break

            except Exception as e:
                if attempt >= args.retries:
                    log(f"[FAIL] idx={idx} attempts={attempt} err={e}")
                    write_tsv(out_path, fields, rows)
                    break
                back = min(60.0, 1.5 ** attempt + random.random())
                log(f"[RETRY] idx={idx} attempt={attempt} sleep={back:.1f}s reason={e}")
                time.sleep(back)

    log(f"[DONE] → {out_path}")

if __name__ == "__main__":
    main()
