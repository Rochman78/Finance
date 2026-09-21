"""Tests du cron nocturne des avoirs : garde horaire, cran de sûreté, filtres de refund.
Aucun appel réseau.  python -m pytest test_cron_avoirs_du_jour.py -q"""
from datetime import datetime, timezone

import pytest

import create_credit_note_drafts as C
import cron_avoirs_du_jour as cron


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest.mark.parametrize("instant, attendu", [
    (utc(2026, 7, 15, 20, 0), True),     # été : 22h Paris
    (utc(2026, 7, 15, 21, 0), False),    # été : 23h Paris
    (utc(2026, 1, 15, 20, 0), False),    # hiver : 21h Paris
    (utc(2026, 1, 15, 21, 0), True),     # hiver : 22h Paris
    (utc(2026, 3, 29, 20, 0), True),     # jour du passage à l'heure d'été
    (utc(2026, 10, 25, 21, 0), True),    # jour du passage à l'heure d'hiver
    (utc(2026, 10, 25, 20, 0), False),
])
def test_une_seule_execution_par_nuit(instant, attendu):
    assert cron.doit_tourner(instant) is attendu


@pytest.fixture
def cran(monkeypatch):
    for v in ("AURALIS_AVOIRS_ARMED", "AURALIS_AVOIRS_CAP",
              "AURALIS_AVOIRS_CRON", "AURALIS_AVOIRS_CRON_CAP"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(C, "_mode_cron", False)
    monkeypatch.setattr(C, "_avoirs_crees", 0)
    monkeypatch.setattr(C, "plafond_atteint", False)
    return monkeypatch


def test_variable_cron_seule_n_arme_pas_un_script_manuel(cran):
    cran.setenv("AURALIS_AVOIRS_CRON", "1")
    assert not C._avoirs_armes()
    assert C.pl_post("customer_invoices", {}).status_code == 403


def test_armed_manuel_n_arme_pas_le_cron(cran):
    cran.setenv("AURALIS_AVOIRS_ARMED", "1")
    C.activer_mode_cron()
    assert not C._avoirs_armes()


def test_voie_cron_et_son_plafond(cran):
    cran.setenv("AURALIS_AVOIRS_CRON", "1")
    C.activer_mode_cron()
    assert C._avoirs_armes()
    assert C._plafond_avoirs() == 40
    cran.setenv("AURALIS_AVOIRS_CRON_CAP", "3")
    cran.setattr(C, "_avoirs_crees", 3)
    r = C.pl_post("customer_invoices", {})
    assert r.status_code == 403 and "plafond" in r.text
    assert C.plafond_atteint


def refund(*txs):
    return {"transactions": [{"kind": k, "status": s, "amount": a} for k, s, a in txs]}


def test_filtres_de_refund():
    assert cron.argent_rendu(refund(("refund", "success", "10.00"), ("refund", "failure", "10.00"))) == 10.0
    assert cron.argent_rendu(refund()) == 0          # restock seul
    assert cron.a_une_transaction_en_attente(refund(("refund", "pending", "5.00")))
    assert not cron.a_une_transaction_en_attente(refund(("refund", "success", "5.00")))


def factures_du_jour(monkeypatch, items):
    monkeypatch.setattr(C, "pl_get", lambda path, params=None: {"items": items, "has_more": False})


def test_facture_qui_cite_une_autre_commande_n_est_pas_prise(monkeypatch):
    # Cas TZ6960 / TZ6971 : la facture de TZ6971 cite TZ6960 dans sa mention.
    tz6971 = {"id": 1, "amount": "159.99",
              "special_mention": "Commande #TZ6971\nNeubestellung nach Storno von #TZ6960"}
    tz6960 = {"id": 2, "amount": "319.98", "special_mention": "Commande #TZ6960\nFacture déjà payée"}
    factures_du_jour(monkeypatch, [tz6971, tz6960])
    inv = C.find_original_invoice("#TZ6960", "2026-08-03")
    assert inv["id"] == 2 and inv["_match"] == "commande"


def test_prefixe_de_numero_ne_matche_pas(monkeypatch):
    factures_du_jour(monkeypatch, [{"id": 1, "amount": "10", "special_mention": "Commande #LFC44260"}])
    assert C.find_original_invoice("#LFC4426", "2026-09-15") is None


@pytest.mark.parametrize("mention", ["A partir du devis numéro D-2026-0042 | RDC3838",
                                     "D-2026-05-221474\nRDC3838"])
def test_facture_nee_d_un_devis(monkeypatch, mention):
    factures_du_jour(monkeypatch, [{"id": 3, "amount": "50", "special_mention": mention,
                                    "label": "Facture Jean Dupont - F-2026-05-25-18203 (label généré)"}])
    inv = C.find_original_invoice("RDC3838", "2026-09-15")
    assert inv["id"] == 3 and inv["_match"] == "commande"


def test_repli_sur_mention_marque(monkeypatch):
    factures_du_jour(monkeypatch, [{"id": 4, "amount": "50",
                                    "special_mention": "Commande #RDC9999\nremplace RDC3838"}])
    inv = C.find_original_invoice("RDC3838", "2026-09-15")
    assert inv["id"] == 4 and inv["_match"] == "mention"


def test_mail_ne_porte_que_les_defauts():
    rapport = {"crees": [("LFC", "#LFC1", 1, 10.0)], "finalises": {1}, "en_attente": ["#LFC2 (refund 9)"],
               "brouillons": ["#LFC3 avoir 7 plafonné de 0.01€"], "a_trancher": [],
               "alertes": [], "deja_faits": 0}
    assert cron.a_revoir(rapport)
    corps = cron.texte_defauts(rapport)
    assert "#LFC3" in corps and "#LFC1" not in corps and "#LFC2" not in corps
    rapport["brouillons"] = []
    assert not cron.a_revoir(rapport)   # que des succès : pas de mail
