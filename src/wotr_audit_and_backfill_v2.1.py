#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_audit_and_backfill_v2.1.py (FINAL)
=====================================

Audit + backfill pro překlad Pathfinder: Wrath of the Righteous (JSON → TSV → JSON).

Co dělá
-------
A) AUDIT
   • Načte:
       - out_dir/map.json             (stabilní idx → GUID)
       - enGB.json                    (strings)
       - out_dir/trans/req_*.trans.tsv (výstupy překladu; poslední výhra)
   • Najde:
       - chybějící překlady (missing)
       - podezřelé překlady (suspect) dle kombinace:
           · Jaccard podobnost (EN vs. CS)
           · heuristika "je to česky?" (diakritika / častá CZ slova)
           · poměr délek (příliš krátké)
           · dvojjazyčné šipky ("->", "→")
           · vložený zdroj v překladu (contains_source)
       - chybné překlady (corrupt) = krátký EN label (1–3 slova) přeložený
         dlouhou větou/odstavcem (podezření na záměnu indexů). Detekce je
         konfigurovatelná prahy (viz CLI).
   • Zapíše:
       - out_dir/audit/missing.tsv      (idx<TAB>Source)
       - out_dir/audit/suspect.tsv      (idx<TAB>Source<TAB>Translation<TAB>reason)
       - out_dir/audit/corrupt.tsv      (idx<TAB>Source<TAB>Translation<TAB>reason)
       - out_dir/audit/summary.json     (počty + parametry)

B) BACKFILL (volitelné)
   • Z (missing/suspect/corrupt/both/all) vytvoří nové dávky requestů:
       - out_dir/requests/req_XXX.jsonl
       - out_dir/states/  req_XXX.state.json (status="prepared")
   • **Bezpečné číslování**: XXX je vždy vyšší než jakákoliv existující dávka
     napříč requests/states/trans/results (nikdy nic nepřepíše).
   • **Limit velikosti dávky v bajtech**: --batch-max-bytes (např. 200000 pro ~200 kB).
     Při serializaci JSONL průběžně měří skutečné UTF-8 bajty a flushne před překročením limitu.
   • V rámci dávky se jednotlivé requesty skládají z TSV řádků (idx<TAB>Source),
     s limity --max-lines / --max-chars na JEDEN request.
   • **Oddělené prompty**: lze předat různé *.json pro MISSING/SUSPECT/CORRUPT
     (viz --prompts-file-xxx). Není-li zadáno, použije se --prompts-file.

Kompatibilita
-------------
• Formát requestů je identický s hlavním skriptem v2 (input: system_rules + user_header + TSV blok).
• custom_id jsou unikátní s prefixy: "AUD_MISS", "AUD_SUS", "AUD_CORR".

Použití – příklady
------------------
1) Jen audit:
   python wotr_audit_and_backfill_v2.py -i enGB.json -o out_wotr --prompts-file prompts.json

2) Audit + backfill (missing + suspect + corrupt), dávky cca 200 kB:
   python wotr_audit_and_backfill_v2.py -i enGB.json -o out_wotr --prompts-file prompts.json ^
     --make-requests all --batch-max-bytes 200000 --max-lines 120 --max-chars 9000

   # Poté přelož jen nově vytvořené dávky (sync):
   # (rozsah viz out_wotr/audit/backfill_manifest.json)
   python wotr_oneclick_translate_v2.py -i enGB.json -o out_wotr --prompts-file prompts.json ^
     --skip-prepare --run --mode sync --sync-progress-secs 5 --merge

Poznámky
--------
• Pokud zadáš i --batch-budget (tokenový limit), uplatní se jako sekundární pojistka; primárně
  se však řídíme limitem --batch-max-bytes (jakmile je nastaven).
