import streamlit as st
import pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta
from collections import defaultdict
from cadrage_frais_shopify import get_frais_shopify_fast, get_frais_shopify_detail, get_pennylane_627_detail, STORES
from ui_common import setup_page

setup_page()

st.title("📊 Cadrage des Frais")
st.markdown("Comparaison des frais Shopify (API) vs Pennylane (627001 / ENCSP)")

STORE_PREFIXES = {"LFC": ["LFC"], "RED": ["RDC"], "HET": ["HC"], "MTC": ["COCO"],
                  "MO": ["MO"], "RETE": ["RM"], "TZ": ["TZ"],
                  "LVO": ["LVO"], "UNIV": ["UNIV"]}

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
ecart_shopify     = round(total_shopify - total_pl_shopify, 2)

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
else:
    col4.metric("Statut", "❌ Écart")

# =============================================================
# VUE JOUR PAR JOUR
# =============================================================
st.markdown("---")
st.subheader("📅 Vue jour par jour")

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
    sp = round(daily_sp.get(day, 0), 2)
    pl_day = round(daily_pl.get(day, 0), 2)
    ecart_day = round(sp - pl_day, 2)
    daily_rows.append({
        "Date": day,
        "Frais Shopify": sp,
        "Txns Shopify": daily_sp_count.get(day, 0),
        "Frais Pennylane": pl_day,
        "Lignes PL": daily_pl_count.get(day, 0),
        "Écart": ecart_day,
        "Statut": "✅" if abs(ecart_day) < 0.05 else f"❌ {ecart_day:+.2f}€",
    })

if daily_rows:
    df_daily = pd.DataFrame(daily_rows)

    def highlight_ecart_row(row):
        if abs(row["Écart"]) >= 0.05:
            return ["background-color: rgba(255, 50, 50, 0.15)"] * len(row)
        return [""] * len(row)

    st.dataframe(
        df_daily.style.apply(highlight_ecart_row, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    nb_jours_ok = len([r for r in daily_rows if abs(r["Écart"]) < 0.05])
    nb_jours_ko = len([r for r in daily_rows if abs(r["Écart"]) >= 0.05])
    col_d1, col_d2, col_d3 = st.columns(3)
    col_d1.metric("Jours", f"{len(daily_rows)}")
    col_d2.metric("Jours OK", f"{nb_jours_ok}")
    col_d3.metric("Jours en écart", f"{nb_jours_ko}", delta=f"{nb_jours_ko}" if nb_jours_ko > 0 else None, delta_color="inverse")

    # Drill-down par jour
    st.write("")
    day_options = ["—"] + [f"{'❌' if abs(r['Écart']) >= 0.05 else '✅'} {r['Date']}  ({r['Écart']:+.2f}€)" for r in daily_rows]
    day_keys = [None] + [r["Date"] for r in daily_rows]
    selected_idx = st.selectbox("🔎 Creuser un jour", range(len(day_options)), format_func=lambda i: day_options[i])

    if selected_idx and selected_idx > 0:
        selected_day = day_keys[selected_idx]
        st.markdown(f"#### Détail du {selected_day}")

        # Boutiques pour ce jour
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

        # Lignes Pennylane sans match Shopify ce jour-là (Mollie, Klarna, etc.)
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

        # Lignes 627001 Pennylane de ce jour
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

# =============================================================
# DÉTAIL PAR BOUTIQUE
# =============================================================
st.markdown("---")
st.subheader("Détail par boutique")

rows = []
for sname, sdata in shopify_fast.items():
    if sdata["frais"] is not None and sdata["frais"] > 0:
        pl_store_frais = sum(
            l["net"] for l in lines["shopify"]
            if any(prefix in (l.get("order_ref", "") or "") for prefix in
                   STORE_PREFIXES.get(sname, []))
        )
        nb_txns = sum(d["nb_txns"] for d in sdata.get("by_date", {}).values())
        rows.append({
            "Boutique": sname,
            "Frais Shopify": sdata["frais"],
            "Payouts": sdata["nb_payouts"],
            "Transactions": nb_txns,
            "Frais Pennylane": round(pl_store_frais, 2),
            "Écart": round(sdata["frais"] - pl_store_frais, 2),
        })

if rows:
    df_boutiques = pd.DataFrame(rows)
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
# DÉTAIL LIGNE À LIGNE (chargement à la demande)
# =============================================================
st.markdown("---")
st.subheader("🔍 Détail ligne à ligne — Shopify vs Pennylane")
st.caption("Chargement plus long : récupère le nom de chaque commande depuis Shopify")

load_detail = st.button("📋 Charger le détail par commande", type="secondary")

if load_detail:
    with st.spinner("Chargement du détail (appels Shopify par commande)..."):
        st.session_state["shopify_detail"] = get_frais_shopify_detail(date_min_str, date_max_str)

detail_rows = []

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

    # Merge
    all_orders = sorted(set(list(sp_by_order.keys()) + list(pl_by_order.keys())))

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

        nb_ok = len(df_lines[df_lines["Écart"] == 0])
        nb_ecart = len(df_lines[df_lines["Écart"] != 0])
        total_sp_detail = df_lines["Frais Shopify"].sum()
        total_pl_detail = df_lines["Frais Pennylane"].sum()

        col_s1, col_s2, col_s3, col_s4 = st.columns(4)
        col_s1.metric("Lignes OK", f"{nb_ok}")
        col_s2.metric("Lignes en écart", f"{nb_ecart}")
        col_s3.metric("Total Shopify", f"{total_sp_detail:.2f} €")
        col_s4.metric("Total Pennylane", f"{total_pl_detail:.2f} €")
else:
    st.info("Cliquez sur le bouton ci-dessus pour charger le détail ligne à ligne")

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
        csv_lines = pd.DataFrame(detail_rows).to_csv(index=False).encode("utf-8")
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
