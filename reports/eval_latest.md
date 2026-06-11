# Evaluation A1 — golden_dataset_a1_v1

- Version dataset : **0.2.0**
- Date run : **2026-04-18T19:08:29**
- Run ID : **run_20260418T180653Z**
- Run profile : **baseline_full**
- Reproducibility fingerprint : **4f835aab63e9b3ab...**
- Context version : **B0.2-1.7.0**
- Context fingerprint : **712fbac41aabc8bb...**
- Primary model : **gpt-4.1-mini-2025-04-14**
- Fallback model : **gemini-2.5-flash**
- Prompt hash (SYSTEM_PROMPT_A1) : **d66bd35202b74d04...**
- Prompt contract version : **B2.1-1.0.0**
- Output schema version : **B2.1-schema-1.0.0**
- Output schema hash : **c62004c9f413ed03...**
- Preflight mode : **strict**
- Preflight status : **PASS**
- Preflight readiness : **28/28**
- Article resolution success rate : **1.0**
- Label ambiguity rate : **0.25**

## Résumé global

- **cases_total** : 28
- **cases_scored** : 28
- **cases_skipped** : 0
- **cases_skipped_provider_error** : 0
- **cases_ok** : 15
- **cases_partial** : 10
- **cases_failed_false_positive** : 0
- **cases_failed_false_negative** : 0
- **cases_failed_mismatch** : 3
- **avg_strict_text_f1** : 0.375
- **avg_strict_text_type_f1** : 0.375
- **avg_relaxed_text_f1** : 0.7841
- **avg_relaxed_text_type_f1** : 0.7609
- **avg_to_validate_rate** : 0.6429
- **precall_units_total** : 56
- **precall_units_sent_to_llm** : 39
- **precall_units_dropped_total** : 17
- **precall_llm_gate_ratio** : 0.6964
- **precall_llm_calls_reduction_pct_estimated** : 30.36
- **precall_normalization_ratio_global** : 1.0474
- **precall_list_blocks_preserved_total** : 1
- **precall_sentence_splits_total** : 24
- **precall_units_high_total** : 25
- **precall_units_low_total** : 13
- **precall_units_drop_total** : 18
- **precall_units_sent_high_total** : 25
- **precall_units_sent_low_total** : 14
- **precall_units_dropped_low_policy_total** : 0
- **precall_units_dropped_priority_drop_total** : 17
- **precall_units_shadow_drop_candidates_total** : 0
- **precall_units_shadow_low_policy_candidates_total** : 0
- **precall_strong_normative_units_total** : 9
- **postcall_candidates_total** : 58
- **postcall_kept_total** : 53
- **postcall_dropped_total** : 5
- **postcall_drop_rate** : 0.0862
- **postcall_status_downgraded_total** : 36
- **postcall_type_mismatch_total** : 0
- **type_consistency_rate** : 1.0
- **postcall_grounding_pass_total** : 51
- **postcall_grounding_soft_fail_total** : 7
- **postcall_grounding_hard_fail_total** : 0
- **grounding_pass_rate** : 0.8793
- **grounding_score_avg** : 0.7339
- **postcall_completeness_pass_total** : 54
- **postcall_completeness_soft_fail_total** : 4
- **postcall_completeness_hard_fail_total** : 0
- **completeness_pass_rate** : 0.931
- **completeness_score_avg** : 0.9759
- **postcall_missing_condition_total** : 0
- **postcall_missing_exception_total** : 4
- **postcall_missing_scope_total** : 0
- **postcall_auto_corrected_total** : 16
- **postcall_auto_corrections_total** : 17
- **auto_corrected_rate** : 0.2759
- **postcall_controlled_reject_total** : 4
- **controlled_reject_rate** : 0.069
- **postcall_duplicates_removed_total** : 1
- **duplicate_rate** : 0.0172
- **postcall_type_conflicts_total** : 0
- **postcall_type_conflicts_resolved_total** : 0
- **postcall_type_arbitration_updates_total** : 0
- **type_conflict_rate** : 0.0
- **postcall_out_of_scope_dropped_total** : 0
- **out_of_scope_fp_rate** : 0.0
- **postcall_quality_score_avg** : 0.8702
- **postcall_quality_decision_draft_total** : 0
- **postcall_quality_decision_to_validate_total** : 53
- **postcall_quality_decision_reject_total** : 0
- **drop_events_total** : 10
- **drop_reason_counts** : {'DROP_DEDUPLICATE': 6, 'DROP_SCORE_LOW': 3, 'DROP_UNKNOWN': 1}
- **drop_in_normalization_total** : 0
- **drop_type_conflict_total** : 0
- **drop_grounding_low_total** : 0
- **drop_score_low_total** : 3
- **drop_empty_after_postcall_total** : 0
- **false_positive_total_relaxed** : 24
- **fp_rate_global** : 0.4528
- **precision_at_draft** : None
- **recall_global** : 0.75
- **to_validate_rate_global** : 1.0
- **to_validate_rate** : 1.0
- **false_accept_rate** : None
- **llm_attempts_total** : 39
- **llm_success_total** : 39
- **llm_network_calls_total** : 35
- **llm_cache_hits_total** : 4
- **llm_cache_hits_strict_total** : 4
- **llm_cache_hits_relaxed_total** : 0
- **llm_cache_negative_hits_total** : 0
- **llm_cache_misses_total** : 35
- **cache_hit_rate** : 0.1026
- **cache_relaxed_hit_rate** : 0.0
- **cache_negative_hit_rate** : 0.0
- **llm_json_valid_total** : 35
- **llm_json_invalid_total** : 0
- **json_valid_rate** : 1.0
- **avg_tokens_per_call** : 5858.5143
- **latency_p95_ms** : 5703.6701
- **availability_extract_calls_total** : 39
- **availability_both_cooldown_blocked_total** : 0
- **availability_both_cooldown_blocked_rate** : 0.0
- **availability_primary_skipped_cooldown_total** : 0
- **availability_fallback_skipped_cooldown_total** : 0
- **availability_failfast_rate_limit_total** : 0
- **availability_cooldown_events_total** : 0
- **availability_cooldown_seconds_total** : 0.0
- **availability_retry_attempts_total** : 0
- **availability_retry_wait_seconds_total** : 0.0
- **availability_retry_exhausted_total** : 0
- **availability_fallback_invoked_total** : 0
- **availability_fallback_success_total** : 0
- **availability_fallback_success_rate** : None
- **availability_primary_error_category_counts** : {}
- **availability_fallback_error_category_counts** : {}
- **effective_calls_per_case** : 1.25
- **cases_planned_total** : 28
- **cases_processed_total** : 28
- **run_stopped_early** : False
- **circuit_breaker_triggered** : False
- **circuit_breaker_reason** : None
- **provider_error_pct_processed** : 0.0
- **circuit_breaker_provider_error_pct** : None
- **quality_calibration_model** : isotonic
- **quality_calibration_samples** : 53
- **quality_calibration_brier** : 0.213042
- **quality_calibration_ece** : 0.018868

