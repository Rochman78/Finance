"""
Interface graphique Streamlit pour le cadrage frais Shopify Payments.
Usage: streamlit run cadrage_app.py
"""

import streamlit as st
import pandas as pd
from datetime import date, timedelta
from cadrage_frais_shopify import get_frais_shopify_detail, get_pennylane_627_detail, STORES

st.set_page_config(page_title="Cadrage Frais Shopify", page_icon="📊", layout="wide")

st.title("📊 Cadrage Frais Shopify Payments")
st.markdown("Comparaison des frais Shopify (API) vs Pennylane (627001 / ENCSP)")

# =============================================================
# SÉLECTION DE PÉRIODE
# =============================================================
col1, col2, col3 = st.columns([1, 1, 2])
with col1:
    date_min = st.date_input("Date début", value=date.today().replace(day=1))
with col2:
    date_max = st.date_input("Date fin", value=date.today() - timedelta(days=1))
with col3:
    st.write("")
    st.write("")
    run = st.button("🚀 Lancer le cadrage", type="primary", use_container_width=True)

# Stocker les résultats en session pour ne pas relancer les API à chaque interaction
if run:
    date_min_str = date_min.strftime("%Y-%m-%d")
    date_max_str = date_max.strftime("%Y-%m-%d")
    with st.spinner("Chargement des frais Shopify..."):
        st.session_state["shopify_data"] = get_frais_shopify_detail(date_min_str, date_max_str)
    with st.spinner("Chargement des écritures Pennylane..."):
        st.session_state["pl_data"] = get_pennylane_627_detail(date_min_str, date_max_str)
    st.session_state["date_min_str"] = date_min_str
    st.session_state["date_max_str"] = date_max_str

if "shopify_data" not in st.session_state:
    st.info("Sélectionnez une période et cliquez sur **Lancer le cadrage**")
    st.stop()

shopify_data = st.session_state["shopify_data"]
pl_data = st.session_state["pl_data"]
date_min_str = st.session_state["date_min_str"]
date_max_str = st.session_state["date_max_str"]

lines = pl_data["lines_627"]

# =============================================================
# CALCULS
# =============================================================
total_shopify     = sum(s["frais"] for s in shopify_data.values() if s["frais"] is not None)
total_pl_shopify  = round(sum(l["net"] for l in lines["shopify"]), 2)
total_pl_mollie   = round(sum(l["net"] for l in lines["mollie"]), 2)
total_pl_klarna   = round(sum(l["net"] for l in lines["klarna"]), 2)
total_pl_ecart    = round(sum(l["net"] for l in lines["ecart"]), 2)
total_pl_autre    = round(sum(l["net"] for l in lines["autre"]), 2)
total_pl_all      = round(total_pl_shopify + total_pl_mollie + total_pl_klarna + total_pl_ecart + total_pl_autre, 2)
ecart_shopify     = round(total_shopify - total_pl_shopify, 2)

# Commandes non matchées
all_shopify_orders = set()
for sdata in shopify_data.values():
    for txn in sdata.get("transactions", []):
        if txn.get("order_name"):
            all_shopify_orders.add(txn["order_name"])
unmatched = all_shopify_orders - pl_data["matched_orders"]

unmatched_details = []
for sname, sdata in shopify_data.items():
    for txn in sdata.get("transactions", []):
        if txn.get("order_name") in unmatched:
            unmatched_details.append({"Boutique": sname, **txn})

# =============================================================
# INDICATEURS CLÉS
# =============================================================
st.markdown("---")
st.subheader("Résultat")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Frais Shopify API", f"{total_shopify:.2f} €")
col2.metric("Frais Pennylane (Shopify)", f"{total_pl_shopify:.2f} €")
col3.metric("Écart", f"{ecart_shopify:.2f} €", delta=f"{ecart_shopify:.2f} €" if ecart_shopify != 0 else None,
            delta_color="inverse" if ecart_shopify != 0 else "off")

if abs(ecart_shopify) < 0.05:
    col4.metric("Statut", "✅ OK")
elif unmatched_details and abs(round(ecart_shopify - sum(u.get("fee", 0) for u in unmatched_details), 2)) < 0.05:
    col4.metric("Statut", "🟡 Expliqué")
else:
    col4.metric("Statut", "❌ Écart")

# =============================================================
# DÉTAIL PAR BOUTIQUE
# =============================================================
st.markdown("---")
st.subheader("Détail par boutique")

