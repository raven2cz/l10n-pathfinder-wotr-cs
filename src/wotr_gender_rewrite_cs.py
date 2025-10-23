#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wotr_gender_rewrite_cs.py
=========================

Účel
----
Bezpečný převod českých vět z maskulina do feminina s pomocí morfosyntaktické analýzy
(Stanza) + opatrných pravidel. Skript chrání herní tagy a placeholdery:
ponechá beze změny segmenty uvnitř {g|...}...{/g} a obecně uvnitř {...}.

Co umí převést (nejdůležitější příklady)
----------------------------------------
- Minulá příčestí: byl → byla, šel → šla, došel → došla, udělal → udělala, řekl → řekla, měl → měla, mohl → mohla, vzal → vzala …
- Adjektiva v sg masc: unavený → unavená, mladý → mladá, nervózní (zůstává stejné), zmatený → zmatená …
- Krátká adjektiva/pronom. tvary: rád → ráda, sám → sama, hotov → hotova (jen když je to opravdu ADJ)

Důležité ochrany
----------------
- Uvnitř tagů {g|...}...{/g} ani jiných {...} skript nic nemění (segmentace textu).
- Používá Stanza (POS+FEATS) a mění jen tokeny, které vypadají jako vhodný kandidát.
- Zachovává kapitalizaci prvního písmena slova (Šel → Šla).

Vstupy/výstupy
--------------
- Umí číst TSV (s hlavičkou) nebo prostý TXT.
- TSV: vyber sloupec s textem pomocí --text-col (default "text").
- TXT: přepínač --format txt (každý řádek = jedna věta/položka).
- Výsledek zapisuje do nového sloupce (--out-col, default "text_fem"), nebo přepíše in-place (--in-place).

Instalace modelu
----------------
  pip install stanza regex
  python -c "import stanza; stanza.download('cs')"

Použití – příklady
------------------
1) TSV (sloupec "dialog") -> nový sloupec "dialog_fem":
   python wotr_gender_rewrite_cs.py -i in.tsv -o out.tsv --format tsv --text-col dialog --out-col dialog_fem

2) TSV in-place přepis sloupce "line":
   python wotr_gender_rewrite_cs.py -i in.tsv -o out.tsv --format tsv --text-col line --in-place

3) TXT řádek po řádku:
   python wotr_gender_rewrite_cs.py -i in.txt -o out.txt --format txt

Poznámky
--------
- Skript je opatrný: raději neudělá nic, než aby text poškodil. Pokud nenajde
  jednoznačný kandidát, ponechá slovo beze změny.