## KPI Baseline (B0.4)

- **relaxed_type_f1** : 0.7609
- **fn_long_article_rate** : None
- **provider_error_pct** : 0.0
- **cost_per_case_usd** : 0.0
- **total_estimated_cost_usd** : 0.0
- **run_status** : VALID
- **gate_cases_scored_gte_95pct** : True
- **gate_provider_error_pct_lte_5** : True

## KPI Runtime LLM (B2.0)

- **json_valid_rate** : 1.0
- **avg_tokens_per_call** : 5858.5143
- **latency_p95_ms** : 5703.6701
- **cache_hit_rate** : 0.1026
- **cache_relaxed_hit_rate** : 0.0
- **cache_negative_hit_rate** : 0.0
- **llm_cache_hits_strict_total** : 4
- **llm_cache_hits_relaxed_total** : 0
- **llm_cache_negative_hits_total** : 0
- **llm_cache_misses_total** : 35
- **effective_calls_per_case** : 1.25

## Availability Control (B2.3)

- **availability_extract_calls_total** : 39
- **availability_both_cooldown_blocked_total** : 0
- **availability_both_cooldown_blocked_rate** : 0.0
- **availability_primary_skipped_cooldown_total** : 0
- **availability_fallback_skipped_cooldown_total** : 0
- **availability_failfast_rate_limit_total** : 0
- **availability_cooldown_events_total** : 0
- **availability_cooldown_seconds_total** : 0.0

## Retry/Fallback Policy (B2.2)

