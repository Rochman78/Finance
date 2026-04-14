import streamlit as st
import pandas as pd
import sys, os, io, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta, datetime
from collections import defaultdict
from cadrage_tva import get_tva_shopify, get_tva_pennylane, STORE_PREFIXES, TVA_ACCOUNT_LABELS, COUNTRY_TO_TVA_ACCOUNT, COUNTRY_NAMES
from ui_common import setup_page
from fpdf import FPDF

setup_page()

SEUIL_ECART = 0.04
SEUIL_ECART_BOUTIQUE = 2.00
TODAY = datetime.now().strftime("%Y-%m-%d")

st.title("🧾 Cadrage de la TVA")
st.markdown("TVA Shopify (commandes) vs Comptabilité Pennylane (445 / VT)")

# =============================================================
# SÉLECTION DE PÉRIODE
# =============================================================
col1, col2, col3 = st.columns([1, 1, 2])
with col1:
    date_min = st.date_input("Date début", value=date(2026, 3, 1), min_value=date(2026, 3, 1), max_value=date(2026, 3, 15))
with col2:
    date_max = st.date_input("Date fin", value=date(2026, 3, 1), min_value=date(2026, 3, 1), max_value=date(2026, 3, 15))
with col3:
    st.write("")
    st.write("")
    run = st.button("🚀 Lancer le cadrage", type="primary", use_container_width=True)

if run:
    date_min_str = date_min.strftime("%Y-%m-%d")
    date_max_str = date_max.strftime("%Y-%m-%d")
    with st.spinner("Chargement de la TVA Shopify..."):
        st.session_state["tva_shopify"] = get_tva_shopify(date_min_str, date_max_str)
    with st.spinner("Chargement de la TVA Pennylane (445/VT)..."):
        st.session_state["tva_pennylane"] = get_tva_pennylane(date_min_str, date_max_str)
    st.session_state["tva_date_min"] = date_min_str
    st.session_state["tva_date_max"] = date_max_str

if "tva_shopify" not in st.session_state:
    st.info("Sélectionnez une période et cliquez sur **Lancer le cadrage**")
    st.stop()

sp = st.session_state["tva_shopify"]
pl = st.session_state["tva_pennylane"]
date_min_str = st.session_state["tva_date_min"]
date_max_str = st.session_state["tva_date_max"]
multi_day = date_min_str != date_max_str
periode_label = f"du {date_min_str} au {date_max_str}" if multi_day else f"du {date_min_str}"

def ecart_ok(e):
    return abs(e) < SEUIL_ECART

def ecart_boutique_ok(e):
    return abs(e) < SEUIL_ECART_BOUTIQUE

# =============================================================
# CALCULS
# =============================================================
total_sp = round(sum(s["tva"] for s in sp.values() if s["tva"] is not None), 2)
total_pl = pl["total_tva"]

# =============================================================
# PREPARE DATA
# =============================================================
# Boutique rows
cadrage_rows = []
for sname, sdata in sp.items():
    sp_tva = sdata["tva"] if sdata["tva"] is not None else 0
    pl_tva = round(sum(l["tva"] for l in pl["lines"] if l.get("store") == sname), 2)
    if sp_tva > 0 or pl_tva != 0:
        ecart_b = round(sp_tva - pl_tva, 2)
        if abs(ecart_b) < SEUIL_ECART_BOUTIQUE:
            ecart_b = 0.0
        is_ok = ecart_boutique_ok(ecart_b)
        cadrage_rows.append({
            "Boutique": sname, "TVA Shopify": sp_tva, "TVA Pennylane": pl_tva,
            "Écart": ecart_b, "Montant écart justifié": ecart_b if is_ok else 0.0, "Commentaire": "",
        })

unknown_tva = round(sum(l["tva"] for l in pl["lines"] if l.get("store") is None), 2)
if unknown_tva:
    cadrage_rows.append({
        "Boutique": "(INCONNU)", "TVA Shopify": 0, "TVA Pennylane": unknown_tva,
        "Écart": round(-unknown_tva, 2), "Montant écart justifié": 0.0, "Commentaire": "",
    })

if "cadrage_tva_boutique_data" not in st.session_state or run:
    st.session_state["cadrage_tva_boutique_data"] = cadrage_rows

ecart_brut = round(sum(r["Écart"] for r in cadrage_rows), 2)

current_data = st.session_state.get("cadrage_tva_boutique_data", cadrage_rows)
ecart_justifie = round(
    sum(r.get("Montant écart justifié", 0) for r in current_data if not ecart_boutique_ok(r["Écart"]))
    + sum(r["Écart"] for r in current_data if ecart_boutique_ok(r["Écart"])), 2)
ecart_residuel = round(ecart_brut - ecart_justifie, 2)
all_justified = all(
    ecart_boutique_ok(r["Écart"]) or r.get("Montant écart justifié", 0) == r["Écart"]
    for r in current_data)

