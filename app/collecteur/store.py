"""Accès PostgreSQL du collecteur (schéma `collecteur`, psycopg2, synchrone ; Supabase en prod).

Toutes les requêtes passent par le search_path de la connexion : le même code sert le schéma de
production et les schémas jetables des tests.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

from .tls import connecter

# db/collecteur/ à la racine du dépôt : parents[3] dans le projet de modélisation (api/app/…),
# parents[2] dans le dépôt autonome du collecteur (app/…).
DDL_DIR = next((r / "db" / "collecteur" for r in Path(__file__).resolve().parents[2:4]
                if (r / "db" / "collecteur" / "001_collecteur.sql").exists()),
               Path(__file__).resolve().parents[2] / "db" / "collecteur")
DDL = DDL_DIR / "001_collecteur.sql"
DDL_CORPS = ("001_collecteur.sql", "003_polygones.sql")      # dans l'ordre
DDL_RETENTION = DDL_DIR / "002_retention_pg_cron.sql"
STATUTS_OUVERTS = ("approaching", "anchored", "berthed")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class Store:
    def __init__(self, dsn: str, schema: str = "collecteur"):
        if not _IDENT.match(schema):
            raise ValueError(f"schéma invalide : {schema}")
        self.schema = schema
        self.cx = connecter(dsn)
        self.cx.autocommit = False
        with self.cx.cursor() as c:
            c.execute(f"set search_path to {schema}, public")
        self.cx.commit()

    # ------------------------------------------------------------------ DDL
    def appliquer_ddl(self, retention: bool = False) -> None:
        """Applique les DDL (idempotents) dans le schéma de la connexion (le CORPS de chaque
        fichier, après le marqueur) ; avec `retention`, aussi le job pg_cron (002)."""
        with self.cx.cursor() as c:
            c.execute(f"create schema if not exists {self.schema}")
            c.execute(f"set search_path to {self.schema}, public")
            for nom in DDL_CORPS:
                c.execute((DDL_DIR / nom).read_text(encoding="utf-8").split("-- @corps", 1)[1])
            if retention:
                c.execute(DDL_RETENTION.read_text(encoding="utf-8"))
                c.execute(f"set search_path to {self.schema}, public")
        self.cx.commit()

    def supprimer_schema(self) -> None:          # tests seulement
        assert self.schema != "collecteur", "refus de supprimer le schéma de production"
        self.cx.rollback()
        with self.cx.cursor() as c:
            c.execute(f"drop schema if exists {self.schema} cascade")
        self.cx.commit()

    def commit(self) -> None:
        self.cx.commit()

    def rollback(self) -> None:
        self.cx.rollback()

    def fermer(self) -> None:
        self.cx.close()

    def _q(self, sql: str, params=None, un: bool = False, tous: bool = False):
        with self.cx.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute(sql, params)
            if un:
                return c.fetchone()
            if tous:
                return c.fetchall()
            return c.rowcount

    # ------------------------------------------------------------------ archive / crédits
    def api_raw(self, endpoint, params, http, credits, corps, statut) -> int:
        r = self._q("insert into api_raw (endpoint, params, http_status, credits_depenses, corps, statut_appel) "
                    "values (%s, %s, %s, %s, %s, %s) returning id",
                    (endpoint, json.dumps(params), http, credits,
                     json.dumps(corps, ensure_ascii=False) if corps is not None else None, statut), un=True)
        return r["id"]

    def api_raw_credits(self, api_raw_id: Optional[int], credits: Optional[int]) -> None:
        if api_raw_id is not None:
            self._q("update api_raw set credits_depenses = %s where id = %s", (credits, api_raw_id))

    # ------------------------------------------------------------------ suivi / horloges (cron)
    def suivi(self, escale_ids: list[int]) -> dict[int, dict]:
        rows = self._q("select * from suivi where escale_id = any(%s)", (list(escale_ids),), tous=True)
        return {r["escale_id"]: r for r in rows}

    def ecrire_suivi(self, escale_id: int, phase, next_due_at, last_poll_at, burst_until, cadence_min,
                     etat: dict) -> None:
        self._q("""
            insert into suivi (escale_id, phase, next_due_at, last_poll_at, burst_until, cadence_min, etat, updated_at)
            values (%s, %s, %s, %s, %s, %s, %s, now())
            on conflict (escale_id) do update set phase = excluded.phase, next_due_at = excluded.next_due_at,
              last_poll_at = excluded.last_poll_at, burst_until = excluded.burst_until,
              cadence_min = excluded.cadence_min, etat = excluded.etat, updated_at = now()
        """, (escale_id, phase, next_due_at, last_poll_at, burst_until, cadence_min,
              json.dumps(etat, ensure_ascii=False, default=str)))

    def horloges(self) -> dict[str, str]:
        return {r["nom"]: r["valeur"] for r in self._q("select nom, valeur from horloges", tous=True)}

    def ecrire_horloge(self, nom: str, valeur: str) -> None:
        self._q("insert into horloges (nom, valeur) values (%s, %s) on conflict (nom) do update "
                "set valeur = excluded.valeur, updated_at = now()", (nom, valeur))

    def session_debut(self, session: str, notes: str) -> None:
        # Le verrou garantit une seule instance : une session encore « en_cours » appartient à un
        # processus mort (arrêt brutal). Marquée, jamais effacée.
        self._q("update collecte_sessions set statut = 'interrompue', notes = coalesce(notes,'') || "
                "' | processus arrêté sans clôture (constaté au démarrage de ' || %s || ')' "
                "where statut = 'en_cours' and session <> %s", (session, session))
        self._q("insert into collecte_sessions (session, statut, notes) values (%s, 'en_cours', %s) "
                "on conflict (session) do update set statut = 'en_cours'", (session, notes))
        self.commit()

    def session_fin(self, session: str, statut: str, notes: str) -> None:
        self._q("update collecte_sessions set ended_at = now(), statut = %s, notes = coalesce(notes,'') || %s "
                "where session = %s", (statut, " | " + notes, session))
        self.commit()

    def api_raw_a_rejouer(self, endpoints: tuple) -> list[dict]:
        return self._q("select id, captured_at, endpoint, params, corps from api_raw "
                       "where statut_appel in ('recue', 'importe') and endpoint = any(%s) order by captured_at, id",
                       (list(endpoints),), tous=True)

    # ------------------------------------------------------------------ navires
    def upsert_vessel(self, v: dict) -> None:
        """Les champs None ne remplacent jamais une valeur connue (coalesce)."""
        self._q("""
            insert into vessels (mmsi, imo, name, ship_type_anp, categorie, gt, length, beam, draught,
                                 country, callsign, type_specific, updated_at)
            values (%(mmsi)s, %(imo)s, %(name)s, %(ship_type_anp)s, %(categorie)s, %(gt)s, %(length)s, %(beam)s,
                    %(draught)s, %(country)s, %(callsign)s, %(type_specific)s, now())
            on conflict (mmsi) do update set
              imo = coalesce(excluded.imo, vessels.imo), name = coalesce(excluded.name, vessels.name),
              ship_type_anp = coalesce(excluded.ship_type_anp, vessels.ship_type_anp),
              categorie = case when excluded.categorie is null or excluded.categorie = 'UNKNOWN'
                               then coalesce(vessels.categorie, excluded.categorie) else excluded.categorie end,
              gt = coalesce(excluded.gt, vessels.gt), length = coalesce(excluded.length, vessels.length),
              beam = coalesce(excluded.beam, vessels.beam), draught = coalesce(excluded.draught, vessels.draught),
              country = coalesce(excluded.country, vessels.country),
              callsign = coalesce(excluded.callsign, vessels.callsign),
              type_specific = coalesce(excluded.type_specific, vessels.type_specific), updated_at = now()
        """, {k: v.get(k) for k in ("mmsi", "imo", "name", "ship_type_anp", "categorie", "gt", "length", "beam",
                                    "draught", "country", "callsign", "type_specific")})

    def vessel(self, mmsi: str) -> Optional[dict]:
        return self._q("select * from vessels where mmsi = %s", (mmsi,), un=True)

    # ------------------------------------------------------------------ escales
    def escale_ouverte(self, mmsi: str) -> Optional[dict]:
        return self._q("select * from escales where mmsi = %s and statut = any(%s) order by escale_id desc limit 1",
                       (mmsi, list(STATUTS_OUVERTS)), un=True)

    def derniere_escale(self, mmsi: str) -> Optional[dict]:
        """Dernière escale du navire, ouverte ou close."""
        return self._q("select * from escales where mmsi = %s order by escale_id desc limit 1", (mmsi,), un=True)

    def creer_escale(self, mmsi: str, imo: Optional[str], statut: str = "approaching",
                     expected_eta=None, arrivee_utc=None) -> dict:
        return self._q("insert into escales (mmsi, imo, statut, expected_eta, arrivee_utc) values (%s,%s,%s,%s,%s) "
                       "returning *", (mmsi, imo, statut, expected_eta, arrivee_utc), un=True)

    def maj_escale(self, escale_id: int, **champs) -> None:
        if not champs:
            return
        cols = ", ".join(f"{k} = %({k})s" for k in champs)
        self._q(f"update escales set {cols} where escale_id = %(escale_id)s", {**champs, "escale_id": escale_id})

    def depense_diagnostic(self, groupe: str) -> int:
        """Crédits débités (journal) pour les navires d'un groupe de diagnostic depuis son début."""
        r = self._q("""
            with g as (select e.mmsi, (s.etat->>'diag_debut')::timestamptz debut
                       from suivi s join escales e using (escale_id) where s.etat->>'diag_groupe' = %s)
            select coalesce(sum(case when j.statut = 'lance' then j.cout_max when not j.emis then 0
                                     when j.depense is null then j.cout_max
                                     when j.depense > j.cout_max or j.depense < 0 then j.cout_max
                                     else j.depense end), 0) as d
            from credits_journal j
            where j.params->>'imo_or_mmsi' in (select mmsi from g) and j.ts >= (select min(debut) from g)""",
                    (groupe,), un=True)
        return int(r["d"])

    def escales_ouvertes(self) -> list[dict]:
        """Escales suivies, avec catégorie et dernière position connue."""
        return self._q("""
            select e.*, v.categorie, v.name, s.next_due_at, s.phase as phase_suivi,
                   p.position_received as dern_pos, p.distance_to_port_km as dern_dist, p.nav_status as dern_ns,
                   p.sog as dern_sog,
                   (select min(distance_to_port_km) from positions q where q.escale_id = e.escale_id) as min_dist
            from escales e
            left join vessels v on v.mmsi = e.mmsi
            left join suivi s on s.escale_id = e.escale_id
            left join lateral (select position_received, distance_to_port_km, nav_status, sog from positions
                               where mmsi = e.mmsi order by position_received desc limit 1) p on true
            where e.statut = any(%s) order by e.escale_id
        """, (list(STATUTS_OUVERTS),), tous=True)

    # ------------------------------------------------------------------ événements
    def evenement(self, escale_id: int, type_: str, ts, confiance: str, signaux: dict) -> bool:
        """Idempotent sur (escale, type) : un seul événement de chaque type par escale (le
        premier constaté). Retourne True s'il est nouveau."""
        if self._q("select 1 from evenements where escale_id = %s and type = %s", (escale_id, type_), un=True):
            return False
        self._q("insert into evenements (escale_id, type, timestamp_utc, confiance, signaux) values (%s,%s,%s,%s,%s)",
                (escale_id, type_, ts, confiance, json.dumps(signaux, ensure_ascii=False, default=str)))
        return True

    def evenement_supprimer(self, escale_id: int, type_: str) -> None:
        self._q("delete from evenements where escale_id = %s and type = %s", (escale_id, type_))

    def evenement_remplacer(self, escale_id: int, type_: str, ts, confiance: str, signaux: dict) -> None:
        """Pour un événement qui peut être CORRIGÉ (arrivee : nouvelle entrée de la chaîne)."""
        self._q("delete from evenements where escale_id = %s and type = %s", (escale_id, type_))
        self._q("insert into evenements (escale_id, type, timestamp_utc, confiance, signaux) values (%s,%s,%s,%s,%s)",
                (escale_id, type_, ts, confiance, json.dumps(signaux, ensure_ascii=False, default=str)))

    def precision_accostages(self) -> list[dict]:
        return self._q("""
            select ev.escale_id, v.name, v.categorie, ev.timestamp_utc, ev.signaux
            from evenements ev join escales e using (escale_id) left join vessels v on v.mmsi = e.mmsi
            where ev.type = 'accostage' order by ev.timestamp_utc
        """, tous=True)

    # ------------------------------------------------------------------ positions
    def inserer_position(self, p: dict) -> bool:
        """True si nouvelle ; False si (mmsi, position_received) déjà connue (dédoublonnage)."""
        n = self._q("""
            insert into positions (mmsi, position_received, escale_id, imo, lat, lon, sog, cog, heading, nav_status,
                                   distance_to_port_km, bearing_to_port, destination, unlocode_destination,
                                   call_time, data_source)
            values (%(mmsi)s, %(position_received)s, %(escale_id)s, %(imo)s, %(lat)s, %(lon)s, %(sog)s, %(cog)s,
                    %(heading)s, %(nav_status)s, %(distance_to_port_km)s, %(bearing_to_port)s, %(destination)s,
                    %(unlocode_destination)s, %(call_time)s, %(data_source)s)
            on conflict (mmsi, position_received) do nothing
        """, p)
        return n == 1

    def dernieres_positions(self, mmsi: str, n: int) -> list[dict]:
        return self._q("select position_received, distance_to_port_km, sog, nav_status from positions "
                       "where mmsi = %s order by position_received desc limit %s", (mmsi, n), tous=True)

    # ------------------------------------------------------------------ congestion
    def congestion_horaire(self, slot: datetime, qualite: str, version: str) -> dict:
        r = self._q("""
            select count(*) filter (where e.statut = 'anchored')                            as rade,
                   count(*) filter (where e.statut = 'anchored' and v.categorie = 'LINER')  as rade_liner,
                   count(*) filter (where e.statut = 'berthed')                             as a_quai,
                   count(*)                                                                 as n_total
            from escales e left join vessels v on v.mmsi = e.mmsi
            where e.statut = any(%s)
        """, (list(STATUTS_OUVERTS),), un=True)
        self._q("""
            insert into congestion_horaire (hour_slot_casa, captured_at, rade, rade_liner, a_quai, n_total, qualite,
                                            derivation_version)
            values (%s, now(), %s, %s, %s, %s, %s, %s)
            on conflict (hour_slot_casa) do update set captured_at = excluded.captured_at, rade = excluded.rade,
              rade_liner = excluded.rade_liner, a_quai = excluded.a_quai, n_total = excluded.n_total,
              qualite = excluded.qualite, derivation_version = excluded.derivation_version
        """, (slot, r["rade"], r["rade_liner"], r["a_quai"], r["n_total"], qualite, version))
        return dict(r)

    # ------------------------------------------------------------------ polygones
    def polygones_actifs(self) -> list[dict]:
        return self._q("select * from polygones where actif order by type, id", tous=True)

    def prochaine_version_polygones(self) -> int:
        return self._q("select coalesce(max(version), 0) + 1 as v from polygones", un=True)["v"]

    def inserer_polygone(self, version: int, type_: str, nom: str, gj: dict, n_points, n_navires, aire,
                         params: dict) -> None:
        self._q("insert into polygones (version, type, nom, geojson, n_points, n_navires, aire_km2, params) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (version, type_, nom, json.dumps(gj), n_points, n_navires, aire, json.dumps(params)))

    def activer_polygones(self, version: int) -> int:
        self._q("update polygones set actif = false where actif")
        return self._q("update polygones set actif = true where version = %s", (version,))

    def polygones_version(self, version: int) -> list[dict]:
        return self._q("select * from polygones where version = %s order by type, id", (version,), tous=True)

    def positions_calibration(self, rayon_km: float = 60.0) -> list[dict]:
        return self._q("""
            select p.mmsi, v.name, p.lat, p.lon, p.sog, p.nav_status, p.distance_to_port_km, p.position_received,
                   p.destination, p.unlocode_destination
            from positions p left join vessels v using (mmsi)
            where p.distance_to_port_km <= %s and p.lat is not null and p.lon is not null
        """, (rayon_km,), tous=True)

    # ------------------------------------------------------------------ congestion (parité ANP)
    def intervalles_congestion(self) -> list[dict]:
        return self._q("""
            select e.escale_id, v.categorie, e.anchorage_entry_at, e.berth_entry_at, e.berth_exit_at,
                   e.departure_at, e.statut
            from escales e left join vessels v using (mmsi)
            where e.anchorage_entry_at is not null or e.berth_entry_at is not null
        """, tous=True)

    def ecrire_congestion(self, slot, rade, rade_liner, a_quai, n_total, qualite, version) -> None:
        self._q("""
            insert into congestion_horaire (hour_slot_casa, captured_at, rade, rade_liner, a_quai, n_total, qualite,
                                            derivation_version)
            values (%s, now(), %s, %s, %s, %s, %s, %s)
            on conflict (hour_slot_casa) do update set captured_at = excluded.captured_at, rade = excluded.rade,
              rade_liner = excluded.rade_liner, a_quai = excluded.a_quai, n_total = excluded.n_total,
              qualite = excluded.qualite, derivation_version = excluded.derivation_version
        """, (slot, rade, rade_liner, a_quai, n_total, qualite, version))

    def compter(self, table: str) -> int:
        assert _IDENT.match(table)
        return self._q(f"select count(*) as n from {table}", un=True)["n"]


def utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
