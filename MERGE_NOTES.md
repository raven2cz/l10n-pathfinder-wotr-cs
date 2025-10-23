# Merge Notes (A vs B)

- **A** = combined GPT-assisted + rule-based + manual fixes (raven2cz)
- **B** = DeepL baseline (Vlkodav)

## Objective Line-by-Line Comparison (~110k lines)

1) **Mechanical rules only (hard constraints)**
   - total=110,436 | picked_A=93,979 | picked_B=16,457 → clear win **A**

2) **Embeddings + small LM**
   - total=110,369 | picked_A=52,588 | picked_B=57,781 → ~50/50 (slight **B**)

3) **Large "LAPSE" model decision**
   - total=110,369 | picked_A=47,570 | picked_B=62,799 → **B +15k**

## Subjective Playtest

- Long sentences read smoother in **B** (DeepL); **A** sometimes “catches” on phrasing.
- Terminology in **B** is occasionally off (immersion / lore consistency issue).

## Final Merge Strategy

- Use the **large model decision** as tie-breaker → merged result ≈ **B 57% / A 43%**.
- Provide **two editions** to address terminology preferences:
  - `final/` → Czech terms (incl. `{g ... /g}` Czech)
  - `final-en-terms/` → English terms inside `{g ... /g}` + Czech prose
- Run a **post-merge female-gender pass** to ensure consistency (especially NPC lines).

## Credits and Consent

- **Vlkodav** explicitly agreed to:
  - allow merging,
  - co-author attribution,
  - and distribution as a new version under this repository.

