"""Public compression/restore is bounded, deterministic and refuses overwrites."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'


def modules():
    # The builder is a standalone sibling script in both git and source ZIPs.
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import build_evidence_bundle
    import restore_evidence
    return build_evidence_bundle, restore_evidence


@pytest.fixture
def bundle(tmp_path):
    builder, restorer = modules()
    folder = tmp_path / 'experiments/example'
    folder.mkdir(parents=True)
    path = folder / 'experiment.json'
    path.write_text(json.dumps({'kind': 'unit-test-fixture', 'message': '原始证据' * 100}))
    original = path.read_bytes()
    result = builder.build_bundle(tmp_path)
    manifest = json.loads((tmp_path/'evidence/manifest.json').read_text())
    return tmp_path, path, original, manifest, result


def write_manifest(root, manifest):
    (root/'evidence/manifest.json').write_text(json.dumps(manifest))


def test_roundtrip_and_existing_plaintext_need_no_compressed_copy(bundle):
    _, restore = modules()
    root, path, original, manifest, _ = bundle
    path.unlink()
    receipt = restore.restore(root)
    assert receipt['restored'] == ['experiments/example/experiment.json']
    assert path.read_bytes() == original
    (root/'evidence'/manifest['files'][0]['compressed_path']).unlink()
    assert restore.restore(root, verify_only=True)['already_verified'] == ['experiments/example/experiment.json']
    assert restore.restore(root)['restored'] == []


def test_bundle_is_reproducible_and_excludes_calls(bundle):
    builder, _ = modules()
    root, _, _, manifest, first = bundle
    cache = root/'experiments/example/calls'
    cache.mkdir()
    (cache/'private-response.json').write_text('NOT-IN-PUBLIC-EVIDENCE')
    second = builder.build_bundle(root)
    assert first['sha256'] == second['sha256']
    assert [r['path'] for r in manifest['files']] == ['experiments/example/experiment.json']
    for row in manifest['files']:
        compressed = (root/'evidence'/row['compressed_path']).read_bytes()
        assert row['compressed_sha256'] == hashlib.sha256(compressed).hexdigest()
        assert b'NOT-IN-PUBLIC-EVIDENCE' not in gzip.decompress(compressed)


def test_changed_existing_evidence_is_not_overwritten(bundle):
    _, restore = modules()
    root, path, _, _, _ = bundle
    path.write_text('keep this different evidence')
    with pytest.raises(restore.RestoreError, match='refusing to overwrite'):
        restore.restore(root)
    assert path.read_text() == 'keep this different evidence'


def test_corrupt_compressed_bytes_do_not_publish_partial_output(bundle):
    _, restore = modules()
    root, path, _, manifest, _ = bundle
    path.unlink()
    obj = root/'evidence'/manifest['files'][0]['compressed_path']
    raw = bytearray(obj.read_bytes());raw[-1] ^= 1;obj.write_bytes(raw)
    with pytest.raises(restore.RestoreError, match='checksum'):
        restore.restore(root)
    assert not path.exists() and not list(path.parent.glob('.restore-*'))


def test_expansion_above_declared_size_is_stopped(bundle):
    _, restore = modules()
    root, path, _, manifest, _ = bundle
    path.unlink()
    manifest['files'][0]['bytes'] = 5
    write_manifest(root, manifest)
    with pytest.raises(restore.RestoreError, match='exceeds declared size'):
        restore.restore(root)
    assert not path.exists() and not list(path.parent.glob('.restore-*'))


@pytest.mark.parametrize('path', ['../outside.json', '/tmp/absolute', 'experiments/../experiment.json',
                                 'experiments/example/../../secret.json', 'experiments/example/.env.local',
                                 '密钥/experiment.json', 'experiments\\example\\experiment.json'])
def test_manifest_path_allowlist_refuses_traversal_and_private_targets(bundle, path):
    _, restore = modules()
    root, _, _, manifest, _ = bundle
    manifest['files'][0]['path'] = path
    write_manifest(root, manifest)
    with pytest.raises(restore.RestoreError):
        restore.restore(root)


def test_symlinked_destination_and_parent_are_refused(bundle, tmp_path_factory):
    _, restore = modules()
    root, path, _, _, _ = bundle
    other = tmp_path_factory.mktemp('outside')/'untouched.json'
    other.write_text('untouched')
    path.unlink();path.symlink_to(other)
    with pytest.raises(OSError):
        restore.restore(root)
    assert other.read_text() == 'untouched'
    path.unlink();path.parent.rmdir();path.parent.symlink_to(other.parent, target_is_directory=True)
    with pytest.raises(OSError):
        restore.restore(root)
    assert not (other.parent/'experiment.json').exists()


def test_object_path_and_size_and_duplicate_destinations_are_rejected(bundle):
    _, restore = modules()
    root, _, _, manifest, _ = bundle
    for field, value in [('compressed_path', '../private.txt'), ('bytes', restore.MAX_FILE_BYTES+1), ('bytes', True)]:
        changed = json.loads(json.dumps(manifest));changed['files'][0][field]=value
        with pytest.raises(restore.RestoreError):restore.validate_manifest(changed)
    manifest['files'].append(manifest['files'][0])
    with pytest.raises(restore.RestoreError, match='Duplicate'):
        restore.validate_manifest(manifest)


def test_builder_refuses_symlink_source(bundle):
    builder, _ = modules()
    root, path, _, _, _ = bundle
    alternate = root/'other.json';alternate.write_text('do not follow')
    path.unlink();path.symlink_to(alternate)
    with pytest.raises(OSError):builder.build_bundle(root)


def test_budget_exports_use_the_same_restricted_restore_chain(bundle):
    builder, restore = modules()
    root, _, _, _, _ = bundle
    receipt=root/'experiments/example/budget-receipt.json';receipt.write_text('{"kind":"budget-fixture"}')
    campaign=root/'artifacts/orderops-v2/campaign-budget.json';campaign.parent.mkdir(parents=True);campaign.write_text('{"kind":"campaign-fixture"}')
    builder.build_bundle(root)
    expected={receipt.relative_to(root).as_posix():receipt.read_bytes(),campaign.relative_to(root).as_posix():campaign.read_bytes()}
    receipt.unlink();campaign.unlink()
    result=restore.restore(root)
    assert set(result['restored'])==set(expected)
    assert all((root/name).read_bytes()==content for name,content in expected.items())
