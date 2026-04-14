import streamlit as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui_common import setup_page

setup_page()

st.title("💰 Suivi de la trésorerie")
st.markdown("Flux de trésorerie et suivi des encaissements / décaissements")

st.info("Module en cours de développement")
