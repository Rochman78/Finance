import streamlit as st
import pandas as pd
import sys, os, io, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime
from cadrage_encaissements import run_cadrage_encaissements, db_exists
from ui_common import setup_page
from fpdf import FPDF

setup_page()

TODAY = datetime.now().strftime("%Y-%m-%d")
SEUIL_ECART = 0.05

st.title("💶 Cadrage Encaissements")
st.markdown("Détection des comptes clients 411 non soldés malgré un payout Shopify")

# =============================================================
# LANCEMENT
# =============================================================
if not db_exists():
    st.error("Base locale introuvable (`encaissements.db`). Lancez d'abord le script de chargement.")
    st.stop()

run = st.button("🚀 Lancer le cadrage", type="primary", use_container_width=True)

if run:
    progress_text = st.empty()
    def update_progress(msg):
        progress_text.text(msg)
    with st.spinner("Analyse des comptes clients 411..."):
        st.session_state["encaissements_data"] = run_cadrage_encaissements(progress_cb=update_progress)
    progress_text.empty()

if "encaissements_data" not in st.session_state:
    st.info("Cliquez sur **Lancer le cadrage** pour analyser les comptes clients")
    st.stop()

data = st.session_state["encaissements_data"]
anomalies = data["anomalies"]
ok_list = data["ok"]
no_order = data["no_order"]
total_accounts = data.get("total_accounts", 0)
total_ok = data.get("total_ok", 0)
total_unsettled = data["total_unsettled"]

# ╔═══════════════════════════════════════════════════════════════╗
# ║  1. RÉSULTAT DU CADRAGE                                      ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Résultat du cadrage")

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Comptes 411 analysés", f"{total_accounts}")
col2.metric("Soldés OK", f"{total_ok}")
col3.metric("Non soldés", f"{total_unsettled}")
col4.metric("Anomalies", f"{len(anomalies)}")
col5.metric("En attente paiement", f"{len(ok_list)}")

total_anomalie = round(sum(a["solde"] for a in anomalies), 2)

if data.get("source") == "db":
    info_parts = []
    if data.get("loaded_at"):
        info_parts.append(f"📦 Données chargées le {data['loaded_at']}")
    if data.get("period_min") and data.get("period_max"):
        info_parts.append(f"📅 Période : {data['period_min']} → {data['period_max']}")
    if data.get("first_account"):
        info_parts.append(f"Premier compte : {data['first_account']}")
    if data.get("last_account"):
        info_parts.append(f"Dernier compte : {data['last_account']}")
    st.caption(" — ".join(info_parts))

if len(anomalies) == 0:
    st.markdown("""
    <div style="background: linear-gradient(135deg, #ECFDF5, #D1FAE5); border: 1px solid #6EE7B7;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #065F46;">✅ Cadrage OK</div>
        <div style="font-size: 0.95rem; color: #047857; margin-top: 0.3rem;">
            Aucune anomalie détectée — tous les encaissements sont correctement imputés
        </div>
    </div>
    """, unsafe_allow_html=True)
else:
    st.markdown(f"""
    <div style="background: linear-gradient(135deg, #FEF2F2, #FECACA); border: 1px solid #FCA5A5;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin: 1rem 0;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #991B1B;">❌ {len(anomalies)} anomalie(s) détectée(s)</div>
        <div style="font-size: 0.95rem; color: #B91C1C; margin-top: 0.3rem;">
            Solde total des anomalies : {total_anomalie:,.2f} € — Payout Shopify effectué mais encaissement non imputé en compta
        </div>
    </div>
    """, unsafe_allow_html=True)

