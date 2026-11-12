-- Rétention des positions : 120 jours glissants, job pg_cron quotidien (03:17 UTC).
-- api_raw (archive intégrale) n'a PAS de rétention.
-- Idempotent : cron.schedule met à jour un job existant du même nom.
create extension if not exists pg_cron;

select cron.schedule(
  'collecteur_retention_positions',
  '17 3 * * *',
  $$delete from collecteur.positions where position_received < now() - interval '120 days'$$
);
