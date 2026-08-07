import sqlite3
from pathlib import Path
import pandas as pd
from typing import List, Dict, Any

DB_PATH = Path(__file__).resolve().parent.parent / "lkq_remote_support.db"

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, make TEXT, model TEXT, status TEXT, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS cases (id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, make TEXT, model TEXT, summary TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS dtc_history (id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, dtc_code TEXT, dtc_description TEXT, source TEXT, recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS ai_summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, make TEXT, model TEXT, dtc_text TEXT, recommendation TEXT, ai_notes TEXT, generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT, workflow TEXT, status TEXT, summary TEXT, file_path TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
]


def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn


def init_db():
    conn = get_connection()
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def fetch_table(name: str) -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql_query(f"SELECT * FROM {name}", conn)
        return df
    finally:
        conn.close()


def search_cases(vin: str = "") -> pd.DataFrame:
    conn = get_connection()
    try:
        if vin.strip():
            df = pd.read_sql_query("SELECT * FROM cases WHERE vin LIKE ? ORDER BY created_at DESC", conn, params=(f"%{vin}%",))
        else:
            df = pd.read_sql_query("SELECT * FROM cases ORDER BY created_at DESC LIMIT 50", conn)
        return df
    finally:
        conn.close()


def save_case(vin: str, make: str, model: str, summary: str):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO cases (vin, make, model, summary) VALUES (?, ?, ?, ?)",
            (vin, make, model, summary),
        )
        conn.commit()
    finally:
        conn.close()


def save_ticket(vin: str, make: str, model: str, status: str):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO tickets (vin, make, model, status) VALUES (?, ?, ?, ?)",
            (vin, make, model, status),
        )
        conn.commit()
    finally:
        conn.close()


def save_dtc(vin: str, dtc_code: str, dtc_description: str, source: str = "manual"):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO dtc_history (vin, dtc_code, dtc_description, source) VALUES (?, ?, ?, ?)",
            (vin, dtc_code, dtc_description, source),
        )
        conn.commit()
    finally:
        conn.close()


def save_ai_summary(vin: str, make: str, model: str, dtc_text: str, recommendation: str, ai_notes: str):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO ai_summaries (vin, make, model, dtc_text, recommendation, ai_notes) VALUES (?, ?, ?, ?, ?, ?)",
            (vin, make, model, dtc_text, recommendation, ai_notes),
        )
        conn.commit()
    finally:
        conn.close()


def save_report(serial: str, workflow: str, status: str, summary: str, file_path: str | None = None):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO reports (serial, workflow, status, summary, file_path) VALUES (?, ?, ?, ?, ?)",
            (serial, workflow, status, summary, file_path),
        )
        conn.commit()
    finally:
        conn.close()


def ensure_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_db()