- Transformace je deterministická (nevolá žádné LLM).
"""

from __future__ import annotations
import argparse
import csv
import sys
import re
from typing import List, Tuple, Dict, Optional

import stanza

# ------------------------------
# Pomocné regexy a utility
# ------------------------------

# Segmenty, které chráníme (nepřepisujeme uvnitř):
#   1) {g|...}...{/g}
#   2) obecné {...}
# Vytvoříme regex, který vrací střídavě: PROTECTED segmenty a OUTSIDE segmenty.
PROTECTED_RE = re.compile(
    r"(\{g\|.*?\}.*?\{\/g\}|\{[^{}]*\})",  # chráněné bloky
    flags=re.DOTALL
)

# jednoduché hlídače diakritiky (kvůli heuristikám; zatím nepoužito pro rozhodování)
CZ_LETTERS = set("AÁBCČDĎEÉĚFGHIÍJKL MNŇOÓPQRŘSŠTŤUÚŮVWXYÝZŽaábcčdďeéěfghiíjklmnňoópqrřsštťuúůvwxyýzž")

def preserve_capitalization(src: str, dst: str) -> str:
    """Když zdroj začíná velkým písmenem, zvedni první písmeno i v cíli."""
    if not src or not dst:
        return dst
    if src[0].isupper() and not dst[0].isupper():
        return dst[0].upper() + dst[1:]
    return dst

# Mapa nepravidelností (malá, rozumné minimum):
IRREG_VERB_FORMS = {
    # přesné tvary → cílové tvary (lowercase porovnání, case upravíme)
    "šel": "šla",
    "došel": "došla",
    "vešel": "vešla",
    "odešel": "odešla",
    "přišel": "přišla",
    "vyšel": "vyšla",
    "zašel": "zašla",
    "přešel": "přešla",
    "sešel": "sešla",
    "rozešel": "rozešla",
    "byl": "byla",
    "řekl": "řekla",
}

# Krátká adjektiva/pronom. tvary + pár častých výjimek
SHORT_ADJ_MAP = {
    "rád": "ráda",
    "sám": "sama",
    "samy": "samy",     # nic
    "hotov": "hotova",
    "živ": "živa",      # archaické/příznakové, ale občas se hodí
}

# ------------------------------
# Stanza pipeline
# ------------------------------

def build_pipeline(force_cpu: bool = True):
    """Stáhne/naladí českou pipeline. Tokenize + POS + lemma stačí."""
    # Pozn.: tokenize_no_ssplit=True drží původní řádkování, ale na větách to nevadí
    return stanza.Pipeline(
        "cs",
        processors="tokenize,pos,lemma",
        tokenize_no_ssplit=False,
        use_gpu=not force_cpu
    )

# ------------------------------
# Transformace jednotlivých tokenů
# ------------------------------

def is_past_part_masc_sg(feats: Optional[str]) -> bool:
    """Je to minulý příčestí masc sg? (Gender=Masc, Number=Sing, VerbForm=Part, (Tense=Past|Mood=Ind))."""
    if not feats:
        return False
    f = feats.split("|")
    have = set(f)
    needed = {"Gender=Masc", "Number=Sing", "VerbForm=Part"}
    if not needed.issubset(have):
        return False
    # Tense=Past je ideál, ale některé taggery nemají vždy Tense – stačí Part + Masc+Sing.
    return True

def to_feminine_past_form(form: str, lemma: str) -> Optional[str]:
    """
    Převod minulého příčestí (sg masc) do sg fem.
    - speciály (šel/byl/…)
    - obecně: *…l → …la*
    Vrací None, pokud to vypadá riskantně.
    """
    low = form.lower()

    # 1) nepravidelné/časté výjimky (včetně prefixovaných tvarů se -šel)
    if low in IRREG_VERB_FORMS:
        tgt = IRREG_VERB_FORMS[low]
        return preserve_capitalization(form, tgt)

    if low.endswith("šel"):
        # vešel/odešel/přešel/… → …šla
        tgt = low[:-3] + "šla"
        return preserve_capitalization(form, tgt)

    # 2) generické pravidlo: koncovka -l → -la
    #    Vyhneme se velmi krátkým či podivným tokenům.
    if len(form) >= 2 and low.endswith("l"):
        # vyhneme se zkratkám typu "URL", "XML" apod. (plně caps → raději nic)
        if form.isupper():
            return None
        return form + "a"

    return None

def is_adj_masc_sg(feats: Optional[str]) -> bool:
    """ADJ v sg masc (bez ohledu na pád – ale měníme jen bezpečné koncovky)."""
    if not feats:
        return False
    have = set(feats.split("|"))
    return "Gender=Masc" in have and "Number=Sing" in have

def to_feminine_adj(form: str, lemma: str) -> Optional[str]:
    """
    Převod adjektiva masc sg -> fem sg (bez ohledu na pád).
    Bezpečně měníme:
      - koncovka -ý → -á
      - krátká adj. map: rád→ráda, sám→sama, hotov→hotova…
      - -í necháváme (cizí, jedinečný → jedinečná: POZOR, to je -ný, ne -ní)
    """
    low = form.lower()
    if low in SHORT_ADJ_MAP:
        tgt = SHORT_ADJ_MAP[low]
        return preserve_capitalization(form, tgt)

    if low.endswith("ý"):
        tgt = form[:-1] + "á"
        return tgt

    # u -í beze změny (typicky invariant)
    return None

# ------------------------------
# Segmentace na PROTECTED a OUTSIDE
# ------------------------------

def split_protected(text: str) -> List[Tuple[str, bool]]:
    """
    Vstupní text rozděl na sekvence: [(segment, is_protected), ...]
    PROTECTED = {g|...}...{/g} a obecné {...}
    """
    out: List[Tuple[str, bool]] = []
    last = 0
    for m in PROTECTED_RE.finditer(text):
        if m.start() > last:
            out.append((text[last:m.start()], False))
        out.append((m.group(0), True))
        last = m.end()
    if last < len(text):
        out.append((text[last:], False))
    return out

# ------------------------------
# Přepis jedné věty/segmentu
# ------------------------------

def rewrite_segment_to_feminine(nlp, segment: str) -> str:
    """
    Přepiš JEN outer segment (bez tagů) – 1:1 délka není garantovaná,
    ale zachováme whitespace a interpunkci (Stanza tokenizuje).
    Postup:
      - vezmeme větu(y), projdeme slova,
      - kde to dává smysl, změníme tvar (past part, adj),
      - složíme zpět.
    """
    if not segment.strip():
        return segment

    doc = nlp(segment)
    # Abychom zachovali spacing, poskládáme podle původního textu z doc:
    # Stanza tokens mají .text a .misc s 'SpaceAfter=No' – ale v cs modelu se to liší.
    # Jednodušší segmentové skládání: použijeme .text a mezi tokeny necháme původní mezery,
    # nicméně Stanza nám spacing přesně nevrátí. Zde tedy složíme větu s jednou mezerou
    # a následně zkusíme nahradit jen uvnitř slov (bez zásahu do whitespace v původním segmentu).
    #
    # Pragmatické řešení: provedeme náhrady tokenů „in place“ podle char-rozsahů,
    # které Stanza expose-uje v token.misc (StartChar, EndChar) – u cs modelu bývají.
    #
    # Fallback: když nejsou char offsety, uděláme jednoduchou token-swap rekonstrukci s mezerami.

    # 1) Pokus o offsety:
    has_offsets = True
    for sent in doc.sentences:
        for w in sent.words:
            if w.start_char is None or w.end_char is None:
                has_offsets = False
                break

    if has_offsets:
        # Pracujeme na char-array kvůli „in place“ náhradám
        buf = list(segment)
        # Sbírej změny jako (start, end, new_text)
        changes: List[Tuple[int, int, str]] = []

        for sent in doc.sentences:
            for w in sent.words:
                upos = w.upos
                feats = w.feats
                span = (w.start_char, w.end_char)  # [start, end), python slice
                orig = segment[span[0]:span[1]]

                # past participle masc sg?
                new_word = None
                if is_past_part_masc_sg(feats) and (upos in {"VERB", "AUX"} or True):
                    cand = to_feminine_past_form(orig, w.lemma or "")
                    if cand:
                        new_word = cand

                # adj masc sg?
                if new_word is None and upos == "ADJ" and is_adj_masc_sg(feats):
                    cand = to_feminine_adj(orig, w.lemma or "")
                    if cand:
                        new_word = cand

                if new_word and new_word != orig:
                    changes.append((span[0], span[1], new_word))

        # Aplikuj změny od konce (aby se neposunuly indexy)
        for s, e, t in sorted(changes, key=lambda x: x[0], reverse=True):
            buf[s:e] = list(t)

        return "".join(buf)

    # 2) Fallback bez offsetů – jednoduché složení:
    out_parts: List[str] = []
    for sent in doc.sentences:
        for i, w in enumerate(sent.words):
            orig = w.text
            repl = None

            if is_past_part_masc_sg(w.feats):
                repl = to_feminine_past_form(orig, w.lemma or "")

            if repl is None and w.upos == "ADJ" and is_adj_masc_sg(w.feats):
                repl = to_feminine_adj(orig, w.lemma or "")

            out_parts.append(repl if repl else orig)
        out_parts.append(" ")  # mezera mezi větami/segmenty
    joined = "".join(out_parts).strip()
    # tato varianta změní whitespace, ale raději minimální zásah
    return joined if joined else segment

def rewrite_text_to_feminine(nlp, text: str) -> str:
    """Rozděl text na PROTECTED/OUTSIDE a přepiš jen OUTSIDE."""
    parts = split_protected(text)
    out = []
    for seg, is_prot in parts:
        if is_prot:
            out.append(seg)
        else:
            out.append(rewrite_segment_to_feminine(nlp, seg))
    return "".join(out)

# ------------------------------
# I/O (TSV/TXT)
# ------------------------------

def process_tsv(nlp, in_path: str, out_path: str, text_col: str, out_col: Optional[str],
                in_place: bool) -> None:
    with open(in_path, "r", encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f, delimiter="\t")
        fieldnames = rd.fieldnames or []
        if text_col not in fieldnames:
            raise SystemExit(f"[ERR] Sloupec '{text_col}' v {in_path} neexistuje. Máš: {fieldnames}")

        if in_place:
            out_fields = fieldnames
            target_col = text_col
        else:
            target_col = out_col or "text_fem"
            if target_col not in fieldnames:
                out_fields = fieldnames + [target_col]
            else:
                out_fields = fieldnames

        rows = list(rd)

    out_rows = []
    for r in rows:
        src = r.get(text_col, "")
        r[target_col] = rewrite_text_to_feminine(nlp, src) if src else src
        out_rows.append(r)

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, delimiter="\t", fieldnames=out_fields, lineterminator="\n")
        wr.writeheader()
        wr.writerows(out_rows)

def process_txt(nlp, in_path: str, out_path: str) -> None:
    lines = [ln.rstrip("\n") for ln in open(in_path, "r", encoding="utf-8")]
    out_lines = [rewrite_text_to_feminine(nlp, ln) if ln else ln for ln in lines]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        for ln in out_lines:
            f.write(ln + "\n")

# ------------------------------
# CLI
# ------------------------------

def main():
    ap = argparse.ArgumentParser(description="Maskulinum → feminimum pro češtinu (bezpečně; chrání {g|...}{/g} bloky).")
    ap.add_argument("-i", "--in", dest="in_path", required=True, help="Vstup (TSV/TXT)")
    ap.add_argument("-o", "--out", dest="out_path", required=True, help="Výstup (TSV/TXT)")
    ap.add_argument("--format", choices=["tsv", "txt"], default="tsv", help="Vstupní/výstupní formát")
    ap.add_argument("--text-col", default="text", help="(TSV) vstupní sloupec s textem (default: text)")
    ap.add_argument("--out-col", default="text_fem", help="(TSV) výstupní sloupec (pokud není --in-place)")
    ap.add_argument("--in-place", action="store_true", help="(TSV) přepiš text přímo ve --text-col")
    ap.add_argument("--cpu", action="store_true", help="Vynutit CPU (výchozí) – pokud chceš GPU, nedávej --cpu")
    args = ap.parse_args()

    nlp = build_pipeline(force_cpu=True if args.cpu else True)  # default CPU; GPU by bylo True/False dle potřeby

    if args.format == "tsv":
        process_tsv(nlp, args.in_path, args.out_path, args.text_col, args.out_col, args.in_place)
    else:
        process_txt(nlp, args.in_path, args.out_path)

if __name__ == "__main__":
    main()
