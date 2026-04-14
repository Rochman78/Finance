"""
Styles et navigation partagés pour toutes les pages Auralis Finances.
"""
import streamlit as st
from datetime import date, timedelta
import calendar


def setup_page():
    """Applique le style violet + sidebar navigation sur toutes les pages."""
    st.set_page_config(layout="wide")

    st.markdown("""
    <style>
        /* Hide default sidebar page names */
        [data-testid="stSidebarNav"] {display: none;}

        /* Sidebar violet gradient */
        [data-testid="stSidebar"] > div:first-child {
            background: linear-gradient(180deg, #4C1D95 0%, #6D28D9 50%, #7C3AED 100%) !important;
        }
        [data-testid="stSidebar"] * {
            color: white !important;
        }
        [data-testid="stSidebar"] .stPageLink > a {
            background: rgba(255,255,255,0.1) !important;
            border: none !important;
            border-radius: 10px !important;
            margin-bottom: 4px !important;
            padding: 0.6rem 1rem !important;
        }
        [data-testid="stSidebar"] .stPageLink > a:hover {
            background: rgba(255,255,255,0.2) !important;
        }

        /* Sidebar section title */
        .sidebar-section {
            font-size: 0.7rem;
            font-weight: 600;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            opacity: 0.6;
            padding: 0.8rem 0 0.3rem 0.2rem;
        }
    </style>
    """, unsafe_allow_html=True)

    # Sidebar navigation
    st.sidebar.markdown("""
    <div style="padding: 0.5rem 0 0.2rem 0.2rem;">
        <div style="font-size: 1.3rem; font-weight: 800; letter-spacing: 0.05em;">AURALIS FINANCES</div>
        <div style="font-size: 0.75rem; opacity: 0.6; margin-top: 0.1rem;">L'outil pensé pour vos dossiers e-commerce.</div>
    </div>
    """, unsafe_allow_html=True)

    if "_app_mode" not in st.session_state:
        st.session_state["_app_mode"] = "Cabinet"

    mode = st.sidebar.radio("", ["🏢 Cabinet", "🛒 E-commerçant"],
        index=0 if st.session_state["_app_mode"] == "Cabinet" else 1,
        key="app_mode_radio", horizontal=True, label_visibility="collapsed")

    selected = "Cabinet" if "Cabinet" in mode else "E-commerçant"
    if selected != st.session_state["_app_mode"]:
        st.session_state["_app_mode"] = selected
        if selected == "Cabinet":
            st.switch_page("pages/1_Cadrage_CA.py")
        else:
            st.switch_page("pages/7_Dashboard.py")

    current_mode = st.session_state["_app_mode"]
    if current_mode == "Cabinet":
        st.sidebar.markdown('<div class="sidebar-section">Cadrage compta vs outils</div>', unsafe_allow_html=True)
        st.sidebar.page_link("pages/1_Cadrage_CA.py", label="📈 Cadrage CA")
        st.sidebar.page_link("pages/2_Cadrage_TVA.py", label="🧾 Cadrage TVA")
        st.sidebar.page_link("pages/3_Cadrage_Frais.py", label="📊 Cadrage Frais")
        st.sidebar.page_link("pages/4_Cadrage_Encaissements.py", label="💶 Cadrage Encaissements")
        st.sidebar.markdown('<div class="sidebar-section">Déclarations & exports</div>', unsafe_allow_html=True)
        st.sidebar.page_link("pages/5_Etat_Recap_TVA.py", label="📋 État récap de TVA")
        st.sidebar.page_link("pages/6_TVA_OSS.py", label="🇪🇺 TVA OSS")
    else:
        st.sidebar.markdown('<div class="sidebar-section">Outils de pilotage</div>', unsafe_allow_html=True)
        st.sidebar.page_link("pages/7_Dashboard.py", label="📊 Dashboard")
        st.sidebar.page_link("pages/8_Suivi_Tresorerie.py", label="💰 Suivi de la trésorerie")


def period_selector(key_prefix: str = "period", default_date: date = None) -> tuple[date, date]:
    """Sélecteur de période simple date début / date fin."""
    if default_date is None:
        default_date = date(2026, 3, 1)
    col1, col2 = st.columns(2)
    with col1:
        d_min = st.date_input("Date début", value=default_date, key=f"{key_prefix}_min")
    with col2:
        d_max = st.date_input("Date fin", value=default_date, key=f"{key_prefix}_max")
    return d_min, d_max
