import streamlit as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui_common import setup_page

setup_page()

st.title("🇪🇺 TVA OSS")
st.markdown("Déclaration TVA One-Stop-Shop — Ventilation par pays de destination")

st.info("Module en cours de développement")