# Order rows
sp_by_order = {}
for sname, sdata in sp.items():
    for o in sdata.get("orders", []):
        sp_by_order[o["order_name"]] = o

pl_by_order = {}
for l in pl["lines"]:
    ref = l.get("order_ref")
    if ref:
        if ref not in pl_by_order:
            pl_by_order[ref] = {"tva": 0.0, "store": l.get("store"), "invoice": l.get("invoice_number")}
        pl_by_order[ref]["tva"] += l["tva"]

all_orders_list = sorted(set(list(sp_by_order.keys()) + list(pl_by_order.keys())))
order_rows = []
for order in all_orders_list:
    sp_o = sp_by_order.get(order)
    pl_o = pl_by_order.get(order)
    sp_tva = sp_o["tva"] if sp_o else 0
    pl_tva = round(pl_o["tva"], 2) if pl_o else 0
    ecart_o = round(sp_tva - pl_tva, 2)
    if abs(ecart_o) < SEUIL_ECART:
        ecart_o = 0.0
    invoice = pl_o.get("invoice", "") if pl_o else ""
    order_rows.append({
        "Commande": order, "Facture": invoice or "",
        "Boutique": (sp_o["store"] if sp_o else pl_o.get("store")) or "?",
        "TVA Shopify": sp_tva, "TVA Pennylane": pl_tva, "Écart": ecart_o, "Commentaire": "",
    })

# Account data (for tab 3)
sp_account_data = defaultdict(lambda: {"tva": 0.0, "count": 0})
sp_country_data = defaultdict(lambda: {"tva": 0.0, "count": 0})
all_sp_orders = []
for sdata in sp.values():
    for o in sdata.get("orders", []):
        all_sp_orders.append(o)
        compte = o.get("compte_tva", "")
        if compte:
            sp_account_data[compte]["tva"] += o["tva"]
            sp_account_data[compte]["count"] += 1
        cc = o.get("country_code", "FR") or "FR"
        sp_country_data[cc]["tva"] += o["tva"]
        sp_country_data[cc]["count"] += 1

pl_account_data = defaultdict(lambda: {"tva": 0.0, "count": 0, "name": ""})
for l in pl["lines"]:
    key = l["account"]
    pl_account_data[key]["tva"] += l["tva"]
    pl_account_data[key]["count"] += 1
    if l.get("account_name") and not pl_account_data[key]["name"]:
        pl_account_data[key]["name"] = l["account_name"]

# ╔═══════════════════════════════════════════════════════════════╗
# ║  1. RÉSULTAT DU CADRAGE                                      ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Résultat du cadrage")

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("TVA Shopify", f"{total_sp:,.2f} €")
col2.metric("TVA Pennylane", f"{total_pl:,.2f} €")
col3.metric("Écart brut", f"{ecart_brut:,.2f} €")
col4.metric("Écart justifié", f"{ecart_justifie:,.2f} €")
col5.metric("Écart résiduel", f"{ecart_residuel:,.2f} €")

if ecart_ok(ecart_brut):
    st.markdown("""
    <div style="background: linear-gradient(135deg, #ECFDF5, #D1FAE5); border: 1px solid #6EE7B7;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #065F46;">✅ Cadrage OK</div>
        <div style="font-size: 0.95rem; color: #047857; margin-top: 0.3rem;">Aucun écart significatif sur la période</div>
    </div>""", unsafe_allow_html=True)
elif all_justified:
    st.markdown(f"""
    <div style="background: linear-gradient(135deg, #F3E8FF, #E9D5FF); border: 1px solid #C084FC;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #6B21A8;">🟣 Cadrage justifié</div>
        <div style="font-size: 0.95rem; color: #7C3AED; margin-top: 0.3rem;">Écart brut de {ecart_brut:,.2f} € entièrement justifié — Écart résiduel : {ecart_residuel:,.2f} €</div>
    </div>""", unsafe_allow_html=True)
else:
    nb_nj = sum(1 for r in current_data if not ecart_boutique_ok(r["Écart"]) and r.get("Montant écart justifié", 0) != r["Écart"])
    st.markdown(f"""
    <div style="background: linear-gradient(135deg, #FEF2F2, #FECACA); border: 1px solid #FCA5A5;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #991B1B;">❌ Écart résiduel : {ecart_residuel:,.2f} €</div>
        <div style="font-size: 0.95rem; color: #B91C1C; margin-top: 0.3rem;">{nb_nj} boutique(s) non justifiée(s)</div>
    </div>""", unsafe_allow_html=True)

# PDF placeholder
pdf_placeholder = st.empty()

