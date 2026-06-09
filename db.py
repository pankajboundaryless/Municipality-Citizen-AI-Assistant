import json
import logging
import os
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)
DB_PATH = os.getenv("DB_PATH", "citizen_sessions.db")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   TEXT PRIMARY KEY,
                user_sub     TEXT NOT NULL,
                user_email   TEXT,
                user_name    TEXT,
                citizen_data TEXT,
                transcript   TEXT,
                status       TEXT DEFAULT 'active',
                created_at   TEXT,
                updated_at   TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_sub ON sessions(user_sub)"
        )
        await db.commit()
    logger.info(f"Database ready: {DB_PATH}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def upsert_session(
    session_id: str,
    user_sub: str,
    user_email: str,
    user_name: str,
    citizen_data: dict,
    transcript: list,
    status: str = "active",
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        now = _now()
        await db.execute(
            """
            INSERT INTO sessions
                (session_id, user_sub, user_email, user_name,
                 citizen_data, transcript, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                citizen_data = excluded.citizen_data,
                transcript   = excluded.transcript,
                status       = excluded.status,
                updated_at   = excluded.updated_at
            """,
            (
                session_id, user_sub, user_email, user_name,
                json.dumps(citizen_data, ensure_ascii=False),
                json.dumps(transcript, ensure_ascii=False),
                status, now, now,
            ),
        )
        await db.commit()


async def get_last_session(user_sub: str) -> dict | None:
    """Return the most recent session for this user, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT session_id, citizen_data, transcript, status, updated_at
            FROM sessions
            WHERE user_sub = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (user_sub,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "session_id": row["session_id"],
                "citizen_data": json.loads(row["citizen_data"] or "{}"),
                "transcript": json.loads(row["transcript"] or "[]"),
                "status": row["status"],
                "updated_at": row["updated_at"],
            }
