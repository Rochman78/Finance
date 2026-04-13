import streamlit as st
import pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta
from collections import defaultdict, Counter
from cadrage_ca import get_ca_shopify, get_ca_pennylane, STORE_PREFIXES
from ui_common import setup_page

setup_page()

st.title("📈 Cadrage du Chiffre d'Affaires")
st.markdown("Commandes Shopify (CA HT) vs Comptabilité Pennylane (707 / VT)")

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
    with st.spinner("Chargement du CA Shopify..."):
        st.session_state["ca_shopify"] = get_ca_shopify(date_min_str, date_max_str)
    with st.spinner("Chargement du CA Pennylane (707/VT)..."):
        st.session_state["ca_pennylane"] = get_ca_pennylane(date_min_str, date_max_str)
    st.session_state["ca_date_min"] = date_min_str
    st.session_state["ca_date_max"] = date_max_str

if "ca_shopify" not in st.session_state:
    st.info("Sélectionnez une période et cliquez sur **Lancer le cadrage**")
    st.stop()

sp = st.session_state["ca_shopify"]
pl = st.session_state["ca_pennylane"]
date_min_str = st.session_state["ca_date_min"]
date_max_str = st.session_state["ca_date_max"]
multi_day = date_min_str != date_max_str

# =============================================================
# CALCULS
# =============================================================
total_sp = round(sum(s["ca_ht"] for s in sp.values() if s["ca_ht"] is not None), 2)
total_pl = pl["total_ht"]
ecart = round(total_sp - total_pl, 2)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  1. RÉSULTAT DU CADRAGE                                      ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")

if abs(ecart) < 0.50:
    st.markdown("""
    <div style="background: linear-gradient(135deg, #ECFDF5, #D1FAE5); border: 1px solid #6EE7B7;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin-bottom: 1rem;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #065F46;">✅ Cadrage OK</div>
        <div style="font-size: 0.95rem; color: #047857; margin-top: 0.3rem;">
            Aucun écart significatif sur la période
        </div>
    </div>
    """, unsafe_allow_html=True)
else:
    st.markdown(f"""
    <div style="background: linear-gradient(135deg, #FEF2F2, #FECACA); border: 1px solid #FCA5A5;
                border-radius: 12px; padding: 1.5rem 2rem; text-align: center; margin-bottom: 1rem;">
        <div style="font-size: 1.5rem; font-weight: 800; color: #991B1B;">❌ Écart détecté : {ecart:,.2f} €</div>
        <div style="font-size: 0.95rem; color: #B91C1C; margin-top: 0.3rem;">
            Investigation requise — voir le détail ci-dessous
        </div>
    </div>
    """, unsafe_allow_html=True)

# 1.1 Résultat de la période
if multi_day:
    st.subheader("Résultat du cadrage")
    st.markdown("**Résultat de la période**")
else:
    st.subheader("Résultat du cadrage")

col1, col2, col3 = st.columns(3)
col1.metric("CA HT Shopify", f"{total_sp:,.2f} €")
col2.metric("CA HT Pennylane", f"{total_pl:,.2f} €")
col3.metric("Écart", f"{ecart:,.2f} €")