# PDF
def generate_pdf():
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "AURALIS FINANCES", ln=True, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Dossier de cadrage des Encaissements", ln=True, align="C")
    pdf.cell(0, 8, f"Date d'edition : {TODAY}", ln=True, align="C")
    pdf.ln(10)

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Resultat du cadrage", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, f"Comptes 411 non soldes :   {total_unsettled}", ln=True)
    pdf.cell(0, 8, f"Anomalies :                {len(anomalies)}", ln=True)
    pdf.cell(0, 8, f"En attente paiement :      {len(ok_list)}", ln=True)
    pdf.cell(0, 8, f"Sans commande Shopify :    {len(no_order)}", ln=True)
    pdf.ln(4)

    if anomalies:
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(153, 27, 27)
        pdf.cell(0, 10, f"ANOMALIES : {len(anomalies)} compte(s), solde total {total_anomalie:,.2f} EUR", ln=True)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(4)

        pdf.set_font("Helvetica", "B", 9)
        col_widths = [25, 35, 22, 20, 22, 22, 44]
        headers = ["Compte", "Client", "Commande", "Boutique", "Solde 411", "Payout", "Statut Shopify"]
        for i, h in enumerate(headers):
            pdf.cell(col_widths[i], 7, h, border=1, align="C")
        pdf.ln()

        pdf.set_font("Helvetica", "", 9)
        for a in anomalies:
            pdf.cell(col_widths[0], 6, str(a.get("account_number", "")), border=1)
            pdf.cell(col_widths[1], 6, str(a.get("account_label", ""))[:22], border=1)
            pdf.cell(col_widths[2], 6, str(a.get("order_ref", "")), border=1)
            pdf.cell(col_widths[3], 6, str(a.get("store", "")), border=1)
            pdf.cell(col_widths[4], 6, f"{a['solde']:,.2f}", border=1, align="R")
            payout_amt = a.get("payout_amount") or a.get("total_price", 0)
            pdf.cell(col_widths[5], 6, f"{payout_amt:,.2f}", border=1, align="R")
            pdf.cell(col_widths[6], 6, str(a.get("financial_status", "")), border=1)
            pdf.ln()
    else:
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(6, 95, 70)
        pdf.cell(0, 10, "CADRAGE OK - Aucune anomalie detectee", ln=True)
        pdf.set_text_color(0, 0, 0)

    return bytes(pdf.output())

pdf_bytes = generate_pdf()
st.download_button(
    "📄 Exporter le dossier de cadrage (PDF)",
    pdf_bytes,
    f"{TODAY} Dossier Cadrage Encaissements.pdf",
    "application/pdf",
)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  2. ANOMALIES                                                 ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Anomalies — Payout Shopify OK mais 411 non soldé")

if anomalies:
    anomaly_rows = []
    for a in anomalies:
        payout_amt = a.get("payout_amount") or a.get("total_price", 0)
        anomaly_rows.append({
            "Compte": a["account_number"],
            "Client": a["account_label"],
            "Commande": a.get("order_ref", ""),
            "Facture": a.get("invoice_number", "") or "",
            "Boutique": a.get("store", ""),
            "Solde 411": a["solde"],
            "Montant payout": round(payout_amt, 2),
            "Date payout": a.get("payout_date") or "—",
            "Statut Shopify": a.get("financial_status", ""),
            "Commentaire": "",
        })

    if "encaissements_anomalies_data" not in st.session_state or run:
        st.session_state["encaissements_anomalies_data"] = anomaly_rows

    current_anomalies = st.session_state.get("encaissements_anomalies_data", anomaly_rows)
    df_anomalies = pd.DataFrame(current_anomalies)

    st.markdown("""
    <style>
        [data-testid="stDataEditor"] [data-testid="column-header"]:has(span[title*="Commentaire"]) {
            background-color: rgba(139, 92, 246, 0.18) !important;
            border-bottom: 3px solid rgba(139, 92, 246, 0.5) !important;
        }
        [data-testid="stDataEditor"] th:has(span[title*="Commentaire"]) {
            background-color: rgba(139, 92, 246, 0.18) !important;
            border-bottom: 3px solid rgba(139, 92, 246, 0.5) !important;
        }
    </style>
    """, unsafe_allow_html=True)

    display_cols = ["Boutique", "Compte", "Client", "Commande", "Facture", "Solde 411", "Montant payout", "Date payout", "Statut Shopify", "Commentaire"]

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        stores_in_anomalies = sorted(set(a.get("Boutique", "") for a in current_anomalies if a.get("Boutique")))
        filter_store = st.selectbox("Boutique", ["Toutes"] + stores_in_anomalies, key="enc_store_filter")
    with col_f2:
        pass

    df_display = df_anomalies.copy()
    if filter_store != "Toutes":
        df_display = df_display[df_display["Boutique"] == filter_store]

    edited = st.data_editor(
        df_display[display_cols],
        use_container_width=True, hide_index=True,
        disabled=["Boutique", "Compte", "Client", "Commande", "Facture", "Solde 411", "Montant payout", "Date payout", "Statut Shopify"],
        column_config={
            "Commentaire": st.column_config.TextColumn("✏️ Commentaire", width="large"),
            "Solde 411": st.column_config.NumberColumn(format="%.2f"),
            "Montant payout": st.column_config.NumberColumn(format="%.2f"),
        },
        height=min(400, 35 * (len(df_display) + 1)),
    )

    if edited is not None:
        _changed = False
        for idx, row in edited.iterrows():
            for r in st.session_state["encaissements_anomalies_data"]:
                if r["Compte"] == row["Compte"] and r["Commande"] == row["Commande"]:
                    new_comment = row.get("Commentaire", "")
                    if r.get("Commentaire", "") != new_comment:
                        r["Commentaire"] = new_comment
                        _changed = True
        if _changed:
            st.rerun()

    # Metrics
    col_m1, col_m2, col_m3 = st.columns(3)
    col_m1.metric("Anomalies", f"{len(anomaly_rows)}")
    col_m2.metric("Solde total 411", f"{sum(r['Solde 411'] for r in anomaly_rows):,.2f} €")
    col_m3.metric("Total payouts", f"{sum(r['Montant payout'] for r in anomaly_rows):,.2f} €")

    csv_anomalies = pd.DataFrame(st.session_state.get("encaissements_anomalies_data", anomaly_rows)).to_csv(index=False).encode("utf-8")
    st.download_button("📥 Export anomalies (CSV)", csv_anomalies, f"{TODAY} Anomalies Encaissements.csv", "text/csv")