- **availability_retry_attempts_total** : 0
- **availability_retry_wait_seconds_total** : 0.0
- **availability_retry_exhausted_total** : 0
- **availability_fallback_invoked_total** : 0
- **availability_fallback_success_total** : 0
- **availability_fallback_success_rate** : None

## Phase 2 Runtime Governance (B2.6)

- **phase2_runtime_version** : B2.6-1.0.0
- **status** : PASS
- **mode_recommendation** : READY_FOR_PHASE3
- **provider_error_pct** : 0.0
- **json_valid_rate** : 1.0
- **latency_p95_ms** : 5703.6701
- **cache_hit_rate** : 0.1026
- **cache_relaxed_hit_rate** : 0.0
- **cache_negative_hit_rate** : 0.0
- **effective_calls_per_case** : 1.25
- **both_cooldown_blocked_rate** : 0.0
- **both_cooldown_blocked_total** : 0
- **failfast_rate_limit_total** : 0
- **retry_attempts_total** : 0
- **retry_wait_seconds_total** : 0.0
- **retry_exhausted_total** : 0
- **retry_exhausted_rate** : 0.0
- **fallback_invoked_total** : 0
- **fallback_success_total** : 0
- **fallback_success_rate** : None
- **llm_cache_hits_strict_total** : 4
- **llm_cache_hits_relaxed_total** : 0
- **llm_cache_negative_hits_total** : 0
- **llm_cache_misses_total** : 35
- **gate_infra_valid** : True
- **gate_provider_error_pct_lte_max** : True
- **gate_json_valid_rate_gte_min** : True
- **gate_latency_p95_ms_lte_max** : True
- **gate_effective_calls_per_case_lte_max** : True
- **gate_both_cooldown_blocked_rate_lte_max** : True
- **gate_fallback_success_rate_gte_min** : None
- **gate_retry_exhausted_rate_lte_max** : True
- **gate_cache_hit_rate_gte_min** : None

## Pre-call NLP (B1.0 + B1.3)

- **precall_version** : B1.0-B1.4-1.3.0
- **llm_calls_reduction_pct_estimated** : 30.36
- **units_sent_to_llm / units_total** : 39 / 56
- **normalization_ratio_global** : 1.0474
- **list_blocks_preserved_total** : 1
- **sentence_splits_total** : 24
- **units_high / low / drop** : 25 / 13 / 18
- **units_dropped_low_policy_total** : 0
- **strong_normative_units_total** : 9
- **shadow_filter_candidates_total** : 0
- **precall_mode_distribution** : shadow=0, soft=0, full=28, unknown=0

## Post-call NLP (B3.2/B3.3/B3.4)

- **postcall_version** : B4.4-1.1.0
- **candidates / kept / dropped** : 58 / 53 / 5
- **drop_rate** : 0.0862
- **status_downgraded_total** : 36
- **type_mismatch_total** : 0
- **type_consistency_rate** : 1.0
- **grounding_pass_total / soft_fail / hard_fail** : 51 / 7 / 0
- **grounding_pass_rate** : 0.8793
- **grounding_score_avg** : 0.7339
- **completeness_pass_total / soft_fail / hard_fail** : 54 / 4 / 0
- **completeness_pass_rate** : 0.931
- **completeness_score_avg** : 0.9759
- **missing_condition/exception/scope** : 0 / 4 / 0
- **auto_corrected_total / corrections_total** : 16 / 17
- **auto_corrected_rate** : 0.2759
- **controlled_reject_total / rate** : 4 / 0.069
- **duplicates_removed_total** : 1
- **duplicate_rate** : 0.0172
- **type_conflicts_total / resolved** : 0 / 0
- **type_arbitration_updates_total** : 0
- **type_conflict_rate** : 0.0
- **out_of_scope_dropped_total** : 0
- **out_of_scope_fp_rate** : 0.0
- **fp_rate_global** : 0.4528

## Phase 4 Scoring (B4.1-B4.4)

- **precision@DRAFT** : None
- **recall_global** : 0.75
- **to_validate_rate** : 1.0
- **false_accept_rate** : None
- **quality_score_avg** : 0.8261
- **quality_decision_draft_total** : 0
- **quality_decision_to_validate_total** : 53
- **quality_decision_reject_total** : 0

