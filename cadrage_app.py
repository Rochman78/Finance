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

if not run:
    st.info("Sélectionnez une période et cliquez sur **Lancer le cadrage**")
    st.stop()

date_min_str = date_min.strftime("%Y-%m-%d")
date_max_str = date_max.strftime("%Y-%m-%d")

# =============================================================
# CHARGEMENT DES DONNÉES
# =============================================================
with st.spinner("Chargement des frais Shopify..."):
    shopify_data = get_frais_shopify_detail(date_min_str, date_max_str)

with st.spinner("Chargement des écritures Pennylane..."):
    pl_data = get_pennylane_627_detail(date_min_str, date_max_str)

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
# EXPORT CSV
# =============================================================
st.markdown("---")
st.subheader("Export")

col1, col2 = st.columns(2)

with col1:
    if rows:
        csv_boutiques = df_boutiques.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Télécharger détail boutiques (CSV)",
            csv_boutiques,
            f"cadrage_shopify_{date_min_str}_{date_max_str}.csv",
            "text/csv",
        )

with col2:
    # Full detail export
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
            "📥 Télécharger détail lignes 627001 (CSV)",
            csv_detail,
            f"detail_627001_{date_min_str}_{date_max_str}.csv",
            "text/csv",
        )
