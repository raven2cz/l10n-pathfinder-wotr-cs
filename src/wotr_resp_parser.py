#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_resp_parser.py
===================

Robustní parser odpovědí z LLM:

- Vytáhne obsah z kódových bloků (```tsv ... ``` i bez jazyka) i z celého textu.
- Umí více formátů řádků:
    1) TAB:      ^\s*(\d+)\t(.*)$
    2) Dvojtečka ^\s*(\d+)\s*:\s*(.+)$
    3) Svislítko ^\s*(\d+)\s*\|\s*(.+)$
    4) Pomlčka   ^\s*(\d+)\s*-\s*(.+)$
    5) Mezery    ^\s*(\d+)\s{2,}(.+)$   (alespoň dvě mezery mezi id a textem)

- Klíče (idx) vždy jako **řetězec** s oříznutím whitespace.
- Přesný výběr jen pro id z aktuální dávky (získá si je z req_text nebo je lze dodat).
- Vrací (chosen_pattern, mapping, stats, sample_list).

Použití:
    chosen, mapping, stats, sample = parse_resp_with_req(resp_text, req_text, expected_ids=None)
"""

from __future__ import annotations
import re
from typing import Dict, List, Tuple, Optional
from pathlib import Path

_CodeFence = re.compile(r"```(?:\w+)?\s*([\s\S]*?)```", re.MULTILINE)
_RX_TAB    = re.compile(r"^\s*(\d+)\t(.*)$")
_RX_COLON  = re.compile(r"^\s*(\d+)\s*:\s*(.+)$")
_RX_PIPE   = re.compile(r"^\s*(\d+)\s*\|\s*(.+)$")
_RX_DASH   = re.compile(r"^\s*(\d+)\s*-\s*(.+)$")
_RX_SPACES = re.compile(r"^\s*(\d+)\s{2,}(.+)$")
_RX_ID_IN_REQ = re.compile(r"^\s*(\d+)\t", re.MULTILINE)

def diag_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def _extract_code_blocks(text: str) -> List[str]:
    blocks = [m.group(1) for m in _CodeFence.finditer(text or "")]
    # pokud nic, zkusíme vrátit aspoň celý text
    return blocks if blocks else [text or ""]

def extract_ids_from_req(req_text: str) -> List[str]:
    """Z requestu vytáhni idčka (řádky začínající číslem a tabem)."""
    ids = []
    for m in _RX_ID_IN_REQ.finditer(req_text or ""):
        s = (m.group(1) or "").strip()
        if s:
            ids.append(s)
    return ids

def _parse_with_regex(block: str, rx) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (block or "").splitlines():
        m = rx.match(line)
        if not m:
            continue
        idx = (m.group(1) or "").strip()
        tr  = (m.group(2) or "").strip()
        if idx:
            out[idx] = tr
    return out

def _best_mapping_for_ids(candidates: List[Tuple[str, Dict[str,str]]],
                          expected: Optional[set]) -> Tuple[str, Dict[str,str], Dict[str,int]]:
    """
    Ze seznamu kandidátů [(name, map)] vybere ten s největším překryvem k expected id,
    případně s největší velikostí mapy, když expected není k dispozici.
    """
    stats = {
        "parsed_tab": 0,
        "parsed_colon": 0,
        "parsed_pipe": 0,
        "parsed_minus": 0,
        "parsed_spaces": 0,
        "overlap": 0
    }
    best_name = "none"
    best_map: Dict[str,str] = {}
    best_score = -1

    for name, mp in candidates:
        count = len(mp)
        if name == "tab":    stats["parsed_tab"]    = count
        if name == "colon":  stats["parsed_colon"]  = count
        if name == "pipe":   stats["parsed_pipe"]   = count
        if name == "minus":  stats["parsed_minus"]  = count
        if name == "spaces": stats["parsed_spaces"] = count

        if expected:
            ov = sum(1 for k in mp.keys() if k in expected)
            score = (ov * 100000) + count  # primárně overlap, sekundárně velikost
        else:
            ov = 0
            score = count

        if ov > stats["overlap"]:
            stats["overlap"] = ov

        if score > best_score:
            best_score = score
            best_name  = name
            best_map   = mp

    return best_name, best_map, stats

def parse_resp_with_req(resp_text: str,
                        req_text: str,
                        expected_ids: Optional[List[str]] = None
                        ) -> Tuple[str, Dict[str,str], Dict[str,int], List[Tuple[str,str]]]:
    """
    Vrátí (chosen_pattern, mapping, stats, sample).

    - Najde kódové bloky nebo použije celý text.
    - Zkusí pět regexů.
    - Vybere nejlepší podle overlapu s id z req (nebo největší mapu).
    - Omezí mapping jen na id z req (pokud máme expected id).
    """
    expected = set(expected_ids or extract_ids_from_req(req_text) or [])
    blocks = _extract_code_blocks(resp_text)

    # posbírej kandidáty ze všech bloků i z celého textu (pokud bloky nebyly)
    cand_all: List[Tuple[str, Dict[str,str]]] = []
    for b in blocks:
        cand_all.append(("tab",    _parse_with_regex(b, _RX_TAB)))
        cand_all.append(("colon",  _parse_with_regex(b, _RX_COLON)))
        cand_all.append(("pipe",   _parse_with_regex(b, _RX_PIPE)))
        cand_all.append(("minus",  _parse_with_regex(b, _RX_DASH)))
        cand_all.append(("spaces", _parse_with_regex(b, _RX_SPACES)))

    chosen, mapping, stats = _best_mapping_for_ids(cand_all, expected if expected else None)

    # filtr jen na id z aktuální dávky (pokud je známe)
    if expected:
        mapping = {k: v for k, v in mapping.items() if k in expected}

    # pár ukázkových položek do logu
    sample: List[Tuple[str,str]] = []
    for i, (k, v) in enumerate(mapping.items()):
        if i >= 5:
            break
        sample.append((k, v))

    return chosen, mapping, stats, sample
