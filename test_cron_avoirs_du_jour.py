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


def refund(*txs, gateway="mollie"):
    return {"transactions": [{"kind": k, "status": s, "amount": a, "gateway": gateway} for k, s, a in txs]}


def test_filtres_de_refund():
    assert cron.argent_rendu(refund(("refund", "success", "10.00"), ("refund", "failure", "10.00"))) == 10.0
    assert cron.argent_rendu(refund()) == 0          # restock seul
    assert cron.a_une_transaction_en_attente(refund(("refund", "pending", "5.00")))
    assert not cron.a_une_transaction_en_attente(refund(("refund", "success", "5.00")))


def test_pending_shopify_payments_credite_le_jour_meme(monkeypatch):
    # LFC44204 : pending le 23/09 à 8h52, réussi le 24/09 à 9h → avoir daté du 24.
    sp = refund(("refund", "pending", "102.00"), gateway="shopify_payments")
    autre = refund(("refund", "pending", "102.00"))
    assert cron.argent_rendu(sp) == 0 and cron.a_une_transaction_en_attente(sp)   # campagnes : strict
    monkeypatch.setattr(C, "ACCEPTER_PENDING_SHOPIFY_PAYMENTS", True)             # cron
    assert cron.argent_rendu(sp) == 102.0
    assert not cron.a_une_transaction_en_attente(sp)
    assert cron.argent_rendu(autre) == 0 and cron.a_une_transaction_en_attente(autre)


def _commande_en_echec(monkeypatch, montant_avoir, refund_id="7"):
    monkeypatch.setattr(C, "ACCEPTER_PENDING_SHOPIFY_PAYMENTS", True)
    monkeypatch.setattr(cron, "verifie_scope", lambda s: True)
    o = {"name": "#LFC1", "created_at": "2026-09-07T10:00:00",
         "refunds": [{"id": 7, "created_at": "2026-09-23T08:52:26",
                      "transactions": [{"kind": "refund", "status": "failure", "amount": "102.00",
                                        "gateway": "shopify_payments"}]}]}
    monkeypatch.setattr(C, "fetch_orders_with_refunds_in_period", lambda *a, **k: [o])
    monkeypatch.setattr(C, "find_original_invoice", lambda *a: {"id": 1, "customer": {"id": 2}})
    monkeypatch.setattr(C, "find_avoirs_for_invoice", lambda *a: [
        {"refund_id": refund_id, "amount": -montant_avoir, "invoice_number": "F-2026-09-23-1"}])
    monkeypatch.setattr(C, "traiter_refunds", lambda *a, **k: pytest.fail("rien à créer"))
    rapport = _rapport()
    cron.traiter_boutique({"name": "LFC", "store": "lfc"}, "2026-09-18", "2026-09-25", True, rapport)
    return rapport


def test_echec_shopify_payments_apres_avoir_alerte(monkeypatch):
    rapport = _commande_en_echec(monkeypatch, 102.0)
    assert len(rapport["alertes"]) == 1 and "ÉCHEC" in rapport["alertes"][0]
    assert "F-2026-09-23-1" in rapport["alertes"][0]


def test_echec_shopify_payments_sans_avoir_silencieux(monkeypatch):
    assert _commande_en_echec(monkeypatch, 102.0, refund_id="999")["alertes"] == []


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


def _rapport_vide():
    return {"crees": [], "finalises": set(), "brouillons": [], "a_trancher": [], "en_attente": [],
            "alertes": [], "deja_faits": 0, "boutiques": 9, "fenetre": "15/09 → 22/09"}


def test_jour_vide_le_dit():
    texte = cron.recap(_rapport_vide(), datetime(2026, 9, 22).date(), False)
    assert "Aucun avoir à faire aujourd'hui" in texte and "9 boutique(s)" in texte
    assert "15/09 → 22/09" in texte


@pytest.mark.parametrize("env, statut", [({}, None), ({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}, 400)])
def test_telegram_ko_bascule_sur_le_mail(monkeypatch, env, statut):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    class Rep:
        status_code = statut
        text = "Bad Request: chat not found"
        def json(self): return {"ok": False}
    monkeypatch.setattr(cron.requests, "post", lambda *a, **k: Rep())
    mails = []
    monkeypatch.setattr(cron, "mail_revue", lambda sujet, corps: mails.append((sujet, corps)))
    code = cron.notifier(_rapport_vide(), "Avoirs du 22/09/2026\n\n✅ Aucun avoir", datetime(2026, 9, 22), False)
    assert code == 1 and len(mails) == 1
    assert "Telegram KO" in mails[0][0] and "Aucun avoir" in mails[0][1]


