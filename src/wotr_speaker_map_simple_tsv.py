# WotR Speaker Mapper — simple token-first (TSV) + speaker_name
# - Priority: explicit Speaker GUID -> token match (folder + Dialogue name)
# - Token index ONLY over BlueprintUnit files (portraits etc. ignored)
# - Writes UTF-8 TSV with columns:
#   key, type, speaker_gender, speaker_name, speaker_guid_canonical, unit_file,
#   unit_name_key, unit_name_shared, dialogue_file, cue_or_answer_file

import argparse, csv, json, re
from pathlib import Path
from typing import Any, Dict, List, Optional

GUID_RE = re.compile(r"^!bp_([0-9a-fA-F]+)$")
HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")

STOPWORDS = {
    "dialogue","dialog","dialogs","dialogues","common","npc","companions",
    "main","cue","answer","act","chapter","quest","event",
    "c0","c1","c2","c3","c4","c5","c6","c7","c8","c9",
    "drezen","kenabres","ktc","goto","redoubt"
}

GENERIC_DIR_TOKENS = {
    "units","npc","npcs","companions","common","commoners","unique","boss","shadow",
    "enemies","friendly","ally","allies","variant","variants","preset","presets"
}

def load_json(p: Path) -> Optional[Dict[str, Any]]:
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def norm_guid(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    m = GUID_RE.match(v)
    if m: return m.group(1).lower()
    m2 = HEX32_RE.match(v)
    return m2.group(0).lower() if m2 else None

def tokenize(s: str) -> List[str]:
    toks = re.split(r"[^A-Za-z0-9]+", s.lower())
    return [t for t in toks if t and len(t) >= 2 and t not in STOPWORDS]

def tokens_from_dialog_folder(folder: Path, dialogue_file: Optional[Path]) -> List[str]:
    toks: List[str] = []
    parts = list(folder.parts)
    for name in parts[-2:]:
        toks += tokenize(name)
    if dialogue_file:
        toks += tokenize(dialogue_file.stem)
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t); out.append(t)
    return out

def path_score(unit_path: Path, tokens: List[str]) -> int:
    text = "_".join(list(unit_path.parts[-6:]) + [unit_path.stem]).lower()
    score = 0
    if "/units/companions/" in text: score += 100
    if "/units/npc" in text:        score += 80
    hits = sum(1 for t in tokens if t in text)
    score += 30 * hits
    score += max(0, 30 - len(text.split("/")))
    return score

def split_camel_words(s: str) -> str:
    # "ArueshalaeNightmare" -> "Arueshalae Nightmare"
    return re.sub(r'([a-z])([A-Z])', r'\1 \2', s)

def clean_name_token(tok: str) -> str:
    # remove leading CR/Level markers etc.
    tok = re.sub(r'^(cr\d+_?)', '', tok, flags=re.IGNORECASE)
    tok = re.sub(r'_?level\d+$', '', tok, flags=re.IGNORECASE)
    tok = tok.replace('_',' ').strip()
    return tok

def guess_speaker_name_from_path(p: Path) -> str:
    parts = [seg for seg in p.parts]
    parts_lower = [seg.lower() for seg in parts]

    # Prefer Companions\<Name>\...
    if "companions" in parts_lower:
        i = parts_lower.index("companions")
        if i + 1 < len(parts):
            cand = parts[i + 1]
            return clean_name_token(cand)

    # NPC: take the last meaningful directory before filename
    parent = p.parent
    # walk up until we hit something non-generic
    for seg in reversed(parent.parts):
        low = seg.lower()
        if low in GENERIC_DIR_TOKENS:
            continue
        # avoid numeric-ish folders
        if re.fullmatch(r'(c\d+|act\d+|\d+)', low):
            continue
        return clean_name_token(seg)

    # Fallback: from filename stem
    stem = p.stem
    stem = re.sub(r'^(cr\d+_?)', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'(_?level\d+)', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'(_?companion)', '', stem, flags=re.IGNORECASE)
    stem = stem.strip('_')
    pretty = split_camel_words(stem).replace('_',' ').strip()
    # take first 1–2 words (keeps "Arueshalae Nightmare", "Horgus")
    words = pretty.split()
    return " ".join(words[:2]) if words else ""

class UnitIndex:
    def __init__(self):
        self.unit_by_guid: Dict[str, Path] = {}
        self.unit_tokens: Dict[str, List[Path]] = {}
        self.units_all: List[Path] = []

    def add_unit(self, guid: str, p: Path):
        self.unit_by_guid[guid] = p
        self.units_all.append(p)
        toks = tokenize("_".join(list(p.parts[-6:]) + [p.stem]))
        for t in toks:
            self.unit_tokens.setdefault(t, []).append(p)

def build_unit_index(units_root: Path) -> UnitIndex:
    idx = UnitIndex()
    for p in units_root.rglob("*.jbp"):
        j = load_json(p)
        if not j: 
            continue
        data = (j.get("Data") or {})
        jtype = str(data.get("$type", ""))
        if "BlueprintUnit" not in jtype:
            continue
        asset_id = j.get("AssetId")
        if not isinstance(asset_id, str):
            continue
        guid = asset_id.replace("-", "").lower()
        idx.add_unit(guid, p)
    return idx

def find_best_unit_by_tokens(idx: UnitIndex, tokens: List[str]) -> Optional[Path]:
    cands: List[Path] = []
    for t in tokens:
        cands.extend(idx.unit_tokens.get(t, []))
    if not cands:
        return None
    best, best_score = None, -10**9
    seen = set()
    for f in cands:
        if f in seen: 
            continue
        seen.add(f)
        s = path_score(f, tokens)
        if s > best_score:
            best_score, best = s, f
    return best

