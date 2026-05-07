import streamlit as st
import pandas as pd
import sys, os, io, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta, datetime
from collections import defaultdict
from cadrage_frais_shopify import get_frais_shopify_fast, get_frais_shopify_detail, get_pennylane_627_detail, STORES
from ui_common import setup_page, period_selector
from fpdf import FPDF

setup_page()

SEUIL_ECART = 0.05
SEUIL_ECART_BOUTIQUE = 2.00
TODAY = datetime.now().strftime("%Y-%m-%d")

st.title("📊 Cadrage des Frais")
st.markdown("Comparaison des frais Shopify (API) vs Pennylane (627001 / ENCSP)")

STORE_PREFIXES = {"LFC": ["LFC"], "RED": ["RDC"], "HET": ["HC"], "MTC": ["COCO"],
                  "MO": ["MO"], "RETE": ["RM"], "TZ": ["TZ"],
                  "LVO": ["LVO"], "UNIV": ["UNIV"]}

def ecart_ok(e):
    return abs(e) < SEUIL_ECART

def ecart_boutique_ok(e):
    return abs(e) < SEUIL_ECART_BOUTIQUE

# =============================================================
# SÉLECTION DE PÉRIODE
# =============================================================
date_min, date_max = period_selector(key_prefix="frais")
run = st.button("🚀 Lancer le cadrage", type="primary", use_container_width=True)

if run:
    date_min_str = date_min.strftime("%Y-%m-%d")
    date_max_str = date_max.strftime("%Y-%m-%d")
    with st.spinner("Chargement des frais Shopify..."):
        st.session_state["shopify_fast"] = get_frais_shopify_fast(date_min_str, date_max_str)
    with st.spinner("Chargement des écritures Pennylane..."):
        st.session_state["pl_data"] = get_pennylane_627_detail(date_min_str, date_max_str)
    st.session_state["date_min_str"] = date_min_str
    st.session_state["date_max_str"] = date_max_str
    st.session_state.pop("shopify_detail", None)

if "shopify_fast" not in st.session_state:
    st.info("Sélectionnez une période et cliquez sur **Lancer le cadrage**")
    st.stop()

shopify_fast = st.session_state["shopify_fast"]
pl_data = st.session_state["pl_data"]
date_min_str = st.session_state["date_min_str"]
date_max_str = st.session_state["date_max_str"]
multi_day = date_min_str != date_max_str
periode_label = f"du {date_min_str} au {date_max_str}" if multi_day else f"du {date_min_str}"
lines = pl_data["lines_627"]

# =============================================================
# CALCULS
# =============================================================
total_shopify     = sum(s["frais"] for s in shopify_fast.values() if s["frais"] is not None)
total_pl_shopify  = round(sum(l["net"] for l in lines["shopify"]), 2)
total_pl_mollie   = round(sum(l["net"] for l in lines["mollie"]), 2)
total_pl_klarna   = round(sum(l["net"] for l in lines["klarna"]), 2)
total_pl_ecart    = round(sum(l["net"] for l in lines["ecart"]), 2)
total_pl_autre    = round(sum(l["net"] for l in lines["autre"]), 2)
total_pl_all      = round(total_pl_shopify + total_pl_mollie + total_pl_klarna + total_pl_ecart + total_pl_autre, 2)
ecart_shopify_raw = round(total_shopify - total_pl_shopify, 2)

# =============================================================
# PREPARE CADRAGE BOUTIQUE DATA
# =============================================================
cadrage_rows = []
for sname, sdata in shopify_fast.items():
    if sdata["frais"] is not None and sdata["frais"] > 0:
        sp_frais = sdata["frais"]
        pl_store_frais = round(sum(
            l["net"] for l in lines["shopify"]
            if any(prefix in (l.get("order_ref", "") or "") for prefix in STORE_PREFIXES.get(sname, []))
        ), 2)
        ecart_b = round(sp_frais - pl_store_frais, 2)
        if abs(ecart_b) < SEUIL_ECART_BOUTIQUE:
            ecart_b = 0.0
        is_ok = ecart_boutique_ok(ecart_b)
        nb_txns = sum(d["nb_txns"] for d in sdata.get("by_date", {}).values())
        cadrage_rows.append({
            "Boutique": sname,
            "Frais Shopify": sp_frais,
            "Frais Pennylane": pl_store_frais,
            "Écart": ecart_b,
            "Montant écart justifié": ecart_b if is_ok else 0.0,
            "Commentaire": "",
            "Payouts": sdata["nb_payouts"],
            "Transactions": nb_txns,
        })

