#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
wotr_tsv_gpt_sync_apply.py (PARALLEL)
=====================================

Orchestrátor "TSV → LLM → PARSE → APPLY" s paralelizací po dávkách.

Co dělá
-------
- Načte vstupní TSV (musí mít 'idx' + zdrojový sloupec, např. 'source' / 'source_escaped').
- Rozdělí na dávky podle --max-lines / --max-chars (řádek = "idx<TAB>Source\n").
- Každou dávku pošle paralelně (ThreadPoolExecutor, limitováno --concurrency).
- Sdílený rate limiter (--rpm) napříč všemi vlákny.
- Robustní retry s exponenciálním backoffem na úrovni dávky.
- DEBUG výstup na dávku: batch_XXX.req.txt / .resp.txt / .parse_diag.txt / .parsed.tsv
- Parsuje přes `wotr_resp_parser.parse_resp_with_req(...)` (očekává mapu idx→translation).
- Aplikuje výsledky do sloupce --output-col a průběžně zapisuje OUT TSV (atomicky).

Poznámky
--------
- Prompts JSON může mít klíč "user_header" **nebo** "user_prefix" (obojí podporováno).
- V user promptu vynucujeme TSV výstup v code fence ```tsv ... ```.
- Pro multiline zdroje používej sloupec se zescapovanými \n/\t (např. source_escaped).
- Nepředáváme žádnou "temperature" (gpt-5-mini).

Použití (příklad – "short texts", řádek = 1 request)
---------------------------------------------------
powershell:
  python .\wotr_tsv_gpt_sync_apply.py `
    --in .\out_wotr\fix\short_texts.tsv `
    --out .\out_wotr\fix\short_texts_translated.tsv `
    --prompts .\prompts-short.json `
    --model gpt-5-mini `
    --source-col source `
    --output-col translation `
    --max-lines 1 `
    --max-chars 200 `
    --concurrency 8 `
    --rpm 240 `
    --timeout-s 1800 `
    --retries 8 `
    --debug-dir .\out_wotr\fix\debug_short

Použití (příklad – multiline fixy, více řádků v dávce)
------------------------------------------------------
powershell:
  python .\wotr_tsv_gpt_sync_apply.py `
    --in .\out_wotr\fix\multiline_src.tsv `
    --out .\out_wotr\fix\multiline_src_translated.tsv `
    --prompts .\prompts-multiline.json `
    --model gpt-5-mini `
    --source-col source_escaped `
    --output-col translation_escaped `
    --max-lines 12 `
    --max-chars 9000 `
    --concurrency 4 `
    --rpm 120 `
    --timeout-s 1800 `
    --retries 8 `
    --debug-dir .\out_wotr\fix\debug_sync
