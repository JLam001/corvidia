"""Private session-file publication/cleanup; no hardware or network access."""
import json
import os
from pathlib import Path
import stat

import pytest

from corvidia_perception.stand_session import default_session_path, remove_session, write_session


def test_default_path_and_port_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert default_session_path() == tmp_path / '.local/state/corvidia/stand-8080.json'
    assert default_session_path(8088).name == 'stand-8088.json'
    for value in (True, 0, 65536, '8080'):
        with pytest.raises(ValueError): default_session_path(value)


def test_publish_is_private_and_replaces_complete_record(tmp_path):
    path = tmp_path / 'owned' / 'stand-8080.json'
    write_session(path, url='http://127.0.0.1:8080', token='first', pid=123)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.stat().st_ino
    write_session(path, url='http://127.0.0.1:8080', token='next', pid=124)
    assert path.stat().st_ino != before
    assert json.loads(path.read_text()) == dict(version=1, url='http://127.0.0.1:8080', token='next', pid=124)
    assert not list(path.parent.glob('*.tmp'))


def test_old_process_cannot_remove_new_session(tmp_path):
    path = tmp_path / 'private' / 'stand.json'
    write_session(path, url='http://localhost:8080', token='old', pid=1)
    write_session(path, url='http://localhost:8080', token='new', pid=2)
    assert remove_session(path, 'old') is False
    assert path.exists()
    assert remove_session(path, 'new') is True
    assert not path.exists()
    assert remove_session(path, 'new') is False


def test_removal_never_follows_final_symlink(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    target = tmp_path / 'target.json'
    target.write_text(json.dumps({'token': 'mine'}))
    link = private / 'stand.json'
    link.symlink_to(target)
    assert remove_session(link, 'mine') is False
    assert target.exists() and link.is_symlink()
    with pytest.raises(PermissionError):
        write_session(link, url='http://localhost:8080', token='mine', pid=1)
    assert target.read_text() == json.dumps({'token': 'mine'})


def test_symlink_parent_and_writable_parent_are_refused(tmp_path):
    target = tmp_path / 'target'
    target.mkdir(mode=0o700)
    link = tmp_path / 'link'
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        write_session(link / 'stand.json', url='http://localhost:8080', token='mine', pid=1)
    target.chmod(0o770)
    with pytest.raises(PermissionError):
        write_session(target / 'stand.json', url='http://localhost:8080', token='mine', pid=1)
    assert not (target / 'stand.json').exists()


def test_owned_readable_parent_becomes_private(tmp_path):
    parent = tmp_path / 'owner'
    parent.mkdir(mode=0o755)
    write_session(parent / 'stand.json', url='http://localhost:8080', token='mine', pid=1)
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


@pytest.mark.parametrize('raw', ['garbage', '[]', '{}', '{"token":"other"}', 'x'*8193])
def test_unreadable_or_unmatched_record_is_ignored(tmp_path, raw):
    parent = tmp_path / 'owner'
    parent.mkdir(mode=0o700)
    path = parent / 'stand.json'
    path.write_text(raw)
    assert remove_session(path, 'mine') is False
    assert path.read_text() == raw


def test_failed_atomic_replace_preserves_old_record_and_removes_temp(monkeypatch, tmp_path):
    path = tmp_path / 'owner' / 'stand.json'
    write_session(path, url='http://localhost:8080', token='old', pid=1)
    def fail(*args, **kwargs): raise OSError('replace failed')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        write_session(path, url='http://localhost:8080', token='new', pid=2)
    assert json.loads(path.read_text())['token'] == 'old'
    assert not list(path.parent.glob('*.tmp'))


def test_symlink_lock_is_refused(tmp_path):
    parent = tmp_path / 'owner'
    parent.mkdir(mode=0o700)
    elsewhere = tmp_path / 'untouched'
    elsewhere.write_text('keep')
    (parent / '.stand.json.lock').symlink_to(elsewhere)
    with pytest.raises(OSError):
        write_session(parent / 'stand.json', url='http://localhost:8080', token='mine', pid=1)
    assert elsewhere.read_text() == 'keep'
