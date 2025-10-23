#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
wotr_merge_by_quality.py
========================
Porovná pro každý řetězec dvě CZ varianty (A vs B) vůči EN zdroji a vybere lepší.
POZOR: Neprovádí žádné kontroly zachování tagů/placeholderů – DeepL není penalizován.

Vstupy:
  --map         map.json               (idx -> GUID)
  --en          enGB.json              (obsahuje "strings")
  --cz-a        csCZ_A.json            (váš překlad)
  --cz-b        csCZ_B.json            (DeepL překlad)
  --out-cz      csCZ-merged.json       (výsledek)
  --report-tsv  merge_report.tsv       (audit/score per řádek)

Volby:
  --prefer {a|b}           preferovaná varianta při remíze (default: a)
  --use-embeddings         zapne sentence-transformers embeddings (lokálně, bez volání cloudu)
  --embed-model NAME       HF model (default: paraphrase-multilingual-MiniLM-L12-v2)
  --embed-device {cuda|cpu}  device volba (default: cuda)
  --embed-batch-size N     batch size pro embed encode (default: 512)
  --embed-fp16             pokus o FP16 na GPU (default off; některé ST modely nemusí FP16 umět)
  --alpha FLOAT            váha embeddings (default 35.0)
  --min-len-ratio 0.4      dolní hranice poměru CZ/EN
  --max-len-ratio 2.5      horní hranice poměru CZ/EN
  --no-penalize-arrow      NEpenalizovat "->"/"→" (default je penalizovat)
  --limit N                zpracuj max N položek (debug)
  --only-common            zpracuj jen GUIDy, které jsou v obou CZ
  --progress-every N       jak často logovat průběh (default 10000)

Použití (GPU):
  python wotr_merge_by_quality.py ^
    --map .\out_wotr\map.json ^
    --en .\enGB.json ^
    --cz-a .\out_wotr\csCZ.json ^
    --cz-b .\deepl\csCZ.json ^
    --out-cz .\out_wotr\csCZ-merged.json ^
    --report-tsv .\out_wotr\reports\merge_report.tsv ^
    --use-embeddings ^
    --embed-model paraphrase-multilingual-MiniLM-L12-v2 ^
    --embed-device cuda ^
    --embed-batch-size 512 ^
    --alpha 35 ^
    --only-common
