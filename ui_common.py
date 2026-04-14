"""
Styles et navigation partagés pour toutes les pages Auralis Finances.
"""
import streamlit as st


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
    st.sidebar.page_link("Accueil.py", label="**AURALIS FINANCES**")
    st.sidebar.markdown('<div class="sidebar-section">Cadrage compta vs outils</div>', unsafe_allow_html=True)
    st.sidebar.page_link("pages/1_Cadrage_CA.py", label="📈 Cadrage CA")
    st.sidebar.page_link("pages/2_Cadrage_TVA.py", label="🧾 Cadrage TVA")
    st.sidebar.page_link("pages/3_Cadrage_Frais.py", label="📊 Cadrage Frais")
    st.sidebar.page_link("pages/4_Cadrage_Encaissements.py", label="💶 Cadrage Encaissements")
    st.sidebar.markdown('<div class="sidebar-section">Déclarations & exports</div>', unsafe_allow_html=True)
    st.sidebar.page_link("pages/5_Etat_Recap_TVA.py", label="📋 État récap de TVA")
    st.sidebar.page_link("pages/6_TVA_OSS.py", label="🇪🇺 TVA OSS")
