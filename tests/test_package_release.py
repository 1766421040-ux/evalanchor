"""Release includes native evidence while never opening credential paths."""

import hashlib
import importlib.util
import json
import os
import zipfile
from pathlib import Path

import pytest


def module():
    path = Path(__file__).parents[1] / "scripts/package_release.py"
    spec = importlib.util.spec_from_file_location("evalanchor_package_release", path)
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    return package


@pytest.fixture
def project(tmp_path):
    packager = module()
    for name in packager.ROOT_FILES:
        (tmp_path / name).write_text("placeholder example\n" if name == ".env.example" else "source\n")
    for name in packager.REQUIRED_EVIDENCE:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"native evidence\x00\xff")
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/ci.yml").write_text("name: test\n")
    return tmp_path


def test_required_evidence_is_byte_preserved_and_manifest_matches(project):
    p = module()
    result = p.build_release(project)
    with zipfile.ZipFile(result["path"]) as archive:
        names = set(archive.namelist())
        assert all(name.startswith("evalanchor-0.2.0/") for name in names)
        for relative in p.REQUIRED_EVIDENCE:
            assert archive.read(p.archive_name(relative)) == (project / relative).read_bytes()
        assert p.archive_name(".github/workflows/ci.yml") in names
        assert p.archive_name(".env.example") in names
        manifest = json.loads(archive.read(p.archive_name("RELEASE_MANIFEST.json")))
        for record in manifest["files"]:
            data = archive.read(p.archive_name(record["path"]))
            assert record["bytes"] == len(data)
            assert record["sha256"] == hashlib.sha256(data).hexdigest()
        assert result["file_count"] == len(names)
    assert result["sha256"] == hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest()


def test_release_bytes_are_reproducible_across_names_and_mtime_changes(project):
    p = module()
    first = p.build_release(project, project / "dist/one.zip")
    os.utime(project / "README.md", (1800000000, 1800000000))
    second = p.build_release(project, project / "dist/two.zip")
    assert first["sha256"] == second["sha256"]


def test_secrets_dependencies_databases_and_weights_are_excluded_without_reading(project, monkeypatch):
    p = module()
    excluded = [
        ".env.local", ".env", "api-key.txt", "artifacts/api-key.txt", "artifacts/access_token.json", "artifacts/.env.local", "examples/.env", "examples/config.env",
        "artifacts/secrets/live.txt", "artifacts/credentials/value.json", "artifacts/api_keys/key.txt",
        "src/private.key", "scripts/token.json", "artifacts/auth.json", "artifacts/model.safetensors",
        "artifacts/journal.sqlite3", "artifacts/journal.sqlite3-wal", "artifacts/state.db-shm",
        "examples/.runtime/cache.json", "examples/.cache/item.json", "src/__pycache__/x.pyc",
        "examples/node_modules/test/index.js", "examples/trace.log", "examples/trace.log.1",
        "examples/package-lock.lock", "docs/.DS_Store", "weights/model.bin",
        "experiments/test-v2/calls/duplicate-response.json", "experiments/test-v2/journal.sqlite3",
        "experiments/test-v2/private-notes.json", "artifacts/old-release.zip", "docs/nested/dist/archive.zip",
    ]
    for name in excluded:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("MUST-NOT-BE-READ")
    original_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        # The walker uses descriptor-relative opens; excluded leaf names should
        # never be opened, regardless of whether their file is readable.
        if str(path) in {".env.local", ".env", "private.key", "live.txt", "value.json", "key.txt", "token.json", "auth.json", "calls", "duplicate-response.json", "old-release.zip"}:
            pytest.fail("An excluded credential path was opened")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {guarded_open})
    result = p.build_release(project)
    with zipfile.ZipFile(result["path"]) as archive:
        assert not any(b"MUST-NOT-BE-READ" in archive.read(name) for name in archive.namelist())


@pytest.mark.parametrize("path", ["../escape", "/absolute", "src/../../escape", "src//file", "src/./file", "C:/windows", "src\\..\\escape", "src/evil\x00name"])
def test_archive_names_reject_traversal_and_cross_platform_ambiguity(path):
    p = module()
    with pytest.raises(p.PackagingError):
        p.archive_name(path)


def test_symlink_to_outside_file_is_rejected_without_reading_target(project, tmp_path_factory):
    p = module()
    outside = tmp_path_factory.mktemp("outside") / "private.txt"
    outside.write_text("PRIVATE-OUTSIDE")
    (project / "docs/linked.txt").symlink_to(outside)
    with pytest.raises(p.PackagingError, match="Symlink"):
        p.build_release(project)
    assert not list((project / "dist").glob("*.zip"))
    assert not list((project / "dist").glob(".release-*"))


def test_directory_symlink_is_rejected_even_inside_project(project):
    p = module()
    (project / "docs/alias").symlink_to(project / "examples", target_is_directory=True)
    with pytest.raises(p.PackagingError, match="Symlink"):
        p.build_release(project)


def test_excluded_env_symlink_is_skipped_without_following(project):
    p = module()
    (project / ".env.local").symlink_to("/nonexistent/private/key")
    result = p.build_release(project)
    with zipfile.ZipFile(result["path"]) as archive:
        assert not any(".env.local" in name for name in archive.namelist())


def test_missing_required_evidence_prevents_publication(project):
    p = module()
    (project / "examples/integrations/verified/inspect/verified.eval").unlink()
    with pytest.raises(p.PackagingError, match="Required source/evidence missing"):
        p.build_release(project)
    assert not list((project / "dist").glob("*.zip"))


def test_destination_must_stay_inside_dist_and_cannot_overwrite(project):
    p = module()
    with pytest.raises(p.PackagingError, match="directly inside"):
        p.build_release(project, project / "dist/../escape.zip")
    result = p.build_release(project)
    with pytest.raises(p.PackagingError, match="already exists"):
        p.build_release(project)
    assert Path(result["path"]).is_file()


def test_symlinked_dist_is_rejected(project, tmp_path_factory):
    p = module()
    (project / "dist").symlink_to(tmp_path_factory.mktemp("outside-dist"), target_is_directory=True)
    with pytest.raises(p.PackagingError, match="dist directory"):
        p.build_release(project)
