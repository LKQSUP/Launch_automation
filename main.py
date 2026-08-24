"""Standalone entry for this repo only.

No landing page and no login — those live on the host app
(https://lkq-toolbox.replit.app/). Copy `pages/` + `modules/` into that
app; do not copy this file (it would replace the toolbox home).
"""

from __future__ import annotations

import streamlit as st

pg = st.navigation(
    [
        st.Page(
            "pages/2_Launch_X431_ADB_Controller.py",
            title="Launch X431 ADB Controller",
            default=True,
        ),
        st.Page("pages/1_Live_Ticket_Monitor.py", title="Live Ticket Monitor"),
        st.Page("pages/3_Admin.py", title="Admin"),
    ]
)
pg.run()
