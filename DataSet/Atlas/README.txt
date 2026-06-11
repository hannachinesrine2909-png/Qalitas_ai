Pack fictif de demonstration pour Atlas Revetement Industriel

Tenant recommande : atlas_revetement_demo

Ce pack est volontairement plus riche que Nova :
- contexte entreprise complet pour A2
- corpus PDF juridique synthetique pour A1
- preuves, audits et non-conformites PDF pour A3
- un cas futur pour tester APPLICABLE_FUTUR

Ordre conseille dans la plateforme :
1. Onboarding nouvelle entreprise avec les infos de company_profile.csv
2. Import CSV : sites, processes, activities, products, chemicals
3. Import CSV : equipment, environmental_aspects, sst_risks, sst_significant_risks, strategic_objectives
4. Import CSV : audit_reports_metadata.csv, nonconformities.csv, compliance_evidence_manifest.csv
5. Upload A1 : legal/ATLAS_Recueil_reglementaire_traitement_surface_2026.pdf
6. Valider les exigences dans la page Exigences
7. Uploader les preuves A3 : preuves/*.pdf, audits/*.pdf, nonconformites/*.pdf
8. Lancer Applicabilite puis Conformite

Attendu en demonstration :
- conformites visibles : registre depot solvants, bordereaux dechets, extincteurs, plan evacuation, controle compresseur, etiquetage
- ecarts visibles : visites medicales annuelles, registre incidents fuite, stockage exterieur sans retention
- cas de preuve expiree : formation solvants 2024
- cas futur : detection continue des COV a partir du 01/01/2027
