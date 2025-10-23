#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translate_json_requests.py
--------------------------

Reads per-row JSON requests (from wotr_prepare_json_requests.py),
calls Chat Completions with function-calling (no temperature),
and writes per-row response JSONs plus running aggregates.

Outputs:
  <responses_dir>/
    idx_<ID>.response.json         # strict {idx, translation_escaped, ...}
    idx_<ID>.chat.json             # raw API response for debugging (optional)
  <out_dir>/
    translations.ndjson            # one JSON per line: {idx, translation_escaped}
    final_translations.json        # { "<idx>": "<translation_escaped>", ... }
    manifest.json                  # run summary

Design goals:
- One request per item, robust retries/backoff, and resume.
- No response_format/temperature (compatible with gpt-5-mini).
- Enforce JSON via function-calling; fallback parses plain JSON text.
- Validate literal '\n' count equality with source_escaped (warn if mismatch).

Usage (PowerShell):
  python .\\translate_json_requests.py `
    --requests-dir .\\out_wotr\\fix\\requests_json `
    --responses-dir .\\out_wotr\\fix\\responses_json `
    --out-dir .\\out_wotr\\fix\\out `
    --prompts .\\prompts-multiline.json `
    --model gpt-5-mini `
    --api-key <YOUR_KEY> `
    --resume `
    --sleep 0.2 `
    --retries 8 `
    --timeout-s 1800
