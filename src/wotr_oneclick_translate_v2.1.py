#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WotR one-click překladač v2 (FINAL)
===================================

enGB.json → (OpenAI Responses: Batch i Sync) → csCZ.json
-------------------------------------------------------

Co skript dělá
--------------
• Připraví dávky (JSONL) s jednotnými prompty a stabilní mapou idx→GUID.
• Umí poslat překlad přes:
    - Batch API (levnější, pomalejší; 24h okno), nebo
    - Sync API (/v1/responses) dávkuje řádek po řádku (rychlé, dražší).
• Umí „reslice“: rozdělit existující dávku 1:N bezpečně do nových dávek
  (správné číslování, unikátní custom_id).
• Umí resume: dávky hotové se přeskočí, rozpracované se dokončí.
• Při selhání/timeoutu dělí dávku auto-splitem (2 části) a pokračuje.
• Ukládá výsledky po dávkách do trans/ a na konci umí merge do csCZ.json.

Adresářová struktura (v OUT_DIR)
--------------------------------
map.json                   … globální index→GUID (stabilní napříč běhy)
manifest.json              … metadata requestů (rozsahy idx, odhad tokenů)
plan.json                  … přehled dávek (počet requestů a odhad tokenů)
requests/req_XXX.jsonl     … JSONL s requesty pro API (každý řádek = 1 request)
states/req_XXX.state.json  … stav dávky (prepared/in_progress/completed/replaced/failed) + batch_id
results/<batchId>_*.jsonl  … artefakty Batch API (errors/output)
results/req_XXX.sync.jsonl … sync výstup (řádek = 1 odpověď), pro vývojáře
results/req_XXX.jsonl      … kopie batch outputu pro danou dávku
trans/req_XXX.trans.tsv    … výsledný TSV dávky (idx<TAB>Translation)
logs/status_log.txt        … průběžný log
csCZ.json                  … finální překlad po --merge/--merge-only

Prompts (rules)
---------------
• Výchozí "system_rules" a "user_header" jsou v kódu.
• Doporučeno je uložit je do JSON souboru (např. prompts.json) a předat
  parametrem --prompts-file, aby je sdílely i pomocné skripty.

Použití – příklady
------------------
1) Příprava plánu (bez volání API):
   python -u wotr_oneclick_translate_v2.py -i enGB.json -o out_wotr --prompts-file prompts.json --dry-run

2) Odeslání přes Batch API + merge (doporučeno pro bulk):
   python -u wotr_oneclick_translate_v2.py -i enGB.json -o out_wotr --prompts-file prompts.json ^
     --skip-prepare --run --mode batch --merge

3) Rozřezání problémových dávek (např. 26–29 do 6 částí) a následný běh:
   # jen náhled:
   python -u wotr_oneclick_translate_v2.py -i enGB.json -o out_wotr --prompts-file prompts.json ^
     --reslice "26-29" --reslice-into 6 --reslice-dry-run
   # aplikovat změny:
   python -u wotr_oneclick_translate_v2.py -i enGB.json -o out_wotr --prompts-file prompts.json ^
     --reslice "26-29" --reslice-into 6 --reslice-commit
   # pak zpracovat jen nově vzniklé dávky (příklad):
   python -u wotr_oneclick_translate_v2.py -i enGB.json -o out_wotr --prompts-file prompts.json ^
     --skip-prepare --run --mode batch --batches "30-40" --merge

4) Sync režim (rychlé dokončení vybraných dávek, např. 30–59) s heartbeatem:
   python -u wotr_oneclick_translate_v2.py -i enGB.json -o out_wotr --prompts-file prompts.json ^
     --skip-prepare --run --mode sync --batches "30-59" --sync-progress-secs 5 --sync-timeout-secs 0 --merge

