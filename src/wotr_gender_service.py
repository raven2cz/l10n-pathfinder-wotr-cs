#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wotr_gender_service.py
----------------------

„Služba“ pro bezpečný převod češtiny z maskulina → feminina, se zaměřením na dialogy hry.
Používá Stanza (cs tokenize,pos,lemma) + opatrná pravidla. Chrání herní tagy:
- uvnitř {g|...}...{/g} a obecně uvnitř {...} se nic NEMĚNÍ.

API:
- CzechGenderService(cpu=True)
- rewrite_to_feminine(text) -> str
- batch_rewrite(texts: Iterable[str]) -> List[str]

Instalace:
  pip install stanza regex
  python - <<'PY'
import stanza; stanza.download("cs"); print("OK")
PY
"""

from __future__ import annotations
from typing import List, Tuple, Optional, Iterable
import re

try:
    import stanza
except Exception as e:
    raise SystemExit(
        "[wotr_gender_service] Stanza není nainstalovaná. Nainstaluj: pip install stanza\n"
        f"Import error: {e}"
    )

# --- Ochrana tagů/placeholderů ------------------------------------------------

PROTECTED_RE = re.compile(
    r"(\{g\|.*?\}.*?\{\/g\}|\{[^{}]*\})",
    flags=re.DOTALL
)

def _split_protected(text: str) -> List[Tuple[str, bool]]:
    """Rozděl text na [(segment, is_protected)]."""
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

def _preserve_capitalization(src: str, dst: str) -> str:
    if not src or not dst:
        return dst
    if src[0].isupper() and not dst[0].isupper():
        return dst[0].upper() + dst[1:]
    return dst

# --- Nepravidelnosti + krátká adj. --------------------------------------------

IRREG_VERB_FORMS = {
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

SHORT_ADJ_MAP = {
    "rád": "ráda",
    "sám": "sama",
    "hotov": "hotova",
    "živ": "živa",
}

# --- Pomocné predikáty ---------------------------------------------------------

def _is_past_part_masc_sg(feats: Optional[str]) -> bool:
    if not feats:
        return False
    have = set(feats.split("|"))
    needed = {"Gender=Masc", "Number=Sing", "VerbForm=Part"}
    return needed.issubset(have)

def _to_feminine_past_form(form: str, lemma: str) -> Optional[str]:
    low = form.lower()
    if low in IRREG_VERB_FORMS:
        return _preserve_capitalization(form, IRREG_VERB_FORMS[low])
    if low.endswith("šel"):  # prefixed šel
        return _preserve_capitalization(form, low[:-3] + "šla")
    if len(form) >= 2 and low.endswith("l"):
        if form.isupper():
            return None
        return form + "a"
    return None

def _is_adj_masc_sg(feats: Optional[str]) -> bool:
    if not feats:
        return False
    have = set(feats.split("|"))
    return "Gender=Masc" in have and "Number=Sing" in have

def _to_feminine_adj(form: str, lemma: str) -> Optional[str]:
    low = form.lower()
    if low in SHORT_ADJ_MAP:
        return _preserve_capitalization(form, SHORT_ADJ_MAP[low])
    if low.endswith("ý"):
        return form[:-1] + "á"
    return None

# --- Služba --------------------------------------------------------------------

class CzechGenderService:
    """Jednoduchá třída okolo Stanza pipeline a přepisovacích pravidel."""
    def __init__(self, cpu: bool = True):
        # Pozn.: GPU lze povolit nastavením use_gpu=True, ale defaultně zůstaneme na CPU.
        self.nlp = stanza.Pipeline(
            "cs",
            processors="tokenize,pos,lemma",
            tokenize_no_ssplit=False,
            use_gpu=not cpu
        )

    def _rewrite_segment(self, segment: str) -> str:
        if not segment.strip():
            return segment

        doc = self.nlp(segment)

        # Zkusíme char offsety (nejbezpečnější in-place náhrady)
        has_offsets = True
        for s in doc.sentences:
            for w in s.words:
                if w.start_char is None or w.end_char is None:
                    has_offsets = False
                    break

        if has_offsets:
            buf = list(segment)
            changes: List[tuple[int, int, str]] = []
            for s in doc.sentences:
                for w in s.words:
                    orig = segment[w.start_char:w.end_char]
                    upos = w.upos
                    feats = w.feats
                    new_word = None

                    if _is_past_part_masc_sg(feats) and (upos in {"VERB", "AUX"} or True):
                        cand = _to_feminine_past_form(orig, w.lemma or "")
                        if cand:
                            new_word = cand

                    if new_word is None and upos == "ADJ" and _is_adj_masc_sg(feats):
                        cand = _to_feminine_adj(orig, w.lemma or "")
                        if cand:
                            new_word = cand

                    if new_word and new_word != orig:
                        changes.append((w.start_char, w.end_char, new_word))

            for s, e, t in sorted(changes, key=lambda x: x[0], reverse=True):
                buf[s:e] = list(t)
            return "".join(buf)

        # Fallback bez offsetů – konzervativní
        out_parts: List[str] = []
        for s in doc.sentences:
            for w in s.words:
                orig = w.text
                repl = None
                if _is_past_part_masc_sg(w.feats):
                    repl = _to_feminine_past_form(orig, w.lemma or "")
                if repl is None and w.upos == "ADJ" and _is_adj_masc_sg(w.feats):
                    repl = _to_feminine_adj(orig, w.lemma or "")
                out_parts.append(repl if repl else orig)
            out_parts.append(" ")
        joined = "".join(out_parts).strip()
        return joined if joined else segment

    def rewrite_to_feminine(self, text: str) -> str:
        """Přepiš text mimo chráněné bloky do feminina."""
        parts = _split_protected(text)
        out = []
        for seg, prot in parts:
            out.append(seg if prot else self._rewrite_segment(seg))
        return "".join(out)

    def batch_rewrite(self, texts: Iterable[str]) -> List[str]:
        return [self.rewrite_to_feminine(t) if t else t for t in texts]
