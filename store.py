"""
Хранилище демо-бота: заявки и занятые слоты в SQLite.

Зачем: бот держал заявки в словаре внутри процесса. После перезапуска
(а на бесплатном хостинге он перезапускается сам) заявки и брони исчезали.
Теперь они лежат в файле bot.db рядом с ботом и поднимаются при старте.

Проверка без Telegram:
    python selftest.py
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = str(Path(__file__).with_name("bot.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id        INTEGER PRIMARY KEY,
    created   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    name      TEXT,
    phone     TEXT,
    comment   TEXT,
    slot      TEXT,
    user_id   INTEGER
);
CREATE TABLE IF NOT EXISTS booked (
    slot       TEXT PRIMARY KEY,
    request_id INTEGER
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    """Открывает базу и заодно создаёт таблицы, если файла ещё нет:
    пустая база не должна ронять бота при первом запуске."""
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def init(path: str | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def next_request_id(path: str | None = None) -> int:
    with connect(path) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM requests").fetchone()
    return int(row["n"])


def save_request(
    request_id: int,
    kind: str,
    name: str = "",
    phone: str = "",
    comment: str = "",
    slot: str = "",
    user_id: int | None = None,
    path: str | None = None,
) -> None:
    """Пишет заявку один раз. Повторная запись с тем же id ничего не портит."""
    with connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO requests (id, created, kind, name, phone, comment, slot, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                datetime.now().isoformat(timespec="seconds"),
                kind,
                name,
                phone,
                comment,
                slot,
                user_id,
            ),
        )


def book_slot(slot: str, request_id: int, path: str | None = None) -> bool:
    """False, если слот уже занят кем-то другим. True - бронь наша."""
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO booked (slot, request_id) VALUES (?, ?)", (slot, request_id)
        )
    return cur.rowcount > 0


def load_state(path: str | None = None) -> dict:
    """Всё, что нужно боту после старта: заявки, брони и счётчики для /stats."""
    with connect(path) as conn:
        requests = {
            int(row["id"]): {
                "kind": row["kind"],
                "name": row["name"],
                "phone": row["phone"],
                "comment": row["comment"],
                "slot": row["slot"],
                "user": row["user_id"],
                "created": row["created"],
            }
            for row in conn.execute("SELECT * FROM requests ORDER BY id")
        }
        booked = {row["slot"] for row in conn.execute("SELECT slot FROM booked")}
        by_day: dict[str, int] = {}
        users: set[int] = set()
        for row in conn.execute("SELECT created, user_id FROM requests"):
            created = row["created"] or ""
            try:
                day = datetime.fromisoformat(created).strftime("%d.%m.%Y")
            except ValueError:
                day = created[:10]
            by_day[day] = by_day.get(day, 0) + 1
            if row["user_id"]:
                users.add(int(row["user_id"]))
    return {
        "requests": requests,
        "booked": booked,
        "total": len(requests),
        "by_day": by_day,
        "users": users,
    }