# Jour par jour
if multi_day:
    st.markdown("**Résultat jour par jour**")
    daily_sp = defaultdict(lambda: {"tva": 0.0, "nb_orders": 0})
    for sdata in sp.values():
        for d, dd in sdata.get("by_date", {}).items():
            daily_sp[d]["tva"] += dd["tva"]
            daily_sp[d]["nb_orders"] += dd["nb_orders"]
    daily_pl = pl["by_date"]
    all_days = sorted(set(list(daily_sp.keys()) + list(daily_pl.keys())))
    daily_rows = []
    for day in all_days:
        sp_day = round(daily_sp[day]["tva"], 2)
        pl_day = round(daily_pl.get(day, {}).get("tva", 0), 2)
        ecart_day = round(sp_day - pl_day, 2)
        if abs(ecart_day) < SEUIL_ECART:
            ecart_day = 0.0
        daily_rows.append({"Date": day, "TVA Shopify": sp_day, "TVA Pennylane": pl_day, "Écart": ecart_day, "Statut": "✅" if ecart_ok(ecart_day) else f"❌ {ecart_day:+,.2f}€"})
    if daily_rows:
        df_daily = pd.DataFrame(daily_rows)
        def hl_day(row):
            if not ecart_ok(row["Écart"]):
                return ["background-color: rgba(255, 50, 50, 0.15)"] * len(row)
            return [""] * len(row)
        st.dataframe(df_daily.style.apply(hl_day, axis=1), use_container_width=True, hide_index=True)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  2. DÉTAIL DU CADRAGE                                         ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Détail du cadrage")

st.markdown("""
<style>
    [data-testid="stDataEditor"] [data-testid="column-header"]:has(span[title*="justifié"]),
    [data-testid="stDataEditor"] [data-testid="column-header"]:has(span[title*="Commentaire"]) {
        background-color: rgba(139, 92, 246, 0.18) !important;
        border-bottom: 3px solid rgba(139, 92, 246, 0.5) !important;
    }
    [data-testid="stDataEditor"] th:has(span[title*="justifié"]),
    [data-testid="stDataEditor"] th:has(span[title*="Commentaire"]) {
        background-color: rgba(139, 92, 246, 0.18) !important;
        border-bottom: 3px solid rgba(139, 92, 246, 0.5) !important;
    }
</style>
""", unsafe_allow_html=True)

# Compte comptable rows
cadrage_compte_rows = []
all_comptes = sorted(set(list(sp_account_data.keys()) + list(pl_account_data.keys())))
for compte in all_comptes:
    if not compte:
        continue
    sp_t = round(sp_account_data[compte]["tva"], 2)
    pl_t = round(pl_account_data[compte]["tva"], 2)
    ecart_c = round(sp_t - pl_t, 2)
    label = pl_account_data[compte]["name"] or TVA_ACCOUNT_LABELS.get(compte, "")
    pays = ""
    for cc, acc in COUNTRY_TO_TVA_ACCOUNT.items():
        if acc == compte:
            pays = f"{COUNTRY_NAMES.get(cc, cc)} ({cc})"
            break
    if abs(ecart_c) < 1.0:
        ecart_c = 0.0
    is_ok = abs(ecart_c) < SEUIL_ECART_BOUTIQUE
    cadrage_compte_rows.append({
        "Pays": pays, "Compte": compte, "Libellé": label,
        "TVA Shopify": sp_t, "TVA Pennylane": pl_t, "Écart": ecart_c,
        "Montant écart justifié": ecart_c if is_ok else 0.0, "Commentaire": "",
    })

if "cadrage_tva_compte_data" not in st.session_state or run:
    st.session_state["cadrage_tva_compte_data"] = cadrage_compte_rows

if "cadrage_tva_commande_data" not in st.session_state or run:
    st.session_state["cadrage_tva_commande_data"] = order_rows

tab_boutique, tab_compte, tab_commande = st.tabs(["📊 Par boutique", "🌍 Par pays de livraison (compte comptable)", "📋 Par commande"])

