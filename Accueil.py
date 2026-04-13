import streamlit as st

st.set_page_config(
    page_title="Auralis Finances",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

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

    /* Title gradient */
    .main-title {
        text-align: center;
        font-size: 3rem;
        font-weight: 800;
        margin-top: 4rem;
        margin-bottom: 0.3rem;
        background: linear-gradient(135deg, #4C1D95, #7C3AED, #A78BFA);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }
    .subtitle {
        text-align: center;
        font-size: 1.15rem;
        color: #6B7280;
        margin-bottom: 3.5rem;
    }

    /* Cards */
    .card {
        background: white;
        border: 1px solid #E9E5F5;
        border-radius: 16px;
        padding: 2.2rem 1.5rem;
        text-align: center;
        transition: all 0.25s ease;
        box-shadow: 0 2px 8px rgba(124, 58, 237, 0.06);
        margin-bottom: 0.8rem;
    }
    .card:hover {
        transform: translateY(-4px);
        box-shadow: 0 8px 24px rgba(124, 58, 237, 0.15);
        border-color: #7C3AED;
    }
    .card-icon {
        width: 52px;
        height: 52px;
        border-radius: 14px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 1.5rem;
        margin-bottom: 1rem;
    }
    .card-icon.ca { background: linear-gradient(135deg, #EDE9FE, #DDD6FE); }
    .card-icon.tva { background: linear-gradient(135deg, #F3E8FF, #E9D5FF); }
    .card-icon.frais { background: linear-gradient(135deg, #EDE9FE, #C4B5FD); }
    .card-title {
        font-size: 1.1rem;
        font-weight: 700;
        color: #1E1B3A;
        margin-bottom: 0.4rem;
    }
    .card-desc {
        font-size: 0.85rem;
        color: #9CA3AF;
    }

    /* Style page links as cards */
    .main .stPageLink > a {
        background: white !important;
        border: 1px solid #E9E5F5 !important;
        border-radius: 16px !important;
        padding: 2.5rem 1.5rem !important;
        box-shadow: 0 2px 8px rgba(124, 58, 237, 0.06) !important;
        transition: all 0.25s ease !important;
        min-height: 180px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        font-size: 1.05rem !important;
        font-weight: 700 !important;
        color: #1E1B3A !important;
        text-decoration: none !important;
    }
    .main .stPageLink > a:hover {
        transform: translateY(-4px) !important;
        box-shadow: 0 8px 24px rgba(124, 58, 237, 0.15) !important;
        border-color: #7C3AED !important;
    }
    .main .stPageLink > a span {
        color: #1E1B3A !important;
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

# Sidebar
st.sidebar.page_link("Accueil.py", label="**AURALIS FINANCES**")
st.sidebar.markdown('<div class="sidebar-section">Cadrage compta vs outils</div>', unsafe_allow_html=True)
st.sidebar.page_link("pages/1_Cadrage_CA.py", label="📈 Cadrage CA")
st.sidebar.page_link("pages/2_Cadrage_TVA.py", label="🧾 Cadrage TVA")
st.sidebar.page_link("pages/3_Cadrage_Frais.py", label="📊 Cadrage Frais")

# Main
st.markdown('<div class="main-title">AURALIS FINANCES</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">L&#39;outil pensé pour vos dossiers e-commerce.</div>', unsafe_allow_html=True)

col1, col2, col3 = st.columns([1, 1, 1])

with col1:
    st.page_link("pages/1_Cadrage_CA.py", label="📈  Cadrage du CA\n\nChiffre d'affaires commandes vs factures", use_container_width=True)

with col2:
    st.page_link("pages/2_Cadrage_TVA.py", label="🧾  Cadrage de la TVA\n\nTVA collectée commandes vs factures", use_container_width=True)

with col3:
    st.page_link("pages/3_Cadrage_Frais.py", label="📊  Cadrage des Frais\n\nFrais de paiement Shopify vs comptabilité", use_container_width=True)
