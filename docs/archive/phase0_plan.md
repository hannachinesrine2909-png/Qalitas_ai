# Phase 0 - Baseline Fiable

## Objectif Global

Mettre en place une baseline mesurable, reproductible et comparable dans le temps avant toute optimisation qualité.

## Blocs

1. `B0.1` Contrat d'exécution reproductible
2. `B0.2` Gel du contexte modèle/prompt/version
3. `B0.3` Préflight des données et de la résolution des cas
4. `B0.4` Run complet du benchmark + publication des KPI de base

## B0.1 - Contrat d'exécution reproductible

### But

Standardiser les runs d'évaluation et empêcher les comparaisons invalides (ex: run partiel comparé à un run complet).

### Livrables

- `reports/contracts/run_contract_latest.json`
- `reports/history/contracts/run_contract_<timestamp>_<run_id>.json`
- Inclusion du contrat dans `reports/eval_latest.json`

### Règles clés

- `baseline_full`: interdit `--case_id` et `--limit`
- `baseline_subset`: exige `--case_id` ou `--limit`
- bornes `max_chars` contrôlées pour la comparabilité

### Traces de reproductibilité

- Fingerprint SHA-256 calculé sur les entrées normalisées
- Hash du dataset (`dataset_sha256`) + taille + date de modification
- Métadonnées runtime (hostname, python, platform, cwd)

### Fichiers principaux

- `baseline_contract.py`
- `evaluation.py`
- `test_baseline_contract.py`

## B0.2 - Gel du contexte modèle/prompt/version

### But

Capturer exactement le contexte technique qui influence les scores d'évaluation.

### Livrables

- `reports/context/run_context_latest.json`
- `reports/history/context/run_context_<timestamp>_<run_id>.json`
- Inclusion du contexte gelé dans `reports/eval_latest.json`
- Affichage résumé du contexte dans `reports/eval_latest.md`

### Éléments gelés

- Hash du prompt (`SYSTEM_PROMPT_A1`)
- Paramètres LLM effectifs (providers, modèles, température, tokens, retries)
- Hashes SHA-256 des fichiers clés de pipeline/extraction
- Versions dépendances critiques (pydantic, psycopg, providers SDK)
- Runtime Python (version, implémentation, exécutable)
- Empreintes des clés API (présence + fingerprint court, sans secret brut)
- Fingerprint global de contexte (`context_fingerprint`)

### Règles clés

- Le fingerprint est déterministe pour un même contexte
- Alerte si variables d'environnement numériques invalides (`parse_warnings`)
- Échec explicite si un fichier critique de contexte est introuvable

### Fichiers principaux

- `run_context_freeze.py`
- `evaluation.py`
- `test_run_context_freeze.py`

## B0.3 - Préflight des données et résolution des cas

### But

Vérifier avant extraction que les cas du run sont structurellement valides et réellement résolvables en base.

### Livrables

- `reports/preflight/preflight_latest.json`
- `reports/history/preflight/preflight_<timestamp>_<run_id>.json`
- Inclusion du preflight dans `reports/eval_latest.json`
- Résumé preflight dans `reports/eval_latest.md`

### Contrôles effectués

- Intégrité dataset:
  - `case_id` dupliqués
  - incohérence `expected_count` vs `expected_requirements`
  - incohérence `expected_zero`
- Résolution opérationnelle:
  - document résolu / non résolu
  - article résolu / non résolu
  - cas bypass `source_text`
  - `ready_cases`, `blocked_cases`, `readiness_rate`

### Modes d'exécution

- `warn`: le run continue malgré les blocages
- `strict`: le run s'arrête si erreurs dataset ou cas non résolus
- `only`: exécute uniquement le préflight et s'arrête

### Fichiers principaux

- `preflight_checks.py`
- `evaluation.py`
- `test_preflight_checks.py`

## B0.4 - Run baseline complet + KPI de référence

### But

Produire les KPI fondamentaux de la baseline sur un run traçable et comparable.

### Livrables

- `reports/kpis/baseline_kpis_latest.json`
- `reports/history/kpis/baseline_kpis_<timestamp>_<run_id>.json`
- Inclusion des KPI dans `reports/eval_latest.json`
- Affichage KPI dans `reports/eval_latest.md`

### KPI publiés

- `relaxed_type_f1`
- `fn_long_article_rate`
- `provider_error_pct`
- `cost_per_case_usd`
- `total_estimated_cost_usd` + métriques de support

### Hypothèses de coût

- Coût basé sur tokens provider quand disponibles
- Tarification configurable par `LLM_RATECARD_JSON`
- Fallback global:
  - `LLM_DEFAULT_INPUT_PER_1K_USD`
  - `LLM_DEFAULT_OUTPUT_PER_1K_USD`

### Fichiers principaux

- `baseline_kpis.py`
- `evaluation.py`
- `llm_extractor.py`
- `test_baseline_kpis.py`
