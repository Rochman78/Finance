import streamlit as st
import pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta
from collections import defaultdict
from cadrage_ca import get_ca_shopify
from ui_common import setup_page

setup_page()

st.title("📊 Dashboard")

# =============================================================
# SÉLECTEUR DE PÉRIODE
# =============================================================
SHORTCUTS = {
    "Aujourd'hui": lambda: (date.today(), date.today()),
    "Hier": lambda: (date.today() - timedelta(days=1), date.today() - timedelta(days=1)),
    "Cette semaine": lambda: (date.today() - timedelta(days=date.today().weekday()), date.today()),
    "Ce mois": lambda: (date.today().replace(day=1), date.today()),
    "Cette année": lambda: (date(date.today().year, 1, 1), date.today()),
    "Mois dernier": lambda: (
        (date.today().replace(day=1) - timedelta(days=1)).replace(day=1),
        date.today().replace(day=1) - timedelta(days=1)
    ),
    "Année dernière": lambda: (date(date.today().year - 1, 1, 1), date(date.today().year - 1, 12, 31)),
    "Personnalisé": None,
}

def auto_compare(shortcut, d_min, d_max):
    delta = (d_max - d_min).days + 1
    if shortcut in ("Aujourd'hui", "Hier"):
        return d_min - timedelta(days=1), d_max - timedelta(days=1)
    elif shortcut == "Cette semaine":
        return d_min - timedelta(days=7), d_max - timedelta(days=7)
    elif shortcut in ("Ce mois", "Mois dernier"):
        prev_end = d_min - timedelta(days=1)
        return prev_end.replace(day=1), prev_end
    elif shortcut in ("Cette année", "Année dernière"):
        return date(d_min.year - 1, 1, 1), date(d_min.year - 1, 12, 31)
    else:
        return d_min - timedelta(days=delta), d_min - timedelta(days=1)

col_period, col_compare = st.columns(2)

with col_period:
    shortcut = st.selectbox("Période", list(SHORTCUTS.keys()), index=3, key="dash_shortcut")
    if shortcut == "Personnalisé":
        c1, c2 = st.columns(2)
        with c1:
            d_min = st.date_input("Début", value=date(2026, 3, 1), key="dash_min")
        with c2:
            d_max = st.date_input("Fin", value=date(2026, 3, 15), key="dash_max")
    else:
        d_min, d_max = SHORTCUTS[shortcut]()

with col_compare:
    compare_mode = st.selectbox("Comparer avec", ["Période précédente", "Année précédente (N-1)", "Personnalisé", "Pas de comparaison"], key="dash_compare_mode")
    if compare_mode == "Année précédente (N-1)":
        cmp_min = d_min.replace(year=d_min.year - 1)
        try:
            cmp_max = d_max.replace(year=d_max.year - 1)
        except ValueError:
            cmp_max = d_max.replace(year=d_max.year - 1, day=28)
    elif compare_mode == "Personnalisé":
        c1, c2 = st.columns(2)
        with c1:
            cmp_min = st.date_input("Début comparaison", value=d_min - timedelta(days=30), key="dash_cmp_min")
        with c2:
            cmp_max = st.date_input("Fin comparaison", value=d_min - timedelta(days=1), key="dash_cmp_max")
    elif compare_mode == "Période précédente":
        cmp_min, cmp_max = auto_compare(shortcut, d_min, d_max)
    else:
        cmp_min, cmp_max = None, None

# Chargement sur bouton
run = st.button("🚀 Charger", type="primary", use_container_width=True)

if run:
    d_min_str = d_min.strftime("%Y-%m-%d")
    d_max_str = d_max.strftime("%Y-%m-%d")
    with st.spinner("Chargement..."):
        st.session_state["dash_data"] = get_ca_shopify(d_min_str, d_max_str)
        st.session_state["dash_dates"] = (d_min_str, d_max_str)

    if cmp_min and cmp_max:
        cmp_min_str = cmp_min.strftime("%Y-%m-%d")
        cmp_max_str = cmp_max.strftime("%Y-%m-%d")
        with st.spinner("Chargement comparaison..."):
            st.session_state["dash_cmp_data"] = get_ca_shopify(cmp_min_str, cmp_max_str)
            st.session_state["dash_cmp_dates"] = (cmp_min_str, cmp_max_str)
    else:
        st.session_state.pop("dash_cmp_data", None)
        st.session_state.pop("dash_cmp_dates", None)

