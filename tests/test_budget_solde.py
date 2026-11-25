"""Sécurité de solde et fin du flux B (0 crédit, aucune base, aucune API)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.collecteur import config as cf
from app.collecteur.config import NIVEAUX, Config
from app.collecteur.scheduler import (besoin_jusqu_au_renouvellement, eligible, flux_b_termine, mode_survie,
                                      plan, prochain_renouvellement, projection)
from app.collecteur.boucles import Etat

UTC = timezone.utc
T = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)
FIN_B = datetime(2026, 12, 2, tzinfo=UTC)


class JournalFaux:
    def totaux(self, t=None):
        return {"jour": 0, "mois": 0, "session": 0, "incertains": 0}


def test_prochain_renouvellement():
    assert prochain_renouvellement(T, 18) == datetime(2026, 12, 18, 23, 59, 59, tzinfo=UTC)
    assert prochain_renouvellement(datetime(2026, 12, 18, 10, tzinfo=UTC), 18) == datetime(2026, 12, 18, 23, 59, 59, tzinfo=UTC)
    assert prochain_renouvellement(datetime(2026, 12, 19, tzinfo=UTC), 18) == datetime(2027, 1, 18, 23, 59, 59, tzinfo=UTC)
    assert prochain_renouvellement(datetime(2027, 2, 1, tzinfo=UTC), 31) == datetime(2027, 2, 28, 23, 59, 59, tzinfo=UTC)


def test_besoin_avec_fin_du_flux_b():
    r = datetime(2026, 12, 18, 23, 59, 59, tzinfo=UTC)
    h_b = (FIN_B - T).total_seconds() / 3600
    h = (r - T).total_seconds() / 3600
    assert besoin_jusqu_au_renouvellement(1440, 700, T, r, FIN_B) == pytest.approx(60 * h_b + 700 / 24 * (h - h_b))
    assert besoin_jusqu_au_renouvellement(1440, 700, T, r, None) == pytest.approx(60 * h)          # pas de fin
    assert besoin_jusqu_au_renouvellement(1440, None, T, r, FIN_B) == pytest.approx(60 * h)        # sans plan LINER


def test_env_fin_flux_b(monkeypatch):
    monkeypatch.setenv("COLLECTOR_FIN_FLUX_B", "2026-12-02T00:00:00Z")
    assert Config().fin_flux_b == FIN_B
    for v in ("aucune", ""):
        monkeypatch.setenv("COLLECTOR_FIN_FLUX_B", v)
        assert Config().fin_flux_b is None
    monkeypatch.setenv("COLLECTOR_FIN_FLUX_B", "2026-12-02T00:00:00")
    with pytest.raises(ValueError):
        Config()


def test_eligibilite_apres_fin_du_flux_b():
    cfg = Config(fin_flux_b=FIN_B)
    vrac = {"categorie": "VRAC", "min_dist": 10.0}
    liner = {"categorie": "LINER"}
    avant, apres = FIN_B - timedelta(minutes=1), FIN_B
    assert eligible(cfg, vrac, {"en_zone": True}, avant) and not eligible(cfg, vrac, {"en_zone": True}, apres)
    assert eligible(cfg, liner, {}, apres)
    assert not flux_b_termine(Config(fin_flux_b=None), datetime(2030, 1, 1, tzinfo=UTC))


def test_niveaux_de_survie_prolongent_l_echelle():
    assert len(NIVEAUX) == 10
    assert NIVEAUX[9]["quai"] > NIVEAUX[7]["quai"] and NIVEAUX[9]["proche"] > NIVEAUX[7]["proche"]


def _escales(n_vrac, n_liner):
    e = [{"escale_id": i, "mmsi": str(i), "statut": "anchored", "categorie": "VRAC", "min_dist": 10.0,
          "dern_dist": 10.0, "expected_eta": None} for i in range(n_vrac)]
    e += [{"escale_id": 1000 + i, "mmsi": str(1000 + i), "statut": "approaching", "categorie": "LINER",
           "dern_dist": 800.0, "expected_eta": None} for i in range(n_liner)]
    etat = Etat()
    for x in e:
        etat.escale(x["escale_id"]).update(phase="rade_stable" if x["categorie"] == "VRAC" else "approche",
                                           en_zone=x["categorie"] == "VRAC", dernier_dist=x["dern_dist"])
    return e, etat


def test_projection_contrainte_par_le_solde():
    cfg = Config(fin_flux_b=None)
    cfg.plafonds.cible_mois, cfg.plafonds.jour = 10**9, 10**9
    e, etat = _escales(30, 8)
    libre = projection(cfg, JournalFaux(), e, etat, T)
    assert libre["niveau"] == 0 and libre["solde"] is None                       # solde inconnu : inchangé
    riche = projection(cfg, JournalFaux(), e, etat, T, solde=10**6)
    assert riche["niveau"] == 0
    # 30 VRAC en rade + 8 LINER au large : ~1 900 cr/j au niveau 0, ~330 au niveau 9 (dont ~250 non dégradables)
    serre = projection(cfg, JournalFaux(), e, etat, T, solde=15_000)
    assert 0 < serre["niveau"] < len(NIVEAUX) - 1 and "alerte" not in serre
    assert serre["lignes"][-1]["besoin_renouvellement"] <= 15_000 - cfg.plafonds.reserve_solde
    assert all(l["besoin_renouvellement"] > 14_000 for l in serre["lignes"][:-1])          # niveaux plus fins refusés
    vide = projection(cfg, JournalFaux(), e, etat, T, solde=5_000)
    assert vide["niveau"] == len(NIVEAUX) - 1 and "survie" in vide["alerte"]


def test_projection_tient_compte_de_la_fin_du_flux_b():
    cfg_b = Config(fin_flux_b=FIN_B)
    cfg_sans = Config(fin_flux_b=None)
    for c in (cfg_b, cfg_sans):
        c.plafonds.cible_mois, c.plafonds.jour = 10**9, 10**9
    e, etat = _escales(30, 8)
    a = projection(cfg_b, JournalFaux(), e, etat, T, solde=15_000)
    b = projection(cfg_sans, JournalFaux(), e, etat, T, solde=15_000)
    assert a["lignes"][0]["besoin_renouvellement"] < b["lignes"][0]["besoin_renouvellement"]
    assert a["niveau"] <= b["niveau"]


def test_mode_survie():
    cfg = Config()
    assert mode_survie(cfg, cfg.plafonds.reserve_solde) and not mode_survie(cfg, cfg.plafonds.reserve_solde + 1)
    assert not mode_survie(cfg, None)
