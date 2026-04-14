import streamlit as st
import pandas as pd
import sys, os, io, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta, datetime
from collections import defaultdict
from cadrage_tva import get_tva_shopify, get_tva_pennylane, STORE_PREFIXES
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
    date_min = st.date_input("Date début", value=date(2026, 3, 1))
with col2:
    date_max = st.date_input("Date fin", value=date(2026, 3, 1))
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
# PREPARE CADRAGE BOUTIQUE DATA
# =============================================================
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
            "Écart": ecart_b, "Justifié": True if is_ok else False, "Commentaire": "",
            "Montant écart justifié": ecart_b if is_ok else 0.0,
        })

unknown_tva = round(sum(l["tva"] for l in pl["lines"] if l.get("store") is None), 2)
if unknown_tva:
    cadrage_rows.append({
        "Boutique": "(INCONNU)", "TVA Shopify": 0, "TVA Pennylane": unknown_tva,
        "Écart": round(-unknown_tva, 2), "Justifié": False, "Commentaire": "",
        "Montant écart justifié": 0.0,
    })

if "cadrage_tva_boutique_data" not in st.session_state or run:
    st.session_state["cadrage_tva_boutique_data"] = cadrage_rows

ecart_brut = round(sum(r["Écart"] for r in cadrage_rows), 2)