# ─── TAB 1 : PAR BOUTIQUE ───
with tab_boutique:
    df_cad = pd.DataFrame(st.session_state["cadrage_tva_boutique_data"])
    def get_statut_b(row):
        if ecart_boutique_ok(row["Écart"]): return "✅"
        elif row.get("Montant écart justifié", 0) == row["Écart"]: return "🟣"
        elif row.get("Montant écart justifié", 0) != 0: return "🟡"
        else: return "❌"
    df_cad["Statut"] = df_cad.apply(get_statut_b, axis=1)
    df_cad["Montant écart justifié"] = df_cad.apply(lambda r: round(r.get("Montant écart justifié", 0.0), 2) if not ecart_boutique_ok(r["Écart"]) else 0.0, axis=1)
    df_cad["Montant écart restant"] = df_cad.apply(lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_boutique_ok(r["Écart"]) else 0.0, axis=1)
    filt_b = st.radio("Filtrer", ["Toutes", "Écarts seulement"], horizontal=True, key="tva_bout_filter")
    df_b_disp = df_cad if filt_b == "Toutes" else df_cad[~df_cad["Écart"].apply(ecart_boutique_ok)]
    cols_b = ["Boutique", "TVA Shopify", "TVA Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Statut", "Commentaire"]
    ed_b = st.data_editor(df_b_disp[cols_b], use_container_width=True, hide_index=True,
        disabled=["Boutique", "TVA Shopify", "TVA Pennylane", "Écart", "Montant écart restant", "Statut"],
        column_config={"Commentaire": st.column_config.TextColumn("✏️ Commentaire", width="large"),
            "TVA Shopify": st.column_config.NumberColumn(format="%.2f"), "TVA Pennylane": st.column_config.NumberColumn(format="%.2f"),
            "Écart": st.column_config.NumberColumn(format="%.2f"), "Montant écart justifié": st.column_config.NumberColumn("✏️ Montant écart justifié", format="%.2f"),
            "Montant écart restant": st.column_config.NumberColumn(format="%.2f")})
    if ed_b is not None:
        _ch = False
        for _, row in ed_b.iterrows():
            for r in st.session_state["cadrage_tva_boutique_data"]:
                if r["Boutique"] == row["Boutique"]:
                    if not ecart_boutique_ok(r["Écart"]):
                        nm = row.get("Montant écart justifié", 0.0)
                        if r.get("Montant écart justifié", 0.0) != nm: r["Montant écart justifié"] = nm; _ch = True
                    nc = row.get("Commentaire", "")
                    if r.get("Commentaire", "") != nc: r["Commentaire"] = nc; _ch = True
        if _ch: st.rerun()
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Shopify", f"{df_cad['TVA Shopify'].sum():,.2f} €")
    c2.metric("Total Pennylane", f"{df_cad['TVA Pennylane'].sum():,.2f} €")
    c3.metric("Écart", f"{df_cad['Écart'].sum():,.2f} €")
    csv_b = df_cad[cols_b].to_csv(index=False).encode("utf-8")
    st.download_button("📥 Export cadrage par boutique (CSV)", csv_b, f"{TODAY} Cadrage TVA Boutiques {periode_label}.csv", "text/csv")

# ─── TAB 2 : PAR PAYS DE LIVRAISON ───
with tab_compte:
    cur_cpt = st.session_state.get("cadrage_tva_compte_data", cadrage_compte_rows)
    if cur_cpt:
        df_cpt = pd.DataFrame(cur_cpt)
        def get_statut_c(row):
            if abs(row["Écart"]) < SEUIL_ECART: return "✅"
            elif row.get("Montant écart justifié", 0) == row["Écart"]: return "🟣"
            elif row.get("Montant écart justifié", 0) != 0: return "🟡"
            else: return "❌"
        df_cpt["Statut"] = df_cpt.apply(get_statut_c, axis=1)
        df_cpt["Montant écart justifié"] = df_cpt.apply(lambda r: round(r.get("Montant écart justifié", 0.0), 2) if abs(r["Écart"]) >= SEUIL_ECART else 0.0, axis=1)
        df_cpt["Montant écart restant"] = df_cpt.apply(lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if abs(r["Écart"]) >= SEUIL_ECART else 0.0, axis=1)
        filt_c = st.radio("Filtrer", ["Tous", "Écarts seulement"], horizontal=True, key="tva_cpt_filter")
        df_c_disp = df_cpt if filt_c == "Tous" else df_cpt[df_cpt["Écart"].apply(lambda e: abs(e) >= SEUIL_ECART)]
        cols_c = ["Pays", "Compte", "Libellé", "TVA Shopify", "TVA Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Statut", "Commentaire"]
        ed_c = st.data_editor(df_c_disp[cols_c], use_container_width=True, hide_index=True,
            disabled=["Pays", "Compte", "Libellé", "TVA Shopify", "TVA Pennylane", "Écart", "Montant écart restant", "Statut"],
            column_config={"Commentaire": st.column_config.TextColumn("✏️ Commentaire", width="large"),
                "TVA Shopify": st.column_config.NumberColumn(format="%.2f"), "TVA Pennylane": st.column_config.NumberColumn(format="%.2f"),
                "Écart": st.column_config.NumberColumn(format="%.2f"), "Montant écart justifié": st.column_config.NumberColumn("✏️ Montant écart justifié", format="%.2f"),
                "Montant écart restant": st.column_config.NumberColumn(format="%.2f")})
        if ed_c is not None:
            _chc = False
            for _, row in ed_c.iterrows():
                for r in st.session_state["cadrage_tva_compte_data"]:
                    if r["Compte"] == row["Compte"]:
                        if abs(r["Écart"]) >= SEUIL_ECART:
                            nm = row.get("Montant écart justifié", 0.0)
                            if r.get("Montant écart justifié", 0.0) != nm: r["Montant écart justifié"] = nm; _chc = True
                        nc = row.get("Commentaire", "")
                        if r.get("Commentaire", "") != nc: r["Commentaire"] = nc; _chc = True
            if _chc: st.rerun()
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Shopify", f"{df_cpt['TVA Shopify'].sum():,.2f} €")
        c2.metric("Total Pennylane", f"{df_cpt['TVA Pennylane'].sum():,.2f} €")
        c3.metric("Écart", f"{df_cpt['Écart'].sum():,.2f} €")
        csv_c = df_cpt[cols_c].to_csv(index=False).encode("utf-8")
        st.download_button("📥 Export cadrage par pays (CSV)", csv_c, f"{TODAY} Cadrage TVA Pays {periode_label}.csv", "text/csv")

