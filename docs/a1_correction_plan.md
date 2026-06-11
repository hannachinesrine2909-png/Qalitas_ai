# Plan De Correction A1 (Optimise Et Professionnel)

## 1) Objectif

Stabiliser et industrialiser l'agent A1 pour qu'il devienne un socle fiable des agents A2-A4.

Valeur ajoutee attendue:
- meilleure fiabilite des donnees juridiques (tracabilite article -> exigence),
- reduction des reprises manuelles (gestion des doublons PDF),
- meilleure auditableite (garde-fous DB + journalisation),
- diminution du risque projet avant extension multi-agents.

## 2) Perimetre

Correction A1 sur 4 axes:
- securite de configuration,
- integrite des donnees,
- robustesse de l'ingestion/extraction,
- qualite de validation avant passage a A2.

## 3) Plan D'execution

### Phase 1 - Hygiene & Securite
- livrable: `.env.example` + `.gitignore` securise.
- action: retirer tout secret du flux de partage et standardiser les variables d'environnement.
- KPI: aucune cle sensible dans les fichiers de reference.

### Phase 2 - Integrite Data A1
- livrable: script de reparation `tools/maintenance/a1_repair_data_integrity.py`.
- actions:
  - backfill de `requirements.article_id` quand il est null (via `citation_ref` + page + article),
  - creation d'index de controle sur `documents(tenant_id, sha256)`,
  - activation d'un garde-fou pour interdire les exigences sans `article_id`.
- KPI:
  - `requirements.article_id null = 0`,
  - aucune FK cassee,
  - contraintes de controle actives.

### Phase 3 - Robustesse Pipeline
- livrables: patch `a1_ingest_pdf_min.py` et `a1_extract_requirements_llm.py`.
- actions:
  - anti-doublon PDF a l'ingestion (par SHA256 + tenant),
  - options explicites de strategie doublon (`reuse` / `fail` / `reinject`),
  - extraction ciblee par `article_id`,
  - blocage des `article_label` ambigus par defaut.
- KPI:
  - pas de nouveaux doublons non voulus,
  - extraction ciblee deterministe.

### Phase 4 - Validation Go/No-Go
- livrables: tests A1 executes + rapport de readiness.
- actions:
  - execution des tests unitaires A1/LLM infra,
  - diagnostic DB post-correction.
- Gate GO:
  - tests verts,
  - tracabilite complete,
  - pipeline A1 stable.

## 4) Criteres De Definition Of Done (DoD)

A1 est declare "fini (version PFE)" si:
1. `requirements.article_id` n'est jamais null.
2. L'ingestion ne cree plus de doublon silencieux.
3. Les runs A1 sont reproductibles et testables.
4. Les controles de coherence DB sont en place.
5. Les erreurs d'ambiguite sont explicites (pas silencieuses).

## 5) Risques Restants (Connus)

- disponibilite reseau LLM (provider externe),
- variabilite qualitative selon types de textes juridiques.

Mesure de mitigation:
- evaluer A1 sur plusieurs cas/metiers avant passage en production A2.
