"""atdUtc du port précédent (lastPort déjà à MACAS) : jamais pris pour le départ de l'escale (0 base, 0 crédit)."""
from datetime import datetime, timedelta, timezone

from app.collecteur.boucles import atd_recevable

U = timezone.utc


def test_atd_du_port_precedent_rejete_cas_xpress_atlas():
    # escale 136 : 1re observation 16/11 11:21, accostage 21/11 04:54 ; réponse du 23/11 : lastPort MACAS, atd 16/11 10:27
    debut = datetime(2026, 11, 16, 11, 21, tzinfo=U)
    escale = {"berth_entry_at": datetime(2026, 11, 21, 4, 54, tzinfo=U), "port_entry_at": datetime(2026, 11, 21, 4, 54, tzinfo=U)}
    assert not atd_recevable(datetime(2026, 11, 16, 10, 27, tzinfo=U), debut, escale)
    # l'atdUtc mis à jour (départ réel, 23/11 03:03) reste recevable
    assert atd_recevable(datetime(2026, 11, 23, 3, 3, tzinfo=U), debut, escale)


def test_atd_anterieur_au_mouillage_rejete():
    debut = datetime(2026, 11, 19, 21, 11, tzinfo=U)
    assert not atd_recevable(datetime(2026, 11, 19, 22, 1, tzinfo=U), debut,
                             {"anchorage_entry_at": datetime(2026, 11, 21, 5, 0, tzinfo=U)})


def test_escale_ouverte_apres_le_depart_tolerance_1h_conservee():
    debut = datetime(2026, 11, 12, 13, 26, tzinfo=U)
    assert atd_recevable(debut - timedelta(minutes=50), debut, {})      # aucune entrée : règle d'origine
    assert not atd_recevable(debut - timedelta(hours=2), debut, {})


def test_valeurs_absentes():
    assert not atd_recevable(None, datetime(2026, 11, 12, tzinfo=U), {})
    assert not atd_recevable(datetime(2026, 11, 12, tzinfo=U), None, {})
