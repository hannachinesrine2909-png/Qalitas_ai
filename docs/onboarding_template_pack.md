# Pack de templates d'onboarding

Ce pack a ete derive des vraies donnees `GDS/Korba` presentes dans `DataSet/` et aligne sur le perimetre officiel des `4 agents`.

## Lecture agent par agent

- `A1`
  Les entrees sont les `PDF juridiques` du tenant.
  La sortie est un corpus d'`exigences` structurees.

- `A2`
  Les entrees sont les `exigences A1` plus le `contexte entreprise`.
  La sortie est un ensemble de `decisions d'applicabilite` par tenant et par scope.

- `A3`
  Les entrees sont les `decisions A2` plus les `preuves`, `audits`, `non-conformites` et autres metadonnees metier.
  La sortie est l'`etat de conformite`, les `gaps` et les `actions`.

- `A4`
  L'entree est l'ensemble `A1 + A2 + A3` pour alimenter le RAG et le chat expert.

## Fichiers sources observes dans `DataSet/`

- `site.xlsx`
- `processus et activites.xls`
- `Liste equipements.xlsx`
- `Liste produits.xlsx`
- `Aspects envir.xlsx`
- `Risques SST.xlsx`
- `risques SST significatifs.xlsx`
- `axe strategiques , objectifs et KPIs QHSE.xlsx`
- `Non conformites.xlsx`
- `ReportAuditFiche01 (*.pdf)`
- `Liste exigences reglementaires.pdf`
- `Liste clients.xlsx`

## Mapping recommande

| Source GDS/Korba | Type | Template cible | Usage |
|---|---|---|---|
| `site.xlsx` | structure | `company_profile.csv`, `sites.csv` | onboarding + A2 |
| `processus et activites.xls` | structure | `processes.csv`, `activities.csv` | A2 |
| `Liste equipements.xlsx` | contexte | `equipment.csv` | A2/A3 |
| `Liste produits.xlsx` | contexte | `products.csv`, `chemicals.csv` | A2 |
| `Aspects envir.xlsx` | risque/env | `environmental_aspects.csv` | A2/A3 |
| `Risques SST.xlsx` | risque/sst | `sst_risks.csv` | A2/A3 |
| `risques SST significatifs.xlsx` | risque/sst | `sst_significant_risks.csv` | A2/A3 |
| `axe strategiques...xlsx` | pilotage | `strategic_objectives.csv` | A2/A4 |
| `Non conformites.xlsx` | conformite | `nonconformities.csv` | A3 |
| `ReportAuditFiche01 (*.pdf)` | preuve/audit | `audit_reports_metadata.csv`, `compliance_evidence_manifest.csv` + PDF | A3 |
| `Liste exigences reglementaires.pdf` | juridique | `legal_documents_manifest.csv` + PDF | A1 |
| `Liste clients.xlsx` | hors perimetre actuel | non standardise | futur |

## Templates fournis

Le pack est disponible dans `frontend/templates/onboarding/` :

- `company_profile.csv`
- `sites.csv`
- `processes.csv`
- `activities.csv`
- `products.csv`
- `chemicals.csv`
- `equipment.csv`
- `environmental_aspects.csv`
- `sst_risks.csv`
- `sst_significant_risks.csv`
- `strategic_objectives.csv`
- `nonconformities.csv`
- `audit_reports_metadata.csv`
- `compliance_evidence_manifest.csv`
- `legal_documents_manifest.csv`

## Conventions de saisie

- Dates: `YYYY-MM-DD`
- Date/heure: `YYYY-MM-DDTHH:MM:SSZ`
- Booleens: `true` ou `false`
- Multi-valeurs: separateur `|`
- Avant creation des UUID, les liaisons doivent se faire avec `site_code`, `process_code` et `activity_code`

## Remarque importante

Les templates sont prets pour le `bulk onboarding`, mais tous les imports ne sont pas encore branches automatiquement dans l'UI actuelle.
Le pack sert deja a standardiser les donnees et a preparer le futur import `CSV/XLSX` sans redefinition du modele.