rows = []
for sname, sdata in shopify_data.items():
    if sdata["frais"] is not None and sdata["frais"] > 0:
        # Count Pennylane lines for this store
        pl_store_frais = sum(
            l["net"] for l in lines["shopify"]
            if any(prefix in (l.get("order_ref", "") or "") for prefix in
                   {"LFC": ["LFC"], "RED": ["RDC"], "HET": ["HC"], "MTC": ["COCO"],
                    "MO": ["MO"], "RETE": ["RETE", "RET"], "TZ": ["TZ"],
                    "LVO": ["LVO"], "UNIV": ["UNIV"]}.get(sname, []))
        )
        rows.append({
            "Boutique": sname,
            "Frais Shopify": sdata["frais"],
            "Payouts": sdata["nb_payouts"],
            "Transactions": len(sdata["transactions"]),
            "Frais Pennylane": round(pl_store_frais, 2),
            "Écart": round(sdata["frais"] - pl_store_frais, 2),
        })

if rows:
    df_boutiques = pd.DataFrame(rows)
    # Add total row
    total_row = {
        "Boutique": "TOTAL",
        "Frais Shopify": df_boutiques["Frais Shopify"].sum(),
        "Payouts": df_boutiques["Payouts"].sum(),
        "Transactions": df_boutiques["Transactions"].sum(),
        "Frais Pennylane": df_boutiques["Frais Pennylane"].sum(),
        "Écart": df_boutiques["Écart"].sum(),
    }
    df_boutiques = pd.concat([df_boutiques, pd.DataFrame([total_row])], ignore_index=True)
    st.dataframe(df_boutiques, use_container_width=True, hide_index=True)

# =============================================================
# DÉCOMPOSITION PENNYLANE
# =============================================================
st.markdown("---")
st.subheader("Décomposition Pennylane 627001 / ENCSP")

decomp = []
decomp.append({"Catégorie": "Shopify (commandes matchées)", "Montant": total_pl_shopify, "Lignes": len(lines["shopify"])})
if total_pl_mollie:
    decomp.append({"Catégorie": "Mollie", "Montant": total_pl_mollie, "Lignes": len(lines["mollie"])})
if total_pl_klarna:
    decomp.append({"Catégorie": "Klarna", "Montant": total_pl_klarna, "Lignes": len(lines["klarna"])})
if total_pl_ecart:
    decomp.append({"Catégorie": "Écarts / Ajustements", "Montant": total_pl_ecart, "Lignes": len(lines["ecart"])})
if total_pl_autre:
    decomp.append({"Catégorie": "Autre", "Montant": total_pl_autre, "Lignes": len(lines["autre"])})
decomp.append({"Catégorie": "TOTAL 627001", "Montant": total_pl_all, "Lignes": sum(len(v) for v in lines.values())})

df_decomp = pd.DataFrame(decomp)
st.dataframe(df_decomp, use_container_width=True, hide_index=True)

# =============================================================
# COMMANDES NON MATCHÉES
# =============================================================
if unmatched_details:
    st.markdown("---")
    st.subheader(f"⚠️ Commandes non matchées ({len(unmatched_details)})")

    rows_um = []
    for u in unmatched_details:
        rows_um.append({
            "Boutique": u["Boutique"],
            "Commande": u.get("order_name", "?"),
            "Client": u.get("customer_name", "?"),
            "Date payout": u.get("payout_date", "?"),
            "Montant": u.get("amount", 0),
            "Frais": u.get("fee", 0),
        })
    df_unmatched = pd.DataFrame(rows_um)
    st.dataframe(df_unmatched, use_container_width=True, hide_index=True)

    total_unmatched_fees = sum(u.get("fee", 0) for u in unmatched_details)
    ecart_residuel = round(ecart_shopify - total_unmatched_fees, 2)

    if abs(ecart_residuel) < 0.05:
        st.success(f"Écart de {ecart_shopify:.2f}€ **totalement expliqué** par {len(unmatched_details)} commande(s) non matchée(s) ({total_unmatched_fees:.2f}€ de frais)")
    else:
        st.warning(f"Écart résiduel après explication : {ecart_residuel:.2f}€")

# =============================================================
# LIGNES 471 EXISTANTES
# =============================================================
if pl_data["ecart_471"]:
    st.markdown("---")
    st.subheader("Lignes 471 (compte d'attente)")

    rows_471 = []
    for e in pl_data["ecart_471"]:
        rows_471.append({
            "Date": e["date"],
            "Écriture": e["entry_label"],
            "Libellé": e["label"],
            "Débit": e["debit"],
            "Crédit": e["credit"],
        })
    df_471 = pd.DataFrame(rows_471)
    st.dataframe(df_471, use_container_width=True, hide_index=True)

# =============================================================
# DÉTAIL LIGNE À LIGNE — Shopify vs Pennylane
# =============================================================
st.markdown("---")
st.subheader("🔍 Détail ligne à ligne — Shopify vs Pennylane")

# Build Shopify index by order_name
sp_by_order = {}
for sname, sdata in shopify_data.items():
    for txn in sdata.get("transactions", []):
        oname = txn.get("order_name")
        if oname:
            sp_by_order[oname] = {
                "store": sname,
                "fee": txn["fee"],
                "amount": txn["amount"],
                "customer": txn.get("customer_name", "?"),
                "payout_date": txn.get("payout_date", "?"),
            }