current_data = st.session_state.get("cadrage_tva_boutique_data", cadrage_rows)
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
def generate_pdf():
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "AURALIS FINANCES", ln=True, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Dossier de cadrage de la TVA", ln=True, align="C")
    pdf.cell(0, 8, f"Periode : {date_min_str} au {date_max_str}", ln=True, align="C")
    pdf.cell(0, 8, f"Date d'edition : {TODAY}", ln=True, align="C")
    pdf.ln(10)

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Resultat du cadrage", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, f"TVA Shopify :      {total_sp:,.2f} EUR", ln=True)
    pdf.cell(0, 8, f"TVA Pennylane :    {total_pl:,.2f} EUR", ln=True)
    pdf.cell(0, 8, f"Ecart brut :       {ecart_brut:,.2f} EUR", ln=True)
    pdf.cell(0, 8, f"Ecart justifie :   {ecart_justifie:,.2f} EUR", ln=True)
    pdf.cell(0, 8, f"Ecart residuel :   {ecart_residuel:,.2f} EUR", ln=True)
    pdf.ln(4)

    if ecart_ok(ecart_brut):
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(6, 95, 70)
        pdf.cell(0, 10, "CADRAGE OK - Aucun ecart significatif", ln=True)
    elif all_justified:
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(107, 33, 168)
        pdf.cell(0, 10, "CADRAGE JUSTIFIE - Tous les ecarts sont justifies", ln=True)
    else:
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(153, 27, 27)
        pdf.cell(0, 10, f"ECART RESIDUEL : {ecart_residuel:,.2f} EUR", ln=True)

    pdf.set_text_color(0, 0, 0)
    pdf.ln(8)

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Detail par boutique", ln=True)

    pdf.set_font("Helvetica", "B", 9)
    col_widths = [22, 25, 25, 22, 22, 22, 52]
    headers = ["Boutique", "TVA Shopify", "TVA PL", "Ecart", "Justifie", "Restant", "Commentaire"]
    for i, h in enumerate(headers):
        pdf.cell(col_widths[i], 7, h, border=1, align="C")
    pdf.ln()

    pdf.set_font("Helvetica", "", 9)
    for r in current_data:
        ecart_b = r["Écart"]
        mnt_justifie = r.get("Montant écart justifié", 0.0)
        mnt_restant = round(ecart_b - mnt_justifie, 2) if not ecart_boutique_ok(ecart_b) else 0.0
        comment = r.get("Commentaire", "")

        pdf.cell(col_widths[0], 6, str(r["Boutique"]), border=1)
        pdf.cell(col_widths[1], 6, f"{r['TVA Shopify']:,.2f}", border=1, align="R")
        pdf.cell(col_widths[2], 6, f"{r['TVA Pennylane']:,.2f}", border=1, align="R")
        pdf.cell(col_widths[3], 6, f"{ecart_b:,.2f}", border=1, align="R")
        pdf.cell(col_widths[4], 6, f"{mnt_justifie:,.2f}", border=1, align="R")
        pdf.cell(col_widths[5], 6, f"{mnt_restant:,.2f}", border=1, align="R")
        pdf.cell(col_widths[6], 6, comment[:35], border=1)
        pdf.ln()

    pdf.set_font("Helvetica", "B", 9)
    total_sp_b = sum(r["TVA Shopify"] for r in current_data)
    total_pl_b = sum(r["TVA Pennylane"] for r in current_data)
    total_ec_b = sum(r["Écart"] for r in current_data)
    total_justifie_b = sum(r.get("Montant écart justifié", 0) for r in current_data if not ecart_boutique_ok(r["Écart"]))
    total_restant_b = round(total_ec_b - total_justifie_b, 2)
    pdf.cell(col_widths[0], 7, "TOTAL", border=1)
    pdf.cell(col_widths[1], 7, f"{total_sp_b:,.2f}", border=1, align="R")
    pdf.cell(col_widths[2], 7, f"{total_pl_b:,.2f}", border=1, align="R")
    pdf.cell(col_widths[3], 7, f"{total_ec_b:,.2f}", border=1, align="R")
    pdf.cell(col_widths[4], 7, f"{total_justifie_b:,.2f}", border=1, align="R")
    pdf.cell(col_widths[5], 7, f"{total_restant_b:,.2f}", border=1, align="R")
    pdf.cell(col_widths[6], 7, "", border=1)
    pdf.ln(10)

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Synthese des ecarts", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, f"Ecart brut :       {ecart_brut:,.2f} EUR", ln=True)
    pdf.cell(0, 8, f"Ecart justifie :   {ecart_justifie:,.2f} EUR", ln=True)
    pdf.cell(0, 8, f"Ecart residuel :   {ecart_residuel:,.2f} EUR", ln=True)
    pdf.ln(4)

    justified_rows = [r for r in current_data if r.get("Montant écart justifié", 0) != 0 and not ecart_boutique_ok(r["Écart"])]
    if justified_rows:
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 8, "Detail des justifications :", ln=True)
        pdf.set_font("Helvetica", "", 10)
        for r in justified_rows:
            mnt_j = r.get("Montant écart justifié", 0)
            mnt_r = round(r["Écart"] - mnt_j, 2)
            pdf.cell(0, 7, f"  {r['Boutique']} : ecart {r['Écart']:,.2f} EUR, justifie {mnt_j:,.2f} EUR, restant {mnt_r:,.2f} EUR - {r.get('Commentaire', '')}", ln=True)

    non_justified = [r for r in current_data if r.get("Montant écart justifié", 0) == 0 and not ecart_boutique_ok(r["Écart"])]
    if non_justified:
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(153, 27, 27)
        pdf.cell(0, 8, "Ecarts non justifies :", ln=True)
        pdf.set_font("Helvetica", "", 10)
        for r in non_justified:
            pdf.cell(0, 7, f"  {r['Boutique']} : ecart {r['Écart']:,.2f} EUR", ln=True)
        pdf.set_text_color(0, 0, 0)

    return bytes(pdf.output())

pdf_bytes = generate_pdf()
st.download_button(
    "📄 Exporter le dossier de cadrage (PDF)",
    pdf_bytes,
    f"{TODAY} Dossier Cadrage TVA {periode_label}.pdf",
    "application/pdf",
)

