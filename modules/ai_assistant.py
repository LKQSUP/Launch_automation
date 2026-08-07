import os
from typing import List
from dotenv import load_dotenv
import openai

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY


def analyze_dtcs(vin: str, make: str, model: str, dtc_list: List[str]) -> dict:
    prompt = (
        "You are an automotive diagnostics analyst. "
        "Evaluate the following DTCs and recommend whether it is safe to perform automated service resets or fault clearing. "
        f"VIN: {vin}, Make: {make}, Model: {model}. "
        f"DTCs: {', '.join(dtc_list)}. "
        "Return a JSON object with keys: recommendation, safe_to_reset, notes."
    )

    if not OPENAI_API_KEY:
        safe = not any(code.startswith("P0") or code.startswith("C0") for code in dtc_list)
        recommendation = "Proceed with reset" if safe else "Review with technician"
        notes = "Mock analysis: no critical powertrain or chassis codes detected." if safe else "Mock analysis: critical codes detected, do not reset automatically."
        return {"recommendation": recommendation, "safe_to_reset": safe, "notes": notes}

    completion = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are an expert automotive diagnostics AI."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=260,
    )

    content = completion.choices[0].message.content.strip()
    return {"recommendation": content, "safe_to_reset": "true" in content.lower()}
