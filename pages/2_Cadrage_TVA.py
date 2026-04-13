import streamlit as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui_common import setup_page

setup_page()

st.title("🧾 Cadrage de la TVA")
st.markdown("Comparaison de la TVA Shopify vs Pennylane")

st.info("Module en cours de développement")
