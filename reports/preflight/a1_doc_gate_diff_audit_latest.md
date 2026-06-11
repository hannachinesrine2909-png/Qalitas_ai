# A1 Document Gate Differential Audit

- docs_executed: 5
- removed_total: 39
- removed_noise_share: 0.6154
- removed_ambiguous_share: 0.3846
- removed_signal_lost_share: 0.0
- majority_noise_removed: True

## Reason Recalibration

| reason_code | removed_count | noise_share | ambiguous_share | signal_lost_share | current_policy | proposed_new_policy |
|---|---:|---:|---:|---:|---|---|
| ARTICLE_ANNEX_MIXED_NORMATIVE | 13 | 0.7692 | 0.2308 | 0.0 | EXTRACT_LIMITED | EXTRACT_LIMITED |
| ARTICLE_ANNEX_TABLE_DATA | 13 | 0.7692 | 0.2308 | 0.0 | EXTRACT_LIMITED_DATA | EXTRACT_LIMITED_DATA |
| ARTICLE_DEFAULT_FULL | 6 | 0.5 | 0.5 | 0.0 | EXTRACT_FULL | EXTRACT_LIMITED (SOFT) |
| ARTICLE_COMMISSION_GOVERNANCE | 5 | 0.2 | 0.8 | 0.0 | EXTRACT_LIMITED | EXTRACT_LIMITED (SOFT) |
| ARTICLE_CONCOURS_PROCEDURAL | 2 | 0.0 | 1.0 | 0.0 | EXTRACT_LIMITED | EXTRACT_LIMITED (SOFT) |

| title | before | after | removed | noise | ambiguous | signal_lost | forced TO_VALIDATE | gate_policy |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Jo0282016 | 6 | 0 | 6 | 3 | 3 | 0 | 0 | DROP |
| Code du travail | 98 | 93 | 5 | 1 | 4 | 0 | 0 | EXTRACT_FULL |
| Jo0612008 | 46 | 34 | 19 | 14 | 5 | 0 | 2 | EXTRACT_FULL |
| Jo0282026 | 10 | 10 | 0 | 0 | 0 | 0 | 0 | EXTRACT_FULL |
| Jo0602021 | 13 | 6 | 9 | 6 | 3 | 0 | 0 | EXTRACT_FULL |
