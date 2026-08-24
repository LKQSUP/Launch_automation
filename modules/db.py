import sqlite3
from contextvars import ContextVar
from pathlib import Path
import pandas as pd
from typing import List, Dict, Any, Optional

# Set on the worker thread when a scan starts so every save_* row is attributed.
_operator: ContextVar[Dict[str, str]] = ContextVar("lkq_operator", default={})

DB_PATH = Path(__file__).resolve().parent.parent / "lkq_remote_support.db"

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, make TEXT, model TEXT, status TEXT, serial TEXT, device_label TEXT, engineer TEXT, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS cases (id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, make TEXT, model TEXT, summary TEXT, serial TEXT, device_label TEXT, engineer TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS dtc_history (id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, dtc_code TEXT, dtc_description TEXT, source TEXT, serial TEXT, device_label TEXT, engineer TEXT, recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS ai_summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, make TEXT, model TEXT, dtc_text TEXT, recommendation TEXT, ai_notes TEXT, serial TEXT, device_label TEXT, engineer TEXT, generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT, device_label TEXT, workflow TEXT, status TEXT, summary TEXT, file_path TEXT, engineer TEXT, report_email TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS vin_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT, device_label TEXT, brand_selected TEXT, make_detected TEXT, model TEXT, vin TEXT, software TEXT, source TEXT, engineer TEXT, report_email TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
]

# Columns added after initial schema — applied on ensure_database().
_SCHEMA_MIGRATIONS = [
    ("vin_audit", "model", "TEXT"),
    ("vin_audit", "device_label", "TEXT"),
    ("reports", "device_label", "TEXT"),
    ("tickets", "serial", "TEXT"),
    ("tickets", "device_label", "TEXT"),
    ("cases", "serial", "TEXT"),
    ("cases", "device_label", "TEXT"),
    ("dtc_history", "serial", "TEXT"),
    ("dtc_history", "device_label", "TEXT"),
    ("ai_summaries", "serial", "TEXT"),
    ("ai_summaries", "device_label", "TEXT"),
    ("tickets", "engineer", "TEXT"),
    ("cases", "engineer", "TEXT"),
    ("dtc_history", "engineer", "TEXT"),
    ("ai_summaries", "engineer", "TEXT"),
    ("reports", "engineer", "TEXT"),
    ("reports", "report_email", "TEXT"),
    ("vin_audit", "engineer", "TEXT"),
    ("vin_audit", "report_email", "TEXT"),
]


def set_operator_context(engineer: str = "", report_email: str = "") -> None:
    """Bind engineer + report recipient for all save_* calls on this thread."""
    _operator.set(
        {
            "engineer": (engineer or "").strip(),
            "report_email": (report_email or "").strip(),
        }
    )


def _operator_fields(
    engineer: Optional[str] = None,
    report_email: Optional[str] = None,
) -> tuple[str, str]:
    ctx = _operator.get() or {}
    eng = (engineer or "").strip() or str(ctx.get("engineer") or "").strip()
    mail = (report_email or "").strip() or str(ctx.get("report_email") or "").strip()
    return eng, mail


def _device_label_for(serial: str, device_label: Optional[str] = None) -> str:
    """Resolve a friendly device label for persistence (snapshot at write time)."""
    explicit = (device_label or "").strip()
    if explicit:
        return explicit
    serial = (serial or "").strip()
    if not serial:
        return ""
    try:
        from modules.adb_controller import get_device_label

        return (get_device_label(serial) or serial).strip()
    except Exception:
        return serial


def with_device_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure device_label is present/filled and ordered next to serial for display."""
    if df is None or df.empty:
        return df
    out = df.copy()
    if "serial" in out.columns and "device_label" not in out.columns:
        out["device_label"] = ""
    if "serial" in out.columns and "device_label" in out.columns:
        def _fill(row: pd.Series) -> str:
            existing = str(row.get("device_label") or "").strip()
            if existing:
                return existing
            serial = str(row.get("serial") or "").strip()
            return _device_label_for(serial) if serial else ""

        out["device_label"] = out.apply(_fill, axis=1)
        cols = list(out.columns)
        preferred = []
        if "id" in cols:
            preferred.append("id")
        if "engineer" in cols:
            preferred.append("engineer")
        if "device_label" in cols:
            preferred.append("device_label")
        if "serial" in cols:
            preferred.append("serial")
        preferred.extend([c for c in cols if c not in preferred])
        out = out[preferred]
    return out


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
        return with_device_columns(df)
    finally:
        conn.close()


def search_cases(vin: str = "") -> pd.DataFrame:
    conn = get_connection()
    try:
        if vin.strip():
            df = pd.read_sql_query("SELECT * FROM cases WHERE vin LIKE ? ORDER BY created_at DESC", conn, params=(f"%{vin}%",))
        else:
            df = pd.read_sql_query("SELECT * FROM cases ORDER BY created_at DESC LIMIT 50", conn)
        return with_device_columns(df)
    finally:
        conn.close()


def save_case(
    vin: str,
    make: str,
    model: str,
    summary: str,
    serial: str = "",
    device_label: Optional[str] = None,
    engineer: Optional[str] = None,
):
    ensure_database()
    serial = (serial or "").strip()
    label = _device_label_for(serial, device_label)
    eng, _ = _operator_fields(engineer)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO cases (vin, make, model, summary, serial, device_label, engineer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (vin, make, model, summary, serial, label, eng),
        )
        conn.commit()
    finally:
        conn.close()


def save_ticket(
    vin: str,
    make: str,
    model: str,
    status: str,
    serial: str = "",
    device_label: Optional[str] = None,
    engineer: Optional[str] = None,
):
    """Persist a real live ticket event (from device workflows / Jifeline)."""
    vin = (vin or "").strip()
    if not vin or vin.upper() == "UNKNOWN":
        return
    ensure_database()
    serial = (serial or "").strip()
    label = _device_label_for(serial, device_label)
    eng, _ = _operator_fields(engineer)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO tickets (vin, make, model, status, serial, device_label, engineer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (vin, make or "", model or "", status or "", serial, label, eng),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_tickets(limit: int = 50) -> pd.DataFrame:
    """Return recent real tickets, newest first."""
    ensure_database()
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            "SELECT * FROM tickets ORDER BY last_seen DESC, id DESC LIMIT ?",
            conn,
            params=(limit,),
        )
        return with_device_columns(df)
    finally:
        conn.close()


def fetch_live_tickets(limit: int = 20) -> pd.DataFrame:
    """Latest status per VIN (one row each), newest activity first."""
    ensure_database()
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            """
            SELECT t.*
            FROM tickets t
            INNER JOIN (
                SELECT vin, MAX(id) AS max_id
                FROM tickets
                WHERE vin IS NOT NULL AND TRIM(vin) != '' AND UPPER(vin) != 'UNKNOWN'
                GROUP BY vin
            ) latest ON t.id = latest.max_id
            ORDER BY t.last_seen DESC, t.id DESC
            LIMIT ?
            """,
            conn,
            params=(limit,),
        )
        return with_device_columns(df)
    finally:
        conn.close()


def clear_tickets() -> int:
    """Remove all ticket rows (used to wipe mock history). Returns deleted count."""
    ensure_database()
    conn = get_connection()
    try:
        cur = conn.execute("SELECT COUNT(*) FROM tickets")
        count = int(cur.fetchone()[0])
        conn.execute("DELETE FROM tickets")
        conn.commit()
        return count
    finally:
        conn.close()


def save_dtc(
    vin: str,
    dtc_code: str,
    dtc_description: str,
    source: str = "manual",
    serial: str = "",
    device_label: Optional[str] = None,
    engineer: Optional[str] = None,
):
    ensure_database()
    serial = (serial or "").strip()
    label = _device_label_for(serial, device_label)
    eng, _ = _operator_fields(engineer)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO dtc_history (vin, dtc_code, dtc_description, source, serial, device_label, engineer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (vin, dtc_code, dtc_description, source, serial, label, eng),
        )
        conn.commit()
    finally:
        conn.close()


def save_ai_summary(
    vin: str,
    make: str,
    model: str,
    dtc_text: str,
    recommendation: str,
    ai_notes: str,
    serial: str = "",
    device_label: Optional[str] = None,
    engineer: Optional[str] = None,
):
    ensure_database()
    serial = (serial or "").strip()
    label = _device_label_for(serial, device_label)
    eng, _ = _operator_fields(engineer)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO ai_summaries (vin, make, model, dtc_text, recommendation, ai_notes, serial, device_label, engineer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vin, make, model, dtc_text, recommendation, ai_notes, serial, label, eng),
        )
        conn.commit()
    finally:
        conn.close()


def save_report(
    serial: str,
    workflow: str,
    status: str,
    summary: str,
    file_path: str | None = None,
    device_label: Optional[str] = None,
    engineer: Optional[str] = None,
    report_email: Optional[str] = None,
):
    ensure_database()
    serial = (serial or "").strip()
    label = _device_label_for(serial, device_label)
    eng, mail = _operator_fields(engineer, report_email)
    # Keep device identity visible inside the summary text for export/search.
    prefix = f"[device={label}|serial={serial}] " if serial else ""
    if eng and "[engineer=" not in str(summary):
        prefix = f"{prefix}[engineer={eng}] "
    if mail and "[to=" not in str(summary):
        prefix = f"{prefix}[to={mail}] "
    summary_text = summary if str(summary).startswith("[device=") else f"{prefix}{summary}"
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO reports (serial, device_label, workflow, status, summary, file_path, engineer, report_email) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (serial, label, workflow, status, summary_text, file_path, eng, mail),
        )
        conn.commit()
    finally:
        conn.close()


def save_dtcs_bulk(
    vin: str,
    codes: List[Dict[str, Any]],
    source: str = "x431_scan",
    serial: str = "",
    device_label: Optional[str] = None,
) -> int:
    """Persist multiple DTC entries. Each item needs ``code`` and optional ``description``.

    Returns:
        Number of rows inserted.
    """
    count = 0
    for item in codes:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        save_dtc(
            vin,
            code,
            str(item.get("description") or ""),
            source=source,
            serial=serial,
            device_label=device_label,
        )
        count += 1
    return count


def save_vin_audit(
    serial: str,
    brand_selected: str,
    make_detected: str,
    vin: str,
    software: str = "",
    source: str = "intelligent_diagnose",
    model: str = "",
    device_label: Optional[str] = None,
    engineer: Optional[str] = None,
    report_email: Optional[str] = None,
) -> None:
    """Persist an AutoDetect VIN result for audit history."""
    ensure_database()
    serial = (serial or "").strip()
    label = _device_label_for(serial, device_label)
    eng, mail = _operator_fields(engineer, report_email)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO vin_audit (serial, device_label, brand_selected, make_detected, model, vin, software, source, engineer, report_email) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (serial, label, brand_selected, make_detected, model or "", vin, software, source, eng, mail),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_vin_audit(limit: int = 20) -> pd.DataFrame:
    """Return recent VIN audit rows (newest first)."""
    ensure_database()
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            "SELECT * FROM vin_audit ORDER BY created_at DESC LIMIT ?",
            conn,
            params=(limit,),
        )
        return with_device_columns(df)
    finally:
        conn.close()


def fetch_reports(limit: int = 50) -> pd.DataFrame:
    """Return recent workflow reports, newest first."""
    ensure_database()
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            "SELECT * FROM reports ORDER BY created_at DESC, id DESC LIMIT ?",
            conn,
            params=(limit,),
        )
        return with_device_columns(df)
    finally:
        conn.close()


ADMIN_TABLES = (
    "tickets",
    "vin_audit",
    "reports",
    "dtc_history",
    "cases",
    "ai_summaries",
)


def table_counts() -> Dict[str, int]:
    """Row counts for admin overview."""
    ensure_database()
    conn = get_connection()
    counts: Dict[str, int] = {}
    try:
        for name in ADMIN_TABLES:
            try:
                counts[name] = int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            except Exception:
                counts[name] = 0
        return counts
    finally:
        conn.close()


def clear_table(name: str) -> int:
    """Delete all rows from an allow-listed table. Returns deleted count."""
    if name not in ADMIN_TABLES:
        raise ValueError(f"Refusing to clear unknown table: {name}")
    ensure_database()
    conn = get_connection()
    try:
        count = int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        conn.execute(f"DELETE FROM {name}")
        conn.commit()
        return count
    finally:
        conn.close()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, typedef in _SCHEMA_MIGRATIONS:
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")


def ensure_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_db()
    conn = get_connection()
    try:
        _apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()
