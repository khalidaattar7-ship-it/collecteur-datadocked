-- Polygones RADE / QUAI / PORT dérivés des positions collectées (python -m app.collecteur
-- polygones calibrer). Versionnés : chaque calibration crée une version ; une seule est
-- active (celle qu'utilise la classification). GeoJSON Polygon, coordonnées [lon, lat].
-- Idempotent. Les tests appliquent le corps (tout ce qui suit le marqueur « @corps »).

create schema if not exists collecteur;
set search_path to collecteur, public;

-- @corps
create table if not exists polygones (
  id bigint generated always as identity primary key,
  version int not null,
  type text not null check (type in ('rade', 'quai', 'port')),
  nom text,
  geojson jsonb not null,
  n_points int, n_navires int, aire_km2 numeric,
  params jsonb,
  actif boolean not null default false,
  created_at timestamptz not null default now()
);
create index if not exists idx_polygones_actif on polygones (actif, type);
create index if not exists idx_polygones_version on polygones (version);