if "dash_data" not in st.session_state:
    st.info("Sélectionnez une période et cliquez sur **Charger**")
    st.stop()

sp_all = st.session_state["dash_data"]
d_min_str, d_max_str = st.session_state["dash_dates"]
has_compare = "dash_cmp_data" in st.session_state
sp_cmp_all = st.session_state.get("dash_cmp_data", {})
cmp_dates = st.session_state.get("dash_cmp_dates", ("", ""))

# Filtre boutique
all_stores = sorted([s for s, d in sp_all.items() if d.get("ca_ht") and d["ca_ht"] > 0])
store_filter = st.selectbox("Boutique", ["Toutes"] + all_stores, key="dash_store_filter")

if store_filter == "Toutes":
    sp = sp_all
    sp_cmp = sp_cmp_all
else:
    sp = {store_filter: sp_all.get(store_filter, {"ca_ht": 0, "nb_orders": 0, "by_date": {}, "orders": []})}
    sp_cmp = {store_filter: sp_cmp_all.get(store_filter, {"ca_ht": 0, "nb_orders": 0, "by_date": {}, "orders": []})} if has_compare else {}

# =============================================================
# CALCULS
# =============================================================
def compute_metrics(data):
    ca = round(sum(s["ca_ht"] for s in data.values() if s["ca_ht"] is not None), 2)
    orders = sum(s["nb_orders"] for s in data.values())
    avg = round(ca / orders, 2) if orders > 0 else 0
    # Remboursements
    refund_ca = 0.0
    refund_count = 0
    for sdata in data.values():
        for o in sdata.get("orders", []):
            fs = o.get("financial_status", "")
            if fs in ("refunded", "partially_refunded"):
                refund_ca += abs(o["ca_ht"])
                refund_count += 1
    refund_ca = round(refund_ca, 2)
    refund_rate = round(refund_ca / ca * 100, 1) if ca > 0 else 0
    return {"ca": ca, "orders": orders, "avg": avg, "refund_ca": refund_ca, "refund_count": refund_count, "refund_rate": refund_rate}

m = compute_metrics(sp)
if has_compare:
    mc = compute_metrics(sp_cmp)
    def delta_pct(val, val_c):
        if val_c == 0: return 0
        return round((val - val_c) / val_c * 100, 1)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  KPI                                                           ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")

periode_txt = f"{d_min_str} → {d_max_str}"
if has_compare:
    st.caption(f"Période : {periode_txt} — Comparaison : {cmp_dates[0]} → {cmp_dates[1]}")
else:
    st.caption(f"Période : {periode_txt}")

col1, col2, col3, col4 = st.columns(4)

if has_compare:
    col1.metric("CA HT", f"{m['ca']:,.2f} €", delta=f"{delta_pct(m['ca'], mc['ca']):+.1f}%")
    col2.metric("Commandes", f"{m['orders']}", delta=f"{delta_pct(m['orders'], mc['orders']):+.1f}%")
    col3.metric("Panier moyen", f"{m['avg']:,.2f} €", delta=f"{delta_pct(m['avg'], mc['avg']):+.1f}%")
    col4.metric("Taux remboursement", f"{m['refund_rate']:.1f}%", delta=f"{m['refund_rate'] - mc['refund_rate']:+.1f} pts", delta_color="inverse")
else:
    col1.metric("CA HT", f"{m['ca']:,.2f} €")
    col2.metric("Commandes", f"{m['orders']}")
    col3.metric("Panier moyen", f"{m['avg']:,.2f} €")
    col4.metric("Taux remboursement", f"{m['refund_rate']:.1f}%")

# ╔═══════════════════════════════════════════════════════════════╗
# ║  TABLEAU PAR BOUTIQUE                                          ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")

def hl_total(row):
    if row["Boutique"] == "TOTAL":
        return ["font-weight: bold"] * len(row)
    return [""] * len(row)

tab_ca, tab_cmd, tab_pm, tab_remb = st.tabs(["📈 CA par boutique", "🛒 Commandes par boutique", "🧺 Panier moyen par boutique", "↩️ Remboursements par boutique"])

