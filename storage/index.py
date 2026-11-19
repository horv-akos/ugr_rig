"""SQLite index of studies, for the library and search.

The study folders are the record; this is only an index over their study.json files, so it is
rebuilt from them whenever it is missing or out of step (`rebuild`). Nothing is ever stored
here that is not also in a study folder.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

COLUMNS = ("id", "started_at", "finished_at", "status", "profile", "vehicle", "lighting",
           "operator", "notes", "n_positions", "ugr_max", "n_flags")


class StudyIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS studies (id TEXT PRIMARY KEY, started_at TEXT, "
            "finished_at TEXT, status TEXT, profile TEXT, vehicle TEXT, lighting TEXT, "
            "operator TEXT, notes TEXT, n_positions INTEGER, ugr_max REAL, n_flags INTEGER)")
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def row(study: dict[str, Any]) -> tuple:
        s = study.get("summary") or {}
        return (study["id"], study.get("started_at"), study.get("finished_at"),
                study.get("status"), study["profile"]["name"], study["profile"].get("vehicle", ""),
                study.get("lighting"), study.get("operator", ""), study.get("notes", ""),
                len(study.get("positions", [])), s.get("ugr_max"), len(study.get("flags", [])))

    def upsert(self, study: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(f"INSERT OR REPLACE INTO studies VALUES "
                             f"({','.join('?' * len(COLUMNS))})", self.row(study))
            self._db.commit()

    def search(self, q: str = "", limit: int = 200) -> list[dict[str, Any]]:
        sql = f"SELECT {','.join(COLUMNS)} FROM studies"
        args: list[Any] = []
        if q.strip():
            like = f"%{q.strip()}%"
            sql += (" WHERE id LIKE ? OR profile LIKE ? OR vehicle LIKE ? OR operator LIKE ?"
                    " OR notes LIKE ? OR lighting LIKE ? OR status LIKE ?")
            args = [like] * 7
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(zip(COLUMNS, r)) for r in rows]

    def rebuild(self, store) -> int:
        """Re-index every study folder; returns how many."""
        n = 0
        with self._lock:
            self._db.execute("DELETE FROM studies")
            self._db.commit()
        for sid in store.ids():
            try:
                self.upsert(store.load(sid))
                n += 1
            except (KeyError, ValueError, OSError):
                continue  # a damaged study.json: the folder is still there to inspect
        return n
