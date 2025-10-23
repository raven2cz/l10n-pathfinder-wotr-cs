# WotR coverage checker for dialog keys (TSV outputs)
# Counts how many BlueprintCue/BlueprintAnswer Text.m_Key are translated in csCZ.json.
# Also reports "shared strings" (Text.Shared) and extras present in csCZ.json but not in Blueprints.

import argparse, json
from pathlib import Path
from typing import Dict, Any, Set, List, Optional

def load_json(p: Path) -> Optional[Dict[str, Any]]:
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def collect_dialog_keys(dialogs_root: Path):
    total_files = 0
    key_set: Set[str] = set()          # all m_Key (lowercased)
    key_rows: List[tuple] = []          # (key, type, file)
    shared_rows: List[tuple] = []       # ((assetguid|stringkey), type, file)
    no_key_rows: List[tuple] = []       # (type, file)

    for p in dialogs_root.rglob("*.jbp"):
        j = load_json(p)
        if not j:
            continue
        data = j.get("Data") or {}
        jtype = str(data.get("$type", ""))
        is_cue = "BlueprintCue" in jtype or p.name.startswith("Cue_")
        is_ans = "BlueprintAnswer" in jtype or p.name.startswith("Answer_")
        if not (is_cue or is_ans):
            continue

        total_files += 1
        typ = "Cue" if is_cue else "Answer"

        text = data.get("Text") or {}
        key = text.get("m_Key") or ""
        if key:
            k = key.strip().lower()
            key_set.add(k)
            key_rows.append((k, typ, str(p)))
            continue

        shared = text.get("Shared")
        if isinstance(shared, dict) and shared.get("assetguid") and shared.get("stringkey"):
            pair = f"{shared['assetguid']}|{shared['stringkey']}"
            shared_rows.append((pair, typ, str(p)))
        else:
            no_key_rows.append((typ, str(p)))

    return {
        "total_cue_answer_files": total_files,
        "keys": key_set,
        "key_rows": key_rows,
        "shared_rows": shared_rows,
        "no_key_rows": no_key_rows,
    }

def load_translation_strings(cs_path: Path) -> Dict[str, str]:
    j = load_json(cs_path) or {}
    strings = j.get("strings") or {}
    # normalize keys to lowercase (GUIDs)
    return { (k.lower() if isinstance(k, str) else k): v for k, v in strings.items() }

def write_tsv(path: Path, headers: List[str], rows: List[List[str]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("\t".join(headers) + "\n")
        for r in rows:
            f.write("\t".join(r) + "\n")

def main():
    ap = argparse.ArgumentParser(description="Compute coverage of WotR dialog keys vs csCZ.json")
    ap.add_argument("--dialogs-dir", type=Path, default=Path(r"C:\Games\Pathfinder\Blueprints\World\Dialogs"))
    ap.add_argument("--cs-json",     type=Path, default=Path(r"C:\Games\Pathfinder\Překlad\skript\out_wotr\csCZ.json"))
    ap.add_argument("--out-missing", type=Path, default=Path(r"C:\Games\Pathfinder\Překlad\skript\out_wotr\audit\coverage_dialogs_missing.tsv"))
    ap.add_argument("--out-shared",  type=Path, default=Path(r"C:\Games\Pathfinder\Překlad\skript\out_wotr\audit\coverage_dialogs_shared.tsv"))
    ap.add_argument("--out-extras",  type=Path, default=Path(r"C:\Games\Pathfinder\Překlad\skript\out_wotr\audit\coverage_translation_extras.tsv"))
    args = ap.parse_args()

    dialogs_root = args.dialogs_dir
    cs_json_path = args.cs_json

    print(f"[info] Scanning dialogs: {dialogs_root}")
    coll = collect_dialog_keys(dialogs_root)
    keys = coll["keys"]
    key_rows = coll["key_rows"]
    shared_rows = coll["shared_rows"]
    no_key_rows = coll["no_key_rows"]

    print(f"[info] Loading translation: {cs_json_path}")
    cs = load_translation_strings(cs_json_path)

    # Coverage stats
    total_keys = len(keys)
    translated = 0
    missing_rows: List[List[str]] = []
    for k, typ, path in key_rows:
        val = cs.get(k)
        if isinstance(val, str) and val.strip() != "":
            translated += 1
        else:
            missing_rows.append([k, typ, path])

    coverage = (translated / total_keys * 100.0) if total_keys > 0 else 100.0

    # Shared dump
    shared_out_rows = [[pair, typ, path] for (pair, typ, path) in shared_rows]

    # Extras in translation (keys that don't occur among dialog m_Key)
    extras_rows: List[List[str]] = []
    for k in cs.keys():
        if k not in keys:
            # NOTE: tahle “extra” může být jiný typ stringu (UI, itemy, barks…), to je v pořádku
            extras_rows.append([k, cs.get(k, "")[:80].replace("\t", " ").replace("\n", " ") + ("…" if len(cs.get(k, "")) > 80 else "")])

    # Write TSVs
    write_tsv(args.out_missing, headers=["key","type","file"], rows=missing_rows)
    write_tsv(args.out_shared,  headers=["shared_pair(assetguid|stringkey)","type","file"], rows=shared_out_rows)
    write_tsv(args.out_extras,  headers=["key_not_in_dialogs","value_preview"], rows=extras_rows)

    # Console summary
    print("====== COVERAGE (Dialogs: BlueprintCue/Answer, Text.m_Key only) ======")
    print(f"Total Cue/Answer files scanned: {coll['total_cue_answer_files']}")
    print(f"Total dialog keys (m_Key):       {total_keys}")
    print(f"Translated keys in csCZ.json:    {translated}")
    print(f"Coverage:                        {coverage:.2f}%")
    print(f"Shared strings encountered:      {len(shared_rows)}   (listed in: {args.out_shared})")
    print(f"Entries without m_Key or Shared: {len(no_key_rows)}")
    print(f"Missing translations (keys):     {len(missing_rows)}  (see: {args.out_missing})")
    print(f"Extra keys in csCZ.json:         {len(extras_rows)}   (see: {args.out_extras})")

if __name__ == "__main__":
    main()
