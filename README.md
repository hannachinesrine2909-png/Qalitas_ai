# QALITAS AI - Plateforme PFE de Conformite Reglementaire

## Perimetre officiel

Le perimetre officiel de cette version PFE est limite a `4 agents IA`:

1. `A1` Extraction et structuration des exigences a partir des documents juridiques
2. `A2` Applicabilite reglementaire selon le contexte de l'entreprise
3. `A3` Analyse de conformite operationnelle et gestion des preuves
4. `A4` Assistant conversationnel expert sur le contexte reglementaire et l'etat de conformite

Le cahier des charges initial a evolue pendant le projet. La version de reference a soutenir et a evaluer est bien la plateforme `A1 -> A4`.

## Architecture

- `Frontend`: pages HTML/JS statiques servies par FastAPI sous `/ui`
- `Backend`: API FastAPI dans `qalitas_api_fastapi.py`
- `Base de donnees`: PostgreSQL avec schema initialise par `qalitas_db_setup.py`
- `Moteurs agents`:
  - `a2_applicability_engine.py`
  - `a3_compliance_engine.py`
  - `a4_chat_engine.py`
- `Client LLM commun`: `llm_client.py` pour A2, A3 et A4
- `Extraction LLM A1`: `a1_llm_extractor.py`, couche metier specialisee pour l'extraction reglementaire
- `Ingestion contexte entreprise`: `bulk_company_import.py` via les templates frontend.

## Demarrage local

1. Configurer `.env` a partir de `.env.example`.
2. Initialiser ou mettre a jour le schema PostgreSQL:
   `python qalitas_db_setup.py`
3. Lancer l'API:
   `uvicorn qalitas_api_fastapi:app --reload`
4. Ouvrir le frontend:
   `http://localhost:8000/ui/`

Les moteurs agents peuvent aussi etre lances en CLI pour maintenance ou reprise:

- `python a2_applicability_engine.py --tenant <tenant_id>`
- `python a3_compliance_engine.py --tenant <tenant_id>`
- `python a4_chat_engine.py --tenant <tenant_id> --index`

## Flux metier

1. `A1` extrait et structure les exigences reglementaires depuis les documents sources.
2. `A2` decide l'applicabilite des exigences selon le contexte du tenant.
3. `A3` evalue la conformite a partir des exigences applicables et des preuves disponibles.
4. `A4` repond aux questions metier en s'appuyant sur la reglementation, le contexte entreprise et les resultats du pipeline.

## Fichiers coeur

- `qalitas_api_fastapi.py`: API FastAPI, authentification, routes frontend, routes A1/A2/A3/A4.
- `tenant_db.py`: connexion PostgreSQL partagee et resolution des tenants.
- `qalitas_db_setup.py`: creation/migration du schema PostgreSQL.
- `llm_client.py`: client LLM commun pour A2, A3 et A4.
- `a1_schemas.py`: contrats Pydantic de sortie LLM pour l'extraction A1.
- `a1_prompt_contract.py` et `a1_prompts.py`: contrats et prompts d'extraction reglementaire.

Agent 1:

- `a1_ingest_extract_registry.py`: orchestration CLI du pipeline A1.
- `a1_ingest_pdf_min.py`: ingestion PDF, OCR si necessaire, stockage document/pages.
- `a1_segment_articles_chunks.py`: segmentation articles/chunks.
- `a1_precall_nlp.py`: preparation NLP avant appel LLM.
- `a1_llm_extractor.py`: couche metier d'extraction LLM A1.
- `a1_extract_requirements_llm.py`: extraction et promotion des exigences.
- `a1_postcall_quality.py`: controles qualite apres appel LLM.
- `a1_document_qualification_gate.py`: qualification documentaire avant extraction.
- `a1_backfill_qse_fields.py`: enrichissement QSE des exigences (domaine et sous domaine).
- `a1_export_registry.py`: export registre reglementaire.
- `a1_shared_helpers.py`: fonctions communes A1.
- `a1_error_memory.py`: memoire des erreurs et exemples de correction.
- `a1_limited_data_parser.py`: parsing robuste de sorties partielles/limitees.

Agents 2, 3 et 4:

- `a2_applicability_engine.py`: moteur d'applicabilite.
- `a2_scope_resolution.py`: resolution des scopes applicables.
- `a3_compliance_engine.py`: moteur de conformite, ecarts et actions correctives.
- `a4_chat_engine.py`: assistant expert RAG et indexation embeddings.

Import entreprise et frontend:

- `bulk_company_import.py`: import multi-tenant nominal depuis les templates frontend.
- `frontend/`: interface HTML/CSS/JavaScript servie par FastAPI.

## Scripts de maintenance

Ces fichiers ne sont pas le chemin nominal de la plateforme, mais restent utiles pour exploitation, reprise ou audit technique:

- `a1_benchmark_real_extraction.py`: benchmark qualite de l'extraction A1.
- `tools/maintenance/a1_batch_ingest_jort_dir.py`: ingestion batch de PDFs reglementaires.
- `tools/maintenance/a1_repair_data_integrity.py`: reparation de donnees A1 en base.
- `tools/maintenance/a1_reingest_pages_inplace.py`: reingestion des pages d'un document.
- `tools/maintenance/a1_recalibrate_thresholds.py`: recalibration des seuils de promotion A1.
- `tools/maintenance/a1_error_memory_replay.py`: replay de la memoire d'erreurs A1.
- `run_context_freeze.py`: capture de contexte technique d'un run.

## Statut du depot

Ce depot contient la version soutenance de la plateforme QALITAS AI autour du perimetre officiel `4 agents`.
Les anciens scripts d'evaluation et rapports generes lourds ont ete retires ou archives pour garder un depot lisible.