"""

from __future__ import annotations
import argparse, json, csv, sys, math, re, time
from pathlib import Path
from typing import Dict, Tuple, List, Optional

TSV = "\t"

# ---------- I/O ----------
def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))

def write_json_atomic(p: Path, obj: dict):
    tmp = p.with_suffix(p.suffix + ".tmp")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)

def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ---------- Heuristiky (bez “guards” na tagy) ----------
WORD_RE = re.compile(r"[0-9A-Za-zÀ-ž'’\-]+", re.UNICODE)

def has_bilingual_arrow(s: str) -> bool:
    return ("->" in (s or "")) or ("→" in (s or ""))

def czechness_score(s: str) -> float:
    """Hrubý odhad 'češtinosti' – diakritika + běžná slova."""
    if not s: return 0.0
    dia = sum(1 for ch in s if ch in "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ")
    base = min(1.0, dia / max(1, len(s)//4))
    low = f" {s.casefold()} "
    common = 0
    for w in (" že "," se "," jsem "," byla "," bude "," aby "," už "," jen ",
              " když "," který "," kterou "," které "," ten "," ta "," to ",
              " a "," v "," na "," pro "," do "):
        if w in low: common += 1
    boost = min(0.3, common * 0.03)
    return max(0.0, min(1.0, base + boost))

def length_ratio(en: str, cz: str) -> float:
    le = len(en or "")
    lc = len(cz or "")
    return (lc / max(1, le))

def identical_to_source(en: str, cz: str) -> bool:
    return (en or "").strip() == (cz or "").strip()

def contains_source_long(en: str, cz: str) -> bool:
    en_s = (en or "").strip()
    cz_s = (cz or "").strip()
    return len(en_s) > 15 and en_s in cz_s

def simple_token_ratio(en: str, cz: str) -> float:
    """Jaccard token overlap EN vs CZ – když je příliš vysoký, je to podezřelé."""
    se = set(WORD_RE.findall(en or ""))
    sc = set(WORD_RE.findall(cz or ""))
    if not se or not sc:
        return 0.0
    inter = len(se & sc)
    uni = len(se | sc)
    return inter / max(1, uni)

# ---------- (Volitelně) Embeddings (lokální, sentence-transformers) ----------
class EmbSim:
    """
    Wrapper nad SentenceTransformer s podporou:
      - device: cuda/cpu
      - batch_size: řízení dávkování
      - volitelné FP16 na GPU (best-effort; některé modely FP16 neumožní)
      - rychlý *batch* výpočet pro celé pole textů
    """
    def __init__(self, model_name: str, device: str = "cuda", batch_size: int = 512, fp16: bool = False):
        self.model_name = model_name
        self.batch_size = int(batch_size)
        self.device = device

        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except Exception as e:
            log(f"[EMB] ERROR: sentence-transformers/torch nejsou nainstalovány: {e}")
            self.ok = False
            self.model = None
            self.torch = None
            return

        self.torch = torch
        use_cuda = (device == "cuda" and torch.cuda.is_available())
        dev_str = "cuda" if use_cuda else "cpu"
        log(f"[EMB] Loading model '{model_name}' on device={dev_str}, batch_size={self.batch_size}, fp16={fp16}")
        try:
            self.model = SentenceTransformer(model_name, device=dev_str)
            # FP16 best-effort (některé modely mají vrstvy bez fp16 podpory – ignorujeme chybu)
            if fp16 and use_cuda:
                try:
                    self.model = self.model.to(torch.device("cuda"))
                    for p in self.model.parameters():
                        p.data = p.data.half()
                except Exception as e:
                    log(f"[EMB] FP16 not applied: {e}")
            self.ok = True
        except Exception as e:
            log(f"[EMB] ERROR: Nelze načíst model '{model_name}': {e}")
            self.ok = False
            self.model = None

    def encode_texts(self, texts: List[str]):
        """
        Vrátí L2-normalizované embeddingy (numpy, shape [N, D]).
        """
        if not self.ok or not texts:
            import numpy as np
            return np.zeros((0, 384), dtype="float32")  # dummy
        return self.model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            batch_size=self.batch_size,
            show_progress_bar=False
        )

# ---------- Skórování kandidáta ----------
def score_candidate(en: str, cz: str,
                    min_len_ratio: float,
                    max_len_ratio: float,
                    penalize_arrow: bool,
                    emb_sim_01: Optional[float],
                    alpha: float) -> Tuple[float, List[str]]:
    """
    Vrátí (skóre 0..100, reasons[]). Bez kontroly zachování tagů/placeholderů.
    emb_sim_01: embedding podobnost už převedená do intervalu 0..1 (nebo None).
    """
    reasons: List[str] = []
    score = 50.0  # základ

    if not cz:
        reasons.append("empty")
        return 0.0, reasons

    if identical_to_source(en, cz):
        reasons.append("identical_to_source")
        score -= 80

    # Bilingual arrow / vložený zdroj
    if penalize_arrow and has_bilingual_arrow(cz):
        reasons.append("bilingual_arrow")
        score -= 40

    if contains_source_long(en, cz):
        reasons.append("contains_source")
        score -= 20

    # Jaccard EN vs CZ – velká shoda je podezřelá
    jac = simple_token_ratio(en, cz)
    if jac > 0.4:
        reasons.append(f"jaccard_high:{jac:.2f}")
        score -= (jac - 0.4) * 60  # až cca -36

    # Češtinost
    czs = czechness_score(cz)
    if czs < 0.2:
        reasons.append(f"low_czechness:{czs:.2f}")
        score -= 25
    elif czs > 0.6:
        score += 6
        reasons.append(f"czechness_ok:{czs:.2f}")

    # Poměr délek
    lr = length_ratio(en, cz)
    if lr < min_len_ratio or lr > max_len_ratio:
        reasons.append(f"len_ratio_out:{lr:.2f}")
        score -= 15
    else:
        reasons.append(f"len_ratio_ok:{lr:.2f}")
        score += 4

    # Embedding similarity 0..1
    if emb_sim_01 is not None:
        score += alpha * float(emb_sim_01)
        reasons.append(f"emb:{emb_sim_01:.2f}")

    # Ořez
    score = max(0.0, min(100.0, score))
    return score, reasons

def make_sort_key(guid2idx: dict):
    """
    Vrátí funkci key() pro sorted(), která:
    - preferuje GUIDy s číselným idx (přijdou jako první),
    - uvnitř té skupiny řadí podle int(idx),
    - ostatní řadí podle samotného GUIDu.
    """
    def _key(g: str):
        s = guid2idx.get(g)
        if s and str(s).isdigit():
            return (0, int(s))
        else:
            return (1, g)
    return _key

# ---------- Merge ----------
def main():
    ap = argparse.ArgumentParser(description="Per-line merge CZ(A) vs CZ(B) dle kvality proti EN (bez tag guardů).")
    ap.add_argument("--map", required=True)
    ap.add_argument("--en",  required=True)
    ap.add_argument("--cz-a", required=True)
    ap.add_argument("--cz-b", required=True)
    ap.add_argument("--out-cz", required=True)
    ap.add_argument("--report-tsv", required=True)

    ap.add_argument("--prefer", choices=["a","b"], default="a")
    ap.add_argument("--use-embeddings", action="store_true")
    ap.add_argument("--embed-model", default="paraphrase-multilingual-MiniLM-L12-v2")
    ap.add_argument("--embed-device", choices=["cpu","cuda"], default="cuda")
    ap.add_argument("--embed-batch-size", type=int, default=512)
    ap.add_argument("--embed-fp16", action="store_true", help="Pokus o FP16 na GPU (best-effort).")
    ap.add_argument("--alpha", type=float, default=35.0)

    ap.add_argument("--min-len-ratio", type=float, default=0.4)
    ap.add_argument("--max-len-ratio", type=float, default=2.5)
    ap.add_argument("--no-penalize-arrow", action="store_true")

    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-common", action="store_true", help="Zpracovat jen GUIDy, které existují v obou češtinách (CZ-A i CZ-B).")
    ap.add_argument("--progress-every", type=int, default=10000, help="Jak často logovat průběh (řádků).")

    args = ap.parse_args()

    # 1) Načtení dat
    idx2guid: Dict[str,str] = read_json(Path(args.map))
    en = read_json(Path(args.en)); en_strings = en.get("strings", {})
    czA = read_json(Path(args.cz_a)); a_strings = czA.get("strings", {})
    czB = read_json(Path(args.cz_b)); b_strings = czB.get("strings", {})

    guid2idx = {guid: idx for idx, guid in idx2guid.items()}
    keyf = make_sort_key(guid2idx)

    # Vyber kandidátní GUIDy
    if args.only_common:
        common = set(a_strings.keys()) & set(b_strings.keys())
        guids = sorted(common, key=keyf)
    else:
        guids = sorted(set(a_strings.keys()) | set(b_strings.keys()), key=keyf)

    if args.limit > 0:
        guids = guids[:args.limit]

    total = len(guids)
    penalize_arrow = not args.no_penalize_arrow

    log(f"[SETUP] candidates={total} | prefer={args.prefer} | embed={args.use_embeddings} | model={args.embed_model} | device={args.embed_device}")

    # 2) Připrav aligned pole textů (pro rychlý batch embedding)
    EN_list: List[str] = []
    A_list:  List[str] = []
    B_list:  List[str] = []
    for g in guids:
        EN_list.append(en_strings.get(g, "") or "")
        A_list.append(a_strings.get(g, "") or "")
        B_list.append(b_strings.get(g, "") or "")

    # 3) (Volitelné) spočítej embeddingy a podobnosti dopředu (rychlejší než dělat po jednom)
    emb_EN = emb_A = emb_B = None
    emb = None
    if args.use_embeddings:
        emb = EmbSim(args.embed_model, device=args.embed_device, batch_size=args.embed_batch_size, fp16=args.embed_fp16)
        if emb.ok:
            log(f"[EMB] Encoding EN/A/B …")
            t0 = time.time()
            emb_EN = emb.encode_texts(EN_list)
            emb_A  = emb.encode_texts(A_list)
            emb_B  = emb.encode_texts(B_list)
            dt = time.time() - t0
            log(f"[EMB] Encoded all ({total}×3) in {dt:.1f}s")
        else:
            log("[EMB] Disabled (model not available).")

    # 4) Merge
    merged = {"$id": czA.get("$id", "csCZ-merged"), "strings": dict(a_strings)}  # start z A
    out_tsv_lines = ["idx\tguid\tpick\tscoreA\tscoreB\treasonsA\treasonsB\tEN\tA\tB"]

    picked_a = 0
    picked_b = 0

    # Helper pro získání emb sim 0..1
    def emb_sim_01(i: int, which: str) -> Optional[float]:
        """which in {'A','B'}"""
        if emb_EN is None:
            return None
        import numpy as np
        if which == "A":
            va = emb_A[i:i+1]
        else:
            va = emb_B[i:i+1]
        if va is None or len(va) == 0:  # prázdný text
            return None
        # embeddings jsou už L2-normalizované -> kosinus = dot
        sim = float(np.dot(emb_EN[i], va[0]))
        # map z [-1,1] na [0,1]
        return max(0.0, min(1.0, 0.5 * (sim + 1.0)))

    # hlavní smyčka
    for i, guid in enumerate(guids):
        if args.progress_every > 0 and i > 0 and i % args.progress_every == 0:
            log(f"[PROGRESS] {i}/{total} ({100.0*i/total:.1f}%) … picked_a={picked_a} picked_b={picked_b}")

        idx = guid2idx.get(guid, "")
        en_text = EN_list[i]
        a_text  = A_list[i]
        b_text  = B_list[i]

        # chybějící strany – vyber existující
        if a_text and not b_text:
            pick = "a"
            merged["strings"][guid] = a_text
            picked_a += 1
            out_tsv_lines.append(f"{idx}\t{guid}\t{pick}\t100\t0\tmissingB\t-\t{en_text}\t{a_text}\t")
            continue
        if b_text and not a_text:
            pick = "b"
            merged["strings"][guid] = b_text
            picked_b += 1
            out_tsv_lines.append(f"{idx}\t{guid}\t{pick}\t0\t100\t-\tmissingA\t{en_text}\t\t{b_text}")
            continue

        # obě existují (nebo prázdné)
        sA, rA = score_candidate(
            en_text, a_text,
            args.min_len_ratio, args.max_len_ratio,
            penalize_arrow,
            emb_sim_01(i, "A") if args.use_embeddings else None,
            args.alpha
        )
        sB, rB = score_candidate(
            en_text, b_text,
            args.min_len_ratio, args.max_len_ratio,
            penalize_arrow,
            emb_sim_01(i, "B") if args.use_embeddings else None,
            args.alpha
        )

        if abs(sA - sB) < 1e-6:
            pick = args.prefer
        else:
            pick = "a" if sA > sB else "b"

        if pick == "a":
            merged["strings"][guid] = a_text
            picked_a += 1
        else:
            merged["strings"][guid] = b_text
            picked_b += 1

        out_tsv_lines.append(
            f"{idx}\t{guid}\t{pick}\t{sA:.1f}\t{sB:.1f}\t{';'.join(rA)}\t{';'.join(rB)}\t"
            f"{en_text}\t{a_text}\t{b_text}"
        )

    # 5) Výstupy
    out_cz = Path(args.out_cz)
    out_rep = Path(args.report_tsv)
    out_cz.parent.mkdir(parents=True, exist_ok=True)
    out_rep.parent.mkdir(parents=True, exist_ok=True)

    write_json_atomic(out_cz, merged)
    out_rep.write_text("\n".join(out_tsv_lines) + "\n", encoding="utf-8")

    log(f"[MERGE] total={total} | picked_a={picked_a} | picked_b={picked_b}")
    log(f"[OUT]  JSON: {out_cz}")
    log(f"[OUT]   TSV: {out_rep}")

if __name__ == "__main__":
    main()
