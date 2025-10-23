#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_embed_client.py
--------------------
Lehký embed klient s průběhem, throttlingem a retry:
- Konfigurovatelný model, batch_size, concurrency (vlákna) a RPM (globální limit).
- Tiskne průběžný progress bar + metriky (zvládne i bez tqdm).
- Vrací list[Embedding] ve STEJNÉM pořadí jako vstup.
- Lze spouštět i samostatně (CLI) pro smoke-test.

Závislosti: openai, requests, numpy (nainstalováno v tvém venv).
"""

from __future__ import annotations
import os, time, json, math, threading
from pathlib import Path
from typing import List, Optional, Iterable, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

# volitelné – pěkný progress bar; když není, použijeme fallback logy
try:
    from tqdm import tqdm  # type: ignore
    HAS_TQDM = True
except Exception:
    HAS_TQDM = False

# SDK je pohodlné; když by selhalo, fallback na requests
try:
    from openai import OpenAI
    HAS_OPENAI = True
except Exception:
    HAS_OPENAI = False

import requests

DEFAULT_MODEL = "text-embedding-3-small"

# ---------------- Rate limiter (thread-safe, rolling window RPM) ----------------
class RateLimiter:
    def __init__(self, rpm: int):
        self.rpm = max(1, int(rpm))
        self.win = []
        self.lock = threading.Lock()
        self.WIN_S = 60.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            # drop stale timestamps
            self.win = [t for t in self.win if now - t < self.WIN_S]
            if len(self.win) >= self.rpm:
                sleep_s = self.WIN_S - (now - self.win[0]) + 0.002
            else:
                sleep_s = 0.0
        if sleep_s > 0:
            time.sleep(sleep_s)
        with self.lock:
            self.win.append(time.monotonic())

# ---------------- Embedding API ----------------
def _embed_batch_openai_sdk(client, model: str, texts: List[str], timeout_s: int) -> List[List[float]]:
    res = client.embeddings.create(model=model, input=texts, timeout=timeout_s)
    # OpenAI vrací embeddings ve stejném pořadí
    return [d.embedding for d in res.data]

def _embed_batch_requests(base_url: str, api_key: str, model: str, texts: List[str], timeout_s: int) -> List[List[float]]:
    url = base_url.rstrip("/") + "/embeddings"
    hdr = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "input": texts}
    r = requests.post(url, headers=hdr, data=json.dumps(payload), timeout=timeout_s)
    r.raise_for_status()
    data = r.json()
    # odpověď drží pořadí
    return [data["data"][i]["embedding"] for i in range(len(texts))]

# ---------------- High-level API s průběhem ----------------
def embed_texts_with_progress(
    texts: List[str],
    model: str = DEFAULT_MODEL,
    batch_size: int = 64,
    concurrency: int = 4,
    rpm: int = 120,
    timeout_s: int = 180,
    retries: int = 4,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    show_progress: bool = True,
) -> List[List[float]]:
    """
    Vypočte embedding pro každý text, zachová pořadí, tiskne průběh/ETA.

    Parametry:
      texts         – vstupní seznam textů
      model         – název embedding modelu (default text-embedding-3-small)
      batch_size    – kolik inputů poslat v jednom API callu
      concurrency   – kolik paralelních workerů
      rpm           – globální limit požadavků za minutu
      timeout_s     – read-timeout
      retries       – kolik pokusů na batch
      base_url      – custom endpoint (default https://api.openai.com/v1)
      api_key       – čti z OPENAI_API_KEY, když None
      show_progress – tisk progress baru/ETA

    Návrat: list embeddingů zarovnaných na pořadí vstupu (len= len(texts)).
    """
    if not texts:
        return []

    api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("Chybí OPENAI_API_KEY.")

    base_url = base_url or "https://api.openai.com/v1"
    limiter = RateLimiter(rpm) if rpm > 0 else None

    # inicializace klienta
    client = None
    if HAS_OPENAI:
        client = OpenAI(api_key=api_key, base_url=base_url)

    # připrav dávky (indexy + texty)
    jobs: List[Tuple[int, List[int], List[str]]] = []  # (job_id, idxs, batch_texts)
    N = len(texts)
    order_indices = list(range(N))
    job_id = 0
    for i in range(0, N, batch_size):
        idxs = order_indices[i:i+batch_size]
        btxt = [texts[k] for k in idxs]
        jobs.append((job_id, idxs, btxt))
        job_id += 1

    # sdílené úložiště výsledků (správné pořadí)
    out = [None] * N  # type: ignore

    # progress
    start = time.time()
    total_jobs = len(jobs)
    completed = 0
    lock = threading.Lock()

    if HAS_TQDM and show_progress:
        pbar = tqdm(total=total_jobs, unit="batch", desc="Embeddings")
    else:
        pbar = None

    def _work(job):
        nonlocal completed
        jid, idxs, btxt = job
        # throttle
        if limiter:
            limiter.wait()
        # retry smyčka
        delay = 2.0
        last_err = None
        for attempt in range(1, retries+1):
            try:
                if client is not None:
                    embs = _embed_batch_openai_sdk(client, model, btxt, timeout_s)
                else:
                    embs = _embed_batch_requests(base_url, api_key, model, btxt, timeout_s)
                # ulož do out podle indexů
                for local_i, glob_i in enumerate(idxs):
                    out[glob_i] = embs[local_i]
                break
            except Exception as e:
                last_err = e
                time.sleep(delay)
                delay = min(delay * 1.8, 60.0)
        else:
            # po vyčerpání pokusů: zapiš None a posuň se dál
            for glob_i in idxs:
                out[glob_i] = None
        # progress
        with lock:
            completed += 1
            if pbar:
                pbar.update(1)
                pbar.set_postfix_str(f"{completed}/{total_jobs}")
            elif show_progress:
                now = time.time()
                rate = completed / max(1e-6, (now - start))
                remain = total_jobs - completed
                eta = remain / max(1e-6, rate)
                print(f"[emb] {completed}/{total_jobs} | rate {rate:.2f} batch/s | elapsed {fmt_hms(now-start)} | eta {fmt_hms(eta)}", flush=True)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(_work, job) for job in jobs]
        for _ in as_completed(futs):
            pass

    if pbar:
        pbar.close()

    # validace – nahradíme None nulovým vektorem (nebo vyhodíme)
    # Zde raději zvednu výjimku, ať víš, že něco selhalo:
    failed = sum(1 for v in out if v is None)
    if failed:
        raise RuntimeError(f"Embeddings selhaly u {failed} batch(e/ů). Sniž --rpm/--concurrency nebo zvyšte retries/timeout.")

    return out  # type: ignore

def fmt_hms(sec: float) -> str:
    sec = int(sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

# ---------------- CLI smoke-test ----------------
if __name__ == "__main__":
    demo = ["Hello from Kenabres.", "Ahoj z Kenabres.", "Staunton Vhane"]
    embs = embed_texts_with_progress(
        demo,
        model=DEFAULT_MODEL,
        batch_size=2,
        concurrency=2,
        rpm=120,
        timeout_s=180,
        retries=3,
        show_progress=True,
    )
    print("OK, got", len(embs), "vectors; dim =", len(embs[0]))
