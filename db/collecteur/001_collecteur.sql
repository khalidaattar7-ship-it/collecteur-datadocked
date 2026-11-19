-- Collecteur temps réel Data Docked — schéma `collecteur`, PostgreSQL NU (Supabase).
--
-- Pas de TimescaleDB : `positions` est une table simple (PK (mmsi, position_received) + index
-- descendant) ; la rétention de 120 jours est un job pg_cron (002_retention_pg_cron.sql).
-- Schéma dédié, et NON `public` : sur Supabase, `public` est exposé par l'API de données ;
-- `collecteur` ne l'est pas.
--
-- Idempotent (rejouable). Application :  python -m app.collecteur ddl
-- Les tests appliquent le CORPS (tout ce qui suit le marqueur « @corps » ci-dessous) dans un
-- schéma jetable.

create schema if not exists collecteur;
set search_path to collecteur, public;

-- @corps
create table if not exists vessels (
  mmsi text primary key, imo text, name text,
  ship_type_anp text, categorie text,         -- LINER|VRAC|SPECIAL|UNKNOWN (VESSEL_TYPE_MAPPING)
  gt numeric, length numeric, beam numeric, draught numeric,
  country text, callsign text, type_specific text,
  updated_at timestamptz not null default now()
);

create table if not exists escales (
  escale_id bigint generated always as identity primary key,
  imo text, mmsi text, arrivee_utc timestamptz, expected_eta timestamptz,
  statut text not null default 'approaching', -- approaching|anchored|berthed|departed|abandoned|hors_perimetre
  port_entry_at timestamptz, anchorage_entry_at timestamptz, anchorage_exit_at timestamptz,
  berth_entry_at timestamptz, berth_exit_at timestamptz, departure_at timestamptz,
  premiere_obs timestamptz, derniere_obs timestamptz, created_at timestamptz not null default now()
);
create index if not exists idx_escales_mmsi on escales (mmsi);
create index if not exists idx_escales_statut on escales (statut);
-- Entrée non observée : navire déjà au mouillage ou déjà à quai à son 1er relevé (NULL = pas encore relevé).
-- Ses durées (mouillage, attente) sont alors tronquées à gauche.
alter table escales add column if not exists entree_censuree boolean;

-- Suivi d'une escale par le scheduler sans état local (cron) : phase, échéance, rafale.
-- `etat` porte les compteurs de la machine à états (relevé précédent, confirmations…).
create table if not exists suivi (
  escale_id bigint primary key references escales(escale_id) on delete cascade,
  phase text check (phase in ('approach_far', 'approach_final', 'in_port_stable', 'in_port_burst', 'departing')),
  next_due_at timestamptz,
  last_poll_at timestamptz,
  burst_until timestamptz,
  cadence_min int,
  etat jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);
create index if not exists idx_suivi_due on suivi (next_due_at);

-- Horloges des boucles périodiques (expected, arrival, découverte, congestion).
create table if not exists horloges (
  nom text primary key, valeur text not null, updated_at timestamptz not null default now()
);

create table if not exists positions (
  mmsi text not null,
  position_received timestamptz not null,
  escale_id bigint references escales(escale_id) on delete set null,
  imo text,
  lat double precision, lon double precision, sog double precision, cog double precision, heading double precision,
  nav_status text, distance_to_port_km double precision, bearing_to_port double precision,
  destination text, unlocode_destination text, call_time timestamptz, data_source text,
  created_at timestamptz not null default now(),
  primary key (mmsi, position_received)
);
create index if not exists idx_positions_mmsi_recu on positions (mmsi, position_received desc);
create index if not exists idx_positions_escale on positions (escale_id);
create index if not exists idx_positions_recu on positions (position_received);   -- rétention

create table if not exists meteo_positions (            -- Phase 3
  mmsi text not null, position_received timestamptz not null,
  source text, data jsonb, created_at timestamptz not null default now(),
  primary key (mmsi, position_received),
  foreign key (mmsi, position_received) references positions (mmsi, position_received) on delete cascade
);

create table if not exists evenements (
  id bigint generated always as identity primary key,
  escale_id bigint references escales(escale_id) on delete cascade,
  type text not null, timestamp_utc timestamptz not null, confiance text, signaux jsonb,
  created_at timestamptz not null default now()
);
create index if not exists idx_evenements_escale on evenements (escale_id);

create table if not exists congestion_horaire (
  hour_slot_casa timestamptz primary key, captured_at timestamptz not null,
  rade int, rade_liner int, a_quai int, n_total int, qualite text default 'ok', derivation_version text
);

create table if not exists api_raw (                    -- archive INTÉGRALE, PAS de rétention
  id bigint generated always as identity primary key, captured_at timestamptz not null default now(),
  endpoint text not null, params jsonb, http_status int,
  credits_depenses int, corps jsonb, statut_appel text
);
create index if not exists idx_api_raw_endpoint_time on api_raw (endpoint, captured_at desc);

-- Journal des crédits = SOURCE DE VÉRITÉ du garde-fou (plus de fichier local en cron).
-- Une ligne par appel : insérée « lance » AVANT l'appel (commit immédiat), complétée après.
-- Une ligne restée « lance » (processus tué pendant l'appel) compte au coût max.
create table if not exists credits_journal (
  id bigint generated always as identity primary key, ts timestamptz not null default now(),
  session text, endpoint text, statut text, emis boolean, solde_avant int, solde_apres int, depense int,
  appel_id text unique, cout_max int, params jsonb, http int, erreur text, termine_le timestamptz
);
create index if not exists idx_credits_session on credits_journal (session, ts);
create index if not exists idx_credits_ts on credits_journal (ts);

create table if not exists collecte_sessions (
  session text primary key, started_at timestamptz not null default now(), ended_at timestamptz, statut text, notes text
);

create or replace view etat_courant as
select distinct on (p.mmsi)
  p.mmsi, p.imo, v.name, v.categorie, p.lat, p.lon, p.sog, p.cog, p.nav_status,
  p.distance_to_port_km, p.position_received, e.escale_id, e.statut, s.phase, s.next_due_at
from positions p
left join vessels v on v.mmsi = p.mmsi
left join escales e on e.escale_id = p.escale_id
left join suivi s on s.escale_id = e.escale_id
order by p.mmsi, p.position_received desc;