if "cadrage_frais_boutique_data" not in st.session_state or run:
    st.session_state["cadrage_frais_boutique_data"] = cadrage_rows

# Écart global = somme des écarts boutiques neutralisés
ecart_shopify = round(sum(r["Écart"] for r in cadrage_rows), 2)

# Compute écart justifié / résiduel from session state
current_data = st.session_state.get("cadrage_frais_boutique_data", cadrage_rows)
ecart_justifie = round(
    sum(r.get("Montant écart justifié", 0) for r in current_data if not ecart_boutique_ok(r["Écart"]))
    + sum(r["Écart"] for r in current_data if ecart_boutique_ok(r["Écart"])),
    2
)
ecart_residuel = round(ecart_shopify - ecart_justifie, 2)
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
col1.metric("Frais Shopify API", f"{total_shopify:,.2f} €")
col2.metric("Frais Pennylane", f"{total_pl_shopify:,.2f} €")
col3.metric("Écart brut", f"{ecart_shopify:,.2f} €")
col4.metric("Écart justifié", f"{ecart_justifie:,.2f} €")
col5.metric("Écart résiduel", f"{ecart_residuel:,.2f} €")

# Bandeau
if ecart_ok(ecart_shopify):
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
            Écart brut de {ecart_shopify:,.2f} € entièrement justifié — Écart résiduel : {ecart_residuel:,.2f} €
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
            {nb_non_justifie} boutique(s) non justifiée(s) — Écart brut : {ecart_shopify:,.2f} € / Justifié : {ecart_justifie:,.2f} €
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
    pdf.cell(0, 8, "Dossier de cadrage des Frais", ln=True, align="C")
    pdf.cell(0, 8, f"Periode : {date_min_str} au {date_max_str}", ln=True, align="C")
    pdf.cell(0, 8, f"Date d'edition : {TODAY}", ln=True, align="C")
    pdf.ln(10)

    # Résultat
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Resultat du cadrage", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, f"Frais Shopify API :   {total_shopify:,.2f} EUR", ln=True)
    pdf.cell(0, 8, f"Frais Pennylane :     {total_pl_shopify:,.2f} EUR", ln=True)
    pdf.cell(0, 8, f"Ecart brut :          {ecart_shopify:,.2f} EUR", ln=True)
    pdf.cell(0, 8, f"Ecart justifie :      {ecart_justifie:,.2f} EUR", ln=True)
    pdf.cell(0, 8, f"Ecart residuel :      {ecart_residuel:,.2f} EUR", ln=True)
    pdf.ln(4)

    if ecart_ok(ecart_shopify):
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

    # Détail par boutique
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Detail par boutique", ln=True)

    pdf.set_font("Helvetica", "B", 9)
    col_widths = [22, 25, 25, 22, 22, 22, 52]
    headers = ["Boutique", "Frais Shopify", "Frais PL", "Ecart", "Justifie", "Restant", "Commentaire"]
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
        pdf.cell(col_widths[1], 6, f"{r['Frais Shopify']:,.2f}", border=1, align="R")
        pdf.cell(col_widths[2], 6, f"{r['Frais Pennylane']:,.2f}", border=1, align="R")
        pdf.cell(col_widths[3], 6, f"{ecart_b:,.2f}", border=1, align="R")
        pdf.cell(col_widths[4], 6, f"{mnt_justifie:,.2f}", border=1, align="R")
        pdf.cell(col_widths[5], 6, f"{mnt_restant:,.2f}", border=1, align="R")
        pdf.cell(col_widths[6], 6, comment[:35], border=1)
        pdf.ln()

    # Total
    pdf.set_font("Helvetica", "B", 9)
    total_sp_b = sum(r["Frais Shopify"] for r in current_data)
    total_pl_b = sum(r["Frais Pennylane"] for r in current_data)
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

    # Synthèse des écarts
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Synthese des ecarts", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, f"Ecart brut :       {ecart_shopify:,.2f} EUR", ln=True)
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

    # Décomposition Pennylane
    pdf.ln(8)
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Decomposition Pennylane 627001 / ENCSP", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, f"Shopify :  {total_pl_shopify:,.2f} EUR ({len(lines['shopify'])} lignes)", ln=True)
    if total_pl_mollie:
        pdf.cell(0, 8, f"Mollie :   {total_pl_mollie:,.2f} EUR ({len(lines['mollie'])} lignes)", ln=True)
    if total_pl_klarna:
        pdf.cell(0, 8, f"Klarna :   {total_pl_klarna:,.2f} EUR ({len(lines['klarna'])} lignes)", ln=True)
    if total_pl_ecart:
        pdf.cell(0, 8, f"Ecarts :   {total_pl_ecart:,.2f} EUR ({len(lines['ecart'])} lignes)", ln=True)
    if total_pl_autre:
        pdf.cell(0, 8, f"Autre :    {total_pl_autre:,.2f} EUR ({len(lines['autre'])} lignes)", ln=True)
    pdf.cell(0, 8, f"TOTAL :    {total_pl_all:,.2f} EUR ({sum(len(v) for v in lines.values())} lignes)", ln=True)

    return bytes(pdf.output())

