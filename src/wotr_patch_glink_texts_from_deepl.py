#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wotr_patch_glink_texts_from_deepl.py
------------------------------------
Vezme DeepL překlad (deepl/csCZ.json) a tvůj aktuální překlad (out_wotr/csCZ.json)
a na stejných GUIDech nahradí POUZE vnitřní texty sekcí {g|...}text{/g} tvého překladu
těmi z DeepL. Strukturu závorek {g|...}{/g}, ostatní placeholdery {…}, a vše okolo
zachová tak, jak je ve tvém JSONu.

Princip:
- pro každý GUID, který je v DeepL i ve tvém csCZ.json:
  * v DeepL větě vytáhne všechny páry {g|REF}TEXT{/g} a vytvoří pořadovou frontu TEXTů pro daný REF,
  * ve tvé větě projde výskyty {g|REF}...{/g} a pro každý REF (v pořadí) dosadí další TEXT z DeepL (pokud je),
    jinak ponechá původní.
- tím pádem se nahrazuje podle REF i pořadí (když je REF víckrát).

Bezpečnost:
- V PŘÍPADĚ, že DeepL věta nemá žádné {g|...}{/g}, nic se na daném GUIDu nemění.
- Pokud se počty výskytů REF liší, nahradí se jen ty, které jsou k dispozici; zbytek zůstane beze změny.
- Ostatní placeholdery/markup ({name}, {mf|...}, <i>…</i>, atd.) se vůbec nedotýkáme.

Výstupy:
- Vytvoří nový csCZ JSON (patched).
- Volitelně TSV report s počty nahrazených odkazů na GUID.

