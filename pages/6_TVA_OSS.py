import streamlit as st
import pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime
from tva_oss import get_controle_base_taux
from ui_common import setup_page, period_selector
from fpdf import FPDF

setup_page()

TODAY = datetime.now().strftime("%Y-%m-%d")
SEUIL_ECART = 0.05

st.title("🇪🇺 TVA OSS")
st.markdown("Aide à la déclaration TVA One-Stop-Shop")

# =============================================================
# SÉLECTION DE PÉRIODE
# =============================================================
date_min, date_max = period_selector(key_prefix="oss")
run = st.button("🚀 Lancer le contrôle", type="primary", use_container_width=True)

if run:
    date_min_str = date_min.strftime("%Y-%m-%d")
    date_max_str = date_max.strftime("%Y-%m-%d")
    periode_label = f"du {date_min_str} au {date_max_str}"
    with st.spinner(f"Contrôle Base × Taux — {periode_label}..."):
        st.session_state["oss_data"] = get_controle_base_taux(date_min_str, date_max_str)
        st.session_state["oss_periode"] = periode_label

if "oss_data" not in st.session_state:
    st.info("Sélectionnez une période et cliquez sur **Lancer le contrôle**")
    st.stop()

data = st.session_state["oss_data"]
mois_label = st.session_state["oss_periode"]
rows = data["rows"]

# ╔═══════════════════════════════════════════════════════════════╗
# ║  1. CONTRÔLE BASE × TAUX                                       ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("1. Contrôle Base × Taux")

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

st.markdown(f"**Détail par pays — {mois_label}**")

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

    # PDF Rapport
    def generate_pdf_oss():
        W = 277
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)

        # Page 1 : Résultat
        pdf.add_page("L")
        pdf.set_font("Helvetica", "B", 20)
        pdf.cell(0, 14, "AURALIS FINANCES", ln=True, align="C")
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 8, "Rapport de controle TVA OSS - Base x Taux", ln=True, align="C")
        pdf.cell(0, 8, f"Periode : {mois_label}", ln=True, align="C")
        pdf.cell(0, 8, f"Date d'edition : {TODAY}", ln=True, align="C")
        pdf.ln(15)

        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 12, "Resultat du controle", ln=True)
        pdf.set_font("Helvetica", "", 13)
        pdf.cell(0, 9, f"CA HT OSS :         {data['total_ca']:,.2f} EUR", ln=True)
        pdf.cell(0, 9, f"TVA collectee :     {data['total_tva']:,.2f} EUR", ln=True)
        pdf.cell(0, 9, f"TVA theorique :     {data['total_theo']:,.2f} EUR", ln=True)
        pdf.cell(0, 9, f"Ecart total :       {data['total_ecart']:,.2f} EUR", ln=True)
        pdf.ln(8)

        if abs(data["total_ecart"]) < SEUIL_ECART:
            pdf.set_font("Helvetica", "B", 14)
            pdf.set_text_color(6, 95, 70)
            pdf.cell(0, 12, "CONTROLE OK - Aucun ecart significatif", ln=True)
        else:
            pdf.set_font("Helvetica", "B", 14)
            pdf.set_text_color(153, 27, 27)
            nb_e = sum(1 for r in rows if abs(r["ecart"]) >= SEUIL_ECART)
            pdf.cell(0, 12, f"ECART DETECTE : {data['total_ecart']:,.2f} EUR ({nb_e} pays)", ln=True)
        pdf.set_text_color(0, 0, 0)

        # Page 2 : Tableau détail
        pdf.add_page("L")
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 12, "Detail par pays", ln=True)
        pdf.ln(4)

        cw = [int(W*0.12), int(W*0.05), int(W*0.06), int(W*0.13), int(W*0.08), int(W*0.13), int(W*0.13), int(W*0.13), int(W*0.10)]
        headers = ["Pays", "Code", "Taux", "Compte produit", "Compte TVA", "CA HT", "TVA theorique", "TVA collectee", "Ecart"]
        pdf.set_font("Helvetica", "B", 9)
        for i, h in enumerate(headers):
            pdf.cell(cw[i], 8, h, border=1, align="C")
        pdf.ln()

        pdf.set_font("Helvetica", "", 9)
        for r in rows:
            pdf.cell(cw[0], 7, r["pays"][:16], border=1)
            pdf.cell(cw[1], 7, r["code"], border=1, align="C")
            pdf.cell(cw[2], 7, f"{r['taux']}%", border=1, align="C")
            pdf.cell(cw[3], 7, r["compte_produit"], border=1, align="C")
            pdf.cell(cw[4], 7, r["compte_tva"], border=1, align="C")
            pdf.cell(cw[5], 7, f"{r['ca_ht']:,.2f}", border=1, align="R")
            pdf.cell(cw[6], 7, f"{r['tva_theorique']:,.2f}", border=1, align="R")
            pdf.cell(cw[7], 7, f"{r['tva_collectee']:,.2f}", border=1, align="R")
            pdf.cell(cw[8], 7, f"{r['ecart']:,.2f}", border=1, align="R")
            pdf.ln()

        # Total
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(cw[0], 8, "TOTAL", border=1)
        pdf.cell(cw[1], 8, "", border=1)
        pdf.cell(cw[2], 8, "", border=1)
        pdf.cell(cw[3], 8, "", border=1)
        pdf.cell(cw[4], 8, "", border=1)
        pdf.cell(cw[5], 8, f"{data['total_ca']:,.2f}", border=1, align="R")
        pdf.cell(cw[6], 8, f"{data['total_theo']:,.2f}", border=1, align="R")
        pdf.cell(cw[7], 8, f"{data['total_tva']:,.2f}", border=1, align="R")
        pdf.cell(cw[8], 8, f"{data['total_ecart']:,.2f}", border=1, align="R")
        pdf.ln()

        return bytes(pdf.output())

    pdf_bytes = generate_pdf_oss()
    st.download_button("📄 Rapport Contrôle Base × Taux (PDF)", pdf_bytes, f"{TODAY} Rapport Controle OSS {mois_label}.pdf", "application/pdf")

    # ╔═══════════════════════════════════════════════════════════════╗
    # ║  2. EXPORT DÉCLARATION TVA OSS                                 ║
    # ╚═══════════════════════════════════════════════════════════════╝
    st.markdown("---")
    st.subheader("2. Export déclaration TVA OSS")

    ec_rows = []
    for r in rows:
        ec_rows.append({
            "Pays": r["pays"],
            "Code ISO": r["code"],
            "CA HT": r["ca_ht"],
            "TVA": r["tva_collectee"],
        })
    ec_rows.append({
        "Pays": "TOTAL",
        "Code ISO": "",
        "CA HT": data["total_ca"],
        "TVA": data["total_tva"],
    })

    df_ec = pd.DataFrame(ec_rows)

    st.dataframe(df_ec, use_container_width=True, hide_index=True,
        column_config={
            "CA HT": st.column_config.NumberColumn(format="%.2f"),
            "TVA": st.column_config.NumberColumn(format="%.2f"),
        })

    csv_ec = df_ec.to_csv(index=False).encode("utf-8")
    st.download_button("📥 Export déclaration TVA OSS (CSV)", csv_ec, f"{TODAY} Declaration TVA OSS {mois_label}.csv", "text/csv", type="primary")

else:
    st.info("Aucune donnée OSS trouvée pour cette période")
