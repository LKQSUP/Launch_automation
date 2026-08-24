import streamlit as st

st.set_page_config(page_title="LKQ Remote Support", page_icon="🚘", layout="wide")

st.title("LKQ Remote Support Dashboard")
st.markdown(
    "Multi-page ops dashboard for Jifeline ticket monitoring, **local** Launch X431 "
    "Euro Link automation (uiautomator2 + EasyOCR), VIN audit, and admin session debug — "
    "."
)

st.sidebar.title("Navigation")
st.sidebar.info(
    "Use the page menu to open Live Ticket Monitor, ADB Controller, or Admin."
)

st.sidebar.markdown("---")
st.sidebar.markdown(
    "Built for remote automotive diagnostics and Launch X431 Euro Link "
    "pre-scan automation on USB/Wi-Fi connected tablets."
)

st.markdown("### Modules")
st.markdown(
    """
| Page | Purpose |
|------|---------|
| **Live Ticket Monitor** | Real live connected-car tickets (VIN / make / model / status) |
| **Launch X431 ADB Controller** | Multi-brand auto detection (VAG focus now) → VIN audit → Diagnostic → ECU scan |
| **Admin** | Full management, audit history, and stuck/error session debug |
"""
)