# 1.2 Résultat jour par jour (si multi-jours)
if multi_day:
    st.markdown("**Résultat jour par jour**")

    daily_sp = defaultdict(lambda: {"ca_ht": 0.0, "nb_orders": 0})
    for sdata in sp.values():
        for d, day_data in sdata.get("by_date", {}).items():
            daily_sp[d]["ca_ht"] += day_data["ca_ht"]
            daily_sp[d]["nb_orders"] += day_data["nb_orders"]

    daily_pl = pl["by_date"]
    all_days = sorted(set(list(daily_sp.keys()) + list(daily_pl.keys())))

    daily_rows = []
    for day in all_days:
        sp_day = round(daily_sp[day]["ca_ht"], 2)
        pl_day = round(daily_pl.get(day, {}).get("ca_ht", 0), 2)
        ecart_day = round(sp_day - pl_day, 2)
        daily_rows.append({
            "Date": day,
            "CA HT Shopify": sp_day,
            "Commandes": daily_sp[day]["nb_orders"],
            "CA HT Pennylane": pl_day,
            "Écritures PL": daily_pl.get(day, {}).get("nb_ecritures", 0),
            "Écart": ecart_day,
            "Statut": "✅" if abs(ecart_day) < 0.50 else f"❌ {ecart_day:+,.2f}€",
        })

    if daily_rows:
        df_daily = pd.DataFrame(daily_rows)

        def highlight_ecart_row(row):
            if abs(row["Écart"]) >= 0.50:
                return ["background-color: rgba(255, 50, 50, 0.15)"] * len(row)
            return [""] * len(row)

        st.dataframe(
            df_daily.style.apply(highlight_ecart_row, axis=1),
            use_container_width=True,
            hide_index=True,
        )

        nb_jours_ok = len([r for r in daily_rows if abs(r["Écart"]) < 0.50])
        nb_jours_ko = len([r for r in daily_rows if abs(r["Écart"]) >= 0.50])
        col_d1, col_d2, col_d3 = st.columns(3)
        col_d1.metric("Jours", f"{len(daily_rows)}")
        col_d2.metric("Jours OK", f"{nb_jours_ok}")
        col_d3.metric("Jours en écart", f"{nb_jours_ko}", delta=f"{nb_jours_ko}" if nb_jours_ko > 0 else None, delta_color="inverse")

        # Drill-down
        st.write("")
        day_options = ["—"] + [f"{'❌' if abs(r['Écart']) >= 0.50 else '✅'} {r['Date']}  ({r['Écart']:+,.2f}€)" for r in daily_rows]
        day_keys = [None] + [r["Date"] for r in daily_rows]
        selected_idx = st.selectbox("🔎 Creuser un jour", range(len(day_options)), format_func=lambda i: day_options[i])

        if selected_idx and selected_idx > 0:
            selected_day = day_keys[selected_idx]
            st.markdown(f"#### Détail du {selected_day}")

            day_store_rows = []
            for sname, sdata in sp.items():
                sp_day_store = sdata.get("by_date", {}).get(selected_day, {}).get("ca_ht", 0)
                pl_day_store_filtered = sum(
                    l["ca_ht"] for l in pl["lines"]
                    if l["date"] == selected_day and l.get("store") == sname
                )
                if sp_day_store or pl_day_store_filtered:
                    day_store_rows.append({
                        "Boutique": sname,
                        "CA HT Shopify": round(sp_day_store, 2),
                        "CA HT Pennylane": round(pl_day_store_filtered, 2),
                        "Écart": round(sp_day_store - pl_day_store_filtered, 2),
                    })

            unknown_day = sum(
                l["ca_ht"] for l in pl["lines"]
                if l["date"] == selected_day and l.get("store") is None
            )
            if unknown_day:
                day_store_rows.append({
                    "Boutique": "(INCONNU)",
                    "CA HT Shopify": 0,
                    "CA HT Pennylane": round(unknown_day, 2),
                    "Écart": round(-unknown_day, 2),
                })

            if day_store_rows:
                st.dataframe(pd.DataFrame(day_store_rows), use_container_width=True, hide_index=True)

            day_lines = [l for l in pl["lines"] if l["date"] == selected_day]
            if day_lines:
                st.markdown("**Écritures 707 (VT) Pennylane ce jour :**")
                line_rows = [{
                    "Écriture": l["entry_label"][:60],
                    "Commande": l.get("order_ref") or "—",
                    "Boutique": l.get("store") or "?",
                    "CA HT": l["ca_ht"],
                    "Compte": l["account"],
                } for l in day_lines]
                st.dataframe(pd.DataFrame(line_rows), use_container_width=True, hide_index=True)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  2. ÉLÉMENTS DE JUSTIFICATION                                 ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Éléments de justification")

# ─────────────────────────────────────────────────────────
# 2.1 CA HT PENNYLANE
# ─────────────────────────────────────────────────────────
st.markdown("### CA HT Pennylane")

# 2.1.1 Par compte comptable
st.markdown("**Par compte comptable**")

account_data = defaultdict(lambda: {"ca_ht": 0.0, "count": 0, "name": ""})
for l in pl["lines"]:
    key = l["account"]
    account_data[key]["ca_ht"] += l["ca_ht"]
    account_data[key]["count"] += 1
    if l.get("account_name") and not account_data[key]["name"]:
        account_data[key]["name"] = l["account_name"]

account_rows = []
for acc in sorted(account_data.keys()):
    d = account_data[acc]
    account_rows.append({
        "Compte": acc,
        "Libellé": d["name"],
        "CA HT": round(d["ca_ht"], 2),
        "Écritures": d["count"],
    })

if account_rows:
    df_accounts = pd.DataFrame(account_rows)
    total_acc = {
        "Compte": "TOTAL",
        "Libellé": "",
        "CA HT": round(df_accounts["CA HT"].sum(), 2),
        "Écritures": df_accounts["Écritures"].sum(),
    }
    df_accounts = pd.concat([df_accounts, pd.DataFrame([total_acc])], ignore_index=True)
    st.dataframe(df_accounts, use_container_width=True, hide_index=True)

# 2.1.2 Par boutique Shopify
st.markdown("**Par boutique Shopify**")

pl_store_rows = []
for sname in sorted(STORE_PREFIXES.keys()):
    pl_store_ca = round(sum(l["ca_ht"] for l in pl["lines"] if l.get("store") == sname), 2)
    pl_store_count = sum(1 for l in pl["lines"] if l.get("store") == sname)
    if pl_store_ca != 0 or pl_store_count > 0:
        pl_store_rows.append({
            "Boutique": sname,
            "CA HT Pennylane": pl_store_ca,
            "Écritures": pl_store_count,
        })