## Phase 4 Calibration Statistique (B4.5)

- **phase4_calibration_version** : B4.5-1.0.0
- **status** : PASS
- **mode_recommendation** : ENABLE_CALIBRATED_SCORE_MONITORING
- **samples_total** : 53
- **positive_rate** : 0.509434
- **selected_model** : isotonic
- **selected_brier** : 0.213042
- **selected_ece** : 0.018868
- **brier_delta_vs_raw** : 0.155573
- **recommended_draft_min** : 0.85
- **recommended_to_validate_min** : 0.65

## Phase 1 Calibration (B1.5/B1.6)

- **status** : PASS
- **mode_recommendation** : KEEP_FULL
- **pre_filter_recall** : 1.0
- **pre_filter_recall_proxy** : 1.0
- **llm_calls_reduction_pct** : 30.36
- **fp_descriptive_reduction_pct** : None
- **article_resolution_success_rate** : 1.0
- **label_ambiguity_rate** : 0.25
- **gate_pre_filter_recall_gte_97** : True
- **gate_pre_filter_recall_proxy_gte_97** : True
- **gate_article_resolution_success_gte_95** : True
- **gate_llm_calls_reduction_20_40** : True
- **gate_fp_descriptive_reduction_positive** : None

## Résultats par cas

| case_id | status | strict_f1 | strict_type_f1 | relaxed_f1 | relaxed_type_f1 | to_validate_rate |
|---|---|---:|---:|---:|---:|---:|
| cdt_art_10 | OK | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 |
| cdt_art_11 | PARTIAL | 0.5 | 0.5 | 1.0 | 0.75 | 1.0 |
| cdt_art_21 | PARTIAL | 0.0 | 0.0 | 0.5 | 0.5 | 1.0 |
| cdt_art_25 | PARTIAL | 0.0 | 0.0 | 0.4 | 0.4 | 1.0 |
| jort_128_2022_art1_concours | FAILED_MISMATCH | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 |
| jort_128_2022_art3_concours | PARTIAL | 0.0 | 0.0 | 0.6667 | 0.6667 | 1.0 |
| jort_121_2025_art1_contributions | FAILED_MISMATCH | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 |
| jort_121_2025_art2_contributions | PARTIAL | 0.0 | 0.0 | 0.8 | 0.8 | 1.0 |
| jort_121_2025_annexe_tarifs | OK | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 |
| jort_121_2025_nomination_member | OK | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 |
| jort_030_2026_art3_secheresse | OK | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 |
| jort_030_2026_art4_secheresse | FAILED_MISMATCH | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 |
| jort_030_2026_sommaire | OK | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 |
| jort_030_2026_nomination | OK | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 |
| jort_028_2026_art2_bct | PARTIAL | 0.0 | 0.0 | 0.75 | 0.75 | 1.0 |
| jort_028_2026_art1_regularisation | PARTIAL | 0.0 | 0.0 | 0.6667 | 0.6667 | 1.0 |
| jort_028_2026_expropriation_table | OK | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 |
| jort_029_2026_art3_redploiement | PARTIAL | 0.0 | 0.0 | 0.8 | 0.8 | 1.0 |
| jort_029_2026_art7_redploiement | PARTIAL | 0.0 | 0.0 | 0.5714 | 0.5714 | 1.0 |
| jort_029_2026_art8_redploiement | OK | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 |
| jort_029_2026_art11_redploiement | OK | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 |
| jort_029_2026_art12_redploiement | PARTIAL | 0.0 | 0.0 | 0.8 | 0.4 | 1.0 |
| jort_029_2026_nomination_member | OK | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 |
| jort_029_2026_demission_huissier | OK | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 |
| jort_029_2026_art1_services_fiscaux | OK | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 |
| jort_093_2020_delegation_legalisation | OK | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 |
| jort_093_2020_discipline | OK | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 |
| jort_093_2020_nomination_generaux | OK | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 |

## Cas à examiner en priorité

### cdt_art_11
- document : **Code du travail**
- article : **Art. 11**
- status : **PARTIAL**
- expected_count : 4
- predicted_count : 4
- strict_f1 : 0.5
- relaxed_f1 : 1.0
- relaxed_type_f1 : 0.75
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 4
- drop_events_total : 0
- chunks_error : 0
- note : Le score relaxed est meilleur que le strict : les écarts sont surtout des reformulations.
- provider_errors_summary : total=0, unique=0

