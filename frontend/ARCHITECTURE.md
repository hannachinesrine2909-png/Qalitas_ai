# Frontend Architecture - QALITAS PFE

## Perimetre officiel

Le frontend couvre le perimetre officiel du PFE sur `4 agents IA`:

1. `A1` Extraction des exigences
2. `A2` Applicabilite reglementaire
3. `A3` Conformite operationnelle
4. `A4` Assistant conversationnel expert

La version de reference du produit n'est pas un frontend "placeholder" pour ces agents: elle expose deja les parcours A1, A2, A3 et A4, avec un niveau de maturite different selon les modules.

## Stack

- Runtime: application web statique servie par FastAPI `StaticFiles` sous `/ui`
- UI layer: HTML + CSS + JavaScript modulaire
- API integration: endpoints exposes par `qalitas_api_fastapi.py`
- Session/auth: stockage client + jeton API

## Routes / Pages

- `/ui/` -> redirection vers `login.html`
- `/ui/login.html` -> entree d'authentification
- `/ui/dashboard.html` -> pilotage global du pipeline A1 -> A4
- `/ui/upload.html` -> ingestion PDF et orchestration du pipeline
- `/ui/documents.html` -> gestion documentaire
- `/ui/requirements.html` -> exigences extraites et validation A1
- `/ui/applicability.html` -> decisions d'applicabilite A2
- `/ui/compliance.html` -> verifications et preuves A3
- `/ui/assistant.html` -> assistant expert A4
- `/ui/company.html` -> contexte entreprise
- `/ui/analytics.html` -> indicateurs pipeline et qualite
- `/ui/reports.html` -> catalogue de rapports
- `/ui/settings.html` -> configuration et administration
- `/ui/runs.html` -> historique et observabilite

## Design system reutilisable

- Shared CSS: `frontend/assets/base.css`, `frontend/assets/layout.css`, `frontend/assets/components.css`
- Shared JS helpers: `frontend/assets/app.js`
- Patterns principaux:
  - KPI cards
  - badges de statut
  - tables de donnees
  - formulaires d'administration
  - visualisation du pipeline
  - panneaux de preuves et de rapports

## Contrats API principaux

- Auth:
  - `POST /api/v1/auth/login`
  - `GET /api/v1/auth/me`
- Dashboard / systeme:
  - `GET /api/v1/dashboard/overview`
  - `GET /api/v1/system/status`
- Documents / exigences:
  - `GET /api/v1/documents`
  - `GET /api/v1/documents/{doc_id}/detail`
  - `GET /api/v1/documents/{doc_id}/summary`
  - `GET /api/v1/requirements`
- Entreprise / applicabilite / conformite / assistant:
  - endpoints entreprise exposes par `qalitas_api_fastapi.py`
  - endpoints A2 exposes par `qalitas_api_fastapi.py`
  - endpoints A3 exposes par `qalitas_api_fastapi.py`
  - endpoints A4 exposes par `qalitas_api_fastapi.py`
- Runs / rapports / analytics:
  - `POST /api/v1/runs`
  - `GET /api/v1/runs`
  - `GET /api/v1/runs/{job_id}/details`
  - `GET /api/v1/reports`
  - `GET /api/v1/analytics/overview`
  - `GET /api/v1/analytics/families`
  - `GET /api/v1/analytics/gate`

## Note d'evolution

Une migration vers React/Next reste possible a terme, mais le modele fonctionnel de reference a conserver est celui du produit `A1 -> A4` actuellement livre dans ce dossier `frontend/`.
