# Collecteur temps réel Data Docked — port de Casablanca

Collecte l'état temps réel des navires en approche et au port de Casablanca (LOCODE MACAS)
depuis l'API Data Docked, et l'écrit dans PostgreSQL (Supabase). Tourne en passes courtes,
sans serveur, déclenchées par GitHub Actions.

## Ce que fait une passe

`python -m app.collecteur tick --executer` :

1. prend un verrou consultatif PostgreSQL (une seule passe à la fois) ;
2. lit les listes dues : navires attendus (`expected`, quotidien), arrivées (`arrival`, tous
   les 3,5 jours), navires présents dans un rayon de 50 km (toutes les 5 h) ;
3. interroge la position des navires dont l'échéance est passée :
   - approche : porte-conteneurs, rouliers et passagers seulement, toutes les 20 min à moins
     de 150 km, toutes les 60 min au-delà ;
   - au port (tout navire de commerce) : 120 min s'il est immobile, rafale de 12 min dès qu'il
     bouge, jusqu'à l'accostage ou au départ ;
4. enregistre positions, escales, événements (mouillage, accostage, appareillage…) et la
   réponse brute de chaque appel, puis l'échéance suivante de chaque navire.

## Budget

Chaque appel est vérifié avant d'être émis contre le journal des crédits en base : plafond
par passe, par jour et par mois, sur la dépense réelle (solde avant − après). Les cadences
s'élargissent automatiquement si la projection mensuelle dépasse la cible. Plafonds réglables
par variables d'environnement (`COLLECTOR_PLAFOND_JOUR`, `COLLECTOR_PLAFOND_MOIS`,
`COLLECTOR_PLAFOND_TICK`, `COLLECTOR_CIBLE_MOIS`).

## Mise en place

1. Schéma : `SUPABASE_DB_DSN=... python -m app.collecteur ddl --retention` (crée le schéma
   `collecteur` et le job pg_cron de rétention des positions à 120 jours).
2. Secrets du dépôt : `DATADOCKED_API_KEY`, `SUPABASE_DB_DSN`.
3. Lancer une passe à la main (onglet Actions, « Run workflow ») ; le workflow `collecteur`
   est planifié toutes les 5 min.
4. `keepalive` pousse chaque lundi un battement (`.github/heartbeat`) : sans activité pendant
   60 jours, GitHub désactive les workflows planifiés d'un dépôt public.

## Commandes

| Commande | Effet |
|---|---|
| `tick` | estimation d'une passe, sans appel ni écriture |
| `tick --executer [--max-appels K]` | une passe réelle |
| `estimer` | plan et projection de dépense par jour et par mois |
| `precision` | précision des heures d'accostage (médiane, p90) |
| `rejouer` | retraite les réponses archivées (contrôle d'idempotence) |
| `etat` | dépense, escales, positions, échéances |
