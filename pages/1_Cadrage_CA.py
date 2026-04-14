import streamlit as st
import pandas as pd
import sys, os, io, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta, datetime
from collections import defaultdict, Counter
from cadrage_ca import get_ca_shopify, get_ca_pennylane, STORE_PREFIXES
from ui_common import setup_page, period_selector
from fpdf import FPDF

setup_page()

SEUIL_ECART = 0.04
SEUIL_ECART_BOUTIQUE = 2.00
TODAY = datetime.now().strftime("%Y-%m-%d")

st.title("📈 Cadrage du Chiffre d'Affaires")
st.markdown("Commandes Shopify (CA HT) vs Comptabilité Pennylane (707 / VT)")

# =============================================================
# SÉLECTION DE PÉRIODE
# =============================================================
date_min, date_max = period_selector(key_prefix="ca")
run = st.button("🚀 Lancer le cadrage", type="primary", use_container_width=True)

if run:
    date_min_str = date_min.strftime("%Y-%m-%d")
    date_max_str = date_max.strftime("%Y-%m-%d")
    with st.spinner("Chargement du CA Shopify..."):
        st.session_state["ca_shopify"] = get_ca_shopify(date_min_str, date_max_str)
    with st.spinner("Chargement du CA Pennylane (707/VT)..."):
        st.session_state["ca_pennylane"] = get_ca_pennylane(date_min_str, date_max_str)
    st.session_state["ca_date_min"] = date_min_str
    st.session_state["ca_date_max"] = date_max_str

if "ca_shopify" not in st.session_state:
    st.info("Sélectionnez une période et cliquez sur **Lancer le cadrage**")
    st.stop()

sp = st.session_state["ca_shopify"]
pl = st.session_state["ca_pennylane"]
date_min_str = st.session_state["ca_date_min"]
date_max_str = st.session_state["ca_date_max"]
multi_day = date_min_str != date_max_str
periode_label = f"du {date_min_str} au {date_max_str}" if multi_day else f"du {date_min_str}"

# =============================================================
# CALCULS
# =============================================================
total_sp = round(sum(s["ca_ht"] for s in sp.values() if s["ca_ht"] is not None), 2)
total_pl = pl["total_ht"]
ecart_brut_raw = round(total_sp - total_pl, 2)

def ecart_ok(e):
    return abs(e) < SEUIL_ECART

def ecart_boutique_ok(e):
    return abs(e) < SEUIL_ECART_BOUTIQUE

# =============================================================
# PREPARE CADRAGE BOUTIQUE DATA
# =============================================================
cadrage_rows = []
for sname, sdata in sp.items():
    sp_ca = sdata["ca_ht"] if sdata["ca_ht"] is not None else 0
    pl_ca = round(sum(l["ca_ht"] for l in pl["lines"] if l.get("store") == sname), 2)
    if sp_ca > 0 or pl_ca != 0:
        ecart_b = round(sp_ca - pl_ca, 2)
        if abs(ecart_b) < SEUIL_ECART_BOUTIQUE:
            ecart_b = 0.0
        is_ok = ecart_boutique_ok(ecart_b)
        cadrage_rows.append({
            "Boutique": sname, "CA HT Shopify": sp_ca, "CA HT Pennylane": pl_ca,
            "Écart": ecart_b, "Justifié": True if is_ok else False, "Commentaire": "",
            "Montant écart justifié": ecart_b if is_ok else 0.0,
        })

unknown_ca = round(sum(l["ca_ht"] for l in pl["lines"] if l.get("store") is None), 2)
if unknown_ca:
    cadrage_rows.append({
        "Boutique": "(INCONNU)", "CA HT Shopify": 0, "CA HT Pennylane": unknown_ca,
        "Écart": round(-unknown_ca, 2), "Justifié": False, "Commentaire": "",
        "Montant écart justifié": 0.0,
    })

if "cadrage_boutique_data" not in st.session_state or run:
    st.session_state["cadrage_boutique_data"] = cadrage_rows

# Écart brut = somme des écarts boutiques déjà neutralisés (arrondis éliminés)
ecart_brut = round(sum(r["Écart"] for r in cadrage_rows), 2)

# Compute écart justifié / résiduel from session state
current_data = st.session_state.get("cadrage_boutique_data", cadrage_rows)
ecart_justifie = round(
    sum(r.get("Montant écart justifié", 0) for r in current_data if not ecart_boutique_ok(r["Écart"]))
    + sum(r["Écart"] for r in current_data if ecart_boutique_ok(r["Écart"])),
    2
)
ecart_residuel = round(ecart_brut - ecart_justifie, 2)
all_justified = all(
    ecart_boutique_ok(r["Écart"]) or r.get("Montant écart justifié", 0) == r["Écart"]
    for r in current_data
)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  1. RÉSULTAT DU CADRAGE                                      ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Résultat du cadrage")

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("CA HT Shopify", f"{total_sp:,.2f} €")
col2.metric("CA HT Pennylane", f"{total_pl:,.2f} €")
col3.metric("Écart brut", f"{ecart_brut:,.2f} €")
col4.metric("Écart justifié", f"{ecart_justifie:,.2f} €")
col5.metric("Écart résiduel", f"{ecart_residuel:,.2f} €")

# Bandeau basé sur l'écart résiduel
if ecart_ok(ecart_brut):
    st.markdown("""
    <div style="background: linear-gradient(135deg, #ECFDF5, #D1FAE5); border: 1px solid #6EE7B7;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #065F46;">✅ Cadrage OK</div>
        <div style="font-size: 0.95rem; color: #047857; margin-top: 0.3rem;">
            Aucun écart significatif sur la période
        </div>
    </div>
    """, unsafe_allow_html=True)
elif all_justified:
    st.markdown(f"""
    <div style="background: linear-gradient(135deg, #F3E8FF, #E9D5FF); border: 1px solid #C084FC;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #6B21A8;">🟣 Cadrage justifié</div>
        <div style="font-size: 0.95rem; color: #7C3AED; margin-top: 0.3rem;">
            Écart brut de {ecart_brut:,.2f} € entièrement justifié — Écart résiduel : {ecart_residuel:,.2f} €
        </div>
    </div>
    """, unsafe_allow_html=True)