unknown_pl = round(sum(l["ca_ht"] for l in pl["lines"] if l.get("store") is None), 2)
unknown_count = sum(1 for l in pl["lines"] if l.get("store") is None)
if unknown_pl or unknown_count:
    pl_store_rows.append({
        "Boutique": "(INCONNU)",
        "CA HT Pennylane": unknown_pl,
        "Écritures": unknown_count,
    })

if pl_store_rows:
    df_pl_stores = pd.DataFrame(pl_store_rows)
    total_pl_stores = {
        "Boutique": "TOTAL",
        "CA HT Pennylane": round(df_pl_stores["CA HT Pennylane"].sum(), 2),
        "Écritures": df_pl_stores["Écritures"].sum(),
    }
    df_pl_stores = pd.concat([df_pl_stores, pd.DataFrame([total_pl_stores])], ignore_index=True)
    st.dataframe(df_pl_stores, use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────────────────
# 2.2 CA HT SHOPIFY
# ─────────────────────────────────────────────────────────
st.markdown("### CA HT Shopify")
st.markdown("**Par boutique**")

sp_store_rows = []
for sname, sdata in sp.items():
    if sdata["ca_ht"] is not None and sdata["ca_ht"] > 0:
        sp_store_rows.append({
            "Boutique": sname,
            "CA HT Shopify": sdata["ca_ht"],
            "Commandes": sdata["nb_orders"],
        })

if sp_store_rows:
    df_sp_stores = pd.DataFrame(sp_store_rows)
    total_sp_stores = {
        "Boutique": "TOTAL",
        "CA HT Shopify": round(df_sp_stores["CA HT Shopify"].sum(), 2),
        "Commandes": df_sp_stores["Commandes"].sum(),
    }
    df_sp_stores = pd.concat([df_sp_stores, pd.DataFrame([total_sp_stores])], ignore_index=True)
    st.dataframe(df_sp_stores, use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────────────────
# 2.3 CADRAGE PAR BOUTIQUE (SP vs PL)
# ─────────────────────────────────────────────────────────
st.markdown("### Cadrage par boutique")

cadrage_rows = []
for sname, sdata in sp.items():
    sp_ca = sdata["ca_ht"] if sdata["ca_ht"] is not None else 0
    pl_ca = round(sum(l["ca_ht"] for l in pl["lines"] if l.get("store") == sname), 2)
    if sp_ca > 0 or pl_ca != 0:
        ecart_b = round(sp_ca - pl_ca, 2)
        cadrage_rows.append({
            "Boutique": sname,
            "CA HT Shopify": sp_ca,
            "CA HT Pennylane": pl_ca,
            "Écart": ecart_b,
            "Statut": "✅" if abs(ecart_b) < 0.50 else f"❌ {ecart_b:+,.2f}€",
        })

unknown_ca = round(sum(l["ca_ht"] for l in pl["lines"] if l.get("store") is None), 2)
if unknown_ca:
    cadrage_rows.append({
        "Boutique": "(INCONNU)",
        "CA HT Shopify": 0,
        "CA HT Pennylane": unknown_ca,
        "Écart": round(-unknown_ca, 2),
        "Statut": f"❌ {-unknown_ca:+,.2f}€",
    })

if cadrage_rows:
    df_cadrage = pd.DataFrame(cadrage_rows)
    total_cadrage = {
        "Boutique": "TOTAL",
        "CA HT Shopify": round(df_cadrage["CA HT Shopify"].sum(), 2),
        "CA HT Pennylane": round(df_cadrage["CA HT Pennylane"].sum(), 2),
        "Écart": round(df_cadrage["Écart"].sum(), 2),
        "Statut": "",
    }
    df_cadrage = pd.concat([df_cadrage, pd.DataFrame([total_cadrage])], ignore_index=True)
    st.dataframe(df_cadrage, use_container_width=True, hide_index=True)

# ╔═══════════════════════════════════════════════════════════════╗
# ║  3. EXPORT                                                     ║
# ╚═══════════════════════════════════════════════════════════════╝
st.markdown("---")
st.subheader("Export")

col1, col2 = st.columns(2)

with col1:
    if cadrage_rows:
        csv_cadrage = df_cadrage.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Cadrage par boutique (CSV)",
            csv_cadrage,
            f"cadrage_ca_{date_min_str}_{date_max_str}.csv",
            "text/csv",
        )

with col2:
    if pl["lines"]:
        df_inv = pd.DataFrame(pl["lines"])
        csv_inv = df_inv.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Écritures 707/VT (CSV)",
            csv_inv,
            f"ecritures_707_{date_min_str}_{date_max_str}.csv",
            "text/csv",
        )