def test_telegram_ok_jour_vide_pas_de_mail(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    class Rep:
        status_code = 200
        text = ""
        def json(self): return {"ok": True}
    monkeypatch.setattr(cron.requests, "post", lambda *a, **k: Rep())
    mails = []
    monkeypatch.setattr(cron, "mail_revue", lambda *a: mails.append(a))
    assert cron.notifier(_rapport_vide(), "x", datetime(2026, 9, 22), False) == 0
    assert mails == []


class _Rep:
    def __init__(self, code, headers=None):
        self.status_code, self.headers, self.text = code, headers or {}, ""


def test_requete_reprend_sur_429(monkeypatch):
    reponses = iter([_Rep(429, {"Retry-After": "1"}), _Rep(503), _Rep(200)])
    monkeypatch.setattr(C.requests, "request", lambda *a, **k: next(reponses))
    attentes = []
    monkeypatch.setattr(C.time, "sleep", attentes.append)
    assert C.requete("GET", "https://x/admin/oauth/access_scopes.json").status_code == 200
    assert len(attentes) == 2


def test_requete_bornee(monkeypatch):
    monkeypatch.setattr(C.requests, "request", lambda *a, **k: _Rep(429))
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    assert C.requete("GET", "https://x").status_code == 429   # rendu, pas de boucle infinie


def test_pennylane_en_erreur_n_est_pas_une_facture_absente(monkeypatch):
    monkeypatch.setattr(C, "pl_get", lambda path, params=None: None)
    with pytest.raises(C.RechercheFactureImpossible):
        C.find_original_invoice("#LFC1", "2026-09-15")


def test_plage_de_facturation_pennylane():
    assert cron.hors_plage_facturation("RED", "2025-07-24")
    assert cron.hors_plage_facturation("LFC", "2025-04-30")
    assert not cron.hors_plage_facturation("LFC", "2025-07-24")
    assert not cron.hors_plage_facturation("TZ", "2026-02-01")
    assert "introuvable" in cron.motif_sans_facture("2026-03-01")


def test_erreur_passagere():
    import requests
    err = requests.HTTPError(response=_Rep(429))
    assert cron.erreur_passagere(err)
    assert not cron.erreur_passagere(requests.HTTPError(response=_Rep(404)))
    assert cron.erreur_passagere(requests.ConnectionError())
    assert not cron.erreur_passagere(KeyError("x"))


def _rapport():
    return {"crees": [], "finalises": set(), "brouillons": [], "a_trancher": [], "en_attente": [],
            "alertes": [], "deja_faits": 0, "boutiques": 0, "rappels": []}


def test_cas_sans_facture_detaille_le_jour_meme_puis_rappel(monkeypatch):
    store = {"name": "RED", "store": "red"}
    monkeypatch.setattr(cron, "verifie_scope", lambda s: True)
    o_ancien = {"name": "RDC5000", "created_at": "2026-04-03T10:00:00",
                "refunds": [{"id": 1, "created_at": "2026-09-21T11:59:00",
                             "transactions": [{"kind": "refund", "status": "success", "amount": "75.51"}]}]}
    o_jour = {"name": "RDC9000", "created_at": "2026-03-02T10:00:00",
              "refunds": [{"id": 2, "created_at": "2026-09-22T10:00:00",
                           "transactions": [{"kind": "refund", "status": "success", "amount": "10"}]}]}
    o_hors_plage = {"name": "RDC2118", "created_at": "2024-08-03T10:00:00",
                    "refunds": [{"id": 3, "created_at": "2026-09-22T11:59:00",
                                 "transactions": [{"kind": "refund", "status": "success", "amount": "75.51"}]}]}
    monkeypatch.setattr(C, "fetch_orders_with_refunds_in_period",
                        lambda *a, **k: [o_ancien, o_jour, o_hors_plage])
    monkeypatch.setattr(C, "traiter_refunds", lambda store, todo, dry_run=False: (
        {}, [], [], [{"status": "skip_no_invoice", "order": o["name"], "refund_id": r["id"]} for o, r in todo]))
    rapport = _rapport()
    cron.traiter_boutique(store, "2026-09-15", "2026-09-22", True, rapport)
    assert rapport["rappels"] == ["RDC5000"]
    assert len(rapport["a_trancher"]) == 1 and "RDC9000" in rapport["a_trancher"][0]
    assert "facture Pennylane introuvable" in rapport["a_trancher"][0]
    texte = cron.recap(rapport, datetime(2026, 9, 22).date(), True)
    assert "déjà signalés (1) : RDC5000" in texte
    assert "RDC2118" not in texte          # hors plage de facturation : jamais remonté
    assert not cron.a_revoir({**rapport, "a_trancher": []})   # un rappel seul n'envoie pas de mail