else:
    nb_non_justifie = sum(1 for r in current_data if not ecart_boutique_ok(r["Écart"]) and r.get("Montant écart justifié", 0) != r["Écart"])
    st.markdown(f"""
    <div style="background: linear-gradient(135deg, #FEF2F2, #FECACA); border: 1px solid #FCA5A5;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #991B1B;">❌ Écart résiduel : {ecart_residuel:,.2f} €</div>
        <div style="font-size: 0.95rem; color: #B91C1C; margin-top: 0.3rem;">
            {nb_non_justifie} boutique(s) non justifiée(s) — Écart brut : {ecart_brut:,.2f} € / Justifié : {ecart_justifie:,.2f} €
        </div>
    </div>
    """, unsafe_allow_html=True)

# PDF
# PDF placeholder — generated after tabs
pdf_placeholder = st.empty()

# Résultat jour par jour (si multi-jours)
if multi_day:
    st.markdown("**Résultat jour par jour**")

    daily_sp = defaultdict(lambda: {"ca_ht": 0.0, "nb_orders": 0})
    for sdata in sp.values():
        for d, day_data in sdata.get("by_date", {}).items():
            daily_sp[d]["ca_ht"] += day_data["ca_ht"]
            daily_sp[d]["nb_orders"] += day_data["nb_orders"]

    daily_pl = pl["by_date"]
    all_days = sorted(set(list(daily_sp.keys()) + list(daily_pl.keys())))

    daily_rows = []
    for day in all_days:
        sp_day = round(daily_sp[day]["ca_ht"], 2)
        pl_day = round(daily_pl.get(day, {}).get("ca_ht", 0), 2)
        ecart_day = round(sp_day - pl_day, 2)
        if abs(ecart_day) < SEUIL_ECART:
            ecart_day = 0.0
        daily_rows.append({
            "Date": day, "CA HT Shopify": sp_day, "Commandes": daily_sp[day]["nb_orders"],
            "CA HT Pennylane": pl_day, "Écritures PL": daily_pl.get(day, {}).get("nb_ecritures", 0),
            "Écart": ecart_day, "Statut": "✅" if ecart_ok(ecart_day) else f"❌ {ecart_day:+,.2f}€",
        })

    if daily_rows:
        df_daily = pd.DataFrame(daily_rows)
        def highlight_ecart_row(row):
            if not ecart_ok(row["Écart"]):
                return ["background-color: rgba(255, 50, 50, 0.15)"] * len(row)
            return [""] * len(row)
        st.dataframe(df_daily.style.apply(highlight_ecart_row, axis=1), use_container_width=True, hide_index=True)

        nb_jours_ok = len([r for r in daily_rows if ecart_ok(r["Écart"])])
        nb_jours_ko = len([r for r in daily_rows if not ecart_ok(r["Écart"])])
        col_d1, col_d2, col_d3 = st.columns(3)
        col_d1.metric("Jours", f"{len(daily_rows)}")
        col_d2.metric("Jours OK", f"{nb_jours_ok}")
        col_d3.metric("Jours en écart", f"{nb_jours_ko}", delta=f"{nb_jours_ko}" if nb_jours_ko > 0 else None, delta_color="inverse")

        st.write("")
        day_options = ["—"] + [f"{'❌' if not ecart_ok(r['Écart']) else '✅'} {r['Date']}  ({r['Écart']:+,.2f}€)" for r in daily_rows]
        day_keys = [None] + [r["Date"] for r in daily_rows]
        selected_idx = st.selectbox("🔎 Creuser un jour", range(len(day_options)), format_func=lambda i: day_options[i])

        if selected_idx and selected_idx > 0:
            selected_day = day_keys[selected_idx]
            st.markdown(f"#### Détail du {selected_day}")
            day_store_rows = []
            for sname, sdata in sp.items():
                sp_day_store = sdata.get("by_date", {}).get(selected_day, {}).get("ca_ht", 0)
                pl_day_store_filtered = sum(l["ca_ht"] for l in pl["lines"] if l["date"] == selected_day and l.get("store") == sname)
                if sp_day_store or pl_day_store_filtered:
                    day_store_rows.append({"Boutique": sname, "CA HT Shopify": round(sp_day_store, 2), "CA HT Pennylane": round(pl_day_store_filtered, 2), "Écart": round(sp_day_store - pl_day_store_filtered, 2)})
            unknown_day = sum(l["ca_ht"] for l in pl["lines"] if l["date"] == selected_day and l.get("store") is None)
            if unknown_day:
                day_store_rows.append({"Boutique": "(INCONNU)", "CA HT Shopify": 0, "CA HT Pennylane": round(unknown_day, 2), "Écart": round(-unknown_day, 2)})
            if day_store_rows:
                st.dataframe(pd.DataFrame(day_store_rows), use_container_width=True, hide_index=True)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  2. DÉTAIL DU CADRAGE                                         ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Détail du cadrage")

# --- Commande data ---
sp_by_order = {}
for sname, sdata in sp.items():
    for o in sdata.get("orders", []):
        sp_by_order[o["order_name"]] = o

pl_by_order = {}
for l in pl["lines"]:
    ref = l.get("order_ref")
    if ref:
        if ref not in pl_by_order:
            pl_by_order[ref] = {"ca_ht": 0.0, "store": l.get("store"), "invoice": l.get("invoice_number")}
        pl_by_order[ref]["ca_ht"] += l["ca_ht"]

all_orders_list = sorted(set(list(sp_by_order.keys()) + list(pl_by_order.keys())))

order_rows = []
for order in all_orders_list:
    sp_o = sp_by_order.get(order)
    pl_o = pl_by_order.get(order)
    sp_ht = sp_o["ca_ht"] if sp_o else 0
    pl_ht = round(pl_o["ca_ht"], 2) if pl_o else 0
    ecart_o = round(sp_ht - pl_ht, 2)
    if abs(ecart_o) < SEUIL_ECART:
        ecart_o = 0.0
    invoice = pl_o.get("invoice", "") if pl_o else ""
    order_rows.append({
        "Commande": order, "Facture": invoice or "",
        "Boutique": (sp_o["store"] if sp_o else pl_o.get("store")) or "?",
        "CA HT Shopify": sp_ht, "CA HT Pennylane": pl_ht, "Écart": ecart_o,
        "Commentaire": "",
    })