pdf_bytes = generate_pdf()
st.download_button(
    "📄 Exporter le dossier de cadrage (PDF)",
    pdf_bytes,
    f"{TODAY} Dossier Cadrage Frais {periode_label}.pdf",
    "application/pdf",
)

# Résultat jour par jour (si multi-jours)
if multi_day:
    st.markdown("**Résultat jour par jour**")

    daily_sp = defaultdict(float)
    daily_sp_count = defaultdict(int)
    for sdata in shopify_fast.values():
        for d, day_data in sdata.get("by_date", {}).items():
            daily_sp[d] += day_data["frais"]
            daily_sp_count[d] += day_data["nb_txns"]

    daily_pl = defaultdict(float)
    daily_pl_count = defaultdict(int)
    for l in lines["shopify"]:
        d = l.get("date", "")
        if d:
            daily_pl[d] += l["net"]
            daily_pl_count[d] += 1

    all_days = sorted(set(list(daily_sp.keys()) + list(daily_pl.keys())))

    daily_rows = []
    for day in all_days:
        sp_day = round(daily_sp.get(day, 0), 2)
        pl_day = round(daily_pl.get(day, 0), 2)
        ecart_day = round(sp_day - pl_day, 2)
        if abs(ecart_day) < SEUIL_ECART:
            ecart_day = 0.0
        daily_rows.append({
            "Date": day,
            "Frais Shopify": sp_day,
            "Txns Shopify": daily_sp_count.get(day, 0),
            "Frais Pennylane": pl_day,
            "Lignes PL": daily_pl_count.get(day, 0),
            "Écart": ecart_day,
            "Statut": "✅" if ecart_ok(ecart_day) else "❌",
        })

    if daily_rows:
        df_daily = pd.DataFrame(daily_rows)

        def highlight_ecart_row(row):
            if not ecart_ok(row["Écart"]):
                return ["background-color: rgba(255, 50, 50, 0.15)"] * len(row)
            return [""] * len(row)

        st.dataframe(
            df_daily.style.apply(highlight_ecart_row, axis=1),
            use_container_width=True,
            hide_index=True,
        )

        nb_jours_ok = len([r for r in daily_rows if ecart_ok(r["Écart"])])
        nb_jours_ko = len([r for r in daily_rows if not ecart_ok(r["Écart"])])
        col_d1, col_d2, col_d3 = st.columns(3)
        col_d1.metric("Jours", f"{len(daily_rows)}")
        col_d2.metric("Jours OK", f"{nb_jours_ok}")
        col_d3.metric("Jours en écart", f"{nb_jours_ko}", delta=f"{nb_jours_ko}" if nb_jours_ko > 0 else None, delta_color="inverse")

        # Drill-down par jour
        st.write("")
        day_options = ["—"] + [f"{'❌' if not ecart_ok(r['Écart']) else '✅'} {r['Date']}  ({r['Écart']:+.2f}€)" for r in daily_rows]
        day_keys = [None] + [r["Date"] for r in daily_rows]
        selected_idx = st.selectbox("🔎 Creuser un jour", range(len(day_options)), format_func=lambda i: day_options[i])

        if selected_idx and selected_idx > 0:
            selected_day = day_keys[selected_idx]
            st.markdown(f"#### Détail du {selected_day}")

            day_store_rows = []
            for sname, sdata in shopify_fast.items():
                day_data = sdata.get("by_date", {}).get(selected_day)
                if day_data:
                    pl_day_store = sum(
                        l["net"] for l in lines["shopify"]
                        if l.get("date") == selected_day and
                        any(prefix in (l.get("order_ref", "") or "") for prefix in STORE_PREFIXES.get(sname, []))
                    )
                    day_store_rows.append({
                        "Boutique": sname,
                        "Frais Shopify": round(day_data["frais"], 2),
                        "Transactions": day_data["nb_txns"],
                        "Frais Pennylane": round(pl_day_store, 2),
                        "Écart": round(day_data["frais"] - pl_day_store, 2),
                    })

            for cat in ["mollie", "klarna", "autre"]:
                cat_day_lines = [l for l in lines[cat] if l.get("date") == selected_day]
                if cat_day_lines:
                    cat_total = round(sum(l["net"] for l in cat_day_lines), 2)
                    day_store_rows.append({
                        "Boutique": f"({cat.upper()})",
                        "Frais Shopify": 0,
                        "Transactions": len(cat_day_lines),
                        "Frais Pennylane": cat_total,
                        "Écart": round(-cat_total, 2),
                    })

            if day_store_rows:
                st.dataframe(pd.DataFrame(day_store_rows), use_container_width=True, hide_index=True)

            day_pl_lines = [l for l in lines["shopify"] + lines["mollie"] + lines["klarna"] + lines["ecart"] + lines["autre"]
                            if l.get("date") == selected_day]
            if day_pl_lines:
                st.markdown("**Lignes 627001 Pennylane ce jour :**")
                pl_day_rows = []
                for l in day_pl_lines:
                    pl_day_rows.append({
                        "Réf commande": l.get("order_ref", "—"),
                        "Libellé": l["label"],
                        "Débit": l["debit"],
                        "Crédit": l["credit"],
                        "Net": round(l["net"], 2),
                        "Écriture": l["entry_label"],
                    })
                st.dataframe(pd.DataFrame(pl_day_rows), use_container_width=True, hide_index=True)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  2. DÉTAIL DU CADRAGE                                         ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Détail du cadrage")

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

