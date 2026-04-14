import streamlit as st
import pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime
from etat_recap_tva import get_etat_recap
from ui_common import setup_page

setup_page()

TODAY = datetime.now().strftime("%Y-%m-%d")

st.title("📋 État récapitulatif de TVA")
st.markdown("Décomposition du compte **707101** par facture — Export ProDouane")

# =============================================================
# SÉLECTION DU MOIS
# =============================================================
col1, col2 = st.columns([1, 3])
with col1:
    mois_options = [
        "Janvier 2026", "Février 2026", "Mars 2026", "Avril 2026",
        "Mai 2026", "Juin 2026", "Juillet 2026", "Août 2026",
        "Septembre 2026", "Octobre 2026", "Novembre 2026", "Décembre 2026",
    ]
    mois_idx = st.selectbox("Mois", range(len(mois_options)), format_func=lambda i: mois_options[i], index=1)
    year = 2026
    month = mois_idx + 1
with col2:
    st.write("")
    st.write("")
    run = st.button("🚀 Générer l'état", type="primary", use_container_width=True)

if run:
    with st.spinner(f"Chargement du compte 707101 — {mois_options[mois_idx]}..."):
        st.session_state["recap_tva_data"] = get_etat_recap(year, month)
        st.session_state["recap_tva_mois"] = mois_options[mois_idx]

if "recap_tva_data" not in st.session_state:
    st.info("Sélectionnez un mois et cliquez sur **Générer l'état**")
    st.stop()

data = st.session_state["recap_tva_data"]
mois_label = st.session_state["recap_tva_mois"]
lines = data["lines"]
total_ht = data["total_ht"]

# =============================================================
# RÉSULTAT
# =============================================================
st.markdown("---")
st.subheader("Résultat")

col1, col2, col3 = st.columns(3)
col1.metric("Total HT (707101)", f"{total_ht:,.2f} €")
col2.metric("Nombre de factures", f"{data['nb_lignes']}")
nb_avec_tva = sum(1 for l in lines if l.get("vat_number"))
col3.metric("Avec n° TVA intracom", f"{nb_avec_tva}")

st.markdown(f"""
<div style="background: linear-gradient(135deg, #EDE9FE, #DDD6FE); border: 1px solid #C4B5FD;
            border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
    <div style="font-size: 1.2rem; font-weight: 800; color: #4C1D95;">Montant à déclarer — {mois_label}</div>
    <div style="font-size: 2rem; font-weight: 800; color: #6D28D9; margin-top: 0.5rem;">{total_ht:,.2f} €</div>
</div>
""", unsafe_allow_html=True)

# =============================================================
# TABLEAU DÉTAILLÉ
# =============================================================
st.markdown("---")
st.subheader("Détail par facture")

if lines:
    df = pd.DataFrame(lines)
    display_cols = ["date", "invoice_number", "client_name", "vat_number", "montant_ht", "regime"]
    df_display = df[display_cols].copy()
    df_display.columns = ["Date", "Facture", "Client", "N° TVA Intracom", "Montant HT", "Régime"]

    def highlight_avoirs(row):
        if row["Montant HT"] < 0:
            return ["background-color: rgba(239, 68, 68, 0.12); color: #991B1B;"] * len(row)
        return [""] * len(row)

    st.dataframe(
        df_display.style.apply(highlight_avoirs, axis=1),
        use_container_width=True,
        hide_index=True,
        height=min(600, 35 * (len(df_display) + 1)),
        column_config={
            "Montant HT": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    # Synthèse par régime
    col_s1, col_s2 = st.columns(2)
    ventes = [l for l in lines if l["regime"] == 21]
    avoirs = [l for l in lines if l["regime"] == 25]
    col_s1.metric("Ventes (régime 21)", f"{sum(l['montant_ht'] for l in ventes):,.2f} € ({len(ventes)} lignes)")
    if avoirs:
        col_s2.metric("Avoirs (régime 25)", f"{sum(l['montant_ht'] for l in avoirs):,.2f} € ({len(avoirs)} lignes)")

    # Export CSV détaillé
    csv_detail = df_display.to_csv(index=False).encode("utf-8")
    st.download_button("📥 Export détail (CSV)", csv_detail, f"{TODAY} Etat Recap TVA {mois_label}.csv", "text/csv")

else:
    st.info("Aucune ligne trouvée sur le compte 707101 pour ce mois")

# =============================================================
# EXPORT PRODOUANE
# =============================================================
st.markdown("---")
st.subheader("🇫🇷 Export ProDouane")

if lines:
    # Filtrer : uniquement les lignes avec un n° de TVA intracom
    prodouane_lines = [l for l in lines if l.get("vat_number")]

    if prodouane_lines:
        st.caption(f"{len(prodouane_lines)} ligne(s) avec n° TVA intracommunautaire")

        prodouane_rows = []
        for l in prodouane_lines:
            prodouane_rows.append({
                "REGIME": l["regime"],
                "VALEUR": int(round(abs(l["montant_ht"]), 0)),
                "NUMERO TVA": l["vat_number"],
            })

        df_prodouane = pd.DataFrame(prodouane_rows)
        st.dataframe(df_prodouane, use_container_width=True, hide_index=True)

        # Export CSV sans en-tête
        csv_prodouane = df_prodouane.to_csv(index=False, header=False).encode("utf-8")
        st.download_button(
            "📥 Export ProDouane (CSV)",
            csv_prodouane,
            f"{TODAY} ProDouane {mois_label}.csv",
            "text/csv",
            type="primary",
        )

        # Lignes sans TVA (pour info)
        sans_tva = [l for l in lines if not l.get("vat_number")]
        if sans_tva:
            st.caption(f"⚠️ {len(sans_tva)} ligne(s) sans n° TVA intracom — non incluses dans l'export ProDouane")
    else:
        st.warning("Aucune ligne avec n° TVA intracommunautaire trouvée")
else:
    st.info("Lancez d'abord la génération de l'état")
