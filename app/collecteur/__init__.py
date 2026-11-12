"""Collecteur temps réel Data Docked (remplace le flux AISStream mort et le scraper ANP).

Modules : config (constantes, secrets par l'environnement), types_navires (périmètre commerce),
garde (garde-fou budgétaire, journal, verrou), client (appels Data Docked classés), store
(PostgreSQL/TimescaleDB, schéma `collecteur`), boucles (roster, découverte, suivi),
scheduler (choix des appels à chaque tick), __main__ (CLI, mode ESTIMATION par défaut).
"""
