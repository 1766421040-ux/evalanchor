"""Local checkpoint identity cannot be replaced by a caller-supplied digest."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from evalanchor import providers, runner
from evalanchor.contracts import ContractError, fingerprint


def safetensors(path, payload=b"1234"):
    header = json.dumps({"weight": {"dtype": "U8", "shape": [len(payload)], "data_offsets": [0, len(payload)]}}).encode()
    header += b" " * ((-len(header)) % 8)
    path.write_bytes(len(header).to_bytes(8, "little") + header + payload)


@pytest.fixture
def checkpoint(tmp_path):
    path = tmp_path / "model"
    path.mkdir()
    (path / "config.json").write_text('{"model_type":"test"}')
    (path / "tokenizer_config.json").write_text('{"chat_template":"first"}')
    (path / "tokenizer.json").write_text('{"version":"1"}')
    safetensors(path / "model.safetensors")
    providers._FILE_DIGEST_CACHE.clear()
    return path


def target(path):
    return {"id": "local", "provider": "transformers", "model_path": str(path), "device": "cpu", "max_tokens": 8}


def test_manifest_contains_actual_file_hashes_and_is_bound_in_public_target(checkpoint):
    config = providers.prepare_target(target(checkpoint))
    manifest = config["model_manifest"]
    assert manifest["format"] == "safetensors"
    entries = {item["path"]: item for item in manifest["files"]}
    assert set(entries) == {"model.safetensors", "config.json", "tokenizer_config.json", "tokenizer.json"}
    for name, record in entries.items():
        assert record["sha256"] == hashlib.sha256((checkpoint / name).read_bytes()).hexdigest()
        assert record["size_bytes"] == (checkpoint / name).stat().st_size
    assert manifest["sha256"] == fingerprint({k: v for k, v in manifest.items() if k != "sha256"})
    assert providers.public_target(config)["model_manifest"] == manifest


def test_same_path_weight_replacement_changes_identity_even_if_size_and_mtime_restored(checkpoint):
    first = providers.prepare_target(target(checkpoint))
    path = checkpoint / "model.safetensors"
    stat = path.stat()
    safetensors(path, b"4321")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    changed = providers.prepare_target(target(checkpoint))
    assert first["model_manifest"]["sha256"] != changed["model_manifest"]["sha256"]
    with pytest.raises(ContractError, match="manifest mismatch"):
        providers.prepare_target(first)


def test_tokenizer_and_chat_template_changes_are_bound(checkpoint):
    before = providers.prepare_target(target(checkpoint))
    (checkpoint / "tokenizer_config.json").write_text('{"chat_template":"other"}')
    changed = providers.prepare_target(target(checkpoint))
    assert changed["model_manifest"] != before["model_manifest"]
    templates = checkpoint / "chat_templates"
    templates.mkdir()
    (templates / "custom.jinja").write_text("{{ messages }}")
    added = providers.prepare_target(target(checkpoint))
    assert added["model_manifest"] != changed["model_manifest"]
    assert "chat_templates/custom.jinja" in {r["path"] for r in added["model_manifest"]["files"]}


def test_user_supplied_digest_is_recomputed_and_never_trusted(checkpoint):
    claimed = providers.prepare_target(target(checkpoint))
    claimed["model_manifest"]["sha256"] = "0" * 64
    providers._FILE_DIGEST_CACHE.clear()
    with pytest.raises(ContractError, match="supplied digest"):
        providers.prepare_target(claimed)
    bogus = target(checkpoint)
    bogus["model_manifest"] = "fake"
    with pytest.raises(ContractError, match="manifest mismatch"):
        providers.public_target(bogus)


@pytest.mark.skipif(os.name != "posix", reason="Only POSIX ctime supports the content cache; other platforms rehash")
def test_repeated_prepare_reuses_only_process_computed_file_hashes(checkpoint, monkeypatch):
    original_sha256 = hashlib.sha256
    stream_hashes = []

    def tracked(*args, **kwargs):
        if not args:
            stream_hashes.append(1)
        return original_sha256(*args, **kwargs)

    monkeypatch.setattr(providers.hashlib, "sha256", tracked)
    first = providers.prepare_target(target(checkpoint))
    count = len(stream_hashes)
    assert count == len(first["model_manifest"]["files"])
    assert providers.prepare_target(first) == first
    assert providers.public_target(first)["model_manifest"] == first["model_manifest"]
    assert len(stream_hashes) == count
    (checkpoint / "tokenizer.json").write_text('{"version":"2"}')
    providers.prepare_target(target(checkpoint))
    assert len(stream_hashes) == count + 1


def test_pickle_only_checkpoint_and_empty_safetensors_are_refused(checkpoint):
    (checkpoint / "model.safetensors").unlink()
    (checkpoint / "pytorch_model.bin").write_bytes(b"not deserialized")
    with pytest.raises(ContractError, match="pickle/bin"):
        providers.prepare_target(target(checkpoint))
    (checkpoint / "model.safetensors").write_bytes(b"")
    with pytest.raises(ContractError, match="safetensors header"):
        providers.prepare_target(target(checkpoint))


def test_sharded_safetensors_are_complete_and_paths_cannot_escape(checkpoint):
    (checkpoint / "model.safetensors").unlink()
    first = checkpoint / "model-00001-of-00002.safetensors"
    second = checkpoint / "model-00002-of-00002.safetensors"
    safetensors(first)
    mapping = {"a": first.name, "b": second.name}
    index = checkpoint / "model.safetensors.index.json"
    index.write_text(json.dumps({"weight_map": mapping}))
    with pytest.raises(ContractError, match="Missing safetensors"):
        providers.prepare_target(target(checkpoint))
    safetensors(second)
    config = providers.prepare_target(target(checkpoint))
    paths = {r["path"] for r in config["model_manifest"]["files"]}
    assert {first.name, second.name, index.name}.issubset(paths)
    index.write_text(json.dumps({"weight_map": {"a": "../outside.safetensors"}}))
    with pytest.raises(ContractError, match="traversal"):
        providers.prepare_target(target(checkpoint))


def test_relative_model_path_resolves_from_config_directory(checkpoint):
    config = providers.prepare_target(target("model"), checkpoint.parent)
    assert config["model_path"] == str(checkpoint.resolve())


def install_fake_loader(monkeypatch, during_load=None):
    calls = []

    class Model:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls.append((path, kwargs))
            if during_load:
                during_load()
            return cls()

        def to(self, device):
            return self

        def eval(self):
            return self

    class Tokenizer:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls.append((path, kwargs))
            return cls()

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(float32="f32", bfloat16="bf16"))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoModelForCausalLM=Model, AutoTokenizer=Tokenizer))
    return calls


def test_loader_explicitly_disables_pickle_fallback_and_remote_code(checkpoint, monkeypatch):
    calls = install_fake_loader(monkeypatch)
    providers.TransformersProvider(target(checkpoint))
    tokenizer_kwargs, model_kwargs = calls[0][1], calls[1][1]
    assert model_kwargs["use_safetensors"] is True
    assert model_kwargs["local_files_only"] is True
    assert model_kwargs["trust_remote_code"] is False
    assert tokenizer_kwargs["trust_remote_code"] is False


def test_checkpoint_change_during_loading_is_detected(checkpoint, monkeypatch):
    install_fake_loader(monkeypatch, lambda: (checkpoint / "tokenizer.json").write_text('{"version":"changed"}'))
    with pytest.raises(ContractError, match="manifest mismatch"):
        providers.TransformersProvider(target(checkpoint))


def test_resume_rejects_changed_checkpoint_before_any_provider_call(checkpoint, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_engine_hash", lambda: "stable-test-engine")
    suite = {"schema_version": 1, "id": "s", "cases": [{"id": "c", "group_id": "g", "tags": [], "messages": [{"role": "user", "content": "q"}], "scorer": "exact", "expected": "a"}]}

    class FakeProvider:
        def __init__(self):
            self.calls = 0

        def generate(self, *args):
            self.calls += 1
            return {"output": "a"}

    first = FakeProvider()
    runner.run_suite(suite, target(checkpoint), tmp_path / "run", workers=1, retries=0, provider=first)
    assert first.calls == 1
    (checkpoint / "tokenizer.json").write_text('{"version":"changed"}')
    second = FakeProvider()
    with pytest.raises(ContractError, match="Resume rejected"):
        runner.run_suite(suite, target(checkpoint), tmp_path / "run", workers=1, retries=0, provider=second, resume=True)
    assert second.calls == 0
