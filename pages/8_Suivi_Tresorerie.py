import streamlit as st
import pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui_common import setup_page
from tresorerie import get_tresorerie

setup_page()

st.title("💰 Trésorerie")
st.markdown("Vue en temps réel de la trésorerie disponible")

# Chargement manuel
run = st.button("🚀 Charger la trésorerie", type="primary", use_container_width=True)
if run:
    with st.spinner("Chargement de la trésorerie..."):
        st.session_state["treso_data"] = get_tresorerie()

if "treso_data" not in st.session_state:
    st.info("Cliquez sur **Charger la trésorerie** pour afficher les données")
    st.stop()

data = st.session_state["treso_data"]
banque = data["banque"]
transit = data["transit"]
shopify = data["shopify"]
total = data["tresorerie_reelle"]

# ╔═══════════════════════════════════════════════════════════════╗
# ║  SYNTHÈSE                                                     ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")

st.markdown(f"""
<div style="background: linear-gradient(135deg, #EDE9FE, #DDD6FE); border: 1px solid #C4B5FD;
            border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 0 0 1.5rem 0;">
    <div style="font-size: 1rem; font-weight: 600; color: #4C1D95;">Trésorerie réelle</div>
    <div style="font-size: 2.5rem; font-weight: 800; color: #6D28D9; margin-top: 0.3rem;">{total:,.2f} €</div>
</div>
""", unsafe_allow_html=True)

# Tableau synthèse
synthese = pd.DataFrame([{
    "Banque (512)": banque["total"],
    "En transit (411INTERNET)": transit["total"],
    "Shopify à verser": shopify["total"],
    "Trésorerie réelle": total,
}])

st.dataframe(synthese, use_container_width=True, hide_index=True,
    column_config={
        "Banque (512)": st.column_config.NumberColumn(format="%.2f"),
        "En transit (411INTERNET)": st.column_config.NumberColumn(format="%.2f"),
        "Shopify à verser": st.column_config.NumberColumn(format="%.2f"),
        "Trésorerie réelle": st.column_config.NumberColumn(format="%.2f"),
    })

# ╔═══════════════════════════════════════════════════════════════╗
# ║  DÉTAIL BANQUE (512)                                           ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader(f"🏦 Comptes bancaires — {banque['total']:,.2f} €")

if banque["accounts"]:
    df_banque = pd.DataFrame(banque["accounts"])
    df_banque.columns = ["Compte", "Libellé", "Solde"]
    df_banque = df_banque.sort_values("Solde", ascending=False)

    # Total
    total_row = pd.DataFrame([{"Compte": "TOTAL", "Libellé": "", "Solde": banque["total"]}])
    df_banque = pd.concat([df_banque, total_row], ignore_index=True)

    def hl_total(row):
        if row["Compte"] == "TOTAL":
            return ["font-weight: bold"] * len(row)
        return [""] * len(row)

    st.dataframe(df_banque.style.apply(hl_total, axis=1), use_container_width=True, hide_index=True,
        column_config={"Solde": st.column_config.NumberColumn(format="%.2f")})
else:
    st.info("Aucun compte 512 avec solde")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  DÉTAIL TRANSIT (411INTERNET)                                  ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader(f"⏳ Encaissements en transit — {transit['total']:,.2f} €")
st.caption("Lignes 411INTERNET / ENCSP non lettrées, date >= aujourd'hui")

if transit["lines"]:
    df_transit = pd.DataFrame(transit["lines"])
    df_transit.columns = ["Date", "Libellé", "Montant"]
    df_transit = df_transit.sort_values("Date")

    total_row = pd.DataFrame([{"Date": "TOTAL", "Libellé": "", "Montant": transit["total"]}])
    df_transit = pd.concat([df_transit, total_row], ignore_index=True)

    def hl_total_t(row):
        if row["Date"] == "TOTAL":
            return ["font-weight: bold"] * len(row)
        return [""] * len(row)

    st.dataframe(df_transit.style.apply(hl_total_t, axis=1), use_container_width=True, hide_index=True,
        column_config={"Montant": st.column_config.NumberColumn(format="%.2f")})
else:
    st.info("Aucun encaissement en transit")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  DÉTAIL SHOPIFY (À VERSER)                                    ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader(f"🛒 Shopify Payments à verser — {shopify['total']:,.2f} €")

if shopify["stores"]:
    df_shopify = pd.DataFrame(shopify["stores"])
    df_shopify.columns = ["Boutique", "Montant", "Devise"]

    total_row = pd.DataFrame([{"Boutique": "TOTAL", "Montant": shopify["total"], "Devise": "EUR"}])
    df_shopify = pd.concat([df_shopify, total_row], ignore_index=True)

    def hl_total_s(row):
        if row["Boutique"] == "TOTAL":
            return ["font-weight: bold"] * len(row)
        return [""] * len(row)

    st.dataframe(df_shopify.style.apply(hl_total_s, axis=1), use_container_width=True, hide_index=True,
        column_config={"Montant": st.column_config.NumberColumn(format="%.2f")})
else:
    st.info("Aucun solde Shopify à verser")
