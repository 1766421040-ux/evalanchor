"""Transactional journal with a lifetime, nonblocking single-writer OS lock.

Advisory locks are supported on local macOS/Linux (fcntl) and Windows (msvcrt).
The lock file is retained permanently: deleting a 'stale' file could split the
lock inode and admit two writers. The OS releases a lock when its process exits.
Network filesystems with unreliable advisory locking are not supported.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .contracts import ContractError, canonical


class RunStore:
    def __init__(self, path: Path, manifest: dict, resume: bool = False):
        self.db = None
        self._lock_file = None
        self._lock_kind = None
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Acquire before checking existence/manifest or reading pending rows.
            # This closes the race between two simultaneous --resume processes.
            self._acquire_lock(path.with_name(path.name + ".lock"))
            existed = path.exists()
            if existed and not resume:
                raise ContractError("Run journal exists; use --resume with the identical configuration")
            self.db = sqlite3.connect(path)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS records (case_id TEXT, repeat INTEGER, value TEXT NOT NULL, PRIMARY KEY(case_id,repeat))")
            old = self.db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
            encoded = canonical(manifest)
            if old and old[0] != encoded:
                raise ContractError("Resume rejected: dataset, target, scorer, or execution policy changed")
            if not old:
                if self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]:
                    raise ContractError("Run journal has evidence but no execution manifest")
                self.db.execute("INSERT INTO metadata VALUES ('manifest',?)", (encoded,))
            self.db.commit()
        except BaseException:
            self.close()
            raise

    def _acquire_lock(self, path: Path):
        self._lock_file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._lock_file.seek(0, os.SEEK_END)
                if self._lock_file.tell() == 0:
                    self._lock_file.write(b"\0")
                    self._lock_file.flush()
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                self._lock_kind = "msvcrt"
            elif os.name == "posix":
                import fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_kind = "fcntl"
            else:
                raise ContractError("Run locking requires macOS/Linux fcntl or Windows msvcrt; unsupported platform")
        except (OSError, ImportError) as exc:
            raise ContractError("Run journal is locked by another writer, or OS advisory locking is unavailable; concurrent resume is refused") from exc

    def get_metadata(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_metadata(self, key, value):
        if key == "manifest":
            raise ContractError("Execution manifest is immutable")
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, canonical(value)))
        self.db.commit()

    def records(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT value FROM records ORDER BY case_id,repeat")]

    def append(self, record):
        try:
            self.db.execute("INSERT INTO records VALUES (?,?,?)", (record["case_id"], record["repeat"], canonical(record)))
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            self.db.rollback()
            raise ContractError("Duplicate result refused by run journal") from exc

    def close(self):
        try:
            if self.db is not None:
                self.db.close()
        finally:
            self.db = None
            if self._lock_file is not None:
                try:
                    if self._lock_kind == "fcntl":
                        import fcntl
                        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                    elif self._lock_kind == "msvcrt":
                        import msvcrt
                        self._lock_file.seek(0)
                        msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    self._lock_file.close()
                    self._lock_file = None
                    self._lock_kind = None
