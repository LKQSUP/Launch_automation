import streamlit as st
from modules.ticket_monitor import poll_tickets
from modules.db import ensure_database, save_ticket, fetch_table

ensure_database()

st.set_page_config(page_title="Live Ticket Monitor", layout="wide")
st.title("Live Ticket Monitor")

st.markdown("Monitor incoming connected cars from Jifeline and follow automation progress in real time.")

tickets = poll_tickets(limit=6)
for ticket in tickets:
    save_ticket(ticket["vin"], ticket["make"], ticket["model"], ticket["status"])

cols = st.columns([3, 2, 2, 2])
cols[0].markdown("**VIN**")
cols[1].markdown("**Make**")
cols[2].markdown("**Model**")
cols[3].markdown("**Status**")
for ticket in tickets:
    cols[0].write(ticket["vin"])
    cols[1].write(ticket["make"])
    cols[2].write(ticket["model"])
    cols[3].write(ticket["status"])

st.markdown("---")
st.subheader("Ticket History")
st.dataframe(fetch_table("tickets").sort_values("last_seen", ascending=False).head(20))
