import streamlit as st
from modules.ai_assistant import analyze_dtcs
from modules.db import ensure_database, save_ai_summary

ensure_database()

st.set_page_config(page_title="AI Diagnostic Assistant", layout="wide")
st.title("AI Diagnostic Assistant")

st.markdown(
    "Paste a list of DTCs and include vehicle metadata. The assistant analyzes whether an automated service reset or fault clearing can be safely performed."
)

vin = st.text_input("VIN")
make = st.text_input("Make")
model = st.text_input("Model")
dtc_text = st.text_area("DTC list (comma-separated)")

if st.button("Analyze DTCs"):
    dtc_list = [code.strip().upper() for code in dtc_text.replace("\n", ",").split(",") if code.strip()]
    if not vin or not dtc_list:
        st.error("VIN and at least one DTC code are required.")
    else:
        result = analyze_dtcs(vin, make, model, dtc_list)
        st.subheader("AI Recommendation")
        st.markdown(f"**Recommendation:** {result.get('recommendation')}\n\n**Safe to reset:** {result.get('safe_to_reset')}\n\n**Notes:** {result.get('notes')}"
        )
        save_ai_summary(vin, make, model, dtc_text, result.get("recommendation", ""), result.get("notes", ""))
        st.success("AI analysis saved.")

st.markdown("---")
st.info(
    "If you connect OpenAI with OPENAI_API_KEY in .env, the assistant will run live analysis. Otherwise, it uses a local mock heuristic."
)
