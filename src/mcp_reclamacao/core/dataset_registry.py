import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd


DB_PATH = Path(__file__).resolve().parents[3] / "db.sqlite3"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def _ensure_tables() -> None:
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_datasets (
                dataset_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                source_name TEXT NOT NULL,
                records_json TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                is_filtered INTEGER NOT NULL DEFAULT 0,
                parent_dataset_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_session_state (
                session_id TEXT PRIMARY KEY,
                active_dataset_id TEXT,
                active_filtered_dataset_id TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_ref(ref: str) -> str:
    import os

    return os.path.normpath(str(ref or "").strip("'\"")).replace("\\", "/")


def save_dataset(
    session_id: str,
    df: pd.DataFrame,
    source_ref: str,
    *,
    is_filtered: bool = False,
    parent_dataset_id: str | None = None,
) -> str:
    _ensure_tables()
    dataset_id = f"dataset_{uuid.uuid4().hex[:12]}"
    source_ref_norm = _norm_ref(source_ref)
    source_name = Path(source_ref_norm).name or source_ref_norm
    payload = df.to_dict(orient="records")

    with _conn() as c:
        c.execute(
            """
            INSERT INTO mcp_datasets(
                dataset_id, session_id, source_ref, source_name, records_json, columns_json,
                row_count, is_filtered, parent_dataset_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dataset_id,
                session_id,
                source_ref_norm,
                source_name,
                json.dumps(payload, ensure_ascii=False, default=str),
                json.dumps(list(df.columns), ensure_ascii=False),
                int(len(df)),
                1 if is_filtered else 0,
                parent_dataset_id,
                _now(),
            ),
        )
        _set_active_state(c, session_id, dataset_id, is_filtered=is_filtered)

    return dataset_id


def _set_active_state(c: sqlite3.Connection, session_id: str, dataset_id: str, *, is_filtered: bool) -> None:
    now = _now()
    cur = c.execute(
        "SELECT session_id, active_dataset_id, active_filtered_dataset_id FROM mcp_session_state WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if cur is None:
        c.execute(
            """
            INSERT INTO mcp_session_state(session_id, active_dataset_id, active_filtered_dataset_id, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, dataset_id if not is_filtered else None, dataset_id if is_filtered else None, now),
        )
        return

    active_dataset_id = dataset_id if not is_filtered else cur["active_dataset_id"]
    active_filtered_dataset_id = dataset_id if is_filtered else cur["active_filtered_dataset_id"]
    c.execute(
        """
        UPDATE mcp_session_state
        SET active_dataset_id = ?, active_filtered_dataset_id = ?, updated_at = ?
        WHERE session_id = ?
        """,
        (active_dataset_id, active_filtered_dataset_id, now, session_id),
    )


def get_active_dataset_id(session_id: str, *, filtered: bool = False) -> str | None:
    _ensure_tables()
    with _conn() as c:
        row = c.execute(
            "SELECT active_dataset_id, active_filtered_dataset_id FROM mcp_session_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return row["active_filtered_dataset_id"] if filtered else row["active_dataset_id"]


def _row_to_df(row: sqlite3.Row | None) -> pd.DataFrame | None:
    if not row:
        return None
    data = json.loads(row["records_json"] or "[]")
    return pd.DataFrame(data)


def load_dataset(dataset_id: str) -> pd.DataFrame | None:
    _ensure_tables()
    with _conn() as c:
        row = c.execute("SELECT records_json FROM mcp_datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
        return _row_to_df(row)


def resolve_dataset(session_id: str, reference: str, *, prefer_filtered: bool = False) -> tuple[str | None, str]:
    _ensure_tables()
    ref = str(reference or "").strip()
    if not ref:
        ref = get_active_dataset_id(session_id, filtered=prefer_filtered) or ""

    with _conn() as c:
        if ref.startswith("dataset_"):
            row = c.execute(
                """
                SELECT dataset_id, source_ref FROM mcp_datasets
                WHERE session_id = ? AND dataset_id = ?
                """,
                (session_id, ref),
            ).fetchone()
            if row:
                return row["dataset_id"], row["source_ref"]

        norm = _norm_ref(ref)
        base = Path(norm).name.lower()
        filtered_flag = 1 if prefer_filtered else 0

        row = c.execute(
            """
            SELECT dataset_id, source_ref
            FROM mcp_datasets
            WHERE session_id = ? AND is_filtered = ? AND source_ref = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (session_id, filtered_flag, norm),
        ).fetchone()
        if row:
            return row["dataset_id"], row["source_ref"]

        row = c.execute(
            """
            SELECT dataset_id, source_ref
            FROM mcp_datasets
            WHERE session_id = ? AND is_filtered = ? AND lower(source_name) = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (session_id, filtered_flag, base),
        ).fetchone()
        if row:
            return row["dataset_id"], row["source_ref"]

        active = get_active_dataset_id(session_id, filtered=prefer_filtered)
        if active:
            row = c.execute(
                "SELECT dataset_id, source_ref FROM mcp_datasets WHERE session_id = ? AND dataset_id = ?",
                (session_id, active),
            ).fetchone()
            if row:
                return row["dataset_id"], row["source_ref"]

    return None, ""


def list_session_datasets(session_id: str) -> list[dict]:
    _ensure_tables()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT dataset_id, source_ref, source_name, row_count, is_filtered, parent_dataset_id, created_at
            FROM mcp_datasets
            WHERE session_id = ?
            ORDER BY created_at DESC
            """,
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
