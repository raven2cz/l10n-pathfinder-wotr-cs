#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
wotr_feminize_apply_tsv_parallel.py
-----------------------------------
Paralelní feminizační průchod přes TSV s průběhem (ETA) a guardy.

- 1 řádek = 1 API volání (bez temperature), volá se paralelně.
- Throttling přes --rpm (requests/min) napříč všemi vlákny.
- Guardy: {g|…}…{/g} i obecné {...} bloky musí zůstat 1:1 (řetězce i pořadí).
- Do výstupu zapisuje POUZE řádky, kde se text změnil a guardy prošly.
- Zachovává pořadí vstupu.
- Bohatý průběh: metriky, rychlost, ETA, progress bar.

Použití (příklad PowerShell):
  python wotr_feminize_apply_tsv_parallel.py `
    --in .\audit\female_heroes_only_filtered.tsv `
    --out .\audit\female_feminized_changed.tsv `
    --prompts .\prompts-feminine.json `
    --speaker-col speaker_name `
    --text-col cs_text `
    --concurrency 6 `
    --rpm 120 `
    --timeout-s 180 `
    --retries 6 `
    --log-every-s 8 `
    --debug-dir .\audit\debug_feminize
"""

from __future__ import annotations
import csv
import time
import json
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from threading import Thread, Lock, Event

import re

from wotr_feminize_service import FeminizeService, DEFAULT_MODEL

# ---------- Guards ----------
BRACED_ANY = re.compile(r"\{[^{}]*\}")
GLINK = re.compile(r"\{g\|[^{}]*\}.*?\{\/g\}", re.DOTALL)

def extract_braced_chunks(s: str) -> List[str]:
    return BRACED_ANY.findall(s or "")

def extract_glink_chunks(s: str) -> List[str]:
    return GLINK.findall(s or "")

def links_preserved(src: str, dst: str) -> bool:
    return extract_glink_chunks(src) == extract_glink_chunks(dst)

def braced_balance_ok(src: str, dst: str) -> bool:
    return extract_braced_chunks(src) == extract_braced_chunks(dst)

# ---------- I/O helpers ----------
def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def write_debug(debug_dir: Optional[Path], stem: str, kind: str, payload: dict | str) -> None:
    if not debug_dir:
        return
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        p = debug_dir / f"{stem}.{kind}.json"
        data = {"data": payload} if isinstance(payload, str) else payload
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

# ---------- Rate limiter (thread-safe) ----------
class RateLimiter:
    """Rolling-window limiter na RPM (requests per minute). Thread-safe."""
    def __init__(self, rpm: int):
        self.rpm = max(1, int(rpm))
        self.window = deque()  # timestamps
        self.window_s = 60.0
        self._lock = Lock()

    def wait_for_slot(self):
        if self.rpm <= 0:
            return
        with self._lock:
            now = time.monotonic()
            while self.window and (now - self.window[0]) > self.window_s:
                self.window.popleft()
            if len(self.window) >= self.rpm:
                sleep_s = self.window_s - (now - self.window[0]) + 0.002
            else:
                sleep_s = 0.0
            if sleep_s > 0:
                # usni mimo zámek
                pass
        if sleep_s > 0:
            time.sleep(sleep_s)
        with self._lock:
            self.window.append(time.monotonic())

# ---------- Progress / ETA ----------
def _fmt_hms(sec: float) -> str:
    if sec < 0 or sec == float("inf"):
        return "--:--"
    sec = int(sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

class Progress:
    def __init__(self, total: int, log_every_s: int = 8, bar_width: int = 28):
        self.total = max(0, int(total))
        self.log_every_s = max(1, int(log_every_s))
        self.bar_width = bar_width

        self.lock = Lock()
        self.start_ts = time.time()
        self.done = 0
        self.changed = 0
        self.unchanged = 0
        self.violations = 0
        self.errors = 0

        self._stop = Event()
        self._thread = Thread(target=self._reporter_loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1.0)

    def tick(self, status: str):
        with self.lock:
            self.done += 1
            if status == "changed":
                self.changed += 1
            elif status == "unchanged":
                self.unchanged += 1
            elif status == "violation":
                self.violations += 1
            elif status == "error":
                self.errors += 1

    def snapshot(self):
        with self.lock:
            return (self.done, self.changed, self.unchanged, self.violations, self.errors, self.start_ts, self.total)

    def _reporter_loop(self):
        last_print = 0.0
        while not self._stop.wait(0.25):
            now = time.time()
            if now - last_print < self.log_every_s:
                continue
            last_print = now
            done, changed, unchanged, viol, errs, st, total = self.snapshot()
            elapsed = now - st
            rate = (done / elapsed) if elapsed > 0 else 0.0
            remain = max(0, total - done)
            eta = (remain / rate) if rate > 0 else float("inf")

            # progress bar
            frac = (done / total) if total > 0 else 1.0
            fill = int(frac * self.bar_width)
            bar = "#" * fill + "-" * (self.bar_width - fill)

            log(
                f"[{bar}] {done}/{total} | "
                f"chg:{changed} ~ same:{unchanged} ~ guard:{viol} ~ err:{errs} | "
                f"rate:{rate:.2f}/s | elapsed:{_fmt_hms(elapsed)} | eta:{_fmt_hms(eta)}"
            )

# ---------- Worker ----------
def _worker_do(
    ordinal: int,
    row: Dict[str, str],
    speaker_col: str,
    text_col: str,
    svc: FeminizeService,
    limiter: Optional[RateLimiter],
    debug_dir: Optional[Path],
) -> Tuple[int, Optional[str], str]:
    """
    Vrací (ordinal, changed_text_or_None, status):
      status ∈ {"changed","unchanged","violation","error"}
    """
    speaker = (row.get(speaker_col) or "").strip()
    src_text = (row.get(text_col) or "").rstrip("\n")

    if not src_text:
        return ordinal, None, "unchanged"

    if limiter:
        limiter.wait_for_slot()

    try:
        out_text = svc.feminize(src_text, speaker=speaker)
    except Exception as e:
        write_debug(debug_dir, f"row_{ordinal:06d}", "error", {
            "speaker": speaker, "src": src_text, "error": str(e)
        })
        return ordinal, None, "error"

    if out_text == src_text:
        return ordinal, None, "unchanged"

    # Guardy – výskyty i pořadí musí sedět
    ok_links = links_preserved(src_text, out_text)
    ok_brace = braced_balance_ok(src_text, out_text)
    if not (ok_links and ok_brace):
        write_debug(debug_dir, f"row_{ordinal:06d}", "violation", {
            "speaker": speaker, "src": src_text, "out": out_text,
            "links_ok": ok_links, "braces_ok": ok_brace,
        })
        return ordinal, None, "violation"

    return ordinal, out_text, "changed"

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="Paralelní feminizační průchod přes TSV – s ETA a guardy.")
    ap.add_argument("--in", dest="in_path", required=True, help="Vstupní TSV")
    ap.add_argument("--out", dest="out_path", required=True, help="Výstupní TSV (jen změněné řádky)")
    ap.add_argument("--prompts", dest="prompts_path", required=True, help="prompts-feminine.json (system_rules + user_prefix)")

    ap.add_argument("--speaker-col", default="speaker_name", help="Sloupec se jménem mluvčí (default: speaker_name)")
    ap.add_argument("--text-col", default="cs_text", help="Sloupec s českým textem k úpravě (default: cs_text)")

    ap.add_argument("--api-key", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout-s", type=int, default=180)
    ap.add_argument("--retries", type=int, default=6)

    ap.add_argument("--concurrency", type=int, default=6, help="Počet paralelních workerů (vláken)")
    ap.add_argument("--rpm", type=int, default=120, help="Max requests per minute (celkově přes všechna vlákna)")
    ap.add_argument("--limit", type=int, default=0, help="Zpracuj max N řádků (0=bez limitu)")
    ap.add_argument("--log-every-s", type=int, default=8, help="Interval status logu (s)")
    ap.add_argument("--debug-dir", default=None, help="Adresář pro debug JSONy (chyby/porušení guardů)")

    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)
    debug_dir = Path(args.debug_dir) if args.debug_dir else None

    # Načtení vstupu
    with in_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = reader.fieldnames or []
        if not fieldnames:
            raise SystemExit("Input TSV nemá header.")
        for col in (args.speaker_col, args.text_col):
            if col not in fieldnames:
                raise SystemExit(f"Chybí sloupec '{col}'. K dispozici: {fieldnames}")
        rows = list(reader)

    if args.limit > 0:
        rows = rows[:args.limit]

    total = len(rows)
    new_col = "cs_text_female"
    out_fieldnames = list(fieldnames)
    if new_col not in out_fieldnames:
        out_fieldnames.append(new_col)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    outf = out_path.open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(outf, fieldnames=out_fieldnames, delimiter="\t", lineterminator="\n")
    writer.writeheader()

    # Info o běhu
    log(f"[RUN] rows={total} | speaker_col={args.speaker_col} | text_col={args.text_col}")
    log(f"[CONF] concurrency={args.concurrency} | rpm={args.rpm} | timeout_s={args.timeout_s} | retries={args.retries} | model={args.model}")

    # Service pool (1 instance / worker)
    def make_service():
        return FeminizeService(
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
            timeout_s=args.timeout_s,
            retries=args.retries,
            prompts_path=args.prompts_path,
            debug_dir=debug_dir,
        )

    services = [make_service() for _ in range(max(1, args.concurrency))]
    limiter = RateLimiter(args.rpm) if args.rpm > 0 else None

    progress = Progress(total=total, log_every_s=args.log_every_s)
    progress.start()

    results: Dict[int, Tuple[Optional[str], str]] = {}

    start_ts = time.time()
    try:
        with ThreadPoolExecutor(max_workers=len(services)) as ex:
            futures = []
            for ordinal, row in enumerate(rows, start=1):
                svc = services[(ordinal - 1) % len(services)]
                fut = ex.submit(_worker_do, ordinal, row, args.speaker_col, args.text_col, svc, limiter, debug_dir)
                futures.append(fut)

            for fut in as_completed(futures):
                ordinal, changed_text, status = fut.result()
                results[ordinal] = (changed_text, status)
                progress.tick(status)

        # zápis jen změněných řádků v pořadí
        written = 0
        for ordinal, row in enumerate(rows, start=1):
            changed, status = results.get(ordinal, (None, "error"))
            if not changed:
                continue
            out_row = dict(row)
            out_row[new_col] = changed
            writer.writerow(out_row)
            written += 1

    finally:
        try:
            outf.close()
        except Exception:
            pass
        progress.stop()

    done, changed, unchanged, viol, errs, st, _ = progress.snapshot()
    elapsed = int(time.time() - start_ts)
    log(f"[DONE] processed={done}/{total} | changed={changed} | same={unchanged} | guard_violations={viol} | errors={errs} | elapsed={_fmt_hms(elapsed)}")
    log(f"[OUT] → {out_path}")

if __name__ == "__main__":
    main()