### cdt_art_21
- document : **Code du travail**
- article : **Art. 21**
- status : **PARTIAL**
- expected_count : 3
- predicted_count : 5
- strict_f1 : 0.0
- relaxed_f1 : 0.5
- relaxed_type_f1 : 0.5
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 5
- drop_events_total : 0
- chunks_error : 0
- note : Le score relaxed est meilleur que le strict : les écarts sont surtout des reformulations.
- provider_errors_summary : total=0, unique=0
- attentes non matchées (relaxed) :
  - La notification doit être accompagnée des justifications nécessaires et de la liste des travailleurs avec leurs données requises, y compris les travailleurs concernés.
- prédictions non matchées (relaxed) :
  - La notification doit indiquer les raisons de la demande de licenciement ou de mise en chômage.
  - La notification doit être également accompagnée par les justifications nécessaires de la demande de licenciement ou de mise en chômage et par la liste de tous les travailleurs de l'entreprise avec indication de leur état civil, de la date de leur recrutement et de leurs qualifications professionnelles ainsi que des travailleurs concernés par le licenciement ou la mise en chômage.
  - La notification doit être accompagnée par la liste de tous les travailleurs de l'entreprise avec indication de leur état civil, de la date de leur recrutement et de leurs qualifications professionnelles ainsi que des travailleurs concernés par le licenciement ou la mise en chômage.

### cdt_art_25
- document : **Code du travail**
- article : **Art. 25**
- status : **PARTIAL**
- expected_count : 2
- predicted_count : 3
- strict_f1 : 0.0
- relaxed_f1 : 0.4
- relaxed_type_f1 : 0.4
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 4
- drop_events_total : 1
- drop_reason_counts (top) :
  - DROP_DEDUPLICATE : 1
- chunks_error : 0
- note : Le score relaxed est meilleur que le strict : les écarts sont surtout des reformulations.
- provider_errors_summary : total=0, unique=0
- attentes non matchées (relaxed) :
  - En cas de fermeture temporaire ou d’interdiction professionnelle prononcée à titre de sanction, le chef d’entreprise doit continuer à verser au personnel les salaires, indemnités et rémunérations dus, dans la limite de trois mois.
- prédictions non matchées (relaxed) :
  - En cas de suspension ou de rupture du contrat de travail, lorsque intervient une décision administrative ou judiciaire prononçant à titre de sanction la fermeture temporaire ou définitive d'une entreprise ou l'interdiction pour le chef de cette entreprise, d'exercer sa profession, ce dernier doit continuer à payer à prise, d'exercer sa profession, ce dernier doit continuer à payer à son personnel, pendant la durée de cette fermeture ou de cette interdiction, les salaires, indemnités et rémunérations de toutes natures auxquels il avait droit jusqu'alors sans que cette obligation puisse s'étendre au-delà de trois mois.
  - Si la fermeture ou l'interdiction doit excéder trois mois, le chef d'entreprise doit payer à son personnel toutes gratifications de fin de service prévues par la loi ou par les conventions collectives ou particulières ou par les usages.

### jort_128_2022_art1_concours
- document : **Jo1282022**
- article : **Article premier**
- status : **FAILED_MISMATCH**
- expected_count : 1
- predicted_count : 5
- strict_f1 : 0.0
- relaxed_f1 : 0.0
- relaxed_type_f1 : 0.0
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 5
- drop_events_total : 0
- chunks_error : 0
- note : Aucun match relaxed : probable écart de granularité, de type ou vrai faux positif.
- provider_errors_summary : total=0, unique=0
- attentes non matchées (relaxed) :
  - Peuvent participer au concours interne sur dossiers les conseillers praticiens concernés qui remplissent les conditions de grade, de diplôme et d’ancienneté prévues.
- prédictions non matchées (relaxed) :
  - Les candidats au concours interne doivent être conseillers praticiens principaux hors classe en éducation, titulaires dans leur grade.
  - Les candidats ne doivent pas avoir le diplôme national de licence ou la maîtrise ou diplôme équivalent.
  - Les candidats doivent justifier d'au moins cinq (5) ans d'ancienneté dans leur grade à la date de clôture de la liste des candidatures.
  - Tout dossier de candidature parvenu après la date limite de dépôt des dossiers de candidature doit être rejeté.
  - Le chef hiérarchique doit attribuer au candidat une note d'évaluation variant entre zéro (0) et vingt (20) qui caractérise l'accomplissement des tâches qui lui sont dévolues, sa discipline et sa rigueur professionnelle.