• Vše idempotentní – dávky se vždy číslují až za maximem; existující soubory se nikdy nepřepisují.
"""

from __future__ import annotations
import re
import os
import sys
import json
import math
import argparse
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional
from datetime import datetime

TSV_SEP = "\t"

# ---------- Pomocné I/O ----------

def read_json(path: Path) -> Any:
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

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ---------- Načtení dat ----------

def load_map(out_dir: Path) -> Dict[str, str]:
    mpath = out_dir / "map.json"
    if not mpath.exists():
        raise FileNotFoundError("Nenalezen out_dir/map.json (spusť nejdřív hlavní překlad – prepare).")
    return read_json(mpath)

def load_en_strings(en_json: Path) -> Dict[str, Any]:
    data = read_json(en_json)
    if "strings" not in data or not isinstance(data["strings"], dict):
        raise ValueError("enGB.json neobsahuje objekt 'strings'.")
    return data["strings"]

def load_all_translations(out_dir: Path) -> Dict[str, str]:
    """Načti všechny trans/req_*.trans.tsv → idx→Translation (poslední výhra)."""
    idx2tr: Dict[str, str] = {}
    tdir = out_dir / "trans"
    if not tdir.exists():
        return idx2tr
    for tsv in sorted(tdir.glob("req_*.trans.tsv")):
        for line in tsv.read_text(encoding="utf-8").splitlines():
            if TSV_SEP not in line:
                continue
            idx, tr = line.split(TSV_SEP, 1)
            idx, tr = idx.strip(), tr.strip()
            if idx and tr:
                idx2tr[idx] = tr
    return idx2tr

# ---------- Tokenizace + metriky ----------

WORD_RE = re.compile(r"[0-9A-Za-zÀ-ž]+", re.UNICODE)  # jednoduché tokeny vč. diakritiky
CZ_DIACR = set("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ")
CZ_HINT_WORDS = {
    "že","se","jsem","jsi","bude","být","tak","jen","už","když","který","která","které",
    "ten","ta","to","a","v","na","pro","z","do","tady","tam","nebo"
}

def tokens(s: str) -> List[str]:
    return [m.group(0).casefold() for m in WORD_RE.finditer(s)]

def jaccard(a: List[str], b: List[str]) -> float:
    if not a and not b:
        return 1.0
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    uni = len(sa | sb)
    return inter / uni if uni else 0.0

def likely_czech(s: str, min_cz_chars: int) -> bool:
    if min_cz_chars <= 0:
        return True
    dia = sum(1 for ch in s if ch in CZ_DIACR)
    if dia >= min_cz_chars:
        return True
    low = s.casefold()
    hits = sum(1 for w in CZ_HINT_WORDS if f" {w} " in f" {low} ")
    return hits >= 2

def has_bilingual_arrow(s: str) -> bool:
    return ("->" in s) or ("→" in s)

# ---------- AUDIT ----------

def build_idx_to_source(id_map: Dict[str, str], en_strings: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for idx, guid in id_map.items():
        out[idx] = en_strings.get(guid, "")
    return out

def detect_corrupt(src: str,
                   tr: str,
                   src_max_words: int,
                   tr_min_words: int,
                   min_len_ratio: float) -> Optional[str]:
    """
    Heuristika „corrupt“:
      - Zdroj je krátký label: words(src) ≤ src_max_words (default 3).
      - Překlad je nepřirozeně dlouhý: words(tr) ≥ tr_min_words (default 10)
        NEBO len(tr)/len(src) ≥ min_len_ratio (default 3.0).
      - Bonus flag: obsahuje větné ukončení/příznaky vět (., !, ?, :), více čárek apod.
    Vrací textový důvod, nebo None.
    """
    s = (src or "").strip()
    t = (tr or "").strip()
    if not s or not t:
        return None

    s_words = tokens(s)
    t_words = tokens(t)
    if len(s_words) == 0:
        return None

    cond_src_short = len(s_words) <= src_max_words
    ratio = (len(t) / max(1, len(s)))
    cond_tr_long = (len(t_words) >= tr_min_words) or (ratio >= min_len_ratio)

    if not (cond_src_short and cond_tr_long):
        return None

    # sentence-like punctuation as extra evidence
    punct_hits = 0
    for ch in (".", "!", "?", ":", ";"):
        punct_hits += t.count(ch)
    commas = t.count(",")
    newlines = t.count("\n")
    extra = []
    if punct_hits > 0: extra.append(f"sent_punct={punct_hits}")
    if commas >= 2:    extra.append(f"commas={commas}")
    if newlines > 0:   extra.append(f"nl={newlines}")

    reason = f"short_src_long_tr: src_words={len(s_words)}, tr_words={len(t_words)}, len_ratio={ratio:.2f}"
    if extra:
        reason += ";" + ",".join(extra)
    return reason

def audit(idx2src: Dict[str, str],
          idx2tr: Dict[str, str],
          jaccard_threshold: float,
          min_czech_chars: int,
          min_len_ratio: float,
          flag_bilingual: bool,
          corrupt_src_max_words: int,
          corrupt_tr_min_words: int,
          corrupt_min_len_ratio: float
          ) -> Tuple[
              List[Tuple[str,str]],
              List[Tuple[str,str,str,str]],
              List[Tuple[str,str,str,str]]
          ]:
    """
    Vrací:
      missing:  [(idx, source)]
      suspect:  [(idx, source, translation, reason)]
      corrupt:  [(idx, source, translation, reason)]
    """
    missing: List[Tuple[str,str]] = []
    suspect: List[Tuple[str,str,str,str]] = []
    corrupt: List[Tuple[str,str,str,str]] = []

    for idx, src in idx2src.items():
        tr = idx2tr.get(idx, "").strip()

        if not tr:
            missing.append((idx, src))
            continue

        # CORRUPT (vyhodnocuj dřív, ať to neulpí jen v "suspect")
        corr_reason = detect_corrupt(src, tr,
                                     src_max_words=corrupt_src_max_words,
                                     tr_min_words=corrupt_tr_min_words,
                                     min_len_ratio=corrupt_min_len_ratio)
        if corr_reason:
            corrupt.append((idx, src, tr, corr_reason))
            # zároveň ale necháme proběhnout i suspect heuristiky (může být v obou reportech)

        # SUSPECT
        reasons: List[str] = []

        if tr == src:
            reasons.append("identical")

        jac = jaccard(tokens(src), tokens(tr))
        if jac >= jaccard_threshold and not likely_czech(tr, min_czech_chars):
            reasons.append(f"jaccard_high:{jac:.2f}_no_czech")

        if len(src) > 0:
            ratio = len(tr) / max(1, len(src))
            if ratio < min_len_ratio:
                reasons.append(f"too_short:{ratio:.2f}")

        if flag_bilingual and has_bilingual_arrow(tr):
            reasons.append("bilingual_arrow")

        if src and src in tr:
            reasons.append("contains_source")

        if reasons:
            suspect.append((idx, src, tr, ";".join(reasons)))

    return missing, suspect, corrupt

# ---------- Zápis reportů ----------

def write_audit_reports(out_dir: Path,
                        missing: List[Tuple[str,str]],
                        suspect: List[Tuple[str,str,str,str]],
                        corrupt: List[Tuple[str,str,str,str]],
                        params: Dict[str, Any]) -> None:
    adir = out_dir / "audit"
    adir.mkdir(parents=True, exist_ok=True)

    miss_path = adir / "missing.tsv"
    sus_path  = adir / "suspect.tsv"
    cor_path  = adir / "corrupt.tsv"
    summ_path = adir / "summary.json"

    write_safely(miss_path, "\n".join(f"{idx}\t{src}" for idx, src in missing) + ("\n" if missing else ""))
    write_safely(sus_path,  "\n".join(f"{idx}\t{src}\t{tr}\t{why}" for idx, src, tr, why in suspect) + ("\n" if suspect else ""))
    write_safely(cor_path,  "\n".join(f"{idx}\t{src}\t{tr}\t{why}" for idx, src, tr, why in corrupt) + ("\n" if corrupt else ""))
    summary = {
        "missing_count": len(missing),
        "suspect_count": len(suspect),
        "corrupt_count": len(corrupt),
        "params": params,
        "timestamp": datetime.now().isoformat(timespec="seconds")
    }
    write_safely(summ_path, json.dumps(summary, ensure_ascii=False, indent=2))
    log(f"[AUDIT] missing={len(missing)} | suspect={len(suspect)} | corrupt={len(corrupt)}")
    log(f"[AUDIT] → {miss_path.name}, {sus_path.name}, {cor_path.name}, {summ_path.name}")

# ---------- Prompts (rules) ----------

def load_prompts(prompts_file: Optional[Path]) -> Tuple[str, str]:
    """Vrátí (system_rules, user_header)."""
    default_sys = (
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
    default_usr = (
        "Pro každý vstupní řádek `idx\\tSource` vrať přesně jeden řádek `idx\\tTranslation` se stejným `idx`. "
        "Uvnitř {g|…}…{/g} nic neměň ani nepřekládej; u ostatních {…} přelož obsah a ponech závorky. "
        "Herní mechaniky překládej, číselné hodnoty ponech.\n\n"
    )
    if prompts_file and prompts_file.exists():
        j = read_json(prompts_file)
        sys_rules = j.get("system_rules", default_sys)
        user_head = j.get("user_header", default_usr)
        return sys_rules, user_head
    return default_sys, default_usr

# ---------- Bezpečné číslování dávek ----------

REQ_NUM_RX = re.compile(r"^req_(\d{3})")

def _collect_used_batch_numbers(out_dir: Path) -> set[int]:
    """Seber všechna už použitá čísla dávek napříč složkami (requests/states/trans/results)."""
    used: set[int] = set()
    patterns = [
        (out_dir / "states",   "req_*.state.json"),
        (out_dir / "requests", "req_*.jsonl"),
        (out_dir / "trans",    "req_*.trans.tsv"),
        (out_dir / "results",  "req_*.jsonl"),
        (out_dir / "results",  "req_*.sync.jsonl"),
    ]
    for base, globpat in patterns:
        if not base.exists():
            continue
        for p in base.glob(globpat):
            m = REQ_NUM_RX.match(p.name)
            if m:
                try:
                    used.add(int(m.group(1)))
                except ValueError:
                    pass
    return used

def batch_no_allocator_safe(out_dir: Path):
    """
    Bezpečný alokátor čísel dávek:
    - startuje na (max použité) + 1 napříč requests/states/trans/results,
    - každé další číslo je nové (v rámci běhu) a nikdy nepoužité,
    - při flushi ještě ověří kolizi na disku a případně posune.
    """
    used = _collect_used_batch_numbers(out_dir)
    cursor = max(used) if used else 0

    def alloc_next() -> int:
        nonlocal cursor
        while True:
            cursor += 1
            if cursor not in used:
                used.add(cursor)
                return cursor

    return alloc_next

# ---------- Backfill: tvorba JSONL dávek + .state ----------

def build_user_block(rows: List[Tuple[str,str]]) -> str:
    """rows = [(idx, source)] → TSV blok pro user část."""
    return "".join(f"{idx}{TSV_SEP}{src}\n" for idx, src in rows)

def estimate_tokens(block: str, system_rules: str, user_header: str) -> int:
    # hrubý odhad tokenů: ~3 znaky = 1 token, plus rezerva a overhead
    TOKENS_PER_CHAR = 0.35
    REQ_OVERHEAD = 220
    FUDGE = 1.30
    chars = len(system_rules) + len(user_header) + len(block)
    return int(math.ceil(chars * TOKENS_PER_CHAR) * FUDGE) + REQ_OVERHEAD

def make_requests(out_dir: Path,
                  idx2src: Dict[str,str],
                  candidate_idxs: List[str],
                  prompts_file: Optional[Path],
                  model: str,
                  max_lines: int,
                  max_chars: int,
                  batch_budget: int,
                  batch_max_bytes: Optional[int],
                  custom_id_prefix: str = "AUD") -> List[Tuple[int, Path]]:
    """
    Vytvoří nové req_XXX.jsonl + req_XXX.state.json tak, aby:
      • každý JSONL soubor nepřekročil --batch-max-bytes (pokud zadáno),
      • jinak se orientačně držel pod --batch-budget (tokeny) jako sekundární limit,
      • uvnitř každé dávky je více requestů (každý request <= max_lines/max_chars),
      • čísla XXX navazují bezpečně za maximem (bez kolizí),
      • custom_id jsou unikátní s daným prefixem (AUD_MISS / AUD_SUS / AUD_CORR).

    Vrací seznam (batch_no, jsonl_path).
    """
    sys_rules, user_head = load_prompts(prompts_file)

    # 1) Sestav jednotlivé requesty z kandidátních indexů (vzestupně)
    rows_sorted = [(idx, idx2src[idx]) for idx in sorted(candidate_idxs, key=lambda s: int(s))]

    requests_texts: List[str] = []  # JSON-serialized lines (1 request per line)
    req_seq = 0
    chunk: List[Tuple[str,str]] = []
    chunk_size_chars = 0

    def flush_request_from_chunk():
        nonlocal chunk, chunk_size_chars, req_seq
        if not chunk:
            return
        user_block = build_user_block(chunk)
        req_seq += 1
        obj = {
            "custom_id": f"{custom_id_prefix}_req{req_seq:06d}",
            "method": "POST",
            "url": "/v1/responses",
            "body": {
                "model": model,
                "input": [
                    {"role": "system", "content": sys_rules},
                    {"role": "user",   "content": user_head + user_block}
                ]
            }
        }
        requests_texts.append(json.dumps(obj, ensure_ascii=False))
        chunk = []
        chunk_size_chars = 0

    for idx, src in rows_sorted:
        line = f"{idx}{TSV_SEP}{src}\n"
        if chunk and (chunk_size_chars + len(line) > max_chars or len(chunk) >= max_lines):
            flush_request_from_chunk()
        chunk.append((idx, src))
        chunk_size_chars += len(line)
    if chunk:
        flush_request_from_chunk()

    # 2) Rozdělení requestů do JSONL dávek dle byte-size (primárně) a token budgetu (sekundárně)
    out_requests = out_dir / "requests"
    out_states   = out_dir / "states"
    out_results  = out_dir / "results"  # jen kvůli bezpečné alokaci čísel v manifestu
    out_trans    = out_dir / "trans"
    for d in (out_requests, out_states, out_results, out_trans):
        d.mkdir(parents=True, exist_ok=True)

    alloc = batch_no_allocator_safe(out_dir)

    batches_written: List[Tuple[int, Path]] = []
    cur_lines: List[str] = []
    cur_tokens = 0
    cur_bytes = 0

    def flush_batch():
        nonlocal cur_lines, cur_tokens, cur_bytes
        if not cur_lines:
            return None
        bno = alloc()
        # double-check kolizí
        while True:
            jpath = out_requests / f"req_{bno:03d}.jsonl"
            spath = out_states   / f"req_{bno:03d}.state.json"
            if not jpath.exists() and not spath.exists():
                break
            bno = alloc()

        payload = ("\n".join(cur_lines) + "\n").encode("utf-8")
        write_safely(jpath, payload, binary=True)
        write_safely(
            spath,
            json.dumps({"batch_no": bno, "jsonl": jpath.name, "status": "prepared"}, ensure_ascii=False, indent=2)
        )
        size_b = jpath.stat().st_size
        log(f"[BACKFILL] {jpath.name} ({size_b:,} B) – připraveno (status=prepared).")

        batches_written.append((bno, jpath))
        # reset
        cur_lines, cur_tokens, cur_bytes = [], 0, 0
        return bno, jpath

    for line in requests_texts:
        # odhad tokenů jako sekundární pojistka
        try:
            obj = json.loads(line)
            content = obj["body"]["input"][1]["content"]
            block = content.split("\n\n", 1)[-1] if "\n\n" in content else content
            est = estimate_tokens(block, sys_rules, user_head)
        except Exception:
            est = 0

        line_bytes = (line + "\n").encode("utf-8")
        would_bytes = cur_bytes + len(line_bytes)
        would_tokens = cur_tokens + est

        overflow_bytes = batch_max_bytes is not None and batch_max_bytes > 0 and would_bytes > batch_max_bytes
        overflow_tokens = batch_budget > 0 and would_tokens > batch_budget

        if cur_lines and (overflow_bytes or overflow_tokens):
            flush_batch()

        cur_lines.append(line)
        cur_bytes += len(line_bytes)
        cur_tokens += est

    if cur_lines:
        flush_batch()

    return batches_written

# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Audit + backfill (missing/suspect/corrupt) pro WotR překlad – v2.1 (fixed)")
    ap.add_argument("-i","--input", required=True, help="Cesta k enGB.json")
    ap.add_argument("-o","--out-dir", required=True, help="Pracovní složka s map.json a trans/*.tsv")

    # Prompts – default + specializované pro kategorie
    ap.add_argument("--prompts-file", type=str, default=None, help="prompts.json (system_rules, user_header) – default pro všechny kategorie")
    ap.add_argument("--prompts-file-missing", type=str, default=None, help="Override prompts pro MISSING")
    ap.add_argument("--prompts-file-suspect", type=str, default=None, help="Override prompts pro SUSPECT")
    ap.add_argument("--prompts-file-corrupt", type=str, default=None, help="Override prompts pro CORRUPT")

    # Audit parametry (suspect)
    ap.add_argument("--jaccard-threshold", type=float, default=0.72)
    ap.add_argument("--min-czech-chars", type=int, default=1)
    ap.add_argument("--min-len-ratio", type=float, default=0.45)
    ap.add_argument("--flag-bilingual", action="store_true")

    # Audit parametry (corrupt)
    ap.add_argument("--corrupt-src-max-words", type=int, default=3, help="Max. slov u EN zdroje (label) pro CORRUPT")
    ap.add_argument("--corrupt-tr-min-words", type=int, default=10, help="Min. slov u překladu pro CORRUPT")
    ap.add_argument("--corrupt-min-len-ratio", type=float, default=3.0, help="Min. poměr len(TR)/len(SRC) pro CORRUPT")

    # Backfill volby
    ap.add_argument("--make-requests",
                    choices=["missing","suspect","corrupt","both","all"],
                    default=None,
                    help="Volitelně vytvoř JSONL + .state: 'missing' | 'suspect' | 'corrupt' | 'both'(missing+suspect) | 'all'(vše).")
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--max-lines", type=int, default=350, help="Max řádků v jednom requestu")
    ap.add_argument("--max-chars", type=int, default=18000, help="Max znaků v jednom requestu")
    ap.add_argument("--batch-budget", type=int, default=900_000, help="Sekundární limit enqueued tokenů na dávku (0=ignorovat)")
    ap.add_argument("--batch-max-bytes", type=int, default=200_000, help="Primární limit velikosti JSONL dávky v bajtech (např. 200000)")

    args = ap.parse_args()

    en_json = Path(args.input)
    out_dir = Path(args.out_dir)

    # Připrav prompty (fallbacky i kategorie)
    pf_default = Path(args.prompts_file) if args.prompts_file else None
    pf_missing = Path(args.prompts_file_missing) if args.prompts_file_missing else pf_default
    pf_suspect = Path(args.prompts_file_suspect) if args.prompts_file_suspect else pf_default
    pf_corrupt = Path(args.prompts_file_corrupt) if args.prompts_file_corrupt else pf_default

    # 1) Načíst podklady
    id_map = load_map(out_dir)
    en_strings = load_en_strings(en_json)
    idx2src = build_idx_to_source(id_map, en_strings)
    idx2tr  = load_all_translations(out_dir)

    # 2) Audit
    missing, suspect, corrupt = audit(
        idx2src=idx2src,
        idx2tr=idx2tr,
        jaccard_threshold=args.jaccard_threshold,
        min_czech_chars=args.min_czech_chars,
        min_len_ratio=args.min_len_ratio,
        flag_bilingual=args.flag_bilingual,
        corrupt_src_max_words=args.corrupt_src_max_words,
        corrupt_tr_min_words=args.corrupt_tr_min_words,
        corrupt_min_len_ratio=args.corrupt_min_len_ratio
    )
    write_audit_reports(out_dir, missing, suspect, corrupt, params={
        "jaccard_threshold": args.jaccard_threshold,
        "min_czech_chars": args.min_czech_chars,
        "min_len_ratio": args.min_len_ratio,
        "flag_bilingual": bool(args.flag_bilingual),
        "corrupt_src_max_words": args.corrupt_src_max_words,
        "corrupt_tr_min_words": args.corrupt_tr_min_words,
        "corrupt_min_len_ratio": args.corrupt_min_len_ratio
    })

    # 3) Backfill (volitelně)
    if not args.make_requests:
        return

    # Sestav seznam kategorií k backfillu
    todo: List[Tuple[str, List[str], Optional[Path], str]] = []
    if args.make_requests == "missing":
        todo.append(("MISSING", [idx for idx,_ in missing], pf_missing, "AUD_MISS"))
    elif args.make_requests == "suspect":
        todo.append(("SUSPECT", [idx for idx,_,_,_ in suspect], pf_suspect, "AUD_SUS"))
    elif args.make_requests == "corrupt":
        todo.append(("CORRUPT", [idx for idx,_,_,_ in corrupt], pf_corrupt, "AUD_CORR"))
    elif args.make_requests == "both":
        todo.append(("MISSING", [idx for idx,_ in missing], pf_missing, "AUD_MISS"))
        todo.append(("SUSPECT", [idx for idx,_,_,_ in suspect], pf_suspect, "AUD_SUS"))
    elif args.make_requests == "all":
        todo.append(("MISSING", [idx for idx,_ in missing], pf_missing, "AUD_MISS"))
        todo.append(("SUSPECT", [idx for idx,_,_,_ in suspect], pf_suspect, "AUD_SUS"))
        todo.append(("CORRUPT", [idx for idx,_,_,_ in corrupt], pf_corrupt, "AUD_CORR"))

    total_created = []
    for label, cand, prompts_path, cid_prefix in todo:
        if not cand:
            log(f"[BACKFILL] Žádní kandidáti ({label}). Přeskakuji.")
            continue
        batches = make_requests(
            out_dir=out_dir,
            idx2src=idx2src,
            candidate_idxs=cand,
            prompts_file=prompts_path,
            model=args.model,
            max_lines=args.max_lines,
            max_chars=args.max_chars,
            batch_budget=max(0, int(args.batch_budget)),
            batch_max_bytes=(int(args.batch_max_bytes) if args.batch_max_bytes and args.batch_max_bytes > 0 else None),
            custom_id_prefix=cid_prefix
        )
        total_created.extend((label, b, p) for b, p in batches)

    if not total_created:
        log("[BACKFILL] Nic se nevytvořilo (pravděpodobně prázdný vstup).")
        return

    # Manifest backfillu
    out_dir.joinpath("audit").mkdir(parents=True, exist_ok=True)
    created = [{
        "category": cat,
        "batch_no": bno,
        "jsonl": str(path.name)
    } for (cat, bno, path) in total_created]
    bman = {
        "created": created,
        "counts": {
            "missing": len(missing),
            "suspect": len(suspect),
            "corrupt": len(corrupt)
        },
        "params": {
            "max_lines": args.max_lines,
            "max_chars": args.max_chars,
            "batch_max_bytes": args.batch_max_bytes,
            "batch_budget": args.batch_budget,
            "model": args.model
        },
        "timestamp": datetime.now().isoformat(timespec="seconds")
    }
    write_safely(out_dir/"audit"/"backfill_manifest.json", json.dumps(bman, ensure_ascii=False, indent=2))

    # Log summary
    for cat, bno, p in total_created:
        log(f"[BACKFILL][{cat}] req_{bno:03d}.jsonl ({p.stat().st_size:,} B) – připraveno (status=prepared).")
    log("[BACKFILL] Hotovo. Spusť hlavní v2:  --skip-prepare --run --merge")

if __name__ == "__main__":
    main()
