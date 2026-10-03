import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from pydantic import BaseModel

DB_PATH = os.environ.get("KJELLER_STATE_DB", "kjeller.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS room_override (
    room TEXT PRIMARY KEY,
    temperature INTEGER,
    night_temperature INTEGER,
    schedule TEXT,
    updated_at TEXT NOT NULL
)
"""


class RoomOverride(BaseModel):
    """Values set from the web UI. None means fall back to config.yaml."""

    temperature: int | None = None
    night_temperature: int | None = None
    schedule: dict[str, list[str] | None] | None = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    return conn


def get_overrides() -> dict[str, RoomOverride]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT room, temperature, night_temperature, schedule FROM room_override"
        ).fetchall()
    return {
        room: RoomOverride(
            temperature=temperature,
            night_temperature=night_temperature,
            schedule=json.loads(schedule) if schedule else None,
        )
        for room, temperature, night_temperature, schedule in rows
    }


def set_override(room: str, override: RoomOverride) -> None:
    with closing(_connect()) as conn, conn:
        if override == RoomOverride():
            conn.execute("DELETE FROM room_override WHERE room = ?", (room,))
            return
        conn.execute(
            """
            INSERT INTO room_override
                (room, temperature, night_temperature, schedule, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (room) DO UPDATE SET
                temperature = excluded.temperature,
                night_temperature = excluded.night_temperature,
                schedule = excluded.schedule,
                updated_at = excluded.updated_at
            """,
            (
                room,
                override.temperature,
                override.night_temperature,
                json.dumps(override.schedule)
                if override.schedule is not None
                else None,
                datetime.now(UTC).isoformat(),
            ),
        )