### jort_128_2022_art3_concours
- document : **Jo1282022**
- article : **Art. 3**
- status : **PARTIAL**
- expected_count : 2
- predicted_count : 4
- strict_f1 : 0.0
- relaxed_f1 : 0.6667
- relaxed_type_f1 : 0.6667
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 4
- drop_events_total : 0
- chunks_error : 0
- note : Le score relaxed est meilleur que le strict : les écarts sont surtout des reformulations.
- provider_errors_summary : total=0, unique=0
- prédictions non matchées (relaxed) :
  - Les candidats au concours interne doivent adresser leurs demandes de candidature par la voie hiérarchique.
  - Les demandes doivent être obligatoirement enregistrées au bureau d'ordre de l'administration à laquelle appartient le candidat.

### jort_121_2025_art1_contributions
- document : **Jo1212025**
- article : **Article premier**
- status : **FAILED_MISMATCH**
- expected_count : 1
- predicted_count : 1
- strict_f1 : 0.0
- relaxed_f1 : 0.0
- relaxed_type_f1 : 0.0
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 1
- drop_events_total : 0
- chunks_error : 0
- note : Aucun match relaxed : probable écart de granularité, de type ou vrai faux positif.
- provider_errors_summary : total=0, unique=0
- attentes non matchées (relaxed) :
  - Les contributions prévues par les articles visés sont fixées conformément aux tableaux annexés au présent arrêté.
- prédictions non matchées (relaxed) :
  - Les contributions prévues par les articles 15 et 21 de la loi n° 92-72 du 3 août 1992 sont fixées conformément aux tableaux.

### jort_121_2025_art2_contributions
- document : **Jo1212025**
- article : **Art. 2**
- status : **PARTIAL**
- expected_count : 2
- predicted_count : 3
- strict_f1 : 0.0
- relaxed_f1 : 0.8
- relaxed_type_f1 : 0.8
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 3
- drop_events_total : 0
- chunks_error : 0
- note : Le score relaxed est meilleur que le strict : les écarts sont surtout des reformulations.
- provider_errors_summary : total=0, unique=0
- prédictions non matchées (relaxed) :
  - Les recettes affectées au compte de la protection des végétaux doivent couvrir les dépenses afférentes au contrôle phytosanitaire et aux différentes analyses et opérations relatives aux pesticides à usage agricole.

### jort_030_2026_art4_secheresse
- document : **Jo0302026**
- article : **Art. 4**
- status : **FAILED_MISMATCH**
- expected_count : 2
- predicted_count : 2
- strict_f1 : 0.0
- relaxed_f1 : 0.0
- relaxed_type_f1 : 0.0
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 3
- drop_events_total : 1
- drop_reason_counts (top) :
  - DROP_UNKNOWN : 1
- chunks_error : 0
- note : Aucun match relaxed : probable écart de granularité, de type ou vrai faux positif.
- provider_errors_summary : total=0, unique=0
- attentes non matchées (relaxed) :
  - Les agriculteurs ayant bénéficié de crédits de campagne et ayant subi des dommages dans les zones prévues bénéficient du rééchelonnement de leurs dettes à condition de présenter le certificat requis.
  - Le Fonds national de garantie prend en charge les intérêts du rééchelonnement sous réserve que celui-ci soit traité au cas par cas et n’inclue pas les agriculteurs des périmètres irrigués.
- prédictions non matchées (relaxed) :
  - Les personnes peuvent bénéficier du rééchelonnement de leurs dettes à condition de présenter un certificat de constat délivré par le commissariat régional au développement agricole concerné prouvant les dommages causés par la sécheresse.
  - Dans les zones fixées par l'article 2 du présent arrêté, bénéficient d'une indemnisation d'un pourcentage des dommages sur la base d'un rapport d'expertise.