def map_dialogs(dialogs_root: Path, idx: UnitIndex, out_tsv: Path):
    cue_files: List[Path] = []
    ans_files: List[Path] = []
    dialog_by_asset: Dict[str, Path] = {}

    for p in dialogs_root.rglob("*.jbp"):
        j = load_json(p)
        if not j: 
            continue
        data = (j.get("Data") or {})
        jtype = str(data.get("$type",""))
        if "BlueprintCue" in jtype or p.name.startswith("Cue_"):
            cue_files.append(p)
        elif "BlueprintAnswer" in jtype or p.name.startswith("Answer_"):
            ans_files.append(p)
        elif "BlueprintDialog" in jtype or p.name.startswith("Dialogue_"):
            aid = j.get("AssetId")
            if isinstance(aid, str):
                dialog_by_asset[aid.replace("-", "").lower()] = p

    headers = [
        "key","type","speaker_gender","speaker_name","speaker_guid_canonical","unit_file",
        "unit_name_key","unit_name_shared","dialogue_file","cue_or_answer_file"
    ]

    out_tsv.parent.mkdir(parents=True, exist_ok=True)
    with out_tsv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, delimiter="\t", lineterminator="\n")
        w.writeheader()

        # CUEs
        for cue_file in cue_files:
            cj = load_json(cue_file) or {}
            d = (cj.get("Data") or {})
            text = (d.get("Text") or {})
            key = text.get("m_Key")
            if not key:
                continue

            # try to get Dialogue file for tokens (optional)
            dlg_file: Optional[Path] = None
            pa = d.get("ParentAsset")
            if isinstance(pa, str):
                dlg_file = dialog_by_asset.get(pa.replace("-", "").lower())
            if not dlg_file:
                dlgs = list(cue_file.parent.glob("Dialogue_*.jbp"))
                if dlgs:
                    dlg_file = dlgs[0]

            sp = (d.get("Speaker") or {})
            sp_guid = norm_guid(sp.get("m_Blueprint"))

            chosen_unit: Optional[Path] = None

            if sp_guid and sp_guid in idx.unit_by_guid:
                chosen_unit = idx.unit_by_guid[sp_guid]
            else:
                toks = tokens_from_dialog_folder(cue_file.parent, dlg_file)
                if toks:
                    chosen_unit = find_best_unit_by_tokens(idx, toks)

            gender = ""
            speaker_name = ""
            speaker_guid_canonical = ""
            unit_name_key = ""
            unit_name_shared = ""

            if chosen_unit:
                uj = load_json(chosen_unit) or {}
                dd = (uj.get("Data") or {})
                gender = dd.get("Gender") or ""
                # canonical guid (PrototypeLink) – pokud chybí, vezmi AssetId
                proto = dd.get("PrototypeLink")
                if isinstance(proto, str):
                    speaker_guid_canonical = norm_guid(proto) or ""
                if not speaker_guid_canonical:
                    aid = uj.get("AssetId")
                    if isinstance(aid, str):
                        speaker_guid_canonical = aid.replace("-", "").lower()
                # name IDs (pro případné budoucí rozřešení z tvých JSONů)
                for fld in ("m_DisplayName","LocalizedName","CharacterName","m_CharacterName"):
                    vv = dd.get(fld)
                    if isinstance(vv, dict):
                        if vv.get("m_Key"):
                            unit_name_key = vv["m_Key"]; break
                        if "assetguid" in vv and "stringkey" in vv:
                            unit_name_shared = f"{vv.get('assetguid','')}|{vv.get('stringkey','')}"
                            break
                # speaker_name z cesty (heuristika)
                speaker_name = guess_speaker_name_from_path(chosen_unit)

            w.writerow({
                "key": key,
                "type": "Cue",
                "speaker_gender": gender,
                "speaker_name": speaker_name,
                "speaker_guid_canonical": speaker_guid_canonical,
                "unit_file": str(chosen_unit) if chosen_unit else "",
                "unit_name_key": unit_name_key,
                "unit_name_shared": unit_name_shared,
                "dialogue_file": "",  # zjednodušeno
                "cue_or_answer_file": str(cue_file),
            })

        # ANSWERs
        for ans_file in ans_files:
            aj = load_json(ans_file) or {}
            d = (aj.get("Data") or {})
            text = (d.get("Text") or {})
            key = text.get("m_Key")
            if not key:
                continue
            w.writerow({
                "key": key, "type": "Answer",
                "speaker_gender": "PC", "speaker_name": "",
                "speaker_guid_canonical": "PLAYER",
                "unit_file": "", "unit_name_key": "", "unit_name_shared": "",
                "dialogue_file": "", "cue_or_answer_file": str(ans_file),
            })

    print(f"[OK] Wrote TSV: {out_tsv}")

def main():
    ap = argparse.ArgumentParser(description="Simple WotR dialog speaker mapper (token-first, TSV, with speaker_name).")
    ap.add_argument("--dialogs-dir", type=Path, default=Path(r"C:\Games\Pathfinder\Blueprints\World\Dialogs"))
    ap.add_argument("--units-dir",   type=Path, default=Path(r"C:\Games\Pathfinder\Blueprints\Units"))
    ap.add_argument("--out",         type=Path, default=Path(r"C:\Games\Pathfinder\Překlad\skript\out_wotr\audit\wotr_dialog_speakers.tsv"))
    args = ap.parse_args()

    unit_index = build_unit_index(args.units_dir)
    map_dialogs(args.dialogs_dir, unit_index, args.out)

if __name__ == "__main__":
    main()
