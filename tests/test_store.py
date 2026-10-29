"""Single-writer protection applies before pending work or provider calls."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import pytest

from evalanchor.contracts import ContractError
from evalanchor.store import RunStore


MANIFEST = {"suite": "suite", "target": "target", "protocol": "v1"}


def attempt_in_child(path):
    code = '''
import sys
from pathlib import Path
from evalanchor.store import RunStore
from evalanchor.contracts import ContractError
try:
    store = RunStore(Path(sys.argv[1]), {"suite":"suite","target":"target","protocol":"v1"}, resume=True)
except ContractError as exc:
    print(str(exc))
    sys.exit(7)
else:
    store.close()
    print("acquired")
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, env=env, timeout=10)


def test_second_process_cannot_resume_while_writer_holds_lock(tmp_path):
    path = tmp_path / "journal.sqlite3"
    first = RunStore(path, MANIFEST)
    try:
        denied = attempt_in_child(path)
        assert denied.returncode == 7
        assert "locked" in denied.stdout
    finally:
        first.close()
    allowed = attempt_in_child(path)
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == "acquired"


def test_same_process_second_handle_is_also_refused(tmp_path):
    path = tmp_path / "journal.sqlite3"
    first = RunStore(path, MANIFEST)
    try:
        with pytest.raises(ContractError, match="locked"):
            RunStore(path, MANIFEST, resume=True)
    finally:
        first.close()


def test_lock_file_is_retained_and_reused_not_deleted_as_stale(tmp_path):
    path = tmp_path / "journal.sqlite3"
    lock = tmp_path / "journal.sqlite3.lock"
    first = RunStore(path, MANIFEST)
    inode = lock.stat().st_ino
    first.close()
    first.close()  # idempotent cleanup is safe after error unwinding
    assert lock.exists()
    second = RunStore(path, MANIFEST, resume=True)
    second.close()
    assert lock.exists() and lock.stat().st_ino == inode


def test_manifest_mismatch_constructor_releases_lock(tmp_path):
    path = tmp_path / "journal.sqlite3"
    first = RunStore(path, MANIFEST)
    first.close()
    with pytest.raises(ContractError, match="Resume rejected"):
        RunStore(path, {**MANIFEST, "target": "changed"}, resume=True)
    final = RunStore(path, MANIFEST, resume=True)
    final.close()


def test_database_open_failure_releases_lock(tmp_path):
    path = tmp_path / "journal.sqlite3"
    with patch("evalanchor.store.sqlite3.connect", side_effect=sqlite3.OperationalError("injected failure")):
        with pytest.raises(sqlite3.OperationalError):
            RunStore(path, MANIFEST)
    # A retained lock file alone does not mean there is a live lock owner.
    recovered = RunStore(path, MANIFEST)
    recovered.close()


def test_unserializable_manifest_failure_releases_database_and_lock(tmp_path):
    path = tmp_path / "journal.sqlite3"
    with pytest.raises(TypeError):
        RunStore(path, {"bad": object()})
    recovered = RunStore(path, MANIFEST, resume=True)
    recovered.close()


def test_duplicate_record_does_not_replace_original(tmp_path):
    store = RunStore(tmp_path / "journal.sqlite3", MANIFEST)
    try:
        original = {"case_id": "c", "repeat": 0, "output": "original"}
        store.append(original)
        with pytest.raises(ContractError, match="Duplicate"):
            store.append({**original, "output": "replacement"})
        assert store.records() == [original]
        with pytest.raises(ContractError, match="immutable"):
            store.set_metadata("manifest", {"target": "another"})
    finally:
        store.close()


def test_missing_manifest_with_existing_evidence_is_rejected(tmp_path):
    path = tmp_path / "journal.sqlite3"
    store = RunStore(path, MANIFEST)
    store.append({"case_id": "c", "repeat": 0})
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM metadata WHERE key='manifest'")
    with pytest.raises(ContractError, match="no execution manifest"):
        RunStore(path, MANIFEST, resume=True)


def test_process_exit_releases_lock_without_deleting_file(tmp_path):
    path = tmp_path / "journal.sqlite3"
    code = '''
import os, sys
from pathlib import Path
from evalanchor.store import RunStore
store=RunStore(Path(sys.argv[1]), {"suite":"suite","target":"target","protocol":"v1"})
os._exit(0)
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    completed = subprocess.run([sys.executable, "-c", code, str(path)], env=env, timeout=10)
    assert completed.returncode == 0
    assert path.with_name(path.name + ".lock").exists()
    recovered = RunStore(path, MANIFEST, resume=True)
    recovered.close()