### jort_028_2026_art2_bct
- document : **Jo0282026**
- article : **Art. 2**
- status : **PARTIAL**
- expected_count : 3
- predicted_count : 5
- strict_f1 : 0.0
- relaxed_f1 : 0.75
- relaxed_type_f1 : 0.75
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 5
- drop_events_total : 0
- chunks_error : 0
- note : Le score relaxed est meilleur que le strict : les écarts sont surtout des reformulations.
- provider_errors_summary : total=0, unique=0
- prédictions non matchées (relaxed) :
  - La Banque centrale de Tunisie doit fixer les modèles unifiés des contrats de régularisation et les délais impartis pour l'accomplissement des procédures.
  - Les délais impartis pour l'accomplissement des procédures ne peuvent dépasser un mois à compter de la date de dépôt de la demande de régularisation.

### jort_028_2026_art1_regularisation
- document : **Jo0282026**
- article : **Article premier**
- status : **PARTIAL**
- expected_count : 2
- predicted_count : 4
- strict_f1 : 0.0
- relaxed_f1 : 0.6667
- relaxed_type_f1 : 0.6667
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 7
- drop_events_total : 3
- drop_reason_counts (top) :
  - DROP_DEDUPLICATE : 3
- chunks_error : 0
- note : Le score relaxed est meilleur que le strict : les écarts sont surtout des reformulations.
- provider_errors_summary : total=0, unique=0
- prédictions non matchées (relaxed) :
  - En cas de règlement intégral de la dette sans rééchelonnement, le débiteur bénéficie de la remise de 50% de la valeur des intérêts contractuels initiaux.
  - En cas de règlement intégral de la dette sans rééchelonnement, le débiteur bénéficie de la remise totale des pénalités de retard et de 50% de la valeur des intérêts contractuels initiaux, à charge d'apurer la totalité de la dette dans un délai maximum de six mois à compter du dépôt de la demande de régularisation.

### jort_029_2026_art3_redploiement
- document : **Jo0292026**
- article : **Art. 3**
- status : **PARTIAL**
- expected_count : 2
- predicted_count : 3
- strict_f1 : 0.0
- relaxed_f1 : 0.8
- relaxed_type_f1 : 0.8
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 3
- drop_events_total : 0
- chunks_error : 0
- note : Le score relaxed est meilleur que le strict : les écarts sont surtout des reformulations.
- provider_errors_summary : total=0, unique=0
- prédictions non matchées (relaxed) :
  - Le président du jury peut, le cas échéant, faire appel à toute personne qualifiée dans sa spécialité pour assister les travaux du jury sans pouvoir participer aux délibérations.

### jort_029_2026_art7_redploiement
- document : **Jo0292026**
- article : **Art. 7**
- status : **PARTIAL**
- expected_count : 3
- predicted_count : 4
- strict_f1 : 0.0
- relaxed_f1 : 0.5714
- relaxed_type_f1 : 0.5714
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 4
- drop_events_total : 0
- chunks_error : 0
- note : Le score relaxed est meilleur que le strict : les écarts sont surtout des reformulations.
- provider_errors_summary : total=0, unique=0
- attentes non matchées (relaxed) :
  - Le dossier de candidature doit comprendre le formulaire, les copies des diplômes requis et les relevés de notes demandés.
- prédictions non matchées (relaxed) :
  - Le communiqué publié doit fixer la date limite de la réception des dossiers de candidature.
  - Le dossier de candidature doit comprendre une copie de l'arrêté fixant la dernière situation administrative du candidat, le formulaire de candidature, une copie du diplôme du baccalauréat ou du diplôme équivalent, et un relevé de services signé par le chef de l'administration ou son représentant.

### jort_029_2026_art12_redploiement
- document : **Jo0292026**
- article : **Art. 12**
- status : **PARTIAL**
- expected_count : 2
- predicted_count : 3
- strict_f1 : 0.0
- relaxed_f1 : 0.8
- relaxed_type_f1 : 0.4
- providers_used : ['openai']
- models_used : ['gpt-4.1-mini-2025-04-14']
- fallback_used : False
- raw_llm_requirements : 3
- drop_events_total : 0
- chunks_error : 0
- note : Le score relaxed est meilleur que le strict : les écarts sont surtout des reformulations.
- provider_errors_summary : total=0, unique=0
- prédictions non matchées (relaxed) :
  - Toute fraude ou tentative de fraude dûment constatée entraîne l'exclusion immédiate du candidat de la salle d'examen.
