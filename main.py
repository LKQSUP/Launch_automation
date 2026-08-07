import streamlit as st

st.set_page_config(page_title="LKQ Remote Support", page_icon="🚘", layout="wide")

st.title("LKQ Remote Support Dashboard")
st.markdown(
    "This workspace provides a multi-page Streamlit dashboard for Jifeline ticket monitoring, Launch X431 ADB automation, diagnostic case tracking, and AI-assisted DTC analysis."
)

st.sidebar.title("Navigation")
st.sidebar.info(
    "Use the page menu at the top to switch between Live Ticket Monitor, ADB Controller, Diagnostic Case Database, and AI Diagnostic Assistant."
)

st.sidebar.markdown("---")
st.sidebar.markdown("Built for remote automotive diagnostics and Launch X431 Euro Link pre-scan automation.")