Poznámky
--------
• Každý request má unikátní custom_id (nutné pro Batch API).
• Parser výsledku je robustní a přísný: bere jen řádky přesného tvaru "idx<TAB>Translation".
• Vše je idempotentní – existující trans/req_XXX.trans.tsv znamená „hotovo“.
"""

from __future__ import annotations
import os
import sys
import re
import json
import time
import math
import argparse
import traceback
import threading
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional
from datetime import datetime
from collections import deque

# OpenAI oficiální klient (2025)
from openai import OpenAI

# ==========================
# VÝCHOZÍ PROMPTS (přepíše je --prompts-file)
# ==========================
DEFAULT_SYSTEM_RULES = (
  "Překládej do češtiny s respektem k RPG/D&D/Pathfinder terminologii; "
  "herní mechaniky překládej (číselné hodnoty ponech); "
  "zachovávej velká písmena vlastních jmen a titulů jako v angličtině; "
  "výchozí 2. osoba mužského rodu (ženský tvar jen pokud je to jisté); "
  "uvnitř odkazů {g|…}…{/g} nic neměň ani nepřekládej, u ostatních {…} překládej obsah a ponech závorky; "
  "piš přirozenou češtinou, krátké UI texty stručně; "
  "jemný archaický nádech tam, kde žánrově sedí; "
  "vrať POUZE TSV řádky `idx\\tTranslation` přesně v pořadí vstupu, bez čehokoli navíc; "
  "uváděj jen výsledný český text, nikdy nepiš dvojjazyčně typem \"EN → CS\"."
)
DEFAULT_USER_HEADER = (
  "Pro každý vstupní řádek `idx\\tSource` vrať přesně jeden řádek `idx\\tTranslation` se stejným `idx`. "
  "Uvnitř {g|…}…{/g} nic neměň ani nepřekládej; u ostatních {…} přelož obsah a ponech závorky. "
  "Herní mechaniky překládej, číselné hodnoty ponech.\n\n"
)

# Tyto proměnné naplníme z prompts souboru (nebo vezmeme defaulty)
SYSTEM_RULES = DEFAULT_SYSTEM_RULES
USER_HEADER = DEFAULT_USER_HEADER

# ==========================
# KONFIGURACE
# ==========================
MODEL_DEFAULT = "gpt-5-mini"     # případně "gpt-4o-mini"
BATCH_BUDGET_TOKENS = 900_000    # bezpečný strop enqueued tokens / 1 batch job (<< 5M org)
MAX_CHARS_PER_REQ = 18_000       # max znaků na jeden request (uživatelský blok)
MAX_LINES_PER_REQ = 350          # max řádků v jednom requestu
POLL_SECS = 15                   # interval dotazování na stav jobu
TIMEOUT_MINS_DEFAULT = 0         # 0 = bez limitu (jinak auto-split po vypršení)
ABORT_FAIL_MIN = 20              # než řešíme mass-fail
ABORT_FAIL_RATIO = 0.90          # 90%+ failů a 0 success → abort+split
TOKENS_PER_CHAR = 0.35           # ~3 znaky ≈ 1 token (konzerv.)
REQ_OVERHEAD = 220               # fixní režie na request
FUDGE = 1.30                     # rezerva
TSV_SEP = "\t"

# Regex pro robustní parsování čísla dávky ze jména souboru
BATCH_STATE_RX = re.compile(r"^req_(\d{3})\.state\.json$")

# ==========================
# UTILITY
# ==========================
def log(msg: str, lf: Optional[Path]):
    s = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(s, flush=True)
    if lf:
        lf.parent.mkdir(parents=True, exist_ok=True)
        with lf.open("a", encoding="utf-8") as f:
            f.write(s + "\n")

def fmt(sec: Optional[float]) -> str:
    if not sec or sec <= 0:
        return "—"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:d}h {m:02d}m {s:02d}s" if h else f"{m:02d}m {s:02d}s"

def write_safely(path: Path, data: bytes | str, binary=False):
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        with open(tmp, "wb") as f:
            f.write(data)
    else:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
    tmp.replace(path)

def ensure_dirs(root: Path) -> Dict[str, Path]:
    d = {
        "root": root,
        "requests": root / "requests",
        "states": root / "states",
        "results": root / "results",
        "trans": root / "trans",
        "logs": root / "logs",
    }
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d

def read_prompts(prompts_file: Optional[Path]):
    """Načti system_rules a user_header z JSON souboru."""
    global SYSTEM_RULES, USER_HEADER
    if prompts_file and prompts_file.exists():
        data = json.loads(prompts_file.read_text(encoding="utf-8"))
        SYSTEM_RULES = data.get("system_rules", SYSTEM_RULES)
        USER_HEADER = data.get("user_header", USER_HEADER)

def read_en_json(src: Path) -> Dict[str, Any]:
    data = json.loads(src.read_text(encoding="utf-8"))
    if "strings" not in data or not isinstance(data["strings"], dict):
        raise ValueError("JSON neobsahuje objekt 'strings'.")
    return data

def flatten_strings(strings: Dict[str, str]) -> List[Tuple[int, str, str]]:
    """Převeď GUID→Text na pořadové řádky (idx, guid, text)."""
    rows = []
    for i, (guid, text) in enumerate(strings.items(), start=1):
        rows.append((i, guid, text if isinstance(text, str) else ""))
    return rows

def chunk_rows(rows: List[Tuple[int, str, str]], max_chars: int, max_lines: int):
    """Rozsekání na chunky pod limity (počítáme délku jako idx+TAB+text+NL)."""
    buf: List[Tuple[int, str, str]] = []
    size = 0
    for idx, guid, text in rows:
        line = f"{idx}{TSV_SEP}{text}\n"
        if buf and (size + len(line) > max_chars or len(buf) >= max_lines):
            yield buf
            buf, size = [], 0
        buf.append((idx, guid, text))
        size += len(line)
    if buf:
        yield buf

def build_user_block(chunk: List[Tuple[int, str, str]]) -> str:
    return "".join(f"{idx}{TSV_SEP}{text}\n" for idx, _, text in chunk)

def estimate_enqueued_tokens_for_user_block(user_block: str) -> int:
    chars = len(SYSTEM_RULES) + len(DEFAULT_USER_HEADER) + len(user_block)
    return int(math.ceil(chars * TOKENS_PER_CHAR) * FUDGE) + REQ_OVERHEAD

def unique_custom_id(prefix: str, seq: int) -> str:
    return f"{prefix}_{seq:06d}"

def build_request_object(custom_id: str, model: str, user_block: str) -> Dict[str, Any]:
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/responses",
        "body": {
            "model": model,
            "input": [
                {"role": "system", "content": SYSTEM_RULES},
                {"role": "user", "content": USER_HEADER + user_block}
            ]
        }
    }

def rc_to_dict(rc_obj: Any) -> Dict[str, int]:
    if rc_obj is None:
        return {}
    if isinstance(rc_obj, dict):
        return {str(k).lower(): int(v) for k, v in rc_obj.items() if isinstance(v, (int, float))}
    try:
        return {str(k).lower(): int(v) for k, v in rc_obj.__dict__.items() if isinstance(v, (int, float))}
    except Exception:
        pass
    try:
        d = json.loads(getattr(rc_obj, "model_dump_json", lambda: "{}")())
        return {str(k).lower(): int(v) for k, v in d.items() if isinstance(v, (int, float))}
    except Exception:
        return {}

def summarize_counts(job) -> Dict[str, int]:
    rc = rc_to_dict(getattr(job, "request_counts", None))
    total = rc.get("total") or rc.get("submitted") or 0
    completed = rc.get("completed", 0)
    succeeded = rc.get("succeeded", 0)
    failed = rc.get("failed", 0)
    errored = rc.get("errored", 0)
    cancelled = rc.get("cancelled", 0)
    processed = completed + succeeded + failed + errored + cancelled
    return {
        "total": int(total),
        "processed": int(processed),
        "failed": int(failed),
        "succeeded": int(succeeded or completed),
        "_raw": rc
    }

def extract_output_text(body: Dict[str, Any]) -> str:
    """
    Responses API → plain text:
      • prefer 'output_text'
      • fallback na 'output[].content[].text' (robustně – hlídá None a typy)
      • fallback na 'choices[0].message.content'
    """
    if not isinstance(body, dict):
        return ""
    # 1) output_text
    out_txt = body.get("output_text")
    if isinstance(out_txt, str) and out_txt.strip():
        return out_txt

    # 2) output[].content[].text (robustně)
    out_chunks: List[str] = []
    out_list = body.get("output") or []
    if isinstance(out_list, list):
        for item in out_list:
            if not isinstance(item, dict):
                continue
            content = item.get("content") or []
            if not isinstance(content, list):
                continue
            for c in content:
                if isinstance(c, dict):
                    t = c.get("text")
                    if isinstance(t, str):
                        out_chunks.append(t)
    if out_chunks:
        return "\n".join(out_chunks)

    # 3) choices[0].message.content
    try:
        ch0 = body.get("choices")[0]
        msg = ch0.get("message", {})
        txt = msg.get("content")
        if isinstance(txt, str):
            return txt
    except Exception:
        pass

    return ""

def parse_range(expr: Optional[str]) -> List[int]:
    """Parse '1,3-5,10' → [1,3,4,5,10]."""
    if not expr:
        return []
    s: set[int] = set()
    for tok in expr.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "-" in tok:
            a, b = tok.split("-", 1)
            a = int(a); b = int(b)
            if a <= b:
                s.update(range(a, b + 1))
            else:
                s.update(range(b, a + 1))
        else:
            s.add(int(tok))
    return sorted(s)

# ====================
# PŘÍPRAVA DÁVEK (PLAN)
# ====================
def prepare_batches(en_json: Path, out_dirs: Dict[str, Path], model: str,
                    batch_budget_tokens: int, max_chars_per_req: int, max_lines_per_req: int,
                    log_file: Path):
    """Sestav plan.json, manifest.json, req_XXX.jsonl a počáteční states (idempotentně)."""
    data = read_en_json(en_json)
    map_path = out_dirs["root"] / "map.json"

    # Stabilní mapování – pokud existuje, ctíme jeho pořadí (resume/merge).
    if map_path.exists():
        id_map = json.loads(map_path.read_text(encoding="utf-8"))
        rows: List[Tuple[int, str, str]] = []
        for idx_str in sorted(id_map.keys(), key=lambda s: int(s)):
            guid = id_map[idx_str]
            src = data["strings"].get(guid, "")
            rows.append((int(idx_str), guid, src))
    else:
        rows = flatten_strings(data["strings"])
        id_map = {str(idx): guid for idx, guid, _ in rows}
        write_safely(map_path, json.dumps(id_map, ensure_ascii=False, indent=0))

    # Vytvoř requesty
    requests: List[Dict[str, Any]] = []
    manifest: List[Dict[str, Any]] = []
    seq = 0
    for chunk in chunk_rows(rows, max_chars=max_chars_per_req, max_lines=max_lines_per_req):
        user_block = build_user_block(chunk)
        seq += 1
        req_id = unique_custom_id("prep", seq)
        requests.append(build_request_object(req_id, model, user_block))
        manifest.append({
            "req_id": req_id,
            "first_idx": chunk[0][0],
            "last_idx": chunk[-1][0],
            "rows": len(chunk),
            "est_tokens": estimate_enqueued_tokens_for_user_block(user_block)
        })

    # Rozdělení requestů do batch JSONL pod rozpočtem
    batches: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    cur_tokens = 0
    for i, req in enumerate(requests, start=1):
        est = manifest[i - 1]["est_tokens"]
        if cur and cur_tokens + est > batch_budget_tokens:
            batches.append(cur)
            cur, cur_tokens = [], 0
        cur.append(req)
        cur_tokens += est
    if cur:
        batches.append(cur)

    # Zápis plánů a stavů
    plan = []
    for bi, batch in enumerate(batches, start=1):
        jsonl_path = out_dirs["requests"] / f"req_{bi:03d}.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as f:
            for obj in batch:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

        est_tokens = 0
        for obj in batch:
            content = obj["body"]["input"][1]["content"]
            block = content.split("\n\n", 1)[-1] if "\n\n" in content else content
            est_tokens += estimate_enqueued_tokens_for_user_block(block)

        plan.append({
            "batch_no": bi,
            "requests": len(batch),
            "file": jsonl_path.name,
            "est_enqueued_tokens": est_tokens
        })
        write_safely(
            out_dirs["states"] / f"req_{bi:03d}.state.json",
            json.dumps({"batch_no": bi, "jsonl": jsonl_path.name, "status": "prepared"}, ensure_ascii=False, indent=2)
        )

    write_safely(out_dirs["root"] / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    write_safely(out_dirs["root"] / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2))

    log(f"[PLAN] řádků: {len(rows)} | requestů: {len(requests)} | dávek: {len(batches)}", log_file)
    for p in plan:
        log(f"[BATCH {p['batch_no']:02d}] requests={p['requests']} | est_enqueued_tokens≈{p['est_enqueued_tokens']:,}", log_file)

# =========================
# RESLICE DÁVEK (bezpečně)
# =========================
def next_batch_no(states_dir: Path) -> int:
    """Najdi nejvyšší existující číslo dávky ve states/ (bez kolizí, robustně)."""
    maxno = 0
    for p in states_dir.glob("req_*.state.json"):
        m = BATCH_STATE_RX.match(p.name)
        if not m:
            continue
        n = int(m.group(1))
        if n > maxno:
            maxno = n
    return maxno + 1

def reslice_batches(out_dirs: Dict[str, Path], select: List[int], into: int,
                    commit: bool, log_file: Path):
    """
    Rozděl vybrané dávky na 'into' částí. Nedělí uvnitř requestů.
    • commit=False → jen vypíše plán (DRY).
    • commit=True  → vytvoří nové req_XXX.jsonl + states/req_XXX.state.json,
                     původní dávku označí jako 'replaced'.
    """
    if not select:
        log("[RESLICE] Nic nevybráno (--reslice).", log_file)
        return

    for bno in select:
        src_jsonl = out_dirs["requests"] / f"req_{bno:03d}.jsonl"
        state_file = out_dirs["states"] / f"req_{bno:03d}.state.json"
        if not src_jsonl.exists() or not state_file.exists():
            log(f"[RESLICE] Dávka {bno}: chybí requests/state – přeskakuji.", log_file)
            continue

        lines = [ln for ln in src_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
        total = len(lines)
        if total == 0:
            log(f"[RESLICE] Dávka {bno}: prázdná – přeskakuji.", log_file)
            continue

        # rozdělení requestů do N částí (bez dělení uvnitř requestů!)
        sizes = [total // into + (1 if i < (total % into) else 0) for i in range(into)]
        parts: List[List[str]] = []
        pos = 0
        for size in sizes:
            parts.append(lines[pos:pos + size])
            pos += size

        if not commit:
            log(f"[RESLICE][DRY] Dávka {bno}: {total} requestů → navrhované dávky: {len(parts)} částí", log_file)
            continue

        # commit: označ původní jako replaced
        try:
            st = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            st = {"batch_no": bno, "jsonl": src_jsonl.name}
        st["status"] = "replaced"
        write_safely(state_file, json.dumps(st, ensure_ascii=False, indent=2))

        # vytvoř nové dávky s unikátními custom_id
        new_ids: List[int] = []
        for k, part in enumerate(parts, start=1):
            new_no = next_batch_no(out_dirs["states"])
            out_jsonl = out_dirs["requests"] / f"req_{new_no:03d}.jsonl"

            out_lines = []
            for i, ln in enumerate(part, start=1):
                obj = json.loads(ln)
                obj["custom_id"] = unique_custom_id(f"rs{bno:03d}_{k}", i)
                out_lines.append(json.dumps(obj, ensure_ascii=False))
            write_safely(out_jsonl, "\n".join(out_lines) + "\n")

            new_state = {"batch_no": new_no, "jsonl": out_jsonl.name, "status": "prepared"}
            write_safely(out_dirs["states"] / f"req_{new_no:03d}.state.json",
                         json.dumps(new_state, ensure_ascii=False, indent=2))
            new_ids.append(new_no)

        log(f"[RESLICE] Dávka {bno}: {total} requestů → nové dávky: {', '.join(f'{n:03d}' for n in new_ids)}", log_file)

# =========================
# ODESLÁNÍ A ZPRACOVÁNÍ – BATCH
# =========================
def auto_split_and_retry(out_dirs: Dict[str, Path], bno: int, state_path: Path, jsonl_path: Path, log_file: Path) -> List[Path]:
    """Rozděl dávku na dvě poloviny, označ původní jako 'replaced', vrať nové state soubory (priorita)."""
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    mid = len(lines) // 2 or 1
    parts = [lines[:mid], lines[mid:]]

    # původní dávka → replaced
    try:
        st = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        st = {"batch_no": bno, "jsonl": jsonl_path.name}
    st["status"] = "replaced"
    write_safely(state_path, json.dumps(st, ensure_ascii=False, indent=2))

    created: List[Path] = []
    for pidx, part in enumerate(parts, start=1):
        new_no = next_batch_no(out_dirs["states"])
        new_jsonl = out_dirs["requests"] / f"req_{new_no:03d}.jsonl"

        # oprav custom_id, aby byly unikátní
        out_lines = []
        for i, ln in enumerate(part, start=1):
            obj = json.loads(ln)
            obj["custom_id"] = unique_custom_id(f"sp{bno:03d}_{pidx}", i)
            out_lines.append(json.dumps(obj, ensure_ascii=False))
        write_safely(new_jsonl, "\n".join(out_lines) + "\n")

        new_state = {"batch_no": new_no, "jsonl": new_jsonl.name, "status": "prepared"}
        state_file = out_dirs["states"] / f"req_{new_no:03d}.state.json"
        write_safely(state_file, json.dumps(new_state, ensure_ascii=False, indent=2))
        created.append(state_file)
        log(f"[SPLIT] Dávka {bno} → nová dávka {new_no} ({len(part)} requestů).", log_file)
    return created

def extract_output_text_from_obj(obj: Dict[str, Any]) -> str:
    if obj.get("error"):
        return ""
    body = (obj.get("response") or {}).get("body", {})
    return extract_output_text(body)

def build_trans_tsv(result_jsonl: Path, manifest_path: Path, out_tsv: Path, log_file: Path):
    """Vyparsuj JSONL s výstupy do TSV 'idx<TAB>Translation'."""
    idx_lines: List[str] = []
    count_in = 0
    count_out = 0
    if not result_jsonl.exists():
        write_safely(out_tsv, "")
        log(f"[TSV] {out_tsv.name}: output není k dispozici.", log_file)
        return
    with result_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            txt = extract_output_text_from_obj(obj)
            if not txt:
                continue
            for ln in (ln for ln in txt.splitlines() if ln.strip()):
                count_in += 1
                if TSV_SEP not in ln:
                    continue
                idx, tr = ln.split(TSV_SEP, 1)
                idx, tr = idx.strip(), tr.strip()
                if not idx or not tr:
                    continue
                idx_lines.append(f"{idx}\t{tr}")
                count_out += 1
    write_safely(out_tsv, "\n".join(idx_lines) + ("\n" if idx_lines else ""))
    log(f"[TSV] {out_tsv.name}: parsed_lines={count_in} | valid_lines={count_out}", log_file)

def run_batches(client: OpenAI, out_dirs: Dict[str, Path], poll: int, timeout_mins: Optional[int],
                select_batches: Optional[List[int]], log_file: Path):
    """Hlavní běh přes Batch API s prioritou pro nové splity."""
    priority: deque[Path] = deque()

    def pick_next_state() -> Optional[Path]:
        if priority:
            return priority.popleft()
        # Nejnovější states mají prioritu (podle mtime)
        for sf in sorted(out_dirs["states"].glob("req_*.state.json"),
                         key=lambda p: (p.stat().st_mtime, p.name)):
            try:
                state = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                continue
            bno = int(state["batch_no"])
            if select_batches and bno not in select_batches:
                continue
            status = state.get("status", "prepared")
            if status in ("completed", "replaced"):
                continue
            trans_tsv = out_dirs["trans"] / f"req_{bno:03d}.trans.tsv"
            if trans_tsv.exists() and trans_tsv.stat().st_size > 0:
                # dorovnej status
                state.update({"status": "completed"})
                write_safely(sf, json.dumps(state, ensure_ascii=False, indent=2))
                continue
            return sf
        return None

    while True:
        sf = pick_next_state()
        if sf is None:
            log("[RUN] Není nic k odeslání/zpracování (batch).", log_file)
            break

        state = json.loads(sf.read_text(encoding="utf-8"))
        bno = int(state["batch_no"])
        jsonl_file = out_dirs["requests"] / state["jsonl"]
        trans_tsv = out_dirs["trans"] / f"req_{bno:03d}.trans.tsv"
        result_jsonl = out_dirs["results"] / f"req_{bno:03d}.jsonl"

        batch_id = state.get("batch_id")
        if not batch_id:
            up = client.files.create(file=open(jsonl_file, "rb"), purpose="batch")
            job = client.batches.create(input_file_id=up.id, endpoint="/v1/responses", completion_window="24h")
            batch_id = job.id
            state.update({"batch_id": batch_id, "status": job.status})
            write_safely(sf, json.dumps(state, ensure_ascii=False, indent=2))
            log(f"[SUBMIT] Dávka {bno} → job={batch_id} | status={job.status}", log_file)

        t0 = time.time()
        dumped_early = False
        while True:
            job = client.batches.retrieve(batch_id)
            c = summarize_counts(job)
            elapsed = time.time() - t0
            eta = ((c["total"] - c["processed"]) / (c["processed"] / elapsed)) if c["processed"] else None
            log(f"[BATCH {bno:02d}] {batch_id} status={job.status} | processed={c['processed']}/{c['total']} | "
                f"failed={c['failed']} | elapsed={fmt(elapsed)} | ETA≈{fmt(eta)}", log_file)

            # Early errors (pokud API nabídne během běhu)
            if not dumped_early:
                fid = getattr(job, "errors_file_id", None) or getattr(job, "error_file_id", None)
                if fid:
                    try:
                        blob = client.files.content(fid).content
                        write_safely(out_dirs["results"] / f"{batch_id}_errors_early.jsonl", blob, binary=True)
                        log(f"[DUMP] Early errors → {batch_id}_errors_early.jsonl", log_file)
                        dumped_early = True
                    except Exception:
                        pass

            # mass-fail ochrana
            if c["processed"] >= ABORT_FAIL_MIN and c["succeeded"] == 0 and c["failed"] >= int(c["processed"] * ABORT_FAIL_RATIO):
                log(f"[ABORT] Dávka {bno}: masivní selhání, ruším job.", log_file)
                try:
                    client.batches.cancel(batch_id)
                except Exception:
                    pass
                for ns in reversed(auto_split_and_retry(out_dirs, bno, sf, jsonl_file, log_file)):
                    priority.appendleft(ns)
                break

            # timeout → split
            if timeout_mins and elapsed > timeout_mins * 60:
                log(f"[TIMEOUT] Dávka {bno}: překročen limit, ruším.", log_file)
                try:
                    client.batches.cancel(batch_id)
                except Exception:
                    pass
                for ns in reversed(auto_split_and_retry(out_dirs, bno, sf, jsonl_file, log_file)):
                    priority.appendleft(ns)
                break

            # finální stavy
            if job.status in ("completed", "failed", "cancelling", "cancelled", "expired"):
                # stáhni artefakty (errors/output, když existují)
                for attr in ("errors_file_id", "error_file_id", "output_file_id"):
                    fid = getattr(job, attr, None)
                    if fid:
                        try:
                            blob = client.files.content(fid).content
                            write_safely(out_dirs["results"] / f"{batch_id}_{attr}.jsonl", blob, binary=True)
                        except Exception:
                            pass

                if job.status != "completed":
                    # token limit? → split a zkusit znovu
                    err = getattr(job, "errors", None) or getattr(job, "error", None)
                    if err and "token_limit_exceeded" in str(err):
                        log(f"[SPLIT] Dávka {bno}: token limit → rozděluji a opakuji.", log_file)
                        for ns in reversed(auto_split_and_retry(out_dirs, bno, sf, jsonl_file, log_file)):
                            priority.appendleft(ns)
                        break
                    log(f"[ERROR] Dávka {bno}: job skončil {job.status}.", log_file)
                    break

                # úspěch → stáhni výstup a vyrob TSV
                blob = client.files.content(job.output_file_id).content
                write_safely(result_jsonl, blob, binary=True)
                build_trans_tsv(result_jsonl, out_dirs["root"] / "manifest.json", trans_tsv, log_file)
                state.update({"status": "completed"})
                write_safely(sf, json.dumps(state, ensure_ascii=False, indent=2))
                log(f"[OK] Dávka {bno}: hotovo → {trans_tsv.name}", log_file)
                break

            time.sleep(max(poll, 5))

# =========================
# ODESLÁNÍ A ZPRACOVÁNÍ – SYNC
# =========================
def run_sync(client: OpenAI, out_dirs: Dict[str, Path], model: str,
             select_batches: Optional[List[int]], progress_secs: int, log_file: Path):
    """
    Sync běh po řádcích JSONL:
      - Každý request odešle přes /v1/responses (blocking).
      - Během čekání běží heartbeat a hlásí, že stále čekáme (každých progress_secs).
      - Po každé odpovědi zapisuje 1 řádek do results/req_XXX.sync.jsonl (idempotentně).
      - Po dávce sestaví trans/req_XXX.trans.tsv a označí ji za completed.
    """
    def iter_states():
        for sf in sorted(out_dirs["states"].glob("req_*.state.json"),
                         key=lambda p: (p.stat().st_mtime, p.name)):
            state = json.loads(sf.read_text(encoding="utf-8"))
            bno = int(state["batch_no"])
            if select_batches and bno not in select_batches:
                continue
            status = state.get("status", "prepared")
            if status in ("completed", "replaced"):
                continue
            # pokud už existuje TSV, považuj za hotové a přeskoč
            tsv = out_dirs["trans"] / f"req_{bno:03d}.trans.tsv"
            if tsv.exists() and tsv.stat().st_size > 0:
                state.update({"status": "completed"})
                write_safely(sf, json.dumps(state, ensure_ascii=False, indent=2))
                continue
            yield sf, bno, state

    had_any = False
    for sf, bno, state in iter_states():
        had_any = True
        jsonl_file = out_dirs["requests"]/state["jsonl"]
        out_jsonl = out_dirs["results"]/f"req_{bno:03d}.sync.jsonl"
        trans_tsv = out_dirs["trans"]/f"req_{bno:03d}.trans.tsv"

        lines = [ln for ln in jsonl_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        total = len(lines)
        done = 0
        errs = 0
        batch_t0 = time.time()
        log(f"[SYNC] Dávka {bno}: start | requests={total}", log_file)

        # otevři výstup pro append + okamžitý flush
        with out_jsonl.open("a", encoding="utf-8") as fout:
            for ridx, ln in enumerate(lines, start=1):
                req_t0 = time.time()
                obj = json.loads(ln)
                body = obj["body"]
                custom_id = obj.get("custom_id", f"b{bno:03d}_r{ridx:05d}")

                # Heartbeat vlákno: každých progress_secs sekund logne „stále čekám“
                stop_evt = threading.Event()

                def heartbeat():
                    while not stop_evt.wait(max(2, progress_secs)):
                        waited = time.time() - req_t0
                        log(f"[SYNC] Dávka {bno}: waiting {ridx}/{total} (custom_id={custom_id}) | "
                            f"errs={errs} | waited={fmt(waited)}", log_file)

                hb = threading.Thread(target=heartbeat, daemon=True)
                hb.start()

                try:
                    # Samotný sync call – blokuje; timeout řídí klient (viz --sync-timeout-secs)
                    resp = client.responses.create(**body)

                    out_obj = {
                        "response": {"body": json.loads(resp.model_dump_json())},
                        "custom_id": custom_id
                    }
                    fout.write(json.dumps(out_obj, ensure_ascii=False) + "\n")
                    fout.flush()

                except Exception as e:
                    errs += 1
                    err_s = f"{type(e).__name__}: {e}"
                    log(f"[SYNC][ERR] Dávka {bno} req {ridx}/{total} (custom_id={custom_id}): {err_s}", log_file)
                    fout.write(json.dumps({"error": err_s, "custom_id": custom_id}) + "\n")
                    fout.flush()

                finally:
                    stop_evt.set()
                    hb.join(timeout=1.0)

                done += 1
                now = time.time()
                log(f"[SYNC] Dávka {bno}: progress {done}/{total} req | errs={errs} | "
                    f"req_time={fmt(now-req_t0)} | elapsed_total={fmt(now-batch_t0)}", log_file)

        # Po dávce: postav TSV a označ completed
        build_trans_tsv(out_jsonl, out_dirs["root"]/ "manifest.json", trans_tsv, log_file)
        state.update({"status": "completed", "mode": "sync"})
        write_safely(sf, json.dumps(state, ensure_ascii=False, indent=2))
        log(f"[OK][SYNC] Dávka {bno}: hotovo → {trans_tsv.name}", log_file)

    if not had_any:
        log("[SYNC] Není nic k odeslání v zadaném rozsahu (vše již completed/replaced nebo existují trans/*.tsv).", log_file)

# ==========================
# MERGE
# ==========================
def merge_to_json(en_json: Path, out_dir: Path, out_file: Path, log_file: Optional[Path]):
    data = read_en_json(en_json)
    strings = data["strings"]
    id_map = json.loads((out_dir / "map.json").read_text(encoding="utf-8"))
    idx2tr: Dict[str, str] = {}
    for tsv in sorted((out_dir / "trans").glob("req_*.trans.tsv")):
        with tsv.open("r", encoding="utf-8") as f:
            for line in f:
                if TSV_SEP not in line:
                    continue
                idx, tr = line.rstrip("\n").split(TSV_SEP, 1)
                if idx and tr:
                    idx2tr[idx] = tr
    applied = 0
    missing = 0
    for idx, guid in id_map.items():
        if idx in idx2tr:
            strings[guid] = idx2tr[idx]
            applied += 1
        else:
            missing += 1
    write_safely(out_file, json.dumps(data, ensure_ascii=False, indent=2))
    if log_file:
        log(f"[MERGE] Zapsán {out_file.name} | přepsáno: {applied} | chybí překlad: {missing}", log_file)
    else:
        print(f"[MERGE] {out_file} | applied={applied} | missing={missing}")

# ==========================
# CLI
# ==========================
def main():
    ap = argparse.ArgumentParser(description="WotR one-click translator v2 (JSON → Responses API → JSON)")
    ap.add_argument("-i", "--input", required=True, help="Cesta k enGB.json")
    ap.add_argument("-o", "--out-dir", required=True, help="Pracovní složka (vytvoří se)")
    ap.add_argument("--prompts-file", default=None, help="JSON se 'system_rules' a 'user_header'")
    ap.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY"), help="API klíč (jinak z env)")
    ap.add_argument("--model", default=MODEL_DEFAULT)

    # příprava
    ap.add_argument("--batch-budget", type=int, default=BATCH_BUDGET_TOKENS, help="Max enqueued tokens / batch")
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS_PER_REQ, help="Max znaků / request")
    ap.add_argument("--max-lines", type=int, default=MAX_LINES_PER_REQ, help="Max řádků / request")
    ap.add_argument("--dry-run", action="store_true", help="Připrav plán (mapa/JSONL), neodesílej")
    ap.add_argument("--prepare-only", action="store_true", help="Jako --dry-run, ponechá JSONL")
    ap.add_argument("--skip-prepare", action="store_true", help="Nepřegenerovávat plán; použij existující requests/states")

    # reslice
    ap.add_argument("--reslice", default=None, help='Dávky k rozdělení, např. "25-29,40"')
    ap.add_argument("--reslice-into", type=int, default=2, help="Na kolik částí rozdělit (default 2)")
    ap.add_argument("--reslice-commit", action="store_true", help="Aplikovat rozdělení (jinak jen dry-run)")
    ap.add_argument("--reslice-dry-run", action="store_true", help="Vynutit dry-run pro reslice (alias k ne-commit)")

    # běh
    ap.add_argument("--run", action="store_true", help="Spustí zpracování dávek")
    ap.add_argument("--mode", choices=["batch", "sync"], default="batch", help="Způsob odeslání")
    ap.add_argument("--batches", default=None, help='Zpracuj jen vybrané dávky, např. "30-34,40"')
    ap.add_argument("--poll", type=int, default=POLL_SECS, help="Polling batch jobu (s)")
    ap.add_argument("--timeout-mins", type=int, default=TIMEOUT_MINS_DEFAULT, help="Batch timeout (0 = bez limitu)")
    ap.add_argument("--sync-progress-secs", type=int, default=5, help="Jak často hlásit progress v sync režimu")
    ap.add_argument("--sync-timeout-secs", type=int, default=0, help="Klientský timeout pro sync request (0 = bez limitu, doporučeno pro dlouhé běhy).")

    # merge
    ap.add_argument("--merge", action="store_true", help="Po běhu sloučí do csCZ.json")
    ap.add_argument("--merge-only", action="store_true", help="Bez API: sloučí existující trans/*.tsv do csCZ.json")

    args = ap.parse_args()

    # prompts
    read_prompts(Path(args.prompts_file) if args.prompts_file else None)

    # IO
    input_json = Path(args.input)
    out_root = Path(args.out_dir)
    dirs = ensure_dirs(out_root)
    log_file = dirs["logs"] / "status_log.txt"

    # MERGE-ONLY
    if args.merge_only:
        if not (out_root / "map.json").exists():
            print("ERROR: chybí map.json v out_dir."); sys.exit(2)
        if not any((out_root / "trans").glob("req_*.trans.tsv")):
            print("ERROR: nenašly se žádné trans/req_*.trans.tsv."); sys.exit(2)
        merge_to_json(input_json, out_root, out_root / "csCZ.json", None)
        print(f"[MERGE-ONLY] Hotovo → {out_root / 'csCZ.json'}")
        return

    # PŘÍPRAVA
    if args.skip_prepare and (out_root / "plan.json").exists():
        log("[PLAN] Existuje plan.json → --skip-prepare: přeskakuji přípravu.", log_file)
    else:
        prepare_batches(
            en_json=input_json, out_dirs=dirs, model=args.model,
            batch_budget_tokens=args.batch_budget,
            max_chars_per_req=args.max_chars, max_lines_per_req=args.max_lines,
            log_file=log_file
        )

    if args.dry_run or args.prepare_only:
        log("[DRY] Plán hotový. Připraveno k odeslání (--run).", log_file)
        return

    # RESLICE (volitelně před během)
    if args.reslice:
        sel = parse_range(args.reslice)
        # řešení konfliktu flagů
        if args.reslice_dry_run and args.reslice_commit:
            print("ERROR: nelze kombinovat --reslice-commit a --reslice-dry-run zároveň.")
            sys.exit(2)
        commit = bool(args.reslice_commit and not args.reslice_dry_run)
        reslice_batches(dirs, sel, args.reslice_into, commit, log_file)

    # RUN
    if args.run:
        if not args.api_key:
            print("ERROR: chybí OPENAI_API_KEY (nebo --api-key)."); sys.exit(1)

        # Batch i Sync používají stejný klient; pro Sync nastavíme timeout (0 => None)
        client_timeout = None if args.sync_timeout_secs == 0 else float(args.sync_timeout_secs)
        client = OpenAI(api_key=args.api_key, timeout=client_timeout)
        selected = parse_range(args.batches)
        try:
            if args.mode == "batch":
                timeout = args.timeout_mins if args.timeout_mins and args.timeout_mins > 0 else None
                run_batches(client, dirs, poll=args.poll, timeout_mins=timeout, select_batches=selected, log_file=log_file)
            else:
                run_sync(client, dirs, model=args.model, select_batches=selected, progress_secs=args.sync_progress_secs, log_file=log_file)
        except Exception:
            (dirs["logs"] / "last_exception.txt").write_text(traceback.format_exc(), encoding="utf-8")
            log(f"[EXC] Neošetřená výjimka – viz logs/last_exception.txt", log_file)
            raise

    # MERGE
    if args.merge:
        merge_to_json(input_json, out_root, out_root / "csCZ.json", log_file)
        log("[DONE] Překlad + merge hotov.", log_file)

if __name__ == "__main__":
    main()
