import streamlit as st
import pandas as pd
from modules.db import ensure_database, save_case, search_cases, fetch_table, save_dtc

ensure_database()

st.set_page_config(page_title="Diagnostic Case Database", layout="wide")
st.title("Diagnostic Case / DTC Database")

with st.expander("Create new diagnostic case"):
    vin = st.text_input("VIN")
    make = st.text_input("Make")
    model = st.text_input("Model")
    summary = st.text_area("Case summary")
    if st.button("Save case"):
        if vin and summary:
            save_case(vin, make, model, summary)
            st.success("Saved case to the database.")
        else:
            st.error("VIN and summary are required.")

with st.expander("Record DTC history"):
    dtc_vin = st.text_input("DTC VIN")
    dtc_code = st.text_input("DTC code")
    dtc_description = st.text_input("DTC description")
    if st.button("Save DTC"):
        if dtc_vin and dtc_code:
            save_dtc(dtc_vin, dtc_code, dtc_description, source="dashboard")
            st.success("Saved DTC history entry.")
        else:
            st.error("VIN and DTC code are required.")

st.markdown("---")
search_vin = st.text_input("Search cases by VIN")
cases = search_cases(search_vin)
st.subheader("Saved Diagnostic Cases")
st.dataframe(cases)

st.markdown("---")
st.subheader("Recent DTC History")
dtc_history = fetch_table("dtc_history")
if not dtc_history.empty:
    st.dataframe(dtc_history.sort_values("recorded_at", ascending=False).head(50))
else:
    st.info("No DTC history entries yet.")