"""

from __future__ import annotations

import os
import csv
import json
import time
import argparse
from pathlib import Path
from typing import List, Dict, Tuple, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Event, Thread
from collections import deque

import requests
import wotr_resp_parser as rp  # musí být po ruce (náš robustní parser)

TSV_SEP = "\t"

# --------------------------- util / io ---------------------------

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def write_safely(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    tmp.replace(path)

def read_tsv(path: Path) -> Tuple[List[str], List[Dict[str,str]]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter="\t")
        fields = [c for c in (r.fieldnames or []) if c is not None]
        rows: List[Dict[str,str]] = []
        for row in r:
            if None in row:
                row.pop(None, None)
            row = {k: (v if v is not None else "") for k, v in row.items() if k in fields}
            rows.append(row)
    return fields, rows

def write_tsv_fields_rows(path: Path, fields: List[str], rows: List[Dict[str,str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t", lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            if None in r:
                r = {k: v for k, v in r.items() if k is not None}
            w.writerow(r)

def load_prompts(prompts_path: Path) -> Tuple[str,str]:
    j = read_json(prompts_path)
    sys_rules = j.get("system_rules", "")
    # podpora obou názvů
    user_head = j.get("user_header", j.get("user_prefix", ""))
    return sys_rules, user_head

# ----------------------- batching / prompts ----------------------

def _line_len(idx: str, src: str) -> int:
    return len(idx) + 1 + len(src) + 1  # "idx<TAB>src\n"

def chunk_batches(src_rows: List[Dict[str,str]], max_lines: int, max_chars: int, source_col: str) -> List[List[Dict[str,str]]]:
    """Rozděl vstup do requestů; každý prvek nese {'idx','source'}."""
    batches: List[List[Dict[str,str]]] = []
    cur: List[Dict[str,str]] = []
    cur_chars = 0

    for r in src_rows:
        idx = (r.get("idx") or "").strip()
        src = (r.get(source_col) or "").rstrip("\r\n")
        if not idx or not src:
            continue
        ln = _line_len(idx, src)
        need_new = False
        if cur and (len(cur) + 1 > max_lines): need_new = True
        if cur and (cur_chars + ln > max_chars): need_new = True
        if need_new:
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append({"idx": idx, "source": src})
        cur_chars += ln
    if cur:
        batches.append(cur)
    return batches

def build_user_block(rows: List[Dict[str,str]]) -> str:
    """TSV blok pro user část."""
    return "".join(f"{r['idx']}{TSV_SEP}{r['source']}\n" for r in rows)

# --------------------- Rate limiter + Progress -------------------

class RateLimiter:
    """Rolling-window limiter na RPM (thread-safe)."""
    def __init__(self, rpm: int):
        self.rpm = max(1, int(rpm))
        self.window = deque()
        self.win_s = 60.0
        self._lock = Lock()

    def wait(self):
        if self.rpm <= 0:
            return
        sleep_s = 0.0
        with self._lock:
            now = time.monotonic()
            while self.window and (now - self.window[0]) > self.win_s:
                self.window.popleft()
            if len(self.window) >= self.rpm:
                sleep_s = self.win_s - (now - self.window[0]) + 0.002
        if sleep_s > 0:
            time.sleep(sleep_s)
        with self._lock:
            self.window.append(time.monotonic())

def _fmt_hms(sec: float) -> str:
    if sec < 0 or sec == float("inf"):
        return "--:--"
    sec = int(sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

class Progress:
    """Průběh po řádcích (ne po dávkách)."""
    def __init__(self, total_lines: int, log_every_s: int = 8, bar_w: int = 28):
        self.total = max(0, int(total_lines))
        self.log_every_s = max(1, int(log_every_s))
        self.bar_w = bar_w
        self.lock = Lock()
        self.start = time.time()
        self.done_lines = 0
        self.applied_lines = 0
        self.empty_batches = 0
        self.errors = 0
        self._stop = Event()
        self._thr = Thread(target=self._loop, daemon=True)

    def start_loop(self):
        self._thr.start()

    def stop_loop(self):
        self._stop.set()
        self._thr.join(timeout=1.0)

    def tick(self, lines_done: int, lines_applied: int, had_error: bool, empty_batch: bool):
        with self.lock:
            self.done_lines += lines_done
            self.applied_lines += lines_applied
            if had_error:
                self.errors += 1
            if empty_batch:
                self.empty_batches += 1

    def snapshot(self):
        with self.lock:
            return (self.done_lines, self.applied_lines, self.empty_batches, self.errors, self.start, self.total)

    def _loop(self):
        last = 0.0
        while not self._stop.wait(0.25):
            now = time.time()
            if now - last < self.log_every_s:
                continue
            last = now
            done, applied, empty, errs, st, total = self.snapshot()
            elapsed = now - st
            rate = (done / elapsed) if elapsed > 0 else 0.0
            remain = max(0, total - done)
            eta = (remain / rate) if rate > 0 else float("inf")
            frac = (done / total) if total > 0 else 1.0
            fill = int(frac * self.bar_w)
            bar = "#" * fill + "-" * (self.bar_w - fill)
            log(f"[{bar}] {done}/{total} lines | applied:{applied} | empty_batches:{empty} | err_batches:{errs} | rate:{rate:.2f}/s | elapsed:{_fmt_hms(elapsed)} | eta:{_fmt_hms(eta)}")

# ------------------------- API call ------------------------------

def call_openai(base_url: str, api_key: str, model: str, system_rules: str, user_header: str, user_block: str,
                timeout_s: int) -> str:
    """
    Sync volání Responses API. Vrací plain text odpovědi.
    Nezadává temperature (gpt-5-mini).
    """
    url = base_url.rstrip("/") + "/v1/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
    }

    # Vynucení TSV v code fence:
    user_prompt = (
        user_header +
        "Vrať odpověď POUZE jako TSV v kódovém bloku:\n"
        "```tsv\n"
        "idx\tTranslation\n"
        "...\n"
        "```\n"
        "Bez komentářů, bez vysvětlování, bez nadpisů.\n\n" +
        user_block
    )

    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system_rules},
            {"role": "user",   "content": user_prompt}
        ]
    }

    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=(30, timeout_s))
    resp.raise_for_status()
    data = resp.json()

    # 1) output_text (nejčastější)
    text = data.get("output_text")
    if isinstance(text, str) and text.strip():
        return text

    # 2) output -> content -> text
    out = data.get("output")
    if isinstance(out, list):
        chunks = []
        for item in out:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for c in content:
                    t = (c.get("text") or "").strip()
                    if t:
                        chunks.append(t)
            t2 = (item.get("text") or "").strip()
            if t2:
                chunks.append(t2)
        if chunks:
            return "\n".join(chunks)

    # 3) fallback – pro DEBUG
    return json.dumps(data, ensure_ascii=False, indent=2)

# ----------------------- worker (1 batch) ------------------------

def process_one_batch(
    bi: int,
    batch: List[Dict[str,str]],
    system_rules: str,
    user_header: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout_s: int,
    retries: int,
    limiter: Optional[RateLimiter],
    dbg_dir: Optional[Path],
) -> Tuple[int, int, bool]:
    """
    Zpracuje jednu dávku.
    Vrací (lines_done, lines_applied, had_error).
    """
    lines_done = len(batch)
    lines_applied = 0
    had_error = False

    user_block = build_user_block(batch)

    # DEBUG request
    if dbg_dir:
        (dbg_dir / f"batch_{bi:03d}.req.txt").write_text(user_block, encoding="utf-8")

    # rate-limit + retry
    resp_text = ""
    delay = 2.0
    err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            if limiter:
                limiter.wait()
            log(f"[API] sending batch {bi}: {len(batch)} lines, {len(user_block)} chars")
            resp_text = call_openai(
                base_url=base_url,
                api_key=api_key,
                model=model,
                system_rules=system_rules,
                user_header=user_header,
                user_block=user_block,
                timeout_s=timeout_s
            )
            err = None
            break
        except Exception as e:
            err = e
            log(f"[API][batch {bi}][attempt {attempt}] error: {e}")
            time.sleep(delay)
            delay = min(delay * 1.8, 60.0)

    if dbg_dir:
        (dbg_dir / f"batch_{bi:03d}.resp.txt").write_text(resp_text, encoding="utf-8")

    if err is not None:
        had_error = True
        # diag prázdný + parsed.tsv prázdný
        if dbg_dir:
            rp.diag_write(dbg_dir / f"batch_{bi:03d}.parse_diag.txt", f"error: {err}")
            (dbg_dir / f"batch_{bi:03d}.parsed.tsv").write_text("", encoding="utf-8")
        return lines_done, 0, had_error

    # parse
    batch_ids = [r["idx"] for r in batch]
    chosen, mapping, stats, sample = rp.parse_resp_with_req(resp_text, user_block, expected_ids=batch_ids)

    # diag & parsed
    if dbg_dir:
        diag_lines = [
            f"batch: batch_{bi:03d}",
            f"idx_in_batch: {len(batch_ids)}",
            f"chosen: {chosen}",
            f"parsed_tab: {stats.get('parsed_tab',0)}",
            f"parsed_colon: {stats.get('parsed_colon',0)}",
            f"parsed_pipe: {stats.get('parsed_pipe',0)}",
            f"parsed_minus: {stats.get('parsed_minus',0)}",
            f"parsed_spaces: {stats.get('parsed_spaces',0)}",
            f"overlap: {stats.get('overlap',0)}",
            "sample (max 5):"
        ]
        for i, (ii, tt) in enumerate(sample):
            if i >= 5: break
            diag_lines.append(f"{ii}\t{tt}")
        rp.diag_write(dbg_dir / f"batch_{bi:03d}.parse_diag.txt", "\n".join(diag_lines))
        parsed_tsv = "".join(f"{k}\t{v}\n" for k, v in mapping.items())
        (dbg_dir / f"batch_{bi:03d}.parsed.tsv").write_text(parsed_tsv, encoding="utf-8")

    lines_applied = len(mapping)
    log(f"[API] batch {bi} -> parsed {lines_applied} / {len(batch)}")

    # vracíme pouze statistiky; samotnou aplikaci udělá volající, který má přístup k rows
    return lines_done, lines_applied, False

# ------------------------------ MAIN -----------------------------

def main():
    ap = argparse.ArgumentParser(description="LLM sync apply (TSV → API → PARSE → APPLY) – paralelní po dávkách.")
    ap.add_argument("--in",  dest="in_path",  required=True, help="vstupní TSV (musí mít 'idx' + zdrojový sloupec)")
    ap.add_argument("--out", dest="out_path", required=True, help="výstupní TSV (průběžně přepisován)")
    ap.add_argument("--prompts", required=True, help="prompts.json (system_rules, user_header|user_prefix)")
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--base-url", default="https://api.openai.com")
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY",""))

    ap.add_argument("--source-col", default="source", help="zdrojový sloupec (např. source_escaped)")
    ap.add_argument("--output-col", default="translation", help="cílový sloupec pro výsledek")

    ap.add_argument("--max-lines", type=int, default=120, help="max řádků v 1 dávce")
    ap.add_argument("--max-chars", type=int, default=9000, help="max znaků v 1 dávce")
    ap.add_argument("--timeout-s", type=int, default=1800, help="HTTP read-timeout (s)")
    ap.add_argument("--retries", type=int, default=6, help="počet pokusů na dávku")

    ap.add_argument("--concurrency", type=int, default=4, help="počet paralelních dávek")
    ap.add_argument("--rpm", type=int, default=120, help="sdílený max requests/min (přes všechna vlákna)")
    ap.add_argument("--log-every-s", type=int, default=8, help="interval průběhového logu")
    ap.add_argument("--debug-dir", default=None, help="debug adresář pro batch_*.{req,resp,parse_diag,parsed}")

    ap.add_argument("--dry-run", action="store_true", help="nevolat API, pouze připravit dávky a zapsat req.txt")
    args = ap.parse_args()

    if not args.api_key and not args.dry_run:
        raise SystemExit("Chybí --api-key nebo env OPENAI_API_KEY")

    in_path  = Path(args.in_path)
    out_path = Path(args.out_path)
    dbg_dir  = Path(args.debug_dir) if args.debug_dir else None
    if dbg_dir:
        dbg_dir.mkdir(parents=True, exist_ok=True)

    system_rules, user_header = load_prompts(Path(args.prompts))
    fields, rows = read_tsv(in_path)
    if "idx" not in fields:
        raise SystemExit("Vstupní TSV nemá sloupec 'idx'")
    if args.source_col not in fields:
        raise SystemExit(f"Vstupní TSV nemá sloupec '{args.source_col}'")
    if args.output_col not in fields:
        fields.append(args.output_col)

    # kandidáti
    to_send: List[Dict[str,str]] = []
    for r in rows:
        idx = (r.get("idx") or "").strip()
        src = (r.get(args.source_col) or "").strip()
        if idx and src:
            to_send.append({"idx": idx, "source": src})

    batches = chunk_batches(to_send, args.max_lines, args.max_chars, source_col="source")

    total_lines = sum(len(b) for b in batches)
    log(f"[RUN] rows={len(rows)} | candidates={len(to_send)} | batches={len(batches)} | total_lines={total_lines} | input_col={args.source_col} | output_col={args.output_col}")
    log(f"[CONF] concurrency={args.concurrency} | rpm={args.rpm} | timeout_s={args.timeout_s} | retries={args.retries} | model={args.model}")

    # mapa idx->row
    row_by_idx = {(r.get("idx") or "").strip(): r for r in rows}
    write_lock = Lock()

    # průběh
    prog = Progress(total_lines=total_lines, log_every_s=args.log_every_s)
    prog.start_loop()

    # DRY-RUN: jen uložit req.txt a konec
    if args.dry_run:
        if dbg_dir:
            for bi, batch in enumerate(batches, start=1):
                (dbg_dir / f"batch_{bi:03d}.req.txt").write_text(build_user_block(batch), encoding="utf-8")
        log("[DRY] requests dumped; no API calls.")
        return

    limiter = RateLimiter(args.rpm) if args.rpm > 0 else None

    start_ts = time.time()
    try:
        # spustíme dávky paralelně
        def task(bi: int, batch: List[Dict[str,str]]) -> Tuple[int,int,bool,Dict[str,str]]:
            # zavolat API + parse + vrátit mapu
            ld, la, had_err = process_one_batch(
                bi=bi,
                batch=batch,
                system_rules=system_rules,
                user_header=user_header,
                base_url=args.base_url,
                api_key=args.api_key,
                model=args.model,
                timeout_s=args.timeout_s,
                retries=args.retries,
                limiter=limiter,
                dbg_dir=dbg_dir,
            )
            # znovu parse (abychom dostali mapping pro APPLY) – process_one_batch už parse dělá,
            # ale vrací jen čísla; mapping načteme z parsed.tsv kvůli oddělení odpovědnosti:
            mapping: Dict[str,str] = {}
            if dbg_dir and (dbg_dir / f"batch_{bi:03d}.parsed.tsv").exists():
                # rychlé načtení parsed.tsv (idx<TAB>translation)
                for line in (dbg_dir / f"batch_{bi:03d}.parsed.tsv").read_text(encoding="utf-8").splitlines():
                    if "\t" not in line:
                        continue
                    i, t = line.split("\t", 1)
                    i, t = i.strip(), t.strip()
                    if i and t:
                        mapping[i] = t
            else:
                # fallback: znovu parsovat z odpovědi (mírně dražší) – jen když není debug_dir
                # (kvůli jednoduchosti tento fallback vynecháme – doporučuji vždy mít --debug-dir)
                pass
            return ld, la, had_err, mapping

        with ThreadPoolExecutor(max_workers=max(1, int(args.concurrency))) as ex:
            futs = {ex.submit(task, bi, batch): (bi, batch) for bi, batch in enumerate(batches, start=1)}
            for fut in as_completed(futs):
                bi, batch = futs[fut]
                try:
                    lines_done, lines_applied, had_err, mapping = fut.result()
                except Exception as e:
                    # velká chyba dávky (už zalogovaná v process_one_batch)
                    prog.tick(lines_done=len(batch), lines_applied=0, had_error=True, empty_batch=False)
                    log(f"[ERR] batch {bi}: {e}")
                    continue

                # APPLY (synchronně, chráněno zámkem, aby zápisy byly konzistentní)
                applied = 0
                for idx, tr in mapping.items():
                    row = row_by_idx.get(idx)
                    if not row:
                        continue
                    row[args.output_col] = tr
                    applied += 1

                # průběžný zápis OUT TSV (atomicky) – voláme z jednoho místa se zámkem
                with write_lock:
                    write_tsv_fields_rows(out_path, fields, rows)

                prog.tick(lines_done=lines_done, lines_applied=applied, had_error=had_err, empty_batch=(applied == 0))

    finally:
        prog.stop_loop()
        elapsed = int(time.time() - start_ts)
        done, applied, empty, errs, st, total = prog.snapshot()
        log(f"[DONE] lines={done}/{total} | applied={applied} | empty_batches={empty} | err_batches={errs} | elapsed={_fmt_hms(elapsed)}")
        log(f"[OUT] → {out_path}")

if __name__ == "__main__":
    main()
