import streamlit as st
from modules.ticket_monitor import poll_tickets, ticket_history
from modules.db import ensure_database

ensure_database()

st.set_page_config(page_title="Live Ticket Monitor", layout="wide")
st.title("Live Ticket Monitor")

st.markdown(
    " Monitor live automation progress in real time. "
   
)

if st.button("Refresh"):
    st.rerun()

tickets = poll_tickets(limit=20)

cols = st.columns([3, 2, 2, 2, 2, 2, 3])
cols[0].markdown("**VIN**")
cols[1].markdown("**Make**")
cols[2].markdown("**Model**")
cols[3].markdown("**Status**")
cols[4].markdown("**Engineer**")
cols[5].markdown("**Device**")
cols[6].markdown("**Last seen**")

if not tickets:
    st.info("No live tickets yet. Run AutoDetect / a workflow on a connected vehicle to populate this list.")
else:
    for ticket in tickets:
        cols[0].write(ticket["vin"])
        cols[1].write(ticket["make"] or "—")
        cols[2].write(ticket["model"] or "—")
        cols[3].write(ticket["status"] or "—")
        cols[4].write(ticket.get("engineer") or "—")
        device = ticket.get("device_label") or ticket.get("serial") or "—"
        cols[5].write(device)
        cols[6].write(ticket.get("last_seen") or "—")

st.markdown("---")
st.subheader("Ticket History")
history = ticket_history(limit=50)
if history.empty:
    st.info("No ticket history yet.")
else:
    st.dataframe(history, use_container_width=True)