# ─── TAB 3 : PAR COMMANDE ───
with tab_commande:
    cur_cmd = st.session_state.get("cadrage_tva_commande_data", order_rows)
    if cur_cmd:
        df_o = pd.DataFrame(cur_cmd)
        def get_statut_o(row):
            if ecart_ok(row["Écart"]): return "✅"
            elif row.get("Montant écart justifié", 0) == row["Écart"]: return "🟣"
            elif row.get("Montant écart justifié", 0) != 0: return "🟡"
            else: return "❌"
        df_o["Statut"] = df_o.apply(get_statut_o, axis=1)
        if "Montant écart justifié" not in df_o.columns: df_o["Montant écart justifié"] = 0.0
        df_o["Montant écart restant"] = df_o.apply(lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_ok(r["Écart"]) else 0.0, axis=1)
        cf1, cf2 = st.columns(2)
        with cf1: filt_o = st.radio("Filtrer", ["Toutes", "Écarts seulement"], horizontal=True, key="tva_order_filter")
        with cf2:
            stores_o = sorted(df_o[df_o["Boutique"] != "?"]["Boutique"].unique())
            filt_s = st.selectbox("Boutique", ["Toutes"] + stores_o, key="tva_order_store")
        df_o_disp = df_o.copy()
        if filt_o == "Écarts seulement": df_o_disp = df_o_disp[~df_o_disp["Écart"].apply(ecart_ok)]
        if filt_s != "Toutes": df_o_disp = df_o_disp[df_o_disp["Boutique"] == filt_s]
        cols_o = ["Boutique", "Commande", "Facture", "TVA Shopify", "TVA Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Statut", "Commentaire"]
        ed_o = st.data_editor(df_o_disp[cols_o], use_container_width=True, hide_index=True, height=min(400, 35*(len(df_o_disp)+1)),
            disabled=["Commande", "Facture", "Boutique", "TVA Shopify", "TVA Pennylane", "Écart", "Montant écart restant", "Statut"],
            column_config={"Commentaire": st.column_config.TextColumn("✏️ Commentaire", width="large"),
                "TVA Shopify": st.column_config.NumberColumn(format="%.2f"), "TVA Pennylane": st.column_config.NumberColumn(format="%.2f"),
                "Écart": st.column_config.NumberColumn(format="%.2f"), "Montant écart justifié": st.column_config.NumberColumn("✏️ Montant écart justifié", format="%.2f"),
                "Montant écart restant": st.column_config.NumberColumn(format="%.2f")})
        if ed_o is not None:
            _cho = False
            for _, row in ed_o.iterrows():
                for r in st.session_state["cadrage_tva_commande_data"]:
                    if r["Commande"] == row["Commande"]:
                        if not ecart_ok(r["Écart"]):
                            nm = row.get("Montant écart justifié", 0.0)
                            if r.get("Montant écart justifié", 0.0) != nm: r["Montant écart justifié"] = nm; _cho = True
                        nc = row.get("Commentaire", "")
                        if r.get("Commentaire", "") != nc: r["Commentaire"] = nc; _cho = True
            if _cho: st.rerun()
        s1, s2, s3 = st.columns(3)
        s1.metric("Commandes", f"{len(df_o)}")
        s2.metric("OK", f"{len(df_o[df_o['Écart'].apply(ecart_ok)])}")
        s3.metric("En écart", f"{len(df_o[~df_o['Écart'].apply(ecart_ok)])}")
        csv_o = df_o[cols_o].to_csv(index=False).encode("utf-8")
        st.download_button("📥 Export cadrage par commande (CSV)", csv_o, f"{TODAY} Cadrage TVA Commandes {periode_label}.csv", "text/csv")

