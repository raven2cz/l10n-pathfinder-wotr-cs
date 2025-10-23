# Jak přispět

Díky, že chceš pomoci!

## Hlášení chyb
- Otevři **Issue** a uveď: verzi překladu (např. `v1.0`), verzi hry, lokaci ve hře, screenshot/citaci textu.

## Pull Requesty
- Forkni repo, vytvoř větev `fix/...` nebo `feat/...`.
- Úpravy dělej v `tests/beta` (nikoli přímo v `releases/`).
- Dbej na konzistenci zvolených termínů (viz `TERMINOLOGY_POLICY.md`).
- Malé PR vítány. Popiš změny stručně a s příkladem.

## Pomocné Soubory
- K dispozici máš czCZ.with_idx.json, který píše na konci idx. Ten můžeš použít s map.json a dohledat originál, nebo nám napsat tyto idxs pro opravy.
- Dále máš k dispozici beta_review.tsv, který kombinuje guid, český překlad a anglický originál, opět pomůže hodně při opravě.

## Build / vydání
- Vydáváme ze složek `releases/vX.Y/final` a `releases/vX.Y/final-en-terms`.
- GitHub Action **release.yml** umí zabalit ZIP + připojit SHA256 a založit draft GitHub Release.

