# LKQ Remote Support Dashboard

A Streamlit dashboard for LKQ Remote Support to monitor Jifeline tickets, manage Launch X431 automation, and store diagnostic history.

## Features
- Live Ticket Monitor for connected cars, current VIN, and active automation status
- Launch X431 ADB Controller for Auto-VIN, System Scan, and Clear Memory actions
- Diagnostic Case / DTC Database with VIN search and history storage
- AI Diagnostic Assistant for DTC analysis and safe action recommendations

## Run
1. Create a virtual environment
2. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

3. Set environment variables in `.env` (optional):

```bash
OPENAI_API_KEY=your_api_key_here
```

4. Launch Streamlit:

```bash
streamlit run main.py
```