# ─── PDF ───
def generate_pdf():
    W = 277
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.add_page("L")
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 14, "AURALIS FINANCES", ln=True, align="C")
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 8, "Rapport de cadrage de la TVA", ln=True, align="C")
    pdf.cell(0, 8, f"Periode : {date_min_str} au {date_max_str}", ln=True, align="C")
    pdf.cell(0, 8, f"Date d'edition : {TODAY}", ln=True, align="C")
    pdf.ln(15)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Resultat du cadrage", ln=True)
    pdf.set_font("Helvetica", "", 13)
    pdf.cell(0, 9, f"TVA Shopify :       {total_sp:,.2f} EUR", ln=True)
    pdf.cell(0, 9, f"TVA Pennylane :     {total_pl:,.2f} EUR", ln=True)
    pdf.cell(0, 9, f"Ecart brut :        {ecart_brut:,.2f} EUR", ln=True)
    pdf.cell(0, 9, f"Ecart residuel :    {ecart_residuel:,.2f} EUR", ln=True)
    pdf.ln(6)
    if ecart_ok(ecart_brut):
        pdf.set_font("Helvetica", "B", 14); pdf.set_text_color(6, 95, 70)
        pdf.cell(0, 12, "CADRAGE OK", ln=True)
    else:
        pdf.set_font("Helvetica", "B", 14); pdf.set_text_color(153, 27, 27)
        pdf.cell(0, 12, f"ECART RESIDUEL : {ecart_residuel:,.2f} EUR", ln=True)
    pdf.set_text_color(0, 0, 0)

    # Page boutique
    pdf.add_page("L")
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Detail du cadrage - Par boutique", ln=True); pdf.ln(4)
    _bd = st.session_state.get("cadrage_tva_boutique_data", cadrage_rows)
    cw = [int(W*0.12), int(W*0.14), int(W*0.14), int(W*0.10), int(W*0.10), int(W*0.10), int(W*0.30)]
    pdf.set_font("Helvetica", "B", 10)
    for i, h in enumerate(["Boutique", "TVA Shopify", "TVA PL", "Ecart", "Justifie", "Restant", "Commentaire"]):
        pdf.cell(cw[i], 8, h, border=1, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 10)
    for r in _bd:
        eb = r["Écart"]; mj = r.get("Montant écart justifié", 0.0); mr = round(eb - mj, 2) if not ecart_boutique_ok(eb) else 0.0
        pdf.cell(cw[0], 7, str(r["Boutique"]), border=1); pdf.cell(cw[1], 7, f"{r['TVA Shopify']:,.2f}", border=1, align="R")
        pdf.cell(cw[2], 7, f"{r['TVA Pennylane']:,.2f}", border=1, align="R"); pdf.cell(cw[3], 7, f"{eb:,.2f}", border=1, align="R")
        pdf.cell(cw[4], 7, f"{mj:,.2f}", border=1, align="R"); pdf.cell(cw[5], 7, f"{mr:,.2f}", border=1, align="R")
        pdf.cell(cw[6], 7, str(r.get("Commentaire", ""))[:50], border=1); pdf.ln()

    # Page pays
    pdf.add_page("L")
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Detail du cadrage - Par pays de livraison (compte comptable)", ln=True); pdf.ln(4)
    _cd = st.session_state.get("cadrage_tva_compte_data", cadrage_compte_rows)
    cw2 = [int(W*0.12), int(W*0.08), int(W*0.17), int(W*0.11), int(W*0.11), int(W*0.09), int(W*0.09), int(W*0.09), int(W*0.14)]
    pdf.set_font("Helvetica", "B", 9)
    for i, h in enumerate(["Pays", "Compte", "Libelle", "TVA Shopify", "TVA PL", "Ecart", "Justifie", "Restant", "Commentaire"]):
        pdf.cell(cw2[i], 8, h, border=1, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    for r in _cd:
        ec = r["Écart"]; mj = r.get("Montant écart justifié", 0.0); mr = round(ec - mj, 2) if abs(ec) >= SEUIL_ECART else 0.0
        pdf.cell(cw2[0], 7, str(r.get("Pays", ""))[:18], border=1); pdf.cell(cw2[1], 7, str(r.get("Compte", "")), border=1)
        pdf.cell(cw2[2], 7, str(r.get("Libellé", ""))[:28], border=1); pdf.cell(cw2[3], 7, f"{r['TVA Shopify']:,.2f}", border=1, align="R")
        pdf.cell(cw2[4], 7, f"{r['TVA Pennylane']:,.2f}", border=1, align="R"); pdf.cell(cw2[5], 7, f"{ec:,.2f}", border=1, align="R")
        pdf.cell(cw2[6], 7, f"{mj:,.2f}", border=1, align="R"); pdf.cell(cw2[7], 7, f"{mr:,.2f}", border=1, align="R")
        pdf.cell(cw2[8], 7, str(r.get("Commentaire", ""))[:22], border=1); pdf.ln()

    # Page commande (écarts)
    pdf.add_page("L")
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Detail du cadrage - Par commande (ecarts uniquement)", ln=True); pdf.ln(4)
    _od = st.session_state.get("cadrage_tva_commande_data", order_rows)
    _oe = [r for r in _od if abs(r.get("Écart", 0)) >= SEUIL_ECART]
    if _oe:
        cw3 = [int(W*0.08), int(W*0.12), int(W*0.14), int(W*0.12), int(W*0.12), int(W*0.10), int(W*0.10), int(W*0.22)]
        pdf.set_font("Helvetica", "B", 10)
        for i, h in enumerate(["Boutique", "Commande", "Facture", "TVA Shopify", "TVA PL", "Ecart", "Justifie", "Commentaire"]):
            pdf.cell(cw3[i], 8, h, border=1, align="C")
        pdf.ln()
        pdf.set_font("Helvetica", "", 9)
        for r in _oe:
            mj = r.get("Montant écart justifié", 0)
            pdf.cell(cw3[0], 7, str(r.get("Boutique", "")), border=1); pdf.cell(cw3[1], 7, str(r.get("Commande", "")), border=1)
            pdf.cell(cw3[2], 7, str(r.get("Facture", ""))[:20], border=1); pdf.cell(cw3[3], 7, f"{r.get('TVA Shopify', 0):,.2f}", border=1, align="R")
            pdf.cell(cw3[4], 7, f"{r.get('TVA Pennylane', 0):,.2f}", border=1, align="R"); pdf.cell(cw3[5], 7, f"{r.get('Écart', 0):,.2f}", border=1, align="R")
            pdf.cell(cw3[6], 7, f"{mj:,.2f}", border=1, align="R"); pdf.cell(cw3[7], 7, str(r.get("Commentaire", ""))[:35], border=1); pdf.ln()
    else:
        pdf.set_font("Helvetica", "", 12); pdf.cell(0, 10, "Aucun ecart significatif par commande", ln=True)

    return bytes(pdf.output())

pdf_bytes = generate_pdf()
pdf_placeholder.download_button("📄 Rapport de cadrage TVA (PDF)", pdf_bytes, f"{TODAY} Rapport Cadrage TVA {periode_label}.pdf", "application/pdf")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  3. ÉLÉMENTS DE JUSTIFICATION                                 ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Éléments de justification")

st.markdown("### TVA Shopify")
st.markdown("**Par boutique**")
sp_rows = [{"Boutique": sn, "TVA Shopify": sd["tva"], "Commandes": sd["nb_orders"]} for sn, sd in sp.items() if sd["tva"] is not None and sd["tva"] > 0]
if sp_rows:
    df_sp = pd.DataFrame(sp_rows)
    df_sp = pd.concat([df_sp, pd.DataFrame([{"Boutique": "TOTAL", "TVA Shopify": round(df_sp["TVA Shopify"].sum(), 2), "Commandes": df_sp["Commandes"].sum()}])], ignore_index=True)
    st.dataframe(df_sp, use_container_width=True, hide_index=True)

st.markdown("**Par pays de livraison (compte comptable)**")
sp_c_rows = []
for cc in sorted(sp_country_data.keys()):
    d = sp_country_data[cc]
    if d["tva"] > 0:
        sp_c_rows.append({"Pays": COUNTRY_NAMES.get(cc, cc), "Code": cc, "TVA Shopify": round(d["tva"], 2), "Commandes": d["count"]})
if sp_c_rows:
    df_sp_c = pd.DataFrame(sp_c_rows)
    df_sp_c = pd.concat([df_sp_c, pd.DataFrame([{"Pays": "TOTAL", "Code": "", "TVA Shopify": round(df_sp_c["TVA Shopify"].sum(), 2), "Commandes": df_sp_c["Commandes"].sum()}])], ignore_index=True)
    st.dataframe(df_sp_c, use_container_width=True, hide_index=True)

if all_sp_orders:
    df_sp_exp = pd.DataFrame(all_sp_orders).rename(columns={"date": "Date", "order_name": "Commande", "store": "Boutique", "tva": "Montant TVA", "country_code": "Pays", "compte_tva": "Compte TVA"})
    exp_cols = [c for c in ["Date", "Boutique", "Commande", "Pays", "Compte TVA", "Montant TVA"] if c in df_sp_exp.columns]
    df_sp_exp = df_sp_exp[exp_cols].sort_values(["Date", "Boutique", "Commande"])
    st.download_button("📥 Export commandes Shopify TVA (CSV)", df_sp_exp.to_csv(index=False).encode("utf-8"), f"{TODAY} Commandes Shopify TVA {periode_label}.csv", "text/csv")

st.markdown("### TVA Pennylane")
st.markdown("**Par boutique**")
pl_rows = []
for sn in sorted(STORE_PREFIXES.keys()):
    t = round(sum(l["tva"] for l in pl["lines"] if l.get("store") == sn), 2)
    c = sum(1 for l in pl["lines"] if l.get("store") == sn)
    if t != 0 or c > 0: pl_rows.append({"Boutique": sn, "TVA Pennylane": t, "Écritures": c})
unk = round(sum(l["tva"] for l in pl["lines"] if l.get("store") is None), 2)
unk_c = sum(1 for l in pl["lines"] if l.get("store") is None)
if unk or unk_c: pl_rows.append({"Boutique": "(INCONNU)", "TVA Pennylane": unk, "Écritures": unk_c})
if pl_rows:
    df_pl = pd.DataFrame(pl_rows)
    df_pl = pd.concat([df_pl, pd.DataFrame([{"Boutique": "TOTAL", "TVA Pennylane": round(df_pl["TVA Pennylane"].sum(), 2), "Écritures": df_pl["Écritures"].sum()}])], ignore_index=True)
    st.dataframe(df_pl, use_container_width=True, hide_index=True)

st.markdown("**Par pays de livraison (compte comptable)**")
pl_c_rows = [{"Compte": a, "Libellé": d["name"] or TVA_ACCOUNT_LABELS.get(a, ""), "TVA Pennylane": round(d["tva"], 2), "Écritures": d["count"]} for a, d in sorted(pl_account_data.items())]
if pl_c_rows:
    df_pl_c = pd.DataFrame(pl_c_rows)
    df_pl_c = pd.concat([df_pl_c, pd.DataFrame([{"Compte": "TOTAL", "Libellé": "", "TVA Pennylane": round(df_pl_c["TVA Pennylane"].sum(), 2), "Écritures": df_pl_c["Écritures"].sum()}])], ignore_index=True)
    st.dataframe(df_pl_c, use_container_width=True, hide_index=True)

if pl["lines"]:
    df_pl_exp = pd.DataFrame(pl["lines"]).rename(columns={"date": "Date", "entry_label": "Écriture", "invoice_number": "Facture", "order_ref": "Commande", "store": "Boutique", "account": "Compte", "account_name": "Libellé compte", "tva": "Montant TVA"})
    exp_pl_cols = [c for c in ["Date", "Boutique", "Compte", "Libellé compte", "Écriture", "Facture", "Commande", "Montant TVA"] if c in df_pl_exp.columns]
    df_pl_exp = df_pl_exp[exp_pl_cols].sort_values(["Date", "Boutique", "Commande"])
    st.download_button("📥 Export TVA Pennylane détaillé (CSV)", df_pl_exp.to_csv(index=False).encode("utf-8"), f"{TODAY} TVA Pennylane Detail {periode_label}.csv", "text/csv")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  4. SYNTHÈSE DES EXPORTS                                      ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("📦 Synthèse des exports")

export_files = {}
export_files[f"{TODAY} Rapport Cadrage TVA {periode_label}.pdf"] = pdf_bytes
if order_rows:
    export_files[f"{TODAY} Cadrage TVA Commandes {periode_label}.csv"] = pd.DataFrame(order_rows).to_csv(index=False).encode("utf-8")
if cadrage_rows:
    export_files[f"{TODAY} Cadrage TVA Boutiques {periode_label}.csv"] = df_cad[cols_b].to_csv(index=False).encode("utf-8")
if cadrage_compte_rows:
    export_files[f"{TODAY} Cadrage TVA Pays {periode_label}.csv"] = pd.DataFrame(cadrage_compte_rows).to_csv(index=False).encode("utf-8")
if all_sp_orders:
    export_files[f"{TODAY} Commandes Shopify TVA {periode_label}.csv"] = df_sp_exp.to_csv(index=False).encode("utf-8")
if pl["lines"]:
    export_files[f"{TODAY} TVA Pennylane Detail {periode_label}.csv"] = df_pl_exp.to_csv(index=False).encode("utf-8")

cols_exp = st.columns(min(len(export_files), 3))
for i, (fn, fb) in enumerate(export_files.items()):
    mime = "application/pdf" if fn.endswith(".pdf") else "text/csv"
    with cols_exp[i % 3]:
        sn = fn.replace(TODAY, "").replace(periode_label, "").strip().rsplit(".", 1)[0].strip()
        ic = "📄" if fn.endswith(".pdf") else "📊"
        st.download_button(f"{ic} {sn}", fb, fn, mime, key=f"tva_synth_{i}")

st.write("")
zb = io.BytesIO()
with zipfile.ZipFile(zb, "w", zipfile.ZIP_DEFLATED) as zf:
    for fn, fb in export_files.items(): zf.writestr(fn, fb)
zb.seek(0)
st.download_button("📦 Exporter tout le dossier de travail (ZIP)", zb.getvalue(), f"{TODAY} Dossier Travail TVA {periode_label}.zip", "application/zip", type="primary", use_container_width=True)