# ─── CA ───
with tab_ca:
    rows = []
    for sname, sdata in sp.items():
        if sdata["ca_ht"] is None or sdata["ca_ht"] <= 0: continue
        row = {"Boutique": sname, "CA HT": sdata["ca_ht"]}
        if has_compare:
            ca_c = (sp_cmp.get(sname, {}).get("ca_ht") or 0)
            row["CA HT N-1"] = ca_c
            row["Var."] = f"{delta_pct(sdata['ca_ht'], ca_c):+.1f}%" if ca_c > 0 else "—"
        rows.append(row)
    rows.sort(key=lambda r: r["CA HT"], reverse=True)
    total = {"Boutique": "TOTAL", "CA HT": m["ca"]}
    if has_compare: total["CA HT N-1"] = mc["ca"]; total["Var."] = f"{delta_pct(m['ca'], mc['ca']):+.1f}%"
    rows.append(total)
    st.dataframe(pd.DataFrame(rows).style.apply(hl_total, axis=1), use_container_width=True, hide_index=True,
        column_config={"CA HT": st.column_config.NumberColumn(format="%.2f"), "CA HT N-1": st.column_config.NumberColumn(format="%.2f")})

# ─── Commandes ───
with tab_cmd:
    rows = []
    for sname, sdata in sp.items():
        if sdata["ca_ht"] is None or sdata["ca_ht"] <= 0: continue
        row = {"Boutique": sname, "Commandes": sdata["nb_orders"]}
        if has_compare:
            o_c = sp_cmp.get(sname, {}).get("nb_orders", 0)
            row["Commandes N-1"] = o_c
            row["Var."] = f"{delta_pct(sdata['nb_orders'], o_c):+.1f}%" if o_c > 0 else "—"
        rows.append(row)
    rows.sort(key=lambda r: r["Commandes"], reverse=True)
    total = {"Boutique": "TOTAL", "Commandes": m["orders"]}
    if has_compare: total["Commandes N-1"] = mc["orders"]; total["Var."] = f"{delta_pct(m['orders'], mc['orders']):+.1f}%"
    rows.append(total)
    st.dataframe(pd.DataFrame(rows).style.apply(hl_total, axis=1), use_container_width=True, hide_index=True)

# ─── Panier moyen ───
with tab_pm:
    rows = []
    for sname, sdata in sp.items():
        if sdata["ca_ht"] is None or sdata["ca_ht"] <= 0: continue
        pm = round(sdata["ca_ht"] / sdata["nb_orders"], 2) if sdata["nb_orders"] > 0 else 0
        row = {"Boutique": sname, "Panier moyen": pm}
        if has_compare:
            ca_c = sp_cmp.get(sname, {}).get("ca_ht") or 0
            o_c = sp_cmp.get(sname, {}).get("nb_orders", 0)
            pm_c = round(ca_c / o_c, 2) if o_c > 0 else 0
            row["Panier moyen N-1"] = pm_c
            row["Var."] = f"{delta_pct(pm, pm_c):+.1f}%" if pm_c > 0 else "—"
        rows.append(row)
    rows.sort(key=lambda r: r["Panier moyen"], reverse=True)
    total = {"Boutique": "TOTAL", "Panier moyen": m["avg"]}
    if has_compare: total["Panier moyen N-1"] = mc["avg"]; total["Var."] = f"{delta_pct(m['avg'], mc['avg']):+.1f}%"
    rows.append(total)
    st.dataframe(pd.DataFrame(rows).style.apply(hl_total, axis=1), use_container_width=True, hide_index=True,
        column_config={"Panier moyen": st.column_config.NumberColumn(format="%.2f"), "Panier moyen N-1": st.column_config.NumberColumn(format="%.2f")})

# ─── Remboursements ───
with tab_remb:
    rows = []
    for sname, sdata in sp.items():
        if sdata["ca_ht"] is None or sdata["ca_ht"] <= 0: continue
        refunds = [o for o in sdata.get("orders", []) if o.get("financial_status") in ("refunded", "partially_refunded")]
        r_ca = round(sum(abs(o["ca_ht"]) for o in refunds), 2)
        r_count = len(refunds)
        r_rate = round(r_ca / sdata["ca_ht"] * 100, 1) if sdata["ca_ht"] > 0 else 0
        row = {"Boutique": sname, "Remboursements": r_ca, "Nb remb.": r_count, "Taux": f"{r_rate:.1f}%"}
        rows.append(row)
    rows.sort(key=lambda r: r["Remboursements"], reverse=True)
    total = {"Boutique": "TOTAL", "Remboursements": m["refund_ca"], "Nb remb.": m["refund_count"], "Taux": f"{m['refund_rate']:.1f}%"}
    rows.append(total)
    st.dataframe(pd.DataFrame(rows).style.apply(hl_total, axis=1), use_container_width=True, hide_index=True,
        column_config={"Remboursements": st.column_config.NumberColumn(format="%.2f")})