tab_boutique, tab_commande = st.tabs(["📊 Par boutique", "📋 Par commande"])

# ─── TAB 1 : PAR BOUTIQUE ───
with tab_boutique:
    df_cadrage = pd.DataFrame(st.session_state["cadrage_frais_boutique_data"])

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

    filter_boutique = st.radio("Filtrer", ["Toutes", "Écarts seulement"], horizontal=True, key="frais_bout_filter")
    df_cadrage_display = df_cadrage.copy()
    if filter_boutique == "Écarts seulement":
        df_cadrage_display = df_cadrage_display[~df_cadrage_display["Écart"].apply(ecart_boutique_ok)]

    display_cols = ["Boutique", "Frais Shopify", "Frais Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Statut", "Commentaire"]

    edited_cadrage = st.data_editor(
        df_cadrage_display[display_cols],
        use_container_width=True, hide_index=True,
        disabled=["Boutique", "Frais Shopify", "Frais Pennylane", "Écart", "Montant écart restant", "Statut"],
        column_config={
            "Commentaire": st.column_config.TextColumn("✏️ Commentaire", width="large"),
            "Frais Shopify": st.column_config.NumberColumn(format="%.2f"),
            "Frais Pennylane": st.column_config.NumberColumn(format="%.2f"),
            "Écart": st.column_config.NumberColumn(format="%.2f"),
            "Montant écart justifié": st.column_config.NumberColumn("✏️ Montant écart justifié", format="%.2f"),
            "Montant écart restant": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    if edited_cadrage is not None:
        _changed = False
        for idx, row in edited_cadrage.iterrows():
            for r in st.session_state["cadrage_frais_boutique_data"]:
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

    # Synthèse écarts boutique (from session state for up-to-date values)
    current_bout = st.session_state.get("cadrage_frais_boutique_data", cadrage_rows)
    ecart_brut_b = round(sum(r["Écart"] for r in current_bout), 2)
    ecart_justifie_b = round(
        sum(r.get("Montant écart justifié", 0) for r in current_bout if not ecart_boutique_ok(r["Écart"]))
        + sum(r["Écart"] for r in current_bout if ecart_boutique_ok(r["Écart"])),
        2
    )
    ecart_residuel_b = round(ecart_brut_b - ecart_justifie_b, 2)

    col_t1, col_t2, col_t3, col_t4, col_t5 = st.columns(5)
    col_t1.metric("Total Shopify", f"{df_cadrage['Frais Shopify'].sum():,.2f} €")
    col_t2.metric("Total Pennylane", f"{df_cadrage['Frais Pennylane'].sum():,.2f} €")
    col_t3.metric("Écart brut", f"{ecart_brut_b:,.2f} €")
    col_t4.metric("Justifié", f"{ecart_justifie_b:,.2f} €")
    col_t5.metric("Résiduel", f"{ecart_residuel_b:,.2f} €")

    # Export from session state
    df_cadrage_export = pd.DataFrame(st.session_state.get("cadrage_frais_boutique_data", cadrage_rows))
    df_cadrage_export["Montant écart restant"] = df_cadrage_export.apply(
        lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_boutique_ok(r["Écart"]) else 0.0, axis=1)
    df_cadrage_export["Statut"] = df_cadrage_export.apply(get_statut_boutique, axis=1)
    export_cols_bout = [c for c in display_cols if c in df_cadrage_export.columns]
    csv_cadrage_bout = df_cadrage_export[export_cols_bout].to_csv(index=False).encode("utf-8")
    st.download_button("📥 Export cadrage par boutique (CSV)", csv_cadrage_bout, f"{TODAY} Cadrage Frais Boutiques {periode_label}.csv", "text/csv")

# ─── TAB 2 : PAR COMMANDE ───
# Detailed comparison requires loading shopify_detail
with tab_commande:
    st.caption("Chargement plus long : récupère le nom de chaque commande depuis Shopify")
    load_detail = st.button("📋 Charger le détail par commande", type="secondary")

    if load_detail:
        with st.spinner("Chargement du détail (appels Shopify par commande)..."):
            st.session_state["shopify_detail"] = get_frais_shopify_detail(date_min_str, date_max_str)

    if "shopify_detail" in st.session_state:
        shopify_detail = st.session_state["shopify_detail"]

        # Build Shopify index by order_name
        sp_by_order = {}
        for sname, sdata in shopify_detail.items():
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

        # Build Pennylane index by order_ref
        pl_by_order = {}
        for line in lines["shopify"]:
            ref = line.get("order_ref")
            if ref:
                if ref not in pl_by_order:
                    pl_by_order[ref] = {"fee": 0, "label": line["label"], "date": line["date"]}
                pl_by_order[ref]["fee"] += line["net"]
        for cat in ["mollie", "klarna", "ecart", "autre"]:
            for line in lines[cat]:
                ref = line.get("order_ref")
                if ref:
                    if ref not in pl_by_order:
                        pl_by_order[ref] = {"fee": 0, "label": line["label"], "date": line["date"], "cat": cat}
                    pl_by_order[ref]["fee"] += line["net"]

        # Build order rows
        all_orders = sorted(set(list(sp_by_order.keys()) + list(pl_by_order.keys())))

        order_rows = []
        for order in all_orders:
            sp_o = sp_by_order.get(order)
            plr = pl_by_order.get(order)
            sp_fee = sp_o["fee"] if sp_o else 0
            pl_fee = round(plr["fee"], 2) if plr else 0
            ecart_line = round(sp_fee - pl_fee, 2)
            if abs(ecart_line) < SEUIL_ECART:
                ecart_line = 0.0
            store = sp_o["store"] if sp_o else "?"

            order_rows.append({
                "Commande": order,
                "Boutique": store,
                "Frais Shopify": sp_fee,
                "Frais Pennylane": pl_fee,
                "Écart": ecart_line,
                "Commentaire": "",
            })

        # Initialize order data in session state
        if "cadrage_frais_commande_data" not in st.session_state or load_detail:
            st.session_state["cadrage_frais_commande_data"] = order_rows

        current_orders = st.session_state.get("cadrage_frais_commande_data", order_rows)
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
                filter_statut = st.radio("Filtrer", ["Toutes", "Écarts seulement"], horizontal=True, key="frais_order_filter")
            with col_f2:
                stores_in_orders = sorted(df_orders[df_orders["Boutique"] != "?"]["Boutique"].unique())
                filter_store = st.selectbox("Boutique", ["Toutes"] + stores_in_orders, key="frais_order_store")

            df_orders_display = df_orders.copy()
            if filter_statut == "Écarts seulement":
                df_orders_display = df_orders_display[~df_orders_display["Écart"].apply(ecart_ok)]
            if filter_store != "Toutes":
                df_orders_display = df_orders_display[df_orders_display["Boutique"] == filter_store]

            display_cols_cmd = ["Commande", "Boutique", "Frais Shopify", "Frais Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Statut", "Commentaire"]

            edited_orders = st.data_editor(
                df_orders_display[display_cols_cmd],
                use_container_width=True, hide_index=True,
                disabled=["Commande", "Boutique", "Frais Shopify", "Frais Pennylane", "Écart", "Montant écart restant", "Statut"],
                column_config={
                    "Commentaire": st.column_config.TextColumn("✏️ Commentaire", width="large"),
                    "Frais Shopify": st.column_config.NumberColumn(format="%.2f"),
                    "Frais Pennylane": st.column_config.NumberColumn(format="%.2f"),
                    "Écart": st.column_config.NumberColumn(format="%.2f"),
                    "Montant écart justifié": st.column_config.NumberColumn("✏️ Montant écart justifié", format="%.2f"),
                    "Montant écart restant": st.column_config.NumberColumn(format="%.2f"),
                },
                height=min(400, 35 * (len(df_orders_display) + 1)),
            )

            if edited_orders is not None:
                _cmd_changed = False
                for idx, row in edited_orders.iterrows():
                    for r in st.session_state["cadrage_frais_commande_data"]:
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

            # Export from session state
            df_orders_exp = pd.DataFrame(st.session_state.get("cadrage_frais_commande_data", order_rows))
            if "Montant écart justifié" not in df_orders_exp.columns:
                df_orders_exp["Montant écart justifié"] = 0.0
            df_orders_exp["Montant écart restant"] = df_orders_exp.apply(
                lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_ok(r["Écart"]) else 0.0, axis=1)
            df_orders_exp["Statut"] = df_orders_exp.apply(get_statut_commande, axis=1)
            exp_cols_cmd = [c for c in display_cols_cmd if c in df_orders_exp.columns]
            csv_orders = df_orders_exp[exp_cols_cmd].to_csv(index=False).encode("utf-8")
            st.download_button("📥 Export cadrage par commande (CSV)", csv_orders, f"{TODAY} Cadrage Frais Commandes {periode_label}.csv", "text/csv")
    else:
        st.info("Cliquez sur le bouton ci-dessus pour charger le détail ligne à ligne")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  3. ÉLÉMENTS DE JUSTIFICATION                                 ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Éléments de justification")

# Décomposition Pennylane
st.markdown("### Décomposition Pennylane 627001 / ENCSP")

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

# Lignes 471
if pl_data["ecart_471"]:
    st.markdown("### Lignes 471 (compte d'attente)")

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

# Détail lignes 627001
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
    st.markdown("### Détail lignes 627001")
    df_detail_627 = pd.DataFrame(all_lines_data)
    st.dataframe(df_detail_627, use_container_width=True, hide_index=True)
    st.download_button("📥 Export lignes 627001 (CSV)", df_detail_627.to_csv(index=False).encode("utf-8"),
                       f"{TODAY} Detail 627001 {periode_label}.csv", "text/csv")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  4. SYNTHÈSE DES EXPORTS                                      ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("📦 Synthèse des exports")

export_files = {}
export_files[f"{TODAY} Dossier Cadrage Frais {periode_label}.pdf"] = pdf_bytes
if cadrage_rows:
    _df_exp_bout = pd.DataFrame(st.session_state.get("cadrage_frais_boutique_data", cadrage_rows))
    _df_exp_bout["Montant écart restant"] = _df_exp_bout.apply(
        lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_boutique_ok(r["Écart"]) else 0.0, axis=1)
    _exp_cols = ["Boutique", "Frais Shopify", "Frais Pennylane", "Écart", "Montant écart justifié", "Montant écart restant", "Commentaire"]
    _exp_cols = [c for c in _exp_cols if c in _df_exp_bout.columns]
    export_files[f"{TODAY} Cadrage Frais Boutiques {periode_label}.csv"] = _df_exp_bout[_exp_cols].to_csv(index=False).encode("utf-8")
if "cadrage_frais_commande_data" in st.session_state:
    _df_exp_cmd = pd.DataFrame(st.session_state["cadrage_frais_commande_data"])
    if "Montant écart justifié" not in _df_exp_cmd.columns:
        _df_exp_cmd["Montant écart justifié"] = 0.0
    _df_exp_cmd["Montant écart restant"] = _df_exp_cmd.apply(
        lambda r: round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2) if not ecart_ok(r["Écart"]) else 0.0, axis=1)
    export_files[f"{TODAY} Cadrage Frais Commandes {periode_label}.csv"] = _df_exp_cmd.to_csv(index=False).encode("utf-8")
if all_lines_data:
    export_files[f"{TODAY} Detail 627001 {periode_label}.csv"] = df_detail_627.to_csv(index=False).encode("utf-8")

# Justification des écarts CSV
justif_rows = [{"Boutique": r["Boutique"], "Écart": r["Écart"], "Montant écart justifié": r.get("Montant écart justifié", 0.0),
                "Montant écart restant": round(r["Écart"] - r.get("Montant écart justifié", 0.0), 2),
                "Commentaire": r.get("Commentaire", "")} for r in current_data if not ecart_boutique_ok(r["Écart"])]
if justif_rows:
    df_justif = pd.DataFrame(justif_rows)
    export_files[f"{TODAY} Justification Ecarts Frais {periode_label}.csv"] = df_justif.to_csv(index=False).encode("utf-8")

cols = st.columns(min(len(export_files), 3))
for i, (fname, fbytes) in enumerate(export_files.items()):
    mime = "application/pdf" if fname.endswith(".pdf") else "text/csv"
    with cols[i % 3]:
        short_name = fname.replace(TODAY, "").replace(periode_label, "").strip()
        short_name = short_name.rsplit(".", 1)[0].strip()
        icon = "📄" if fname.endswith(".pdf") else "📊"
        st.download_button(f"{icon} {short_name}", fbytes, fname, mime, key=f"synth_frais_{i}")

st.write("")
zip_buffer = io.BytesIO()
with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
    for fname, fbytes in export_files.items():
        zf.writestr(fname, fbytes)
zip_buffer.seek(0)

st.download_button(
    "📦 Exporter tout le dossier de travail (ZIP)",
    zip_buffer.getvalue(),
    f"{TODAY} Dossier Travail Frais {periode_label}.zip",
    "application/zip",
    type="primary",
    use_container_width=True,
)