# Build Pennylane index by order_ref (sum if multiple lines for same order)
pl_by_order = {}
for line in lines["shopify"]:
    ref = line.get("order_ref")
    if ref:
        if ref not in pl_by_order:
            pl_by_order[ref] = {"fee": 0, "label": line["label"], "date": line["date"]}
        pl_by_order[ref]["fee"] += line["net"]
# Also include other categories that have order refs
for cat in ["mollie", "klarna", "ecart", "autre"]:
    for line in lines[cat]:
        ref = line.get("order_ref")
        if ref:
            if ref not in pl_by_order:
                pl_by_order[ref] = {"fee": 0, "label": line["label"], "date": line["date"], "cat": cat}
            pl_by_order[ref]["fee"] += line["net"]

# Merge all orders
all_orders = sorted(set(list(sp_by_order.keys()) + list(pl_by_order.keys())))

detail_rows = []
for order in all_orders:
    sp = sp_by_order.get(order)
    plr = pl_by_order.get(order)

    sp_fee = sp["fee"] if sp else 0
    pl_fee = round(plr["fee"], 2) if plr else 0
    ecart_line = round(sp_fee - pl_fee, 2)
    store = sp["store"] if sp else "?"
    client = sp["customer"] if sp else plr["label"].split(" - ")[0] if plr else "?"
    payout_date = sp["payout_date"] if sp else (plr["date"] if plr else "?")

    detail_rows.append({
        "Commande": order,
        "Boutique": store,
        "Client": client,
        "Date payout": payout_date,
        "Frais Shopify": sp_fee,
        "Frais Pennylane": pl_fee,
        "Écart": ecart_line,
        "Statut": "✅" if ecart_line == 0 else f"⚠️ {ecart_line:+.2f}€",
    })

if detail_rows:
    df_lines = pd.DataFrame(detail_rows)

    # Filters
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        filter_statut = st.radio("Filtrer", ["Toutes", "Écarts seulement"], horizontal=True)
    with col_f2:
        filter_store = st.selectbox("Boutique", ["Toutes"] + sorted(set(r["Boutique"] for r in detail_rows if r["Boutique"] != "?")))

    df_display = df_lines.copy()
    if filter_statut == "Écarts seulement":
        df_display = df_display[df_display["Écart"] != 0]
    if filter_store != "Toutes":
        df_display = df_display[df_display["Boutique"] == filter_store]

    # Color styling
    def color_ecart(val):
        if val == 0:
            return "color: green"
        return "color: red; font-weight: bold"

    st.dataframe(
        df_display.style.map(color_ecart, subset=["Écart"]),
        use_container_width=True,
        hide_index=True,
        height=min(400, 35 * (len(df_display) + 1)),
    )

    # Summary under table
    nb_ok = len(df_lines[df_lines["Écart"] == 0])
    nb_ecart = len(df_lines[df_lines["Écart"] != 0])
    total_sp_detail = df_lines["Frais Shopify"].sum()
    total_pl_detail = df_lines["Frais Pennylane"].sum()

    col_s1, col_s2, col_s3, col_s4 = st.columns(4)
    col_s1.metric("Lignes OK", f"{nb_ok}")
    col_s2.metric("Lignes en écart", f"{nb_ecart}")
    col_s3.metric("Total Shopify", f"{total_sp_detail:.2f} €")
    col_s4.metric("Total Pennylane", f"{total_pl_detail:.2f} €")

# =============================================================
# EXPORT CSV
# =============================================================
st.markdown("---")
st.subheader("Export")

col1, col2, col3 = st.columns(3)

with col1:
    if rows:
        csv_boutiques = df_boutiques.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Détail boutiques (CSV)",
            csv_boutiques,
            f"cadrage_shopify_{date_min_str}_{date_max_str}.csv",
            "text/csv",
        )

with col2:
    if detail_rows:
        csv_lines = df_lines.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Ligne à ligne SP vs PL (CSV)",
            csv_lines,
            f"ligne_a_ligne_{date_min_str}_{date_max_str}.csv",
            "text/csv",
        )

with col3:
    all_lines_data = []
    for cat, cat_lines in lines.items():
        for l in cat_lines:
            all_lines_data.append({
                "Catégorie": cat,
                "Date": l["date"],
                "Écriture": l["entry_label"],
                "Libellé ligne": l["label"],
                "Débit": l["debit"],
                "Crédit": l["credit"],
                "Net": l["net"],
                "Réf commande": l.get("order_ref", ""),
            })
    if all_lines_data:
        df_detail = pd.DataFrame(all_lines_data)
        csv_detail = df_detail.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Détail lignes 627001 (CSV)",
            csv_detail,
            f"detail_627001_{date_min_str}_{date_max_str}.csv",
            "text/csv",
        )