# Résultat jour par jour (si multi-jours)
if multi_day:
    st.markdown("**Résultat jour par jour**")

    daily_sp = defaultdict(lambda: {"tva": 0.0, "nb_orders": 0})
    for sdata in sp.values():
        for d, day_data in sdata.get("by_date", {}).items():
            daily_sp[d]["tva"] += day_data["tva"]
            daily_sp[d]["nb_orders"] += day_data["nb_orders"]

    daily_pl = pl["by_date"]
    all_days = sorted(set(list(daily_sp.keys()) + list(daily_pl.keys())))

    daily_rows = []
    for day in all_days:
        sp_day = round(daily_sp[day]["tva"], 2)
        pl_day = round(daily_pl.get(day, {}).get("tva", 0), 2)
        ecart_day = round(sp_day - pl_day, 2)
        if abs(ecart_day) < SEUIL_ECART:
            ecart_day = 0.0
        daily_rows.append({
            "Date": day, "TVA Shopify": sp_day, "Commandes": daily_sp[day]["nb_orders"],
            "TVA Pennylane": pl_day, "Écritures PL": daily_pl.get(day, {}).get("nb_ecritures", 0),
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
                sp_day_store = sdata.get("by_date", {}).get(selected_day, {}).get("tva", 0)
                pl_day_store_filtered = sum(l["tva"] for l in pl["lines"] if l["date"] == selected_day and l.get("store") == sname)
                if sp_day_store or pl_day_store_filtered:
                    day_store_rows.append({"Boutique": sname, "TVA Shopify": round(sp_day_store, 2), "TVA Pennylane": round(pl_day_store_filtered, 2), "Écart": round(sp_day_store - pl_day_store_filtered, 2)})
            unknown_day = sum(l["tva"] for l in pl["lines"] if l["date"] == selected_day and l.get("store") is None)
            if unknown_day:
                day_store_rows.append({"Boutique": "(INCONNU)", "TVA Shopify": 0, "TVA Pennylane": round(unknown_day, 2), "Écart": round(-unknown_day, 2)})
            if day_store_rows:
                st.dataframe(pd.DataFrame(day_store_rows), use_container_width=True, hide_index=True)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  2. DÉTAIL DU CADRAGE                                         ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Détail du cadrage")

# --- Order data ---
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
    order_rows.append({
        "Commande": order, "Boutique": (sp_o["store"] if sp_o else pl_o.get("store")) or "?",
        "TVA Shopify": sp_tva, "TVA Pennylane": pl_tva, "Écart": ecart_o,
        "Commentaire": "",
    })

st.markdown("""
<style>
    [data-testid="stDataEditor"] [data-testid="column-header"]:has(span[title*="justifié"]),
    [data-testid="stDataEditor"] [data-testid="column-header"]:has(span[title*="Commentaire"]) {
        background-color: rgba(124, 58, 237, 0.12) !important;
    }
    [data-testid="stDataEditor"] th:has(span[title*="justifié"]),
    [data-testid="stDataEditor"] th:has(span[title*="Commentaire"]) {
        background-color: rgba(124, 58, 237, 0.12) !important;
    }
</style>
""", unsafe_allow_html=True)

tab_boutique, tab_commande = st.tabs(["📊 Par boutique", "📋 Par commande"])

# ─── TAB 1 : PAR BOUTIQUE ───
with tab_boutique:
    df_cadrage = pd.DataFrame(st.session_state["cadrage_tva_boutique_data"])

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

    filter_boutique = st.radio("Filtrer", ["Toutes", "Écarts seulement"], horizontal=True, key="tva_bout_filter")
    df_cadrage_display = df_cadrage.copy()
    if filter_boutique == "Écarts seulement":
        df_cadrage_display = df_cadrage_display[~df_cadrage_display["Écart"].apply(ecart_boutique_ok)]

    display_cols = ["Boutique", "TVA Shopify", "TVA Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Statut", "Commentaire"]

    edited_cadrage = st.data_editor(
        df_cadrage_display[display_cols],
        use_container_width=True, hide_index=True,
        disabled=["Boutique", "TVA Shopify", "TVA Pennylane", "Écart", "Montant écart restant", "Statut"],
        column_config={
            "Commentaire": st.column_config.TextColumn("✏️ Commentaire", width="large"),
            "TVA Shopify": st.column_config.NumberColumn(format="%.2f"),
            "TVA Pennylane": st.column_config.NumberColumn(format="%.2f"),
            "Écart": st.column_config.NumberColumn(format="%.2f"),
            "Montant écart justifié": st.column_config.NumberColumn("✏️ Montant écart justifié", format="%.2f"),
            "Montant écart restant": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    if edited_cadrage is not None:
        _changed = False
        for idx, row in edited_cadrage.iterrows():
            for r in st.session_state["cadrage_tva_boutique_data"]:
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

    current_bout = st.session_state.get("cadrage_tva_boutique_data", cadrage_rows)
    ecart_brut_b = round(sum(r["Écart"] for r in current_bout), 2)
    ecart_justifie_b = round(
        sum(r.get("Montant écart justifié", 0) for r in current_bout if not ecart_boutique_ok(r["Écart"]))
        + sum(r["Écart"] for r in current_bout if ecart_boutique_ok(r["Écart"])),
        2
    )
    ecart_residuel_b = round(ecart_brut_b - ecart_justifie_b, 2)

    col_t1, col_t2, col_t3, col_t4, col_t5 = st.columns(5)
    col_t1.metric("Total Shopify", f"{df_cadrage['TVA Shopify'].sum():,.2f} €")
    col_t2.metric("Total Pennylane", f"{df_cadrage['TVA Pennylane'].sum():,.2f} €")
    col_t3.metric("Écart brut", f"{ecart_brut_b:,.2f} €")
    col_t4.metric("Justifié", f"{ecart_justifie_b:,.2f} €")
    col_t5.metric("Résiduel", f"{ecart_residuel_b:,.2f} €")

    df_cadrage_export = pd.DataFrame(st.session_state.get("cadrage_tva_boutique_data", cadrage_rows))
    df_cadrage_export["Montant écart restant"] = df_cadrage_export.apply(
        lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_boutique_ok(r["Écart"]) else 0.0, axis=1)
    df_cadrage_export["Statut"] = df_cadrage_export.apply(get_statut_boutique, axis=1)
    export_cols_bout = [c for c in display_cols if c in df_cadrage_export.columns]
    csv_cadrage_bout = df_cadrage_export[export_cols_bout].to_csv(index=False).encode("utf-8")
    st.download_button("📥 Export cadrage par boutique (CSV)", csv_cadrage_bout, f"{TODAY} Cadrage TVA Boutiques {periode_label}.csv", "text/csv")

# ─── TAB 2 : PAR COMMANDE ───
if "cadrage_tva_commande_data" not in st.session_state or run:
    st.session_state["cadrage_tva_commande_data"] = order_rows

with tab_commande:
    current_orders = st.session_state.get("cadrage_tva_commande_data", order_rows)
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
            filter_statut = st.radio("Filtrer", ["Toutes", "Écarts seulement"], horizontal=True, key="tva_order_filter")
        with col_f2:
            stores_in_orders = sorted(df_orders[df_orders["Boutique"] != "?"]["Boutique"].unique())
            filter_store = st.selectbox("Boutique", ["Toutes"] + stores_in_orders, key="tva_order_store")

        df_orders_display = df_orders.copy()
        if filter_statut == "Écarts seulement":
            df_orders_display = df_orders_display[~df_orders_display["Écart"].apply(ecart_ok)]
        if filter_store != "Toutes":
            df_orders_display = df_orders_display[df_orders_display["Boutique"] == filter_store]

        display_cols_cmd = ["Commande", "Boutique", "TVA Shopify", "TVA Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Statut", "Commentaire"]

        edited_orders = st.data_editor(
            df_orders_display[display_cols_cmd],
            use_container_width=True, hide_index=True,
            disabled=["Commande", "Boutique", "TVA Shopify", "TVA Pennylane", "Écart", "Montant écart restant", "Statut"],
            column_config={
                "Commentaire": st.column_config.TextColumn("✏️ Commentaire", width="large"),
                "TVA Shopify": st.column_config.NumberColumn(format="%.2f"),
                "TVA Pennylane": st.column_config.NumberColumn(format="%.2f"),
                "Écart": st.column_config.NumberColumn(format="%.2f"),
                "Montant écart justifié": st.column_config.NumberColumn("✏️ Montant écart justifié", format="%.2f"),
                "Montant écart restant": st.column_config.NumberColumn(format="%.2f"),
            },
            height=min(400, 35 * (len(df_orders_display) + 1)),
        )

        if edited_orders is not None:
            _cmd_changed = False
            for idx, row in edited_orders.iterrows():
                for r in st.session_state["cadrage_tva_commande_data"]:
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

        df_orders_exp = pd.DataFrame(st.session_state.get("cadrage_tva_commande_data", order_rows))
        if "Montant écart justifié" not in df_orders_exp.columns:
            df_orders_exp["Montant écart justifié"] = 0.0
        df_orders_exp["Montant écart restant"] = df_orders_exp.apply(
            lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_ok(r["Écart"]) else 0.0, axis=1)
        df_orders_exp["Statut"] = df_orders_exp.apply(get_statut_commande, axis=1)
        exp_cols_cmd = [c for c in display_cols_cmd if c in df_orders_exp.columns]
        csv_orders = df_orders_exp[exp_cols_cmd].to_csv(index=False).encode("utf-8")
        st.download_button("📥 Export cadrage par commande (CSV)", csv_orders, f"{TODAY} Cadrage TVA Commandes {periode_label}.csv", "text/csv")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  3. ÉLÉMENTS DE JUSTIFICATION                                 ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Éléments de justification")

# ─── TVA Pennylane ───
st.markdown("### TVA Pennylane")

st.markdown("**Par compte comptable**")
account_data = defaultdict(lambda: {"tva": 0.0, "count": 0, "name": ""})
for l in pl["lines"]:
    key = l["account"]
    account_data[key]["tva"] += l["tva"]
    account_data[key]["count"] += 1
    if l.get("account_name") and not account_data[key]["name"]:
        account_data[key]["name"] = l["account_name"]

account_rows = [{"Compte": acc, "Libellé": d["name"], "TVA": round(d["tva"], 2), "Écritures": d["count"]} for acc, d in sorted(account_data.items())]
if account_rows:
    df_accounts = pd.DataFrame(account_rows)
    df_accounts = pd.concat([df_accounts, pd.DataFrame([{"Compte": "TOTAL", "Libellé": "", "TVA": round(df_accounts["TVA"].sum(), 2), "Écritures": df_accounts["Écritures"].sum()}])], ignore_index=True)
    st.dataframe(df_accounts, use_container_width=True, hide_index=True)

st.markdown("**Par boutique Shopify**")
pl_store_rows = []
for sname in sorted(STORE_PREFIXES.keys()):
    pl_store_tva = round(sum(l["tva"] for l in pl["lines"] if l.get("store") == sname), 2)
    pl_store_count = sum(1 for l in pl["lines"] if l.get("store") == sname)
    if pl_store_tva != 0 or pl_store_count > 0:
        pl_store_rows.append({"Boutique": sname, "TVA Pennylane": pl_store_tva, "Écritures": pl_store_count})

unknown_pl = round(sum(l["tva"] for l in pl["lines"] if l.get("store") is None), 2)
unknown_count = sum(1 for l in pl["lines"] if l.get("store") is None)
if unknown_pl or unknown_count:
    pl_store_rows.append({"Boutique": "(INCONNU)", "TVA Pennylane": unknown_pl, "Écritures": unknown_count})

if pl_store_rows:
    df_pl_stores = pd.DataFrame(pl_store_rows)
    df_pl_stores = pd.concat([df_pl_stores, pd.DataFrame([{"Boutique": "TOTAL", "TVA Pennylane": round(df_pl_stores["TVA Pennylane"].sum(), 2), "Écritures": df_pl_stores["Écritures"].sum()}])], ignore_index=True)
    st.dataframe(df_pl_stores, use_container_width=True, hide_index=True)

if pl["lines"]:
    df_pl_export = pd.DataFrame(pl["lines"]).rename(columns={"date": "Date", "entry_label": "Écriture", "invoice_number": "Facture", "order_ref": "Commande", "store": "Boutique", "account": "Compte", "account_name": "Libellé compte", "tva": "Montant TVA"})
    df_pl_export = df_pl_export[["Date", "Boutique", "Compte", "Libellé compte", "Écriture", "Facture", "Commande", "Montant TVA"]].sort_values(["Date", "Boutique", "Commande"])
    st.download_button("📥 Export TVA Pennylane détaillé (CSV)", df_pl_export.to_csv(index=False).encode("utf-8"), f"{TODAY} TVA Pennylane Detail {periode_label}.csv", "text/csv")

# ─── TVA Shopify ───
st.markdown("### TVA Shopify")
st.markdown("**Par boutique**")

sp_store_rows = [{"Boutique": sname, "TVA Shopify": sdata["tva"], "Commandes": sdata["nb_orders"]} for sname, sdata in sp.items() if sdata["tva"] is not None and sdata["tva"] > 0]
if sp_store_rows:
    df_sp_stores = pd.DataFrame(sp_store_rows)
    df_sp_stores = pd.concat([df_sp_stores, pd.DataFrame([{"Boutique": "TOTAL", "TVA Shopify": round(df_sp_stores["TVA Shopify"].sum(), 2), "Commandes": df_sp_stores["Commandes"].sum()}])], ignore_index=True)
    st.dataframe(df_sp_stores, use_container_width=True, hide_index=True)

all_sp_orders = []
for sdata in sp.values():
    all_sp_orders.extend(sdata.get("orders", []))
if all_sp_orders:
    df_sp_export = pd.DataFrame(all_sp_orders).rename(columns={"date": "Date", "order_name": "Commande", "store": "Boutique", "tva": "Montant TVA"})
    df_sp_export = df_sp_export[["Date", "Boutique", "Commande", "Montant TVA"]].sort_values(["Date", "Boutique", "Commande"])
    st.download_button("📥 Export commandes Shopify TVA (CSV)", df_sp_export.to_csv(index=False).encode("utf-8"), f"{TODAY} Commandes Shopify TVA {periode_label}.csv", "text/csv")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  4. SYNTHÈSE DES EXPORTS                                      ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("📦 Synthèse des exports")

export_files = {}
export_files[f"{TODAY} Dossier Cadrage TVA {periode_label}.pdf"] = pdf_bytes
if cadrage_rows:
    _df_exp_bout = pd.DataFrame(st.session_state.get("cadrage_tva_boutique_data", cadrage_rows))
    _df_exp_bout["Montant écart restant"] = _df_exp_bout.apply(
        lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_boutique_ok(r["Écart"]) else 0.0, axis=1)
    _exp_cols = ["Boutique", "TVA Shopify", "TVA Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Commentaire"]
    _exp_cols = [c for c in _exp_cols if c in _df_exp_bout.columns]
    export_files[f"{TODAY} Cadrage TVA Boutiques {periode_label}.csv"] = _df_exp_bout[_exp_cols].to_csv(index=False).encode("utf-8")
if order_rows:
    export_files[f"{TODAY} Cadrage TVA Commandes {periode_label}.csv"] = pd.DataFrame(order_rows).to_csv(index=False).encode("utf-8")
if pl["lines"]:
    export_files[f"{TODAY} TVA Pennylane Detail {periode_label}.csv"] = df_pl_export.to_csv(index=False).encode("utf-8")
if all_sp_orders:
    export_files[f"{TODAY} Commandes Shopify TVA {periode_label}.csv"] = df_sp_export.to_csv(index=False).encode("utf-8")

justif_rows = [{"Boutique": r["Boutique"], "Écart": r["Écart"], "Montant écart justifié": r.get("Montant écart justifié", 0.0), "Montant écart restant": round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2), "Commentaire": r.get("Commentaire", "")} for r in current_data if not ecart_boutique_ok(r["Écart"])]
if justif_rows:
    df_justif = pd.DataFrame(justif_rows)
    export_files[f"{TODAY} Justification Ecarts TVA {periode_label}.csv"] = df_justif.to_csv(index=False).encode("utf-8")

cols = st.columns(min(len(export_files), 3))
for i, (fname, fbytes) in enumerate(export_files.items()):
    mime = "application/pdf" if fname.endswith(".pdf") else "text/csv"
    with cols[i % 3]:
        short_name = fname.replace(TODAY, "").replace(periode_label, "").strip()
        short_name = short_name.rsplit(".", 1)[0].strip()
        icon = "📄" if fname.endswith(".pdf") else "📊"
        st.download_button(f"{icon} {short_name}", fbytes, fname, mime, key=f"tva_synth_{i}")

st.write("")
zip_buffer = io.BytesIO()
with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
    for fname, fbytes in export_files.items():
        zf.writestr(fname, fbytes)
zip_buffer.seek(0)

st.download_button(
    "📦 Exporter tout le dossier de travail (ZIP)",
    zip_buffer.getvalue(),
    f"{TODAY} Dossier Travail TVA {periode_label}.zip",
    "application/zip",
    type="primary",
    use_container_width=True,
)