Použití (PowerShell):
  python wotr_patch_glink_texts_from_deepl.py `
    --deepl .\deepl\csCZ.json `
    --cs .\out_wotr\csCZ.json `
    --out .\out_wotr\csCZ-glink-patched.json `
    --report-tsv .\out_wotr\reports\glink_patched.tsv `
    --backup

Volby:
  --limit N          zpracuj max N GUIDů (debug)
  --dry-run          nic nezapisovat, jen vypsat statistiku
  --backup           uloží <cs>.bak před zápisem
"""

from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from typing import Dict, List, Tuple

GLINK_RE = re.compile(r"\{g\|([^}]+)\}(.*?)\{\/g\}", re.DOTALL)

def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR reading JSON {p}: {e}", file=sys.stderr)
        sys.exit(2)

def write_json_atomic(p: Path, obj: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)

def backup_file(src: Path) -> Path:
    bak = src.with_suffix(src.suffix + ".bak")
    bak.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return bak

def extract_glinks(s: str) -> List[Tuple[str, str]]:
    """Vrátí list (ref, text) pro všechny {g|ref}text{/g} ve stringu."""
    return [(m.group(1), m.group(2)) for m in GLINK_RE.finditer(s or "")]

def replace_glinks_by_ref_queue(src: str, ref2queue: Dict[str, List[str]]) -> Tuple[str, int]:
    """
    Ve stringu `src` najdi {g|REF}…{/g}. Pokud pro REF existuje neprázdná fronta v `ref2queue`,
    vezmi z ní další text a nahraď vnitřek. Jinak ponech původní. Vrací (nový_text, count_replaced).
    """
    if not src or "{g|" not in src:
        return src, 0

    out_parts: List[str] = []
    last = 0
    replaced = 0

    for m in GLINK_RE.finditer(src):
        ref = m.group(1)
        inner_old = m.group(2)

        out_parts.append(src[last:m.start()])  # co bylo před linkem
        out_parts.append("{g|")
        out_parts.append(ref)
        out_parts.append("}")

        q = ref2queue.get(ref)
        if q and len(q) > 0:
            inner_new = q.pop(0)
            out_parts.append(inner_new)
            replaced += 1
        else:
            out_parts.append(inner_old)

        out_parts.append("{/g}")
        last = m.end()

    out_parts.append(src[last:])  # zbytek
    return "".join(out_parts), replaced

def main():
    ap = argparse.ArgumentParser(description="Patchni v csCZ.json vnitřky {g|…}…{/g} z DeepL verze.")
    ap.add_argument("--deepl", required=True, help="DeepL csCZ.json")
    ap.add_argument("--cs",     required=True, help="Tvůj csCZ.json (zdroj k patchi)")
    ap.add_argument("--out",    required=True, help="Výstupní csCZ.json (patched)")

    ap.add_argument("--report-tsv", default=None, help="Volitelný TSV report (guid\\treplaced\\tdeepl_glinks\\tmine_glinks)")
    ap.add_argument("--limit", type=int, default=0, help="Zpracuj max N GUIDů (debug)")
    ap.add_argument("--dry-run", action="store_true", help="Nezapisovat JSON, jen reportovat")
    ap.add_argument("--backup", action="store_true", help="Před zápisem udělat .bak")
    args = ap.parse_args()

    deepl_path = Path(args.deepl)
    mine_path  = Path(args.cs)
    out_path   = Path(args.out)

    deepl = read_json(deepl_path)
    mine  = read_json(mine_path)

    if "strings" not in deepl or not isinstance(deepl["strings"], dict):
        print("ERROR: DeepL JSON neobsahuje objekt 'strings'.", file=sys.stderr)
        sys.exit(2)
    if "strings" not in mine or not isinstance(mine["strings"], dict):
        print("ERROR: csCZ.json (tvoje) neobsahuje objekt 'strings'.", file=sys.stderr)
        sys.exit(2)

    deepl_str: Dict[str, str] = deepl["strings"]
    mine_str:  Dict[str, str] = mine["strings"]

    guids = list(deepl_str.keys())
    if args.limit > 0:
        guids = guids[:args.limit]

    total = 0
    touched_strings = 0
    total_replaced = 0
    no_change = 0
    missing_in_mine = 0

    rep_lines: List[str] = []
    if args.report_tsv:
        rep_lines.append("guid\treplaced\tdeepl_glinks\tmine_glinks")

    for guid in guids:
        total += 1
        d_text = deepl_str.get(guid, "")
        m_text = mine_str.get(guid, None)
        if m_text is None:
            missing_in_mine += 1
            continue

        # DeepL i moje verze – extrahuj glinky z DeepL
        d_pairs = extract_glinks(d_text)
        if not d_pairs:
            # DeepL pro tuhle větu nemá žádné {g|…} – neděláme nic
            no_change += 1
            if args.report_tsv:
                rep_lines.append(f"{guid}\t0\t0\t{len(extract_glinks(m_text))}")
            continue

        # Připrav fronty textů podle REF
        ref2q: Dict[str, List[str]] = {}
        for ref, txt in d_pairs:
            ref2q.setdefault(ref, []).append(txt)

        new_text, replaced = replace_glinks_by_ref_queue(m_text, ref2q)

        if replaced > 0 and new_text != m_text:
            mine_str[guid] = new_text
            touched_strings += 1
            total_replaced += replaced
        else:
            no_change += 1

        if args.report_tsv:
            rep_lines.append(f"{guid}\t{replaced}\t{len(d_pairs)}\t{len(extract_glinks(m_text))}")

    # Výstupy / report
    print(f"[GLINK-PATCH] total_guid_in_deepl={total} | changed_strings={touched_strings} | "
          f"replaced_glinks={total_replaced} | no_change={no_change} | missing_in_mine={missing_in_mine}")

    if args.report_tsv:
        rep_path = Path(args.report_tsv)
        rep_path.parent.mkdir(parents=True, exist_ok=True)
        rep_path.write_text("\n".join(rep_lines) + "\n", encoding="utf-8")
        print(f"[REPORT] → {rep_path}")

    if args.dry_run:
        print("[DRY-RUN] JSON nezapsán.")
        return

    if args.backup:
        bak = backup_file(mine_path)
        print(f"[BACKUP] {bak}")

    write_json_atomic(out_path, mine)
    print(f"[OUT] → {out_path}")

if __name__ == "__main__":
    main()
