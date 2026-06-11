# Document Gate Evaluation Set

- cases_total: 5
- policy_accuracy: 1.0
- document_extractability_precision: 1.0
- administrative_noise_recall: 1.0
- source_missing_detection_rate: 1.0

| case_id | expected_policy | predicted_policy | policy_match | reason_overlap |
|---|---|---|---:|---|
| gate_regulatory_decret | EXTRACT_FULL | EXTRACT_FULL | 1 | DOC_REGULATORY_TITLE |
| gate_concours_procedural | EXTRACT_LIMITED | EXTRACT_LIMITED | 1 | DOC_CONCOURS_PROCEDURAL |
| gate_summary_drop | DROP | DROP | 1 | DOC_SUMMARY_NOTICE |
| gate_arabic_only_missing | TO_VALIDATE_SOURCE_MISSING | TO_VALIDATE_SOURCE_MISSING | 1 | DOC_SOURCE_ARABIC_ONLY |
| gate_annex_data | EXTRACT_LIMITED_DATA | EXTRACT_LIMITED_DATA | 1 | DOC_ANNEX_TABLE_DATA |
