# Čeština pro Pathfinder: Wrath of the Righteous (WotR)

Neoficiální český překlad (fan translation) celé hry **Pathfinder: Wrath of the Righteous** včetně DLC.  
Vznikl kombinací **AI asistence (GPT-5/DeepL)** a **celou řadou manuálních úprav** a kontrol konzistence.

- 🕹 Pokrytí: dialogy, UI, in-game texty
- 🎯 Cíl: přirozená čeština při zachování lore a stylu originálu
- 💾 Distribuce: GitHub (zdroj, průběžné verze) + **Nexus Mods a Komunitní-překlady**
- 📄 Licence: **CC BY-NC 4.0** (nekomerční)
- 👥 Autoři: **raven2cz** & **Vlkodav** (souhlas s mergem a společnou distribucí)

> **Pozn.:** Překlad není oficiální, není spojen s Owlcat Games ani Paizo. Veškerý obsah hry je majetkem jejich vlastníků.

---

## Instalace (stručně)
Instalace je **manuální** (nelze přes UMM/ModFinder).  
Plný návod (CZ): viz **[INSTALL.cs.md](./INSTALL.cs.md)**.  
Krátká EN verze: **[INSTALL.en.md](./INSTALL.en.md)**.

**Základ:**
1) Zálohuj původní `enGB.json`.  
2) Stáhni ZIP z *Releases* nebo Nexusu a rozbal.  
3) Zkopíruj `enGB.json` do:
```

.../Wrath_Data/StreamingAssets/Localization/

```
4) Nainstaluj **FontMod** přes UMM a vlož `Fonts/comic.ttf` pro diakritiku.

---

## Varianty vydání
V `releases/vX.Y` distribuujeme dvě varianty:

- **`final/`** – **výchozí** varianta: termíny v češtině (včetně textů uvnitř `{g … /g}`).  
- **`final-en-terms/`** – speciální varianta: **anglické termíny** uvnitř `{g … /g}` (glossary-style), zbytek česky.

Volba varianty řeší dlouhodobý spor „překládat/nepřekládat termíny“ a dává hráčům možnost volby.

---

## Metodika porovnání a merge (A vs B)
Detailní popis v **[MERGE_NOTES.md](./MERGE_NOTES.md)**, zkráceně:

- **A** = Raven2cz kombinovaný překlad (GPT + manuální pravidla)  
- **B** = DeepL překlad od **Vlkodav**

**Objektivní porovnání po řádcích (cca 110k řádků):**
- Mechanická pravidla → vítězí **A** (A=93 979, B=16 457 z 110 436)
- Embeddings + menší LM → **půl na půl** (A=52 588, B=57 781 z 110 369)
- Velký „LAPSE“ model → vítězí **B** o ~15k (A=47 570, B=62 799 z 110 369)

**Subjektivně při hraní:** U delších vět působil DeepL (*B*) uhlazeněji; A občas „drhne“.  
**Termíny:** DeepL má místy nevhodné překlady termínů → proto dvě **varianty** (viz výše).  
**Výsledek merge:** přibližně **B 57 % / A 43 %**, rozhodováno velkým modelem; následně probíhá **gender-pass** (ženské tvary) a sjednocení termínů podle zvolené varianty.

---

## Ke stažení
- **GitHub Releases:** https://github.com/raven2cz/l10n-pathfinder-wotr-cs/releases
- **Nexus Mods (doporučeno pro hráče):** odkaz doplníme po prvním zveřejnění.
- **Komunitni-preklady.org**: odkaz doplníme po prvním zveřejnění.

---

## Přispívání a hlášení chyb
- Hlášení chyb: GitHub **Issues** (šablony k dispozici) nebo přímo udělat fork a MR (ideál).
- Jak přispět/korektury: viz **[CONTRIBUTING.md](./CONTRIBUTING.md)**

---

## Transparentnost ohledně AI
První průchod překladem proběhl přes **GPT-5-mini**, poté následovala **manuální revize** a **automatizované kontroly** (embeddings, diffy, audit TSV atd.). Dále potom rozhodovací merge s DeepL překladem.

---

## Podpora autora
Pokud chceš podpořit další práci na lokalizacích, můžeš mi **koupit kávu** ☕:  
**https://www.buymeacoffee.com/raven2cz**  
Překlad je zdarma a nekomerční.

---

- **Translation content** (JSON/texts in `releases/`, `src/*.json`): **CC BY-NC 4.0**
- **Tools & scripts** (`src/*.py`, `tools/*.sh`): **MIT License**

This is an unofficial fan translation. Not affiliated with Owlcat Games or Paizo. All trademarks belong to their respective owners.

---

## English (short)
Unofficial Czech translation of WotR (incl. DLC). AI-assisted (GPT-5) + extensive manual edits.  
Manual install only (see `INSTALL.en.md`). Two editions: **final** (Czech terms) and **final-en-terms** (English terms within `{g ... /g}`).  
License: CC BY-NC 4.0. Authors: raven2cz & Vlkodav. Not affiliated with Owlcat/Paizo.