"""

from __future__ import annotations
import argparse, json, sys, time, random
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

def log(msg: str) -> None:
    print(msg, flush=True)

# ---------- IO helpers ----------

def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))

def write_json(p: Path, obj: dict, indent: int = 2) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=indent), encoding="utf-8")

def append_ndjson_line(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def count_literal_newlines(s: str) -> int:
    # count occurrences of two-character sequence backslash-n
    return s.count("\\n")

# ---------- prompts ----------

def load_prompts(prompts_path: Path) -> Tuple[str, str]:
    j = read_json(prompts_path)
    return j.get("system_rules",""), j.get("user_header","")

def build_user_prompt(user_header: str, idx: str, source_escaped: str) -> str:
    """
    Compose a per-item user prompt. We explicitly mention function-calling
    and the required JSON shape. We also require 1:1 preservation of \\n count.
    """
    return (
        (user_header or "").strip() + "\n\n"
        "TASK:\n"
        f"- idx: {idx}\n"
        f"- source_escaped: {source_escaped}\n\n"
        "REQUIREMENTS:\n"
        "- Return ONLY structured data via the provided function, with fields:\n"
        "    { idx: string, translation_escaped: string }\n"
        "- Keep Pathfinder terminology consistent.\n"
        "- Inside {g|...}...{/g} do NOT modify anything (keep tags and inner text EXACTLY).\n"
        "- Inside other {...}, translate the inner text but KEEP the braces.\n"
        "- The translation must be Czech only (no bilingual mixing, no arrows like '->').\n"
        "- Preserve literal \\n exactly 1:1 with source_escaped (same count and positions).\n"
        "- Preserve any literal \\t if present.\n"
        "- Do not add or remove braces, do not add trailing/leading whitespace.\n"
    )

# ---------- OpenAI call (Chat Completions + tools) ----------

def _post_json(url: str, headers: Dict[str,str], payload: dict, timeout_s: int) -> dict:
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=(30, timeout_s))
    if r.status_code != 200:
        # Surface server error text for debugging
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:4000]}")
    return r.json()

def call_openai_tool(base_url: str, api_key: str, model: str,
                     system_rules: str, user_prompt: str, timeout_s: int) -> Tuple[dict, dict]:
    """
    Prefer function-calling (tools). Return (parsed_obj, raw_api_json).
    parsed_obj is expected to be {'idx': str, 'translation_escaped': str}.
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
            "description": "Return JSON with {idx, translation_escaped}. Keep literal \\n as \\n; do not change {g|...}...{/g}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "idx": {"type": "string"},
                    "translation_escaped": {"type": "string"}
                },
                "required": ["idx", "translation_escaped"],
                "additionalProperties": False
            }
        }
    }]

    # A) Try tools 'auto'
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
                return obj, data
    except Exception:
        pass

    # B) Force the function
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
                return obj, data
    except Exception:
        pass

    # C) No tools fallback: require plain JSON in assistant content (still no temperature)
    payload_c = {"model": model, "messages": messages}
    data = _post_json(url, headers, payload_c, timeout_s)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    try:
        obj = json.loads(content)
        if isinstance(obj, dict) and "idx" in obj and "translation_escaped" in obj:
            return obj, data
    except Exception as e:
        raise RuntimeError(f"Unexpected non-JSON: {content[:1200]} (err: {e})")

    raise RuntimeError("No usable JSON in any path.")

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Translate per-row JSON requests into per-row JSON responses and aggregates.")
    ap.add_argument("--requests-dir", required=True, help="Directory with idx_*.json request files")
    ap.add_argument("--responses-dir", required=True, help="Directory to write per-row response JSONs")
    ap.add_argument("--out-dir", required=True, help="Directory for aggregates (ndjson, final json, manifest)")
    ap.add_argument("--prompts", required=True, help="JSON file with system_rules and user_header")
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--base-url", default="https://api.openai.com")
    ap.add_argument("--api-key", default=None)

    ap.add_argument("--timeout-s", type=int, default=1800)
    ap.add_argument("--retries", type=int, default=8)
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--resume", action="store_true", help="Skip items that already have a response JSON")
    args = ap.parse_args()

    if not args.api_key:
        log("ERROR: Provide --api-key or set OPENAI_API_KEY")
        sys.exit(2)

    req_dir = Path(args.requests_dir)
    resp_dir = Path(args.responses_dir)
    out_dir  = Path(args.out_dir)
    resp_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True,  exist_ok=True)

    system_rules, user_header = load_prompts(Path(args.prompts))

    # aggregate files
    ndjson_path   = out_dir / "translations.ndjson"
    final_path    = out_dir / "final_translations.json"
    manifest_path = out_dir / "manifest.json"

    # if resuming, don't truncate aggregates; else start fresh
    if not args.resume:
        if ndjson_path.exists(): ndjson_path.unlink()
        if final_path.exists():  final_path.unlink()

    # load old final (resume) or start empty
    final_map: Dict[str, str] = {}
    if final_path.exists():
        try:
            final_map = read_json(final_path)
        except Exception:
            final_map = {}

    # enumerate request files
    req_files = sorted(req_dir.glob("idx_*.json"))
    total = len(req_files)
    done = 0
    ok_count = 0
    fail_count = 0
    warnings_total = 0

    log(f"[RUN] requests={total} | model={args.model}")
    for i, p in enumerate(req_files, 1):
        try:
            req = read_json(p)
        except Exception as e:
            log(f"[SKIP] unreadable {p.name}: {e}")
            continue

        idx  = str(req.get("idx", "")).strip()
        src  = req.get("source_escaped", "")
        if not idx or not isinstance(src, str) or not src:
            log(f"[SKIP] {p.name} missing idx/source_escaped")
            continue

        # if resume and response exists, load it to final map and continue
        per_resp = resp_dir / f"idx_{idx}.response.json"
        per_chat = resp_dir / f"idx_{idx}.chat.json"
        if args.resume and per_resp.exists():
            try:
                rr = read_json(per_resp)
                tr = rr.get("translation_escaped", "")
                if tr:
                    final_map[idx] = tr
                    append_ndjson_line(ndjson_path, {"idx": idx, "translation_escaped": tr})
                    done += 1
                    continue
            except Exception:
                pass  # fall through and reprocess if unreadable

        user_prompt = build_user_prompt(user_header, idx, src)

        # retry loop
        attempt = 0
        while True:
            attempt += 1
            try:
                obj, raw = call_openai_tool(args.base_url, args.api_key, args.model,
                                            system_rules, user_prompt, args.timeout_s)

                # write raw chat for debugging
                write_json(per_chat, raw, indent=2)

                # sanity: idx match
                if str(obj.get("idx","")).strip() != idx:
                    raise RuntimeError(f"idx mismatch: expected {idx}, got {obj.get('idx')}")

                tr = obj.get("translation_escaped")
                if not isinstance(tr, str) or not tr.strip():
                    raise RuntimeError("empty translation_escaped")

                # literal \n count validation
                src_n = count_literal_newlines(src)
                tr_n  = count_literal_newlines(tr)
                warns: List[str] = []
                if src_n != tr_n:
                    warns.append(f"newline_count_mismatch: src={src_n}, tr={tr_n}")

                # response envelope
                response_obj = {
                    "idx": idx,
                    "translation_escaped": tr,
                    "ok": True,
                    "warnings": warns
                }
                write_json(per_resp, response_obj, indent=2)

                # aggregate updates
                final_map[idx] = tr
                append_ndjson_line(ndjson_path, {"idx": idx, "translation_escaped": tr})
                write_json(final_path, final_map, indent=2)  # keep it durable

                done += 1
                ok_count += 1
                warnings_total += len(warns)
                if warns:
                    log(f"[OK*] {done}/{total} idx={idx} len={len(tr)} WARN={warns}")
                else:
                    log(f"[OK]  {done}/{total} idx={idx} len={len(tr)}")
                if args.sleep > 0:
                    time.sleep(args.sleep)
                break

            except Exception as e:
                if attempt >= args.retries:
                    fail_count += 1
                    err = f"{e}"
                    # write a failure stub for inspection
                    fail_obj = {
                        "idx": idx,
                        "ok": False,
                        "error": err
                    }
                    write_json(per_resp, fail_obj, indent=2)
                    log(f"[FAIL] idx={idx} attempts={attempt} err={err}")
                    break
                back = min(60.0, 1.5 ** attempt + random.random())
                log(f"[RETRY] idx={idx} attempt={attempt} sleep={back:.1f}s reason={e}")
                time.sleep(back)

    manifest = {
        "total_requests": total,
        "processed": done,
        "ok": ok_count,
        "failed": fail_count,
        "warnings_total": warnings_total,
        "requests_dir": str(req_dir),
        "responses_dir": str(resp_dir),
        "out_dir": str(out_dir),
        "ndjson": str(ndjson_path),
        "final_json": str(final_path)
    }
    write_json(manifest_path, manifest, indent=2)
    log(f"[DONE] ok={ok_count} fail={fail_count} → {final_path}")

if __name__ == "__main__":
    main()
