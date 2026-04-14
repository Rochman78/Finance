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
    mois_idx = st.selectbox("Mois", range(len(mois_options)), format_func=lambda i: mois_options[i], index=2)
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

# Store lines in session state for editing
if "recap_tva_lines" not in st.session_state or run:
    st.session_state["recap_tva_lines"] = lines

current_lines = st.session_state.get("recap_tva_lines", lines)

nb_total = len(current_lines)
nb_avec_tva = sum(1 for l in current_lines if l.get("vat_number"))
nb_sans_tva = nb_total - nb_avec_tva

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total HT (707101)", f"{total_ht:,.2f} €")
col2.metric("Nombre de factures", f"{nb_total}")
col3.metric("Avec n° TVA intracom", f"{nb_avec_tva}")
col4.metric("Sans n° TVA", f"{nb_sans_tva}")

st.markdown(f"""
<div style="background: linear-gradient(135deg, #EDE9FE, #DDD6FE); border: 1px solid #C4B5FD;
            border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
    <div style="font-size: 1.2rem; font-weight: 800; color: #4C1D95;">Montant à déclarer — {mois_label}</div>
    <div style="font-size: 2rem; font-weight: 800; color: #6D28D9; margin-top: 0.5rem;">{total_ht:,.2f} €</div>
</div>
""", unsafe_allow_html=True)

if nb_sans_tva > 0:
    st.markdown(f"""
    <div style="background: linear-gradient(135deg, #FEF2F2, #FECACA); border: 1px solid #FCA5A5;
                border-radius: 12px; padding: 1rem 1.5rem; text-align: center; margin: 0.5rem 0;">
        <div style="font-size: 1.1rem; font-weight: 700; color: #991B1B;">⚠️ {nb_sans_tva} facture(s) sans n° TVA intracommunautaire</div>
        <div style="font-size: 0.85rem; color: #B91C1C; margin-top: 0.3rem;">
            Complétez les informations manquantes dans le tableau ci-dessous pour pouvoir exporter vers ProDouane
        </div>
    </div>
    """, unsafe_allow_html=True)

# =============================================================
# TABLEAU DÉTAILLÉ (éditable)
# =============================================================
st.markdown("---")
st.subheader("Détail par facture")

st.markdown("""
<style>
    [data-testid="stDataEditor"] [data-testid="column-header"]:has(span[title*="Client"]),
    [data-testid="stDataEditor"] [data-testid="column-header"]:has(span[title*="TVA Intracom"]) {
        background-color: rgba(139, 92, 246, 0.18) !important;
        border-bottom: 3px solid rgba(139, 92, 246, 0.5) !important;
    }
    [data-testid="stDataEditor"] th:has(span[title*="Client"]),
    [data-testid="stDataEditor"] th:has(span[title*="TVA Intracom"]) {
        background-color: rgba(139, 92, 246, 0.18) !important;
        border-bottom: 3px solid rgba(139, 92, 246, 0.5) !important;
    }
</style>
""", unsafe_allow_html=True)

if current_lines:
    df_edit = pd.DataFrame(current_lines)
    display_cols = ["date", "invoice_number", "client_name", "vat_number", "montant_ht", "regime"]
    df_edit_display = df_edit[display_cols].copy()
    df_edit_display.columns = ["Date", "Facture", "✏️ Client", "✏️ N° TVA Intracom", "Montant HT", "Régime"]

    edited = st.data_editor(
        df_edit_display,
        use_container_width=True,
        hide_index=True,
        height=min(600, 35 * (len(df_edit_display) + 1)),
        disabled=["Date", "Facture", "Montant HT", "Régime"],
        column_config={
            "Montant HT": st.column_config.NumberColumn(format="%.2f"),
            "✏️ Client": st.column_config.TextColumn(width="medium"),
            "✏️ N° TVA Intracom": st.column_config.TextColumn(width="medium"),
        },
    )

    # Sync edits back to session state
    if edited is not None:
        _changed = False
        for idx, row in edited.iterrows():
            if idx < len(st.session_state["recap_tva_lines"]):
                r = st.session_state["recap_tva_lines"][idx]
                new_name = row.get("✏️ Client", "")
                new_vat = row.get("✏️ N° TVA Intracom", "")
                if r.get("client_name", "") != new_name:
                    r["client_name"] = new_name
                    _changed = True
                if r.get("vat_number", "") != new_vat:
                    r["vat_number"] = new_vat
                    _changed = True
        if _changed:
            st.rerun()

    # Synthèse par régime
    col_s1, col_s2 = st.columns(2)
    ventes = [l for l in current_lines if l["regime"] == 21]
    avoirs = [l for l in current_lines if l["regime"] == 25]
    col_s1.metric("Ventes (régime 21)", f"{sum(l['montant_ht'] for l in ventes):,.2f} € ({len(ventes)} lignes)")
    if avoirs:
        col_s2.metric("Avoirs (régime 25)", f"{sum(l['montant_ht'] for l in avoirs):,.2f} € ({len(avoirs)} lignes)")

    # Export CSV détaillé (from session state)
    df_exp = pd.DataFrame(current_lines)[["date", "invoice_number", "client_name", "vat_number", "montant_ht", "regime"]]
    df_exp.columns = ["Date", "Facture", "Client", "N° TVA Intracom", "Montant HT", "Régime"]
    csv_detail = df_exp.to_csv(index=False).encode("utf-8")
    st.download_button("📥 Export détail (CSV)", csv_detail, f"{TODAY} Etat Recap TVA {mois_label}.csv", "text/csv")

else:
    st.info("Aucune ligne trouvée sur le compte 707101 pour ce mois")

# =============================================================
# EXPORT PRODOUANE
# =============================================================
st.markdown("---")
st.subheader("🇫🇷 Export ProDouane")

if current_lines:
    # Utiliser les données éditées (session state)
    prodouane_lines = [l for l in current_lines if l.get("vat_number")]
    sans_tva = [l for l in current_lines if not l.get("vat_number")]

    if prodouane_lines:
        prodouane_rows = []
        for l in prodouane_lines:
            prodouane_rows.append({
                "REGIME": l["regime"],
                "VALEUR": int(round(abs(l["montant_ht"]), 0)),
                "NUMERO TVA": l["vat_number"],
            })

        df_prodouane = pd.DataFrame(prodouane_rows)

        def highlight_avoirs_prodouane(row):
            if row["REGIME"] == 25:
                return ["background-color: rgba(239, 68, 68, 0.15); color: #991B1B;"] * len(row)
            return [""] * len(row)

        st.dataframe(df_prodouane.style.apply(highlight_avoirs_prodouane, axis=1), use_container_width=True, hide_index=True)

        st.caption(f"{len(prodouane_lines)} ligne(s) dans l'export")

        if sans_tva:
            st.warning(f"⚠️ {len(sans_tva)} ligne(s) sans n° TVA — complétez-les dans le tableau ci-dessus pour les inclure")

        # Export CSV sans en-tête
        csv_prodouane = df_prodouane.to_csv(index=False, header=False).encode("utf-8")
        st.download_button(
            "📥 Export ProDouane (CSV)",
            csv_prodouane,
            f"{TODAY} ProDouane {mois_label}.csv",
            "text/csv",
            type="primary",
        )
    else:
        st.warning("Aucune ligne avec n° TVA — complétez les informations dans le tableau ci-dessus")
else:
    st.info("Lancez d'abord la génération de l'état")
