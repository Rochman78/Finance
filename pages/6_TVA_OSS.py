import streamlit as st
import pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime
from tva_oss import get_controle_base_taux
from ui_common import setup_page

setup_page()

TODAY = datetime.now().strftime("%Y-%m-%d")
SEUIL_ECART = 0.05

st.title("🇪🇺 TVA OSS")
st.markdown("Aide à la déclaration TVA One-Stop-Shop")

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
    run = st.button("🚀 Lancer le contrôle", type="primary", use_container_width=True)

if run:
    with st.spinner(f"Contrôle Base × Taux — {mois_options[mois_idx]}..."):
        st.session_state["oss_data"] = get_controle_base_taux(year, month)
        st.session_state["oss_mois"] = mois_options[mois_idx]

if "oss_data" not in st.session_state:
    st.info("Sélectionnez un mois et cliquez sur **Lancer le contrôle**")
    st.stop()

data = st.session_state["oss_data"]
mois_label = st.session_state["oss_mois"]
rows = data["rows"]

# ╔═══════════════════════════════════════════════════════════════╗
# ║  1. RÉSULTAT                                                   ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Contrôle Base × Taux")

col1, col2, col3, col4 = st.columns(4)
col1.metric("CA HT OSS", f"{data['total_ca']:,.2f} €")
col2.metric("TVA collectée", f"{data['total_tva']:,.2f} €")
col3.metric("TVA théorique", f"{data['total_theo']:,.2f} €")
col4.metric("Écart total", f"{data['total_ecart']:,.2f} €")

if abs(data["total_ecart"]) < SEUIL_ECART:
    st.markdown("""
    <div style="background: linear-gradient(135deg, #ECFDF5, #D1FAE5); border: 1px solid #6EE7B7;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #065F46;">✅ Contrôle OK</div>
        <div style="font-size: 0.95rem; color: #047857; margin-top: 0.3rem;">
            Base × Taux = TVA collectée — aucun écart significatif
        </div>
    </div>
    """, unsafe_allow_html=True)
else:
    nb_ecarts = sum(1 for r in rows if abs(r["ecart"]) >= SEUIL_ECART)
    st.markdown(f"""
    <div style="background: linear-gradient(135deg, #FEF2F2, #FECACA); border: 1px solid #FCA5A5;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #991B1B;">⚠️ {nb_ecarts} pays en écart</div>
        <div style="font-size: 0.95rem; color: #B91C1C; margin-top: 0.3rem;">
            Écart total : {data['total_ecart']:,.2f} € — Vérifiez les taux appliqués
        </div>
    </div>
    """, unsafe_allow_html=True)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  2. DÉTAIL PAR PAYS                                            ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader(f"Détail par pays — {mois_label}")

if rows:
    df = pd.DataFrame(rows)
    df_display = df[["pays", "code", "taux", "compte_produit", "compte_tva", "ca_ht", "tva_theorique", "tva_collectee", "ecart"]].copy()
    df_display.columns = ["Pays", "Code", "Taux (%)", "Compte produit", "Compte TVA", "CA HT", "TVA théorique", "TVA collectée", "Écart"]

    # Total row
    total_row = pd.DataFrame([{
        "Pays": "TOTAL", "Code": "", "Taux (%)": "",
        "Compte produit": "", "Compte TVA": "",
        "CA HT": data["total_ca"], "TVA théorique": data["total_theo"],
        "TVA collectée": data["total_tva"], "Écart": data["total_ecart"],
    }])
    df_display = pd.concat([df_display, total_row], ignore_index=True)

    def highlight_rows(row):
        if row["Pays"] == "TOTAL":
            return ["font-weight: bold"] * len(row)
        if isinstance(row["Écart"], (int, float)) and abs(row["Écart"]) >= SEUIL_ECART:
            return ["background-color: rgba(239, 68, 68, 0.12)"] * len(row)
        return [""] * len(row)

    st.dataframe(
        df_display.style.apply(highlight_rows, axis=1),
        use_container_width=True,
        hide_index=True,
        column_config={
            "CA HT": st.column_config.NumberColumn(format="%.2f"),
            "TVA théorique": st.column_config.NumberColumn(format="%.2f"),
            "TVA collectée": st.column_config.NumberColumn(format="%.2f"),
            "Écart": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    # Export
    csv_oss = df_display.to_csv(index=False).encode("utf-8")
    st.download_button("📥 Export Contrôle Base × Taux (CSV)", csv_oss, f"{TODAY} Controle Base Taux OSS {mois_label}.csv", "text/csv")

else:
    st.info("Aucune donnée OSS trouvée pour ce mois")