st.markdown("""
<style>
    /* Highlight editable column headers in violet */
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

from cadrage_ca import ACCOUNT_LABELS, COUNTRY_TO_ACCOUNT

# Prepare account data for tab 3
sp_account_data = defaultdict(lambda: {"ca_ht": 0.0, "count": 0})
sp_country_data = defaultdict(lambda: {"ca_ht": 0.0, "count": 0, "compte": ""})
all_sp_orders = []
for sdata in sp.values():
    for o in sdata.get("orders", []):
        all_sp_orders.append(o)
        compte = o.get("compte", "707")
        sp_account_data[compte]["ca_ht"] += o["ca_ht"]
        sp_account_data[compte]["count"] += 1
        cc = o.get("country_code", "FR") or "FR"
        sp_country_data[cc]["ca_ht"] += o["ca_ht"]
        sp_country_data[cc]["count"] += 1
        sp_country_data[cc]["compte"] = compte

pl_account_data = defaultdict(lambda: {"ca_ht": 0.0, "count": 0, "name": ""})
for l in pl["lines"]:
    key = l["account"]
    pl_account_data[key]["ca_ht"] += l["ca_ht"]
    pl_account_data[key]["count"] += 1
    if l.get("account_name") and not pl_account_data[key]["name"]:
        pl_account_data[key]["name"] = l["account_name"]

# Country code to label
COUNTRY_NAMES = {"FR": "France", "DE": "Allemagne", "AT": "Autriche", "BE": "Belgique", "HR": "Croatie",
    "DK": "Danemark", "ES": "Espagne", "GR": "Grèce", "HU": "Hongrie", "IE": "Irlande", "IT": "Italie",
    "LU": "Luxembourg", "NL": "Pays-Bas", "PT": "Portugal", "CZ": "Rép. Tchèque", "RO": "Roumanie",
    "SI": "Slovénie", "SE": "Suède", "GB": "Royaume-Uni", "CH": "Suisse", "US": "États-Unis"}

tab_boutique, tab_compte, tab_commande = st.tabs(["📊 Par boutique", "🌍 Par pays de livraison (compte comptable)", "📋 Par commande"])

# ─── TAB 1 : PAR COMMANDE ───
if "cadrage_commande_data" not in st.session_state or run:
    st.session_state["cadrage_commande_data"] = order_rows

with tab_commande:
    current_orders = st.session_state.get("cadrage_commande_data", order_rows)
    if current_orders:
        df_orders = pd.DataFrame(current_orders)

        def get_statut_commande(row):
            if ecart_ok(row["Écart"]):
                return "✅"
            elif row.get("Montant écart justifié", 0) == row["Écart"]:
                return "🟣"
            elif row.get("Montant écart justifié", 0) != 0:
                return "🟡"
            else:
                return "❌"

        df_orders["Statut"] = df_orders.apply(get_statut_commande, axis=1)
        if "Montant écart justifié" not in df_orders.columns:
            df_orders["Montant écart justifié"] = 0.0
        df_orders["Montant écart restant"] = df_orders.apply(
            lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_ok(r["Écart"]) else 0.0, axis=1)

        col_f1, col_f2 = st.columns(2)
        with col_f1:
            filter_statut = st.radio("Filtrer", ["Toutes", "Écarts seulement"], horizontal=True, key="ca_order_filter")
        with col_f2:
            stores_in_orders = sorted(df_orders[df_orders["Boutique"] != "?"]["Boutique"].unique())
            filter_store = st.selectbox("Boutique", ["Toutes"] + stores_in_orders, key="ca_order_store")

        df_orders_display = df_orders.copy()
        if filter_statut == "Écarts seulement":
            df_orders_display = df_orders_display[~df_orders_display["Écart"].apply(ecart_ok)]
        if filter_store != "Toutes":
            df_orders_display = df_orders_display[df_orders_display["Boutique"] == filter_store]

        display_cols_cmd = ["Boutique", "Commande", "Facture", "CA HT Shopify", "CA HT Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Statut", "Commentaire"]

        edited_orders = st.data_editor(
            df_orders_display[display_cols_cmd],
            use_container_width=True, hide_index=True,
            disabled=["Commande", "Facture", "Boutique", "CA HT Shopify", "CA HT Pennylane", "Écart", "Montant écart restant", "Statut"],
            column_config={
                "Commentaire": st.column_config.TextColumn("✏️ Commentaire", width="large"),
                "CA HT Shopify": st.column_config.NumberColumn(format="%.2f"),
                "CA HT Pennylane": st.column_config.NumberColumn(format="%.2f"),
                "Écart": st.column_config.NumberColumn(format="%.2f"),
                "Montant écart justifié": st.column_config.NumberColumn("✏️ Montant écart justifié", format="%.2f"),
                "Montant écart restant": st.column_config.NumberColumn(format="%.2f"),
            },
            height=min(400, 35 * (len(df_orders_display) + 1)),
        )

        if edited_orders is not None:
            _cmd_changed = False
            for idx, row in edited_orders.iterrows():
                for r in st.session_state["cadrage_commande_data"]:
                    if r["Commande"] == row["Commande"]:
                        if not ecart_ok(r["Écart"]):
                            new_mnt = row.get("Montant écart justifié", 0.0)
                            if r.get("Montant écart justifié", 0.0) != new_mnt:
                                r["Montant écart justifié"] = new_mnt
                                _cmd_changed = True
                        new_comment = row.get("Commentaire", "")
                        if r.get("Commentaire", "") != new_comment:
                            r["Commentaire"] = new_comment
                            _cmd_changed = True
            if _cmd_changed:
                st.rerun()

        nb_ok = len(df_orders[df_orders["Écart"].apply(ecart_ok)])
        nb_ecart_o = len(df_orders[~df_orders["Écart"].apply(ecart_ok)])
        col_s1, col_s2, col_s3 = st.columns(3)
        col_s1.metric("Commandes", f"{len(df_orders)}")
        col_s2.metric("OK", f"{nb_ok}")
        col_s3.metric("En écart", f"{nb_ecart_o}", delta=f"{nb_ecart_o}" if nb_ecart_o > 0 else None, delta_color="inverse")

        df_orders_exp = pd.DataFrame(st.session_state.get("cadrage_commande_data", order_rows))
        if "Montant écart justifié" not in df_orders_exp.columns:
            df_orders_exp["Montant écart justifié"] = 0.0
        df_orders_exp["Montant écart restant"] = df_orders_exp.apply(
            lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_ok(r["Écart"]) else 0.0, axis=1)
        df_orders_exp["Statut"] = df_orders_exp.apply(get_statut_commande, axis=1)
        exp_cols_cmd = [c for c in display_cols_cmd if c in df_orders_exp.columns]
        csv_orders = df_orders_exp[exp_cols_cmd].to_csv(index=False).encode("utf-8")
        st.download_button("📥 Export cadrage par commande (CSV)", csv_orders, f"{TODAY} Cadrage CA Commandes {periode_label}.csv", "text/csv")

# ─── TAB 2 : PAR BOUTIQUE ───
with tab_boutique:
    df_cadrage = pd.DataFrame(st.session_state["cadrage_boutique_data"])

    def get_statut_boutique(row):
        if ecart_boutique_ok(row["Écart"]):
            return "✅"
        elif row.get("Montant écart justifié", 0) == row["Écart"]:
            return "🟣"
        elif row.get("Montant écart justifié", 0) != 0:
            return "🟡"
        else:
            return "❌"

    df_cadrage["Statut"] = df_cadrage.apply(get_statut_boutique, axis=1)
    df_cadrage["Montant écart justifié"] = df_cadrage.apply(
        lambda r: round(r.get("Montant écart justifié", 0.0), 2) if not ecart_boutique_ok(r["Écart"]) else 0.0, axis=1)
    df_cadrage["Montant écart restant"] = df_cadrage.apply(
        lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_boutique_ok(r["Écart"]) else 0.0, axis=1)

    filter_boutique = st.radio("Filtrer", ["Toutes", "Écarts seulement"], horizontal=True, key="cadrage_bout_filter")
    df_cadrage_display = df_cadrage.copy()
    if filter_boutique == "Écarts seulement":
        df_cadrage_display = df_cadrage_display[~df_cadrage_display["Écart"].apply(ecart_boutique_ok)]

    display_cols = ["Boutique", "CA HT Shopify", "CA HT Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Statut", "Commentaire"]

    edited_cadrage = st.data_editor(
        df_cadrage_display[display_cols],
        use_container_width=True, hide_index=True,
        disabled=["Boutique", "CA HT Shopify", "CA HT Pennylane", "Écart", "Montant écart restant", "Statut"],
        column_config={
            "Commentaire": st.column_config.TextColumn("✏️ Commentaire", width="large"),
            "CA HT Shopify": st.column_config.NumberColumn(format="%.2f"),
            "CA HT Pennylane": st.column_config.NumberColumn(format="%.2f"),
            "Écart": st.column_config.NumberColumn(format="%.2f"),
            "Montant écart justifié": st.column_config.NumberColumn("✏️ Montant écart justifié", format="%.2f"),
            "Montant écart restant": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    if edited_cadrage is not None:
        _changed = False
        for idx, row in edited_cadrage.iterrows():
            for r in st.session_state["cadrage_boutique_data"]:
                if r["Boutique"] == row["Boutique"]:
                    if not ecart_boutique_ok(r["Écart"]):
                        new_mnt = row.get("Montant écart justifié", 0.0)
                        if r.get("Montant écart justifié", 0.0) != new_mnt:
                            r["Montant écart justifié"] = new_mnt
                            _changed = True
                    new_comment = row.get("Commentaire", "")
                    if r.get("Commentaire", "") != new_comment:
                        r["Commentaire"] = new_comment
                        _changed = True
        if _changed:
            st.rerun()

    current_bout = st.session_state.get("cadrage_boutique_data", cadrage_rows)
    ecart_brut_b = round(sum(r["Écart"] for r in current_bout), 2)
    ecart_justifie_b = round(
        sum(r.get("Montant écart justifié", 0) for r in current_bout if not ecart_boutique_ok(r["Écart"]))
        + sum(r["Écart"] for r in current_bout if ecart_boutique_ok(r["Écart"])), 2)
    ecart_residuel_b = round(ecart_brut_b - ecart_justifie_b, 2)

    col_t1, col_t2, col_t3, col_t4, col_t5 = st.columns(5)
    col_t1.metric("Total Shopify", f"{df_cadrage['CA HT Shopify'].sum():,.2f} €")
    col_t2.metric("Total Pennylane", f"{df_cadrage['CA HT Pennylane'].sum():,.2f} €")
    col_t3.metric("Écart brut", f"{ecart_brut_b:,.2f} €")
    col_t4.metric("Justifié", f"{ecart_justifie_b:,.2f} €")
    col_t5.metric("Résiduel", f"{ecart_residuel_b:,.2f} €")

    df_cadrage_export = pd.DataFrame(st.session_state.get("cadrage_boutique_data", cadrage_rows))
    df_cadrage_export["Montant écart restant"] = df_cadrage_export.apply(
        lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_boutique_ok(r["Écart"]) else 0.0, axis=1)
    df_cadrage_export["Statut"] = df_cadrage_export.apply(get_statut_boutique, axis=1)
    export_cols_bout = [c for c in display_cols if c in df_cadrage_export.columns]
    csv_cadrage_bout = df_cadrage_export[export_cols_bout].to_csv(index=False).encode("utf-8")
    st.download_button("📥 Export cadrage par boutique (CSV)", csv_cadrage_bout, f"{TODAY} Cadrage CA Boutiques {periode_label}.csv", "text/csv")

# ─── TAB 3 : PAR PAYS DE LIVRAISON (COMPTE COMPTABLE) ───
# Build rows
all_comptes = sorted(set(list(sp_account_data.keys()) + list(pl_account_data.keys())))
cadrage_compte_rows = []
for compte in all_comptes:
    sp_ca = round(sp_account_data[compte]["ca_ht"], 2)
    pl_ca = round(pl_account_data[compte]["ca_ht"], 2)
    ecart_c = round(sp_ca - pl_ca, 2)
    label = pl_account_data[compte]["name"] or ACCOUNT_LABELS.get(compte, "")
    pays = ""
    for cc, acc in COUNTRY_TO_ACCOUNT.items():
        if acc == compte:
            pays = f"{COUNTRY_NAMES.get(cc, cc)} ({cc})"
            break
    if compte == "707":
        pays = "France (FR)"
    elif compte in ("70702", "707020"):
        pays = "Hors UE (Export)"
    elif compte == "707101":
        pays = "UE B2B (Intracom)"
    if abs(ecart_c) < 1.0:
        ecart_c = 0.0
    is_ok = abs(ecart_c) < SEUIL_ECART_BOUTIQUE
    cadrage_compte_rows.append({
        "Pays": pays, "Compte": compte, "Libellé": label,
        "CA HT Shopify": sp_ca, "CA HT Pennylane": pl_ca, "Écart": ecart_c,
        "Montant écart justifié": ecart_c if is_ok else 0.0, "Commentaire": "",
    })

if "cadrage_compte_data" not in st.session_state or run:
    st.session_state["cadrage_compte_data"] = cadrage_compte_rows

with tab_compte:
    current_comptes = st.session_state.get("cadrage_compte_data", cadrage_compte_rows)
    if current_comptes:
        df_comptes = pd.DataFrame(current_comptes)

        def get_statut_compte(row):
            if abs(row["Écart"]) < SEUIL_ECART:
                return "✅"
            elif row.get("Montant écart justifié", 0) == row["Écart"]:
                return "🟣"
            elif row.get("Montant écart justifié", 0) != 0:
                return "🟡"
            else:
                return "❌"

        df_comptes["Statut"] = df_comptes.apply(get_statut_compte, axis=1)
        df_comptes["Montant écart justifié"] = df_comptes.apply(
            lambda r: round(r.get("Montant écart justifié", 0.0), 2) if abs(r["Écart"]) >= SEUIL_ECART else 0.0, axis=1)
        df_comptes["Montant écart restant"] = df_comptes.apply(
            lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if abs(r["Écart"]) >= SEUIL_ECART else 0.0, axis=1)

        filter_compte = st.radio("Filtrer", ["Tous", "Écarts seulement"], horizontal=True, key="cadrage_cpt_filter")
        df_comptes_display = df_comptes.copy()
        if filter_compte == "Écarts seulement":
            df_comptes_display = df_comptes_display[df_comptes_display["Écart"].apply(lambda e: abs(e) >= SEUIL_ECART)]

        display_cols_cpt = ["Pays", "Compte", "Libellé", "CA HT Shopify", "CA HT Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Statut", "Commentaire"]

        edited_comptes = st.data_editor(
            df_comptes_display[display_cols_cpt],
            use_container_width=True, hide_index=True,
            disabled=["Pays", "Compte", "Libellé", "CA HT Shopify", "CA HT Pennylane", "Écart", "Montant écart restant", "Statut"],
            column_config={
                "Commentaire": st.column_config.TextColumn("✏️ Commentaire", width="large"),
                "CA HT Shopify": st.column_config.NumberColumn(format="%.2f"),
                "CA HT Pennylane": st.column_config.NumberColumn(format="%.2f"),
                "Écart": st.column_config.NumberColumn(format="%.2f"),
                "Montant écart justifié": st.column_config.NumberColumn("✏️ Montant écart justifié", format="%.2f"),
                "Montant écart restant": st.column_config.NumberColumn(format="%.2f"),
            },
        )

        if edited_comptes is not None:
            _cpt_changed = False
            for idx, row in edited_comptes.iterrows():
                for r in st.session_state["cadrage_compte_data"]:
                    if r["Compte"] == row["Compte"]:
                        if abs(r["Écart"]) >= SEUIL_ECART:
                            new_mnt = row.get("Montant écart justifié", 0.0)
                            if r.get("Montant écart justifié", 0.0) != new_mnt:
                                r["Montant écart justifié"] = new_mnt
                                _cpt_changed = True
                        new_comment = row.get("Commentaire", "")
                        if r.get("Commentaire", "") != new_comment:
                            r["Commentaire"] = new_comment
                            _cpt_changed = True
            if _cpt_changed:
                st.rerun()

        # Totaux
        col_c1, col_c2, col_c3 = st.columns(3)
        col_c1.metric("Total Shopify", f"{df_comptes['CA HT Shopify'].sum():,.2f} €")
        col_c2.metric("Total Pennylane", f"{df_comptes['CA HT Pennylane'].sum():,.2f} €")
        col_c3.metric("Écart", f"{df_comptes['Écart'].sum():,.2f} €")

        df_comptes_exp = pd.DataFrame(st.session_state.get("cadrage_compte_data", cadrage_compte_rows))
        df_comptes_exp["Montant écart restant"] = df_comptes_exp.apply(
            lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if abs(r["Écart"]) >= SEUIL_ECART else 0.0, axis=1)
        df_comptes_exp["Statut"] = df_comptes_exp.apply(get_statut_compte, axis=1)
        exp_cols_cpt = [c for c in display_cols_cpt if c in df_comptes_exp.columns]
        csv_comptes = df_comptes_exp[exp_cols_cpt].to_csv(index=False).encode("utf-8")
        st.download_button("📥 Export cadrage par pays (CSV)", csv_comptes, f"{TODAY} Cadrage CA Pays {periode_label}.csv", "text/csv")

# ─── PDF GENERATION (after tabs so all data is available) ───
def generate_pdf():
    W = 277  # Largeur utile A4 paysage (297 - 2*10 marges)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    # ─── PAGE 1 : Résultat du cadrage ───
    pdf.add_page("L")
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 14, "AURALIS FINANCES", ln=True, align="C")
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 8, "Rapport de cadrage du Chiffre d'Affaires", ln=True, align="C")
    pdf.cell(0, 8, f"Periode : {date_min_str} au {date_max_str}", ln=True, align="C")
    pdf.cell(0, 8, f"Date d'edition : {TODAY}", ln=True, align="C")
    pdf.ln(15)

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Resultat du cadrage", ln=True)
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 13)
    pdf.cell(0, 9, f"CA HT Shopify :       {total_sp:,.2f} EUR", ln=True)
    pdf.cell(0, 9, f"CA HT Pennylane :     {total_pl:,.2f} EUR", ln=True)
    pdf.cell(0, 9, f"Ecart brut :          {ecart_brut:,.2f} EUR", ln=True)
    pdf.cell(0, 9, f"Ecart justifie :      {ecart_justifie:,.2f} EUR", ln=True)
    pdf.cell(0, 9, f"Ecart residuel :      {ecart_residuel:,.2f} EUR", ln=True)
    pdf.ln(8)

    if ecart_ok(ecart_brut):
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_text_color(6, 95, 70)
        pdf.cell(0, 12, "CADRAGE OK - Aucun ecart significatif", ln=True)
    elif all_justified:
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_text_color(107, 33, 168)
        pdf.cell(0, 12, "CADRAGE JUSTIFIE - Tous les ecarts sont justifies", ln=True)
    else:
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_text_color(153, 27, 27)
        pdf.cell(0, 12, f"ECART RESIDUEL : {ecart_residuel:,.2f} EUR", ln=True)
    pdf.set_text_color(0, 0, 0)

    # ─── PAGE 2 : Par boutique ───
    pdf.add_page("L")
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Detail du cadrage - Par boutique", ln=True)
    pdf.ln(4)

    _bout_data = st.session_state.get("cadrage_boutique_data", cadrage_rows)
    cw_bout = [int(W*0.12), int(W*0.14), int(W*0.14), int(W*0.10), int(W*0.10), int(W*0.10), int(W*0.30)]
    pdf.set_font("Helvetica", "B", 10)
    for i, h in enumerate(["Boutique", "CA Shopify", "CA Pennylane", "Ecart", "Justifie", "Restant", "Commentaire"]):
        pdf.cell(cw_bout[i], 8, h, border=1, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 10)
    for r in _bout_data:
        eb = r["Écart"]
        mj = r.get("Montant écart justifié", 0.0)
        mr = round(eb - mj, 2) if not ecart_boutique_ok(eb) else 0.0
        pdf.cell(cw_bout[0], 7, str(r["Boutique"]), border=1)
        pdf.cell(cw_bout[1], 7, f"{r['CA HT Shopify']:,.2f}", border=1, align="R")
        pdf.cell(cw_bout[2], 7, f"{r['CA HT Pennylane']:,.2f}", border=1, align="R")
        pdf.cell(cw_bout[3], 7, f"{eb:,.2f}", border=1, align="R")
        pdf.cell(cw_bout[4], 7, f"{mj:,.2f}", border=1, align="R")
        pdf.cell(cw_bout[5], 7, f"{mr:,.2f}", border=1, align="R")
        pdf.cell(cw_bout[6], 7, str(r.get("Commentaire", ""))[:50], border=1)
        pdf.ln()
    pdf.set_font("Helvetica", "B", 10)
    t_sp = sum(r["CA HT Shopify"] for r in _bout_data)
    t_pl = sum(r["CA HT Pennylane"] for r in _bout_data)
    t_ec = sum(r["Écart"] for r in _bout_data)
    pdf.cell(cw_bout[0], 8, "TOTAL", border=1)
    pdf.cell(cw_bout[1], 8, f"{t_sp:,.2f}", border=1, align="R")
    pdf.cell(cw_bout[2], 8, f"{t_pl:,.2f}", border=1, align="R")
    pdf.cell(cw_bout[3], 8, f"{t_ec:,.2f}", border=1, align="R")
    pdf.cell(cw_bout[4], 8, "", border=1)
    pdf.cell(cw_bout[5], 8, "", border=1)
    pdf.cell(cw_bout[6], 8, "", border=1)
    pdf.ln()

    # ─── PAGE 3 : Par pays de livraison ───
    pdf.add_page("L")
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Detail du cadrage - Par pays de livraison (compte comptable)", ln=True)
    pdf.ln(4)

    _cpt_data = st.session_state.get("cadrage_compte_data", cadrage_compte_rows)
    cw_cpt = [int(W*0.12), int(W*0.07), int(W*0.18), int(W*0.11), int(W*0.11), int(W*0.09), int(W*0.09), int(W*0.09), int(W*0.14)]
    pdf.set_font("Helvetica", "B", 9)
    for i, h in enumerate(["Pays", "Compte", "Libelle", "CA Shopify", "CA PL", "Ecart", "Justifie", "Restant", "Commentaire"]):
        pdf.cell(cw_cpt[i], 8, h, border=1, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    for r in _cpt_data:
        ec = r["Écart"]
        mj = r.get("Montant écart justifié", 0.0)
        mr = round(ec - mj, 2) if abs(ec) >= SEUIL_ECART else 0.0
        pdf.cell(cw_cpt[0], 7, str(r.get("Pays", ""))[:18], border=1)
        pdf.cell(cw_cpt[1], 7, str(r.get("Compte", "")), border=1)
        pdf.cell(cw_cpt[2], 7, str(r.get("Libellé", ""))[:30], border=1)
        pdf.cell(cw_cpt[3], 7, f"{r['CA HT Shopify']:,.2f}", border=1, align="R")
        pdf.cell(cw_cpt[4], 7, f"{r['CA HT Pennylane']:,.2f}", border=1, align="R")
        pdf.cell(cw_cpt[5], 7, f"{ec:,.2f}", border=1, align="R")
        pdf.cell(cw_cpt[6], 7, f"{mj:,.2f}", border=1, align="R")
        pdf.cell(cw_cpt[7], 7, f"{mr:,.2f}", border=1, align="R")
        pdf.cell(cw_cpt[8], 7, str(r.get("Commentaire", ""))[:22], border=1)
        pdf.ln()
    pdf.set_font("Helvetica", "B", 9)
    t_sp_c = sum(r["CA HT Shopify"] for r in _cpt_data)
    t_pl_c = sum(r["CA HT Pennylane"] for r in _cpt_data)
    t_ec_c = sum(r["Écart"] for r in _cpt_data)
    pdf.cell(cw_cpt[0], 8, "", border=1)
    pdf.cell(cw_cpt[1], 8, "TOTAL", border=1)
    pdf.cell(cw_cpt[2], 8, "", border=1)
    pdf.cell(cw_cpt[3], 8, f"{t_sp_c:,.2f}", border=1, align="R")
    pdf.cell(cw_cpt[4], 8, f"{t_pl_c:,.2f}", border=1, align="R")
    pdf.cell(cw_cpt[5], 8, f"{t_ec_c:,.2f}", border=1, align="R")
    pdf.cell(cw_cpt[6], 8, "", border=1)
    pdf.cell(cw_cpt[7], 8, "", border=1)
    pdf.cell(cw_cpt[8], 8, "", border=1)
    pdf.ln()

    # ─── PAGE 4 : Par commande (écarts uniquement) ───
    pdf.add_page("L")
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Detail du cadrage - Par commande (ecarts uniquement)", ln=True)
    pdf.ln(4)

    _cmd_data = st.session_state.get("cadrage_commande_data", order_rows)
    _cmd_ecarts = [r for r in _cmd_data if abs(r.get("Écart", 0)) >= SEUIL_ECART]
    if _cmd_ecarts:
        cw_cmd = [int(W*0.08), int(W*0.12), int(W*0.14), int(W*0.12), int(W*0.12), int(W*0.10), int(W*0.10), int(W*0.22)]
        pdf.set_font("Helvetica", "B", 10)
        for i, h in enumerate(["Boutique", "Commande", "Facture", "CA Shopify", "CA PL", "Ecart", "Justifie", "Commentaire"]):
            pdf.cell(cw_cmd[i], 8, h, border=1, align="C")
        pdf.ln()
        pdf.set_font("Helvetica", "", 9)
        for r in _cmd_ecarts:
            mj = r.get("Montant écart justifié", 0)
            pdf.cell(cw_cmd[0], 7, str(r.get("Boutique", "")), border=1)
            pdf.cell(cw_cmd[1], 7, str(r.get("Commande", "")), border=1)
            pdf.cell(cw_cmd[2], 7, str(r.get("Facture", ""))[:20], border=1)
            pdf.cell(cw_cmd[3], 7, f"{r.get('CA HT Shopify', 0):,.2f}", border=1, align="R")
            pdf.cell(cw_cmd[4], 7, f"{r.get('CA HT Pennylane', 0):,.2f}", border=1, align="R")
            pdf.cell(cw_cmd[5], 7, f"{r.get('Écart', 0):,.2f}", border=1, align="R")
            pdf.cell(cw_cmd[6], 7, f"{mj:,.2f}", border=1, align="R")
            pdf.cell(cw_cmd[7], 7, str(r.get("Commentaire", ""))[:35], border=1)
            pdf.ln()
    else:
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 10, "Aucun ecart significatif par commande", ln=True)

    return bytes(pdf.output())

pdf_bytes = generate_pdf()
pdf_placeholder.download_button(
    "📄 Rapport de cadrage CA (PDF)",
    pdf_bytes,
    f"{TODAY} Rapport Cadrage CA {periode_label}.pdf",
    "application/pdf",
)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  3. ÉLÉMENTS DE JUSTIFICATION                                 ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Éléments de justification")

# ─── Shopify ───
st.markdown("### CA HT Shopify")

st.markdown("**Par boutique**")
sp_store_rows = [{"Boutique": sname, "CA HT Shopify": sdata["ca_ht"], "Commandes": sdata["nb_orders"]} for sname, sdata in sp.items() if sdata["ca_ht"] is not None and sdata["ca_ht"] > 0]
if sp_store_rows:
    df_sp_stores = pd.DataFrame(sp_store_rows)
    df_sp_stores = pd.concat([df_sp_stores, pd.DataFrame([{"Boutique": "TOTAL", "CA HT Shopify": round(df_sp_stores["CA HT Shopify"].sum(), 2), "Commandes": df_sp_stores["Commandes"].sum()}])], ignore_index=True)
    st.dataframe(df_sp_stores, use_container_width=True, hide_index=True)

st.markdown("**Par pays de livraison (compte comptable)**")
sp_country_rows = []
for cc in sorted(sp_country_data.keys()):
    d = sp_country_data[cc]
    sp_country_rows.append({"Pays": COUNTRY_NAMES.get(cc, cc), "Code": cc, "CA HT Shopify": round(d["ca_ht"], 2), "Commandes": d["count"]})
if sp_country_rows:
    df_sp_country = pd.DataFrame(sp_country_rows)
    df_sp_country = pd.concat([df_sp_country, pd.DataFrame([{"Pays": "TOTAL", "Code": "", "CA HT Shopify": round(df_sp_country["CA HT Shopify"].sum(), 2), "Commandes": df_sp_country["Commandes"].sum()}])], ignore_index=True)
    st.dataframe(df_sp_country, use_container_width=True, hide_index=True)

if all_sp_orders:
    df_sp_export = pd.DataFrame(all_sp_orders).rename(columns={"date": "Date", "order_name": "Commande", "store": "Boutique", "ca_ht": "Montant HT", "country_code": "Pays", "compte": "Compte"})
    export_sp_cols = ["Date", "Boutique", "Commande", "Pays", "Compte", "Montant HT"]
    export_sp_cols = [c for c in export_sp_cols if c in df_sp_export.columns]
    df_sp_export = df_sp_export[export_sp_cols].sort_values(["Date", "Boutique", "Commande"])
    st.download_button("📥 Export commandes Shopify (CSV)", df_sp_export.to_csv(index=False).encode("utf-8"), f"{TODAY} Commandes Shopify {periode_label}.csv", "text/csv")

# ─── Pennylane ───
st.markdown("### CA HT Pennylane")

st.markdown("**Par boutique**")
pl_store_rows = []
for sname in sorted(STORE_PREFIXES.keys()):
    pl_store_ca = round(sum(l["ca_ht"] for l in pl["lines"] if l.get("store") == sname), 2)
    pl_store_count = sum(1 for l in pl["lines"] if l.get("store") == sname)
    if pl_store_ca != 0 or pl_store_count > 0:
        pl_store_rows.append({"Boutique": sname, "CA HT Pennylane": pl_store_ca, "Écritures": pl_store_count})
unknown_pl = round(sum(l["ca_ht"] for l in pl["lines"] if l.get("store") is None), 2)
unknown_count = sum(1 for l in pl["lines"] if l.get("store") is None)
if unknown_pl or unknown_count:
    pl_store_rows.append({"Boutique": "(INCONNU)", "CA HT Pennylane": unknown_pl, "Écritures": unknown_count})
if pl_store_rows:
    df_pl_stores = pd.DataFrame(pl_store_rows)
    df_pl_stores = pd.concat([df_pl_stores, pd.DataFrame([{"Boutique": "TOTAL", "CA HT Pennylane": round(df_pl_stores["CA HT Pennylane"].sum(), 2), "Écritures": df_pl_stores["Écritures"].sum()}])], ignore_index=True)
    st.dataframe(df_pl_stores, use_container_width=True, hide_index=True)

st.markdown("**Par pays de livraison (compte comptable)**")
pl_country_rows = []
for acc, d in sorted(pl_account_data.items()):
    label = d["name"] or ACCOUNT_LABELS.get(acc, "")
    pl_country_rows.append({"Compte": acc, "Libellé": label, "CA HT Pennylane": round(d["ca_ht"], 2), "Écritures": d["count"]})
if pl_country_rows:
    df_pl_country = pd.DataFrame(pl_country_rows)
    df_pl_country = pd.concat([df_pl_country, pd.DataFrame([{"Compte": "TOTAL", "Libellé": "", "CA HT Pennylane": round(df_pl_country["CA HT Pennylane"].sum(), 2), "Écritures": df_pl_country["Écritures"].sum()}])], ignore_index=True)
    st.dataframe(df_pl_country, use_container_width=True, hide_index=True)

if pl["lines"]:
    df_pl_export = pd.DataFrame(pl["lines"]).rename(columns={"date": "Date", "entry_label": "Écriture", "invoice_number": "Facture", "order_ref": "Commande", "store": "Boutique", "account": "Compte", "account_name": "Libellé compte", "ca_ht": "Montant HT"})
    df_pl_export = df_pl_export[["Date", "Boutique", "Compte", "Libellé compte", "Écriture", "Facture", "Commande", "Montant HT"]].sort_values(["Date", "Boutique", "Commande"])
    st.download_button("📥 Export CA Pennylane détaillé (CSV)", df_pl_export.to_csv(index=False).encode("utf-8"), f"{TODAY} CA Pennylane Detail {periode_label}.csv", "text/csv")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  4. SYNTHÈSE DES EXPORTS                                      ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("📦 Synthèse des exports")

export_files = {}
export_files[f"{TODAY} Rapport Cadrage CA {periode_label}.pdf"] = pdf_bytes
if order_rows:
    export_files[f"{TODAY} Cadrage CA Commandes {periode_label}.csv"] = pd.DataFrame(order_rows).to_csv(index=False).encode("utf-8")
if cadrage_rows:
    _df_exp_bout = pd.DataFrame(st.session_state.get("cadrage_boutique_data", cadrage_rows))
    _df_exp_bout["Montant écart restant"] = _df_exp_bout.apply(
        lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_boutique_ok(r["Écart"]) else 0.0, axis=1)
    _exp_cols = ["Boutique", "CA HT Shopify", "CA HT Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Commentaire"]
    _exp_cols = [c for c in _exp_cols if c in _df_exp_bout.columns]
    export_files[f"{TODAY} Cadrage CA Boutiques {periode_label}.csv"] = _df_exp_bout[_exp_cols].to_csv(index=False).encode("utf-8")
if cadrage_compte_rows:
    export_files[f"{TODAY} Cadrage CA Pays {periode_label}.csv"] = df_comptes_exp[exp_cols_cpt].to_csv(index=False).encode("utf-8")
if all_sp_orders:
    export_files[f"{TODAY} Commandes Shopify {periode_label}.csv"] = df_sp_export.to_csv(index=False).encode("utf-8")
if pl["lines"]:
    export_files[f"{TODAY} CA Pennylane Detail {periode_label}.csv"] = df_pl_export.to_csv(index=False).encode("utf-8")

justif_rows = [{"Boutique": r["Boutique"], "Écart": r["Écart"], "Montant écart justifié": r.get("Montant écart justifié", 0.0), "Montant écart restant": round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2), "Commentaire": r.get("Commentaire", "")} for r in current_data if not ecart_boutique_ok(r["Écart"])]
if justif_rows:
    export_files[f"{TODAY} Justification Ecarts {periode_label}.csv"] = pd.DataFrame(justif_rows).to_csv(index=False).encode("utf-8")

cols = st.columns(min(len(export_files), 3))
for i, (fname, fbytes) in enumerate(export_files.items()):
    mime = "application/pdf" if fname.endswith(".pdf") else "text/csv"
    with cols[i % 3]:
        short_name = fname.replace(TODAY, "").replace(periode_label, "").strip()
        short_name = short_name.rsplit(".", 1)[0].strip()
        icon = "📄" if fname.endswith(".pdf") else "📊"
        st.download_button(f"{icon} {short_name}", fbytes, fname, mime, key=f"synth_{i}")

st.write("")
zip_buffer = io.BytesIO()
with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
    for fname, fbytes in export_files.items():
        zf.writestr(fname, fbytes)
zip_buffer.seek(0)

st.download_button(
    "📦 Exporter tout le dossier de travail (ZIP)",
    zip_buffer.getvalue(),
    f"{TODAY} Dossier Travail CA {periode_label}.zip",
    "application/zip",
    type="primary",
    use_container_width=True,
)
