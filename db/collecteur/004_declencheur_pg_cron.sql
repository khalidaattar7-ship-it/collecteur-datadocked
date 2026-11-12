-- Déclencheur de secours : Supabase lance la passe du collecteur toutes les 5 min, sans
-- dépendre du cron natif de GitHub (dont les déclenchements planifiés peuvent ne jamais partir).
--
-- pg_cron (toutes les 5 min, minutes 3, 8, 13…) -> collecteur.declencher_passe() -> pg_net
-- -> POST https://api.github.com/repos/<dépôt>/actions/workflows/collecteur.yml/dispatches
-- Le jeton GitHub (fine-grained, ce dépôt seulement, permission « Actions : Read and write »)
-- est lu dans Supabase Vault (secret « github_dispatch_pat ») : il n'apparaît ni ici, ni
-- dans cron.job, ni dans les journaux.
--
-- Le cron natif de GitHub reste en place : la concurrence du workflow (une passe à la fois)
-- et le verrou consultatif PostgreSQL rendent un double déclenchement sans effet.
--
-- Couper : select cron.unschedule('collecteur_declencheur');
-- Suspendre : update cron.job set active = false where jobname = 'collecteur_declencheur';
-- Réactiver : active = true, ou rejouer ce fichier (cron.schedule met à jour le job existant).
-- Jeton : vault.create_secret(<jeton>, 'github_dispatch_pat') ou vault.update_secret(id, <jeton>).
-- Idempotent.

create extension if not exists pg_net;

create or replace function collecteur.declencher_passe() returns bigint
language plpgsql
security definer
set search_path = collecteur, public
as $fn$
declare
  jeton text;
begin
  select decrypted_secret into jeton from vault.decrypted_secrets where name = 'github_dispatch_pat' limit 1;
  if jeton is null then
    raise notice 'collecteur.declencher_passe : secret github_dispatch_pat absent de Vault, aucun appel';
    return null;
  end if;
  return net.http_post(
    url := 'https://api.github.com/repos/khalidaattar7-ship-it/collecteur-datadocked/actions/workflows/collecteur.yml/dispatches',
    body := jsonb_build_object('ref', 'main'),
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || jeton,
      'Accept', 'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent', 'collecteur-datadocked-pg-cron'),
    timeout_milliseconds := 10000);
end;
$fn$;

-- Seul le propriétaire (postgres) exécute la fonction : pas d'accès par les rôles de l'API.
revoke all on function collecteur.declencher_passe() from public;

select cron.schedule('collecteur_declencheur', '3-58/5 * * * *', 'select collecteur.declencher_passe()');
