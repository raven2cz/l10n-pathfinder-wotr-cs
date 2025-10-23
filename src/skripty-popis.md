# WOTR překladové skripty

# wotr_oneclick_translate_v2.1.py ---------------------------------------

Hlavní „orchestrátor“ překladu: umí prepare (připraví mapu a vstupy), run (spustí dávky – batch/sync – podle requests/ a states/), hlídá průběh a chybové stavy, a merge (sloučí nové překlady do trans/*.tsv a finálního csCZ.json); podporuje --skip-prepare, čte prompts.json, bezpečně navazuje na rozpracované dávky a nic nepřepisuje kolizně.
Jen prepare (poprvé vytvoří map.json, strukturu složek atd.)
```shell
python .\wotr_oneclick_translate_v2.1.py `
  -i enGB.json `
  -o out_wotr `
  --prepare
```

Full pipeline: prepare → run (batch) → merge
```shell
python .\wotr_oneclick_translate_v2.1.py `
  -i enGB.json `
  -o out_wotr `
  --prompts-file .\prompts-default.json `
  --prepare `
  --run --mode batch `
  --merge
```

Přeskakování přípravy: run (sync) → merge
```shell
python .\wotr_oneclick_translate_v2.1.py `
  -i enGB.json `
  -o out_wotr `
  --prompts-file .\prompts-default.json `
  --skip-prepare `
  --run --mode sync --sync-progress-secs 5 `
  --merge
```

Dry-run (ověří, co by se posílalo, ale nic nevolá na API)
```shell
python .\wotr_oneclick_translate_v2.1.py `
  -i enGB.json `
  -o out_wotr `
  --prompts-file .\prompts-default.json `
  --skip-prepare `
  --run --mode batch `
  --dry-run
```

Překlad jen konkrétních dávek
(čísla najdeš v out_wotr/audit/backfill_manifest.json nebo podle souborů states/req_XXX.state.json)
```shell
python .\wotr_oneclick_translate_v2.1.py `
  -i enGB.json `
  -o out_wotr `
  --skip-prepare `
  --run --mode sync `
  --only-batches 12,15 `
  --merge
```

```shell
python .\wotr_oneclick_translate_v2.1.py `
  -i enGB.json `
  -o out_wotr `
  --skip-prepare `
  --run --mode sync `
  --only-batches 20-25 `
  --merge
```

Pouze merge už hotových výsledků (když chceš jen přegenerovat csCZ.json)
```shell
python .\wotr_oneclick_translate_v2.1.py `
  -i enGB.json `
  -o out_wotr `
  --merge
```

```shell
--mode batch = pošle dávky a skončí; --mode sync = průběžně sleduje/propisuje výsledky (hodí se na menší JSONL ~200 kB).
--only-batches bere seznam (např. 1,3,8) i interval (10-15).
--dry-run provede všechny kroky kromě skutečných POSTů na API (logne, co by šlo ven).
--prompts-file je volitelný; když ho vynecháš, použije skript defaultní prompty.
```

# wotr_audit_and_backfill_v2.1.py -------------------------------------

Dělá audit aktuálního stavu (reporty missing.tsv, suspect.tsv, corrupt.tsv + summary.json) s heuristikami na shodu/délku/jazyk/dvojjazyčnost a nově „corrupt“ (krátký EN štítek vs. dlouhý CZ text – podezření na záměnu indexu); volitelně vygeneruje backfill JSONL dávky s bezpečným číslováním za maximem a tvrdým limitem velikosti souboru v bajtech, včetně separátních promptů pro kategorie.

```shell
python .\wotr_audit_and_backfill_v2.1.py `
   -i enGB.json -o out_wotr `
   --prompts-file .\prompts-default.json `
   --prompts-file-corrupt .\prompts-corrupt.json `
   --make-requests missing `
   --batch-max-bytes 200000 `
   --max-lines 120 `
   --max-chars 9000 `
   --jaccard-threshold 0.72 `
   --min-czech-chars 1 `
   --min-len-ratio 0.45 `
   --flag-bilingual `
   --corrupt-src-max-words 3 `
   --corrupt-tr-min-words 10 `
   --corrupt-min-len-ratio 3.0
```

# wotr_verify_shortlabel_anomalies.py ------------------------

Kontrolní join enGB.json × csCZ.json přes původní klíče a výpis error.tsv pro dva režimy: (A) „krátký EN (≤N slov) × dlouhý CZ (>M slov)“ jako pravděpodobná chyba směny textů; (B) „krátký EN (≤N) = CZ“ jako podezřelá totožnost, která vyžaduje ruční posouzení; prahy nastavitelné parametry.

```shell
python wotr_verify_shortlabel_anomalies.py `
  -e enGB.json -c out_wotr/csCZ.json -m out_wotr/map.json `
  -o out_wotr/audit/identical.tsv --mode identical --max-src-words 3

python .\wotr_verify_shortlabel_anomalies.py `
  -e enGB.json `
  -c out_wotr\csCZ.json `
  -m out_wotr\map.json `
  -o out_wotr\audit\errors_long.tsv `
  --mode longer `
  --max-src-words 3 `
  --min-tr-words 7
```

# wotr_patch_cz_from_tsv.py -----------------------------------

Aplikuje tvoje ruční opravy z TSV zpět do finálního csCZ.json: podle map.json dohledá GUIDy, vezme zvolený sloupec (default původní „Translation“, lze přepnout) a bezpečně přepíše příslušné hodnoty; idempotentní běh, nepřidává nové klíče, zapisuje atomicky.

```shell
python wotr_patch_cz_from_tsv.py `
  --tsv out_wotr/audit/errors.tsv `
  --map out_wotr/map.json `
  --cs-in out_wotr/csCZ.json `
  --cs-out out_wotr/csCZ.patched.json

  python wotr_patch_cz_from_tsv.py `
  --tsv out_wotr/audit/corrupt_fixed.tsv `
  --map out_wotr/map.json `
  --key-col idx `
  --patch-col Auto `
  --cs-in out_wotr/csCZ.json `
  --cs-out out_wotr/csCZ.patched.json

  python wotr_patch_cz_from_tsv.py `
  --tsv out_wotr/audit/to_patch_guid.tsv `
  --key-type guid `
  --patch-col 3 `
  --cs-in out_wotr/csCZ.json `
  --cs-out out_wotr/csCZ.patched.json
```

# wotr_idx_overlay_and_tsv.py -----------------------------------

Vygeneruje speciální czCZ-idx.json, kde namísto překladu vloží „idx + až dvě slova z anglického originálu“ (pro snadnou identifikaci chyby přímo ve hře); zároveň umí z parametru/souboru se seznamem indexů vyrobit korekční TSV šablonu ve stejném formátu jako missing.tsv/corrupt.tsv.

```shell
# 1) Generace korekčního TSV ze seznamu ID v parametru
python .\wotr_idx_overlay_and_tsv.py `
  --make tsv `
  --map .\out_wotr\map.json `
  --en .\enGB.json `
  --cz .\out_wotr\csCZ.json `
  --idx-list "2200,2282,5158,5160" `
  --out-tsv .\out_wotr\audit\corrections.tsv

# 2) Stejné, ale seznam ID ze souboru
python .\wotr_idx_overlay_and_tsv.py `
  --make tsv `
  --map .\out_wotr\map.json `
  --en .\enGB.json `
  --cz .\out_wotr\csCZ.json `
  --idx-file .\fix_me.txt `
  --out-tsv .\out_wotr\audit\corrections.tsv `
  --reason "manual-check"

# 3) Overlay czCZ-idx.json pro hru (zachová $id!)
python .\wotr_idx_overlay_and_tsv.py `
  --make overlay `
  --map .\out_wotr\map.json `
  --en .\enGB.json `
  --cz .\out_wotr\csCZ.json `
  --out-json .\out_wotr\czCZ-idx.json

# Překlad na zpět
python .\wotr_patch_cz_from_tsv.py `
  --tsv .\out_wotr\audit\corrections.tsv `
  --map .\out_wotr\map.json `
  --cs .\out_wotr\csCZ.json `
  --out .\out_wotr\csCZ.patched.json `
  --patch-col Translation  
```

# wotr_tsv_gpt_sync_apply.py -------------------------------------
 
Synchronně prožene libovolné TSV skrz GPT s daným prompts.json (system+user), vezme zvolený zdrojový sloupec (např. Source) a výsledek zapíše do sloupce translate (vždy obnoví), s dávkováním/limitací velikosti a možností „dry-run“ pro rychlou validaci bez zápisu.

```shell
python .\wotr_tsv_gpt_sync_apply.py `
  --in .\out_wotr\fix\multiline_src.tsv `
  --out .\out_wotr\fix\multiline_src_translated.tsv `
  --prompts .\prompts-multiline.json `
  --model gpt-5-mini `
  --source-col source_escaped `
  --output-col translation_escaped `
  --max-lines 12 `
  --max-chars 9000 `
  --timeout-s 1800 `
  --debug-dir .\out_wotr\fix\debug_sync  
```

# wotr_fix_bilingual_contains_source.py --------------------------------

Problem s " -> " a "  " tabs.

Očistit jen problematické důvody:
```shell
python .\wotr_fix_bilingual_contains_source.py `
  -i .\out_wotr\audit\suspect.tsv `
  -o .\out_wotr\fix\bilingual_only_fixed.tsv `
  --only-reasons bilingual_arrow
```

jen contains_source s pojistkou na krátké labely
Opraví vložený anglický zdroj v překladu, ale přeskočí krátké jména/labely (≤ 3 slova).
```shell
python .\wotr_fix_bilingual_contains_source.py `
  -i .\out_wotr\audit\suspect.tsv `
  -o .\out_wotr\fix\contains_source_fixed.tsv `
  --only-reasons contains_source `
  --min-src-words 3
```

Varianta C – vše v jednom průchodu
Užitečné, pokud chceš udělat oboje a mít vše v jednom výsledku:

```shell
python .\wotr_fix_bilingual_contains_source.py `
  -i .\out_wotr\audit\suspect.tsv `
  -o .\out_wotr\fix\both_fixed.tsv
```

Patch do csCZ.json:
```shell
# Příklad – nejdřív šipky:
python .\wotr_patch_cz_from_tsv.py `
  -t .\out_wotr\fix\bilingual_only_fixed.tsv `
  -m .\out_wotr\map.json `
  -c .\out_wotr\csCZ.json `
  --value-col translate_fixed `
  --inplace `
  --backup .\out_wotr\backup\csCZ.before_bilingual_fix.json

# Potom contains_source:
python .\wotr_patch_cz_from_tsv.py `
  -t .\out_wotr\fix\contains_source_fixed.tsv `
  -m .\out_wotr\map.json `
  -c .\out_wotr\csCZ.json `
  --value-col translate_fixed `
  --inplace `
  --backup .\out_wotr\backup\csCZ.before_contains_fix.json
```

# wotr_fix_bilingual_contains_source.py --------------------------------

Zjistí detaily z Blueprints ohledně toho, kdo dané dialogy mluví, zjistí gender a zapíše výsledky do csv.

```shell
python .\wotr_speaker_map.py `
  --dialogs-dir "C:\Games\Pathfinder\Blueprints\World\Dialogs" `
  --units-dir   "C:\Games\Pathfinder\Blueprints\Units" `
  --out         "C:\Games\Pathfinder\Překlad\skript\out_wotr\audit\wotr_dialog_speakers.csv"
```
  C:\Games\Pathfinder\Blueprints\World\Dialogs\c3\Drezen_C3\KTC_Arueshalae_GotoRedoubt\Cue_0002.jbp

# wotr_apply_female_by_speaker.py --------------------------------

Překlady určených ženských mluvčí na ženský rod.

A) Náhled 200 ženských replik (bez patchování)
```shell
python wotr_apply_female_by_speaker.py `
  --speakers .\out_wotr\audit\wotr_dialog_speakers.tsv `
  --cs-json .\out_wotr\csCZ.json `
  --out-tsv .\out_wotr\audit\female_preview.tsv `
  --limit 200
```

B) Náhodných 300 (seed 42), jen TSV
```shell
python wotr_apply_female_by_speaker.py `
  --speakers .\out_wotr\audit\wotr_dialog_speakers.tsv `
  --cs-json .\out_wotr\csCZ.json `
  --out-tsv .\out_wotr\audit\female_preview_sample.tsv `
  --limit 300 --random --seed 42
```

C) Všechno → TSV + patch JSON
```shell
python wotr_apply_female_by_speaker.py `
  --speakers .\out_wotr\audit\wotr_dialog_speakers.tsv `
  --cs-json .\out_wotr\csCZ.json `
  --out-tsv .\out_wotr\audit\female_full_preview.tsv `
  --out-json .\out_wotr\csCZ-patched.json `
  --apply --apply-all
```

# wotr_feminize_apply_tsv_parallel.py --------------------------------

```shell
python wotr_feminize_apply_tsv_parallel.py `
   --in .\out_wotr\audit\female_heroes_only_filtered.tsv `
   --out .\out_wotr\audit\female_feminized_changed.tsv `
   --prompts .\prompts-feminine.json `
   --speaker-col speaker_name `
   --text-col cs_text `
   --concurrency 6 `
   --rpm 120 `
   --timeout-s 600 `
   --retries 6 `
   --log-every-s 8 `
   --debug-dir .\out_wotr\audit\debug_feminize
```

```shell
python wotr_patch_cz_from_guid_tsv.py `
  --in .\out_wotr\audit\female_heroes_only_fixed.tsv `
  --cs .\out_wotr\csCZ.json `
  --out .\out_wotr\csCZ-patched.json `
  --guid-col key `
  --text-col cs_fixed `
  --report .\out_wotr\audit\female_patch_report.tsv
```

# wotr_cz_append_idx_overlay.py --------------------------------

### Přidat ` (IDX)` za každý CZ text:

```shell
python wotr_cz_append_idx_overlay.py `
  --cs out_wotr\csCZ.json `
  --map out_wotr\map.json `
  --out out_wotr\csCZ.with_idx.json
```

### Přidat ` (IDX)` jen pro vybrané indexy:

```shell
python wotr_cz_append_idx_overlay.py `
  --cs out_wotr\csCZ.json `
  --map out_wotr\map.json `
  --out out_wotr\csCZ.with_idx.some.json `
  --only-idxs "8527,8532,27034"
```

### Odstranit overlay (reverz):

```shell
python wotr_cz_append_idx_overlay.py `
  --cs out_wotr\csCZ.with_idx.json `
  --map out_wotr\map.json `
  --out out_wotr\csCZ.clean.json `
  --strip
```

# `wotr_make_idx_en_cs_tsv.py` --------------------------------

```shell
# standardně s escapováním \t/\n do \t a \n
python wotr_make_idx_en_cs_tsv.py `
  --map .\out_wotr\map.json `
  --en .\enGB.json `
  --cs .\out_wotr\csCZ.json `
  --out .\out_wotr\audit\beta_review.tsv

# jen pár indexů
python wotr_make_idx_en_cs_tsv.py `
  --map .\out_wotr\map.json `
  --en .\enGB.json `
  --cs .\out_wotr\csCZ.json `
  --out .\out_wotr\audit\beta_review_subset.tsv `
  --idxs 8527,8532,27034

# „raw“ bez escapování (pokud víš, že tvůj editor TSV zvládne \t a \n uvnitř buněk)
python wotr_make_idx_en_cs_tsv.py --map out_wotr/map.json --en enGB.json --cs out_wotr/csCZ.json --out out_wotr/audit/beta_review_raw.tsv --no-escape
```

# `wotr_export_heroine_lines.py` ---------------------------------

Extrahuje specifické texty hrdinů do tsv souborů, které potom lze následně přeložit např. do ženského rodu.
```shell
python .\wotr_export_heroine_lines.py `
   --speakers .\out_wotr\audit\wotr_dialog_speakers.tsv `
   --cs .\out_wotr\csCZ.json `
   --out .\out_wotr\audit\female_heroes_only_filtered.tsv
```

# `wotr_patch_glink_texts_from_deepl.py` ---------------------------------

Patch {g...} sekcí z jednoho souboru do druhého.
```shell
python .\wotr_patch_glink_texts_from_deepl.py `
   --deepl .\deepl\csCZ.json `
   --cs .\out_wotr\csCZ.json `
   --out .\out_wotr\csCZ-glink-patched.json `
   --report-tsv .\out_wotr\reports\glink_patched.tsv `
   --backup
```

# Po update csCZ.json zavolat --------------------------------

```shell
python wotr_cz_append_idx_overlay.py `
  --cs out_wotr\csCZ.json `
  --map out_wotr\map.json `
  --out out_wotr\csCZ.with_idx.json
```

```shell
# standardně s escapováním \t/\n do \t a \n
python wotr_make_idx_en_cs_tsv.py `
  --map .\out_wotr\map.json `
  --en .\enGB.json `
  --cs .\out_wotr\csCZ.json `
  --out .\out_wotr\beta_review.tsv
```