# Phase 1 - Pre-call NLP v1

## Objectif

Réduire le bruit avant LLM pour baisser les faux positifs descriptifs et améliorer la stabilité sur textes juridiques longs.

## B1.0 - Contrat de données + instrumentation

### Livrables

- `reports/precall/precall_latest.json`
- `reports/history/precall/precall_<timestamp>_<run_id>.json`
- champ `precall` par cas dans `reports/eval_latest.json`

### Champs instrumentés

- normalisation: longueur avant/après, ratio, symptômes OCR/ponctuation
- gating: unités totales, unités envoyées LLM, unités rejetées
- indicateur global estimé: `llm_calls_reduction_pct_estimated`

### Fichiers

- `precall_nlp.py`
- `evaluation.py`

## B1.1 - Normalisation texte robuste

### But

Stabiliser les entrées juridiques avant segmentation/extraction:

- NFKC Unicode
- correction ligatures (`œ`, `ﬁ`, `ﬂ`)
- normalisation ponctuation/apostrophes/tirets
- réparation des césures OCR (`mot-\nif` -> `motif`)
- nettoyage headers/pages bruitées
- compactage espaces et retours ligne

### Fichiers

- `precall_nlp.py`
- `test_precall_nlp.py`

## B1.2 - Segmentation juridique orientée droit

### But

Segmenter avant LLM en conservant la structure normative:

- phrases légales (ponctuation forte)
- blocs de listes normatives introduits par `:`
- alinéas/listes à puces ou numérotées

### Mesures ajoutées

- `list_blocks_preserved_total`
- `sentence_splits_total`
- `units_from_list_blocks_total`
- `units_from_sentence_split_total`
- `units_from_paragraph_fallback_total`

### Fichiers

- `precall_nlp.py`
- `evaluation.py`
- `test_precall_nlp.py`

## B1.3 - Scoring normatif + priorisation HIGH/LOW/DROP

### But

Classifier chaque unité avant appel LLM pour réduire les appels peu utiles:

- `HIGH`: unité fortement normative
- `LOW`: unité ambiguë (appel configurable)
- `DROP`: unité descriptive/bruit

### Politique configurable

- `--precall_high_threshold`
- `--precall_low_threshold`
- `--precall_low_mode` (`call` ou `skip`)

### Mesures ajoutées

- `units_high_total`, `units_low_total`, `units_drop_total`
- `units_sent_high_total`, `units_sent_low_total`
- `units_dropped_low_policy_total`
- `units_dropped_priority_drop_total`

### Fichiers

- `precall_nlp.py`
- `evaluation.py`
- `test_precall_nlp.py`

## B1.4 - Post-call qualité (anti-FP + cohérence)

### But

Appliquer des garde-fous après sortie LLM pour réduire les faux positifs:

- suppression des sorties descriptives non normatives
- contrôle cohérence `req_type` vs texte
- contrôle de chevauchement lexical avec le snippet source
- downgrade automatique `DRAFT -> TO_VALIDATE` quand risque élevé

### Livrables

- `reports/postcall/postcall_latest.json`
- `reports/history/postcall/postcall_<timestamp>_<run_id>.json`
- champ `postcall` par cas dans `reports/eval_latest.json`

### Mesures ajoutées

- `postcall_candidates_total`, `postcall_kept_total`, `postcall_dropped_total`
- `postcall_drop_rate`
- `postcall_status_downgraded_total`
- `postcall_type_mismatch_total`
- distribution des raisons (`reason_counts`)

### Fichiers

- `postcall_quality.py`
- `evaluation.py`
- `test_postcall_quality.py`
- `test_evaluation_precall.py`

## B1.5 - Évaluation offline + calibration seuils

### But

Mesurer les gains Phase 1 avec des gates explicites Go/No-Go:

- `llm_calls_reduction_pct`
- `pre_filter_recall`
- `fp_descriptive_reduction_pct`

### Livrables

- `reports/phase1/phase1_calibration_latest.json`
- `reports/history/phase1/phase1_calibration_<timestamp>_<run_id>.json`
- section calibration dans `reports/eval_latest.md`

### Notes d'implémentation

- baseline de référence optionnelle via `--phase1_baseline_json`
- fallback sans baseline: métriques delta partielles + statut `WARN`
- gate critique: `pre_filter_recall >= 0.97`
- KPI proxy ajouté: `pre_filter_recall_proxy` (couverture pré-filtre avant qualité LLM)
- `mode_recommendation` généré automatiquement (`PROMOTE_TO_SOFT`, `PROMOTE_TO_FULL`, etc.)
- charge également un baseline au format `phase1_calibration_latest.json` ou `eval_latest.json`

### Fichiers

- `phase1_calibration.py`
- `evaluation.py`
- `test_phase1_calibration.py`

## B1.6 - Déploiement progressif sécurisé

### But

Déployer le filtrage pré-call sans risque de régression brutale.

### Modes runtime

- `shadow`: n'applique pas de blocage, mesure les candidats qui seraient filtrés
- `soft`: applique `DROP`, conserve `LOW`
- `full`: applique `DROP` + politique `LOW` (`call/skip`)

### Paramètres CLI

- `--precall_mode {shadow,soft,full}`
- `--precall_low_mode {call,skip}`
- `--precall_high_threshold`
- `--precall_low_threshold`

### Observabilité ajoutée

- `precall_mode_distribution` dans le report pré-call
- `shadow_filter_candidates_total` pour mesurer ce qui serait filtré en mode shadow

### Fichiers

- `evaluation.py`
- `test_evaluation_precall.py`
