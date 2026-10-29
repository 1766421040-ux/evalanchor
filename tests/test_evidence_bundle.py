"""Public compression/restore is bounded, deterministic and refuses overwrites."""
import base64
import gzip
import hashlib
import importlib.util
import json
import random
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


def compressed_bytes(root, row):
    return b''.join(base64.b64decode((root/'evidence'/p['path']).read_bytes(), validate=True)
                    for p in row['compressed_parts'])


def test_roundtrip_and_existing_plaintext_need_no_compressed_copy(bundle):
    _, restore = modules()
    root, path, original, manifest, _ = bundle
    path.unlink()
    receipt = restore.restore(root)
    assert receipt['restored'] == ['experiments/example/experiment.json']
    assert path.read_bytes() == original
    for part in manifest['files'][0]['compressed_parts']:
        (root/'evidence'/part['path']).unlink()
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
        compressed = compressed_bytes(root, row)
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
    obj = root/'evidence'/manifest['files'][0]['compressed_parts'][0]['path']
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


@pytest.fixture
def multipart(tmp_path):
    builder, _ = modules()
    path = tmp_path/'experiments/example/experiment.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'noise': random.Random(22).randbytes(360_000).hex()}))
    raw = path.read_bytes()
    builder.build_bundle(tmp_path)
    manifest = json.loads((tmp_path/'evidence/manifest.json').read_text())
    assert manifest['files'][0]['part_count'] >= 3
    return tmp_path, path, raw, manifest


def test_bounded_ascii_parts_roundtrip(multipart):
    _, restore = modules()
    root, path, raw, manifest = multipart
    row = manifest['files'][0]
    assert manifest['schema_version'] == 2 and manifest['part_encoding'] == 'base64'
    for part in row['compressed_parts']:
        encoded = (root/'evidence'/part['path']).read_bytes()
        encoded.decode('ascii')
        assert len(encoded) == part['bytes'] <= 174_764
        assert part['sha256'] == hashlib.sha256(encoded).hexdigest()
        decoded = base64.b64decode(encoded, validate=True)
        assert len(decoded) == part['decoded_bytes'] <= 128 * 1024
        assert part['decoded_sha256'] == hashlib.sha256(decoded).hexdigest()
    assert hashlib.sha256(compressed_bytes(root, row)).hexdigest() == row['compressed_sha256']
    path.unlink()
    assert restore.restore(root)['restored'] == ['experiments/example/experiment.json']
    assert path.read_bytes() == raw


def test_missing_part_does_not_publish_partial_original(multipart):
    _, restore = modules()
    root, path, _, manifest = multipart
    path.unlink()
    (root/'evidence'/manifest['files'][0]['compressed_parts'][1]['path']).unlink()
    with pytest.raises(FileNotFoundError):
        restore.restore(root)
    assert not path.exists() and not list(path.parent.glob('.restore-*'))


@pytest.mark.parametrize('change', ['count', 'missing', 'order', 'duplicate', 'path', 'stored-size', 'decoded-size', 'encoding'])
def test_part_manifest_structure_is_strict(multipart, change):
    _, restore = modules()
    root, _, _, manifest = multipart
    row = manifest['files'][0]
    if change == 'count': row['part_count'] += 1
    elif change == 'missing': row['compressed_parts'].pop()
    elif change == 'order': row['compressed_parts'].reverse()
    elif change == 'duplicate': row['compressed_parts'][1] = row['compressed_parts'][0]
    elif change == 'path': row['compressed_parts'][0]['path'] = '../private.b64'
    elif change == 'stored-size': row['compressed_parts'][0]['bytes'] = True
    elif change == 'decoded-size': row['compressed_parts'][0]['decoded_bytes'] -= 1
    elif change == 'encoding': manifest['part_encoding'] = 'base64-permissive'
    write_manifest(root, manifest)
    with pytest.raises(restore.RestoreError): restore.restore(root)


def test_invalid_base64_is_rejected_even_with_matching_stored_hash(bundle):
    _, restore = modules()
    root, path, _, manifest, _ = bundle
    row = manifest['files'][0]; part = row['compressed_parts'][0]
    old = root/'evidence'/part['path']; encoded = b'!' + old.read_bytes()[1:]
    part['sha256'] = hashlib.sha256(encoded).hexdigest()
    part['path'] = f"parts/{row['compressed_sha256']}.0000.{part['sha256']}.b64"
    (root/'evidence'/part['path']).write_bytes(encoded)
    write_manifest(root, manifest); path.unlink()
    with pytest.raises(restore.RestoreError, match='strict base64'): restore.restore(root)
    assert not path.exists()


def test_decoded_and_whole_compressed_hashes_are_independent_checks(bundle):
    _, restore = modules()
    root, path, _, manifest, _ = bundle
    row=manifest['files'][0]; part=row['compressed_parts'][0]; correct=part['decoded_sha256']
    part['decoded_sha256']='0'*64; write_manifest(root,manifest);path.unlink()
    with pytest.raises(restore.RestoreError,match='Decoded part'): restore.restore(root)
    part['decoded_sha256']=correct
    old=root/'evidence'/part['path'];row['compressed_sha256']='0'*64
    part['path']=f"parts/{row['compressed_sha256']}.0000.{part['sha256']}.b64"
    (root/'evidence'/part['path']).write_bytes(old.read_bytes());write_manifest(root,manifest)
    with pytest.raises(restore.RestoreError,match='Compressed evidence'): restore.restore(root)
    assert not path.exists()


def test_part_symlink_and_directory_symlink_are_refused(bundle, tmp_path_factory):
    _, restore=modules()
    root,path,_,manifest,_=bundle;path.unlink()
    part=root/'evidence'/manifest['files'][0]['compressed_parts'][0]['path']
    outside=tmp_path_factory.mktemp('outside-part');saved=outside/'saved.b64';saved.write_bytes(part.read_bytes())
    part.unlink();part.symlink_to(saved)
    with pytest.raises(OSError):restore.restore(root)
    part.unlink();part.parent.rmdir();part.parent.symlink_to(outside,target_is_directory=True)
    with pytest.raises(OSError):restore.restore(root)
    assert not path.exists()


def test_legacy_single_gzip_manifest_remains_readable(bundle):
    _, restore=modules()
    root,path,raw,manifest,_=bundle;row=manifest['files'][0]
    compressed=compressed_bytes(root,row)
    row.pop('compressed_parts');row.pop('part_count');row['compressed_path']=f"objects/{row['compressed_sha256']}.gz"
    manifest['schema_version']=1;manifest.pop('part_encoding')
    obj=root/'evidence'/row['compressed_path'];obj.parent.mkdir();obj.write_bytes(compressed)
    write_manifest(root,manifest);path.unlink();restore.restore(root)
    assert path.read_bytes()==raw


def test_only_exact_extra_public_files_are_compressed(bundle):
    builder,restore=modules()
    root,_,_,_,_=bundle
    for name in restore.PUBLIC_TRANSPORT_FILES:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('public fixture')
    builder.build_bundle(root)
    manifest=json.loads((root/'evidence/manifest.json').read_text())
    assert restore.PUBLIC_TRANSPORT_FILES <= {row['path'] for row in manifest['files']}
    for name in ['artifacts/demo/other.html','artifacts/orderops-v2/other.json',
                 'examples/integrations/verified/inspect/logs/other.json','examples/integrations/verified/promptfoo/private.json']:
        with pytest.raises(restore.RestoreError):restore.safe_source(name)