else:
    st.success("Aucune anomalie détectée")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  3. EN ATTENTE DE PAIEMENT                                    ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("En attente de paiement — Pas de payout Shopify")

if ok_list:
    ok_rows = [{
        "Compte": o["account_number"],
        "Client": o["account_label"],
        "Commande": o.get("order_ref", ""),
        "Facture": o.get("invoice_number", "") or "",
        "Boutique": o.get("store", ""),
        "Solde 411": o["solde"],
    } for o in ok_list]

    df_ok = pd.DataFrame(ok_rows)
    st.dataframe(df_ok, use_container_width=True, hide_index=True)
    st.caption(f"{len(ok_rows)} compte(s) en attente — pas de payout Shopify, le 411 non soldé est normal")
else:
    st.info("Aucun compte en attente de paiement")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  4. SANS COMMANDE SHOPIFY                                     ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Sans commande Shopify identifiée")

if no_order:
    no_rows = [{
        "Compte": n["account_number"],
        "Client": n["account_label"],
        "Solde 411": n["solde"],
        "Commande": n.get("order_ref", "—"),
    } for n in no_order]

    df_no = pd.DataFrame(no_rows)
    st.dataframe(df_no, use_container_width=True, hide_index=True)
    st.caption(f"{len(no_rows)} compte(s) ignoré(s) — pas de commande Shopify identifiable (hors périmètre)")
else:
    st.info("Tous les comptes 411 non soldés sont liés à une commande Shopify")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  5. SYNTHÈSE DES EXPORTS                                      ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("📦 Synthèse des exports")

export_files = {}
export_files[f"{TODAY} Dossier Cadrage Encaissements.pdf"] = pdf_bytes
if anomalies:
    export_files[f"{TODAY} Anomalies Encaissements.csv"] = pd.DataFrame(
        st.session_state.get("encaissements_anomalies_data", anomaly_rows)
    ).to_csv(index=False).encode("utf-8")
if ok_list:
    export_files[f"{TODAY} Attente Paiement.csv"] = pd.DataFrame(ok_rows).to_csv(index=False).encode("utf-8")
if no_order:
    export_files[f"{TODAY} Sans Commande Shopify.csv"] = pd.DataFrame(no_rows).to_csv(index=False).encode("utf-8")

cols = st.columns(min(len(export_files), 3))
for i, (fname, fbytes) in enumerate(export_files.items()):
    mime = "application/pdf" if fname.endswith(".pdf") else "text/csv"
    with cols[i % 3]:
        short_name = fname.replace(TODAY, "").strip()
        short_name = short_name.rsplit(".", 1)[0].strip()
        icon = "📄" if fname.endswith(".pdf") else "📊"
        st.download_button(f"{icon} {short_name}", fbytes, fname, mime, key=f"enc_synth_{i}")

st.write("")
zip_buffer = io.BytesIO()
with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
    for fname, fbytes in export_files.items():
        zf.writestr(fname, fbytes)
zip_buffer.seek(0)

st.download_button(
    "📦 Exporter tout le dossier de travail (ZIP)",
    zip_buffer.getvalue(),
    f"{TODAY} Dossier Travail Encaissements.zip",
    "application/zip",
    type="primary",
    use_container_width=True,
)
