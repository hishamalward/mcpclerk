from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

from mcpclerk import storage


@pytest.fixture
def environ(tmp_path):
    return {"MCPCLERK_HOME": str((tmp_path / "state").resolve())}


def test_read_and_undo_restore_exact_bytes_and_mode(tmp_path, environ):
    config = tmp_path / "client.json"
    before = b'{\n  "servers": ["\xff"]\n}\n'
    config.write_bytes(before)
    config.chmod(0o640)

    assert storage.read(config) == before
    transaction = storage.write(config, before, b'{"servers": []}\n', environ)
    backup = Path(environ["MCPCLERK_HOME"]) / "backups" / transaction
    assert config.read_bytes() == b'{"servers": []}\n'
    assert config.stat().st_mode & 0o777 == 0o640
    assert backup.stat().st_mode & 0o777 == 0o600
    assert (backup.parent.stat().st_mode & 0o777) == 0o700
    assert storage.undo(transaction, environ) == str(config)
    assert config.read_bytes() == before
    assert config.stat().st_mode & 0o777 == 0o640
    assert not backup.exists()


def test_new_file_is_private_and_undo_removes_it(tmp_path, environ):
    config = tmp_path / "new.json"
    assert storage.read(config) is None
    transaction = storage.write(config, None, b'{"token":"private"}', environ)
    assert config.read_bytes() == b'{"token":"private"}'
    assert config.stat().st_mode & 0o777 == 0o600
    storage.undo(transaction, environ)
    assert not config.exists()


def test_missing_parent_is_absent_and_write_creates_private_parents(tmp_path, environ):
    config = tmp_path / "new" / "nested" / "client.json"
    assert storage.read(config) is None
    transaction = storage.write(config, None, b"created", environ)
    assert config.read_bytes() == b"created"
    assert config.parent.stat().st_mode & 0o777 == 0o700
    assert config.parent.parent.stat().st_mode & 0o777 == 0o700
    storage.undo(transaction, environ)
    assert not config.exists()


def test_default_state_directory_uses_injected_home(tmp_path):
    home = tmp_path / "injected-home"
    config = tmp_path / "client.json"
    transaction = storage.write(config, None, b"created", {"HOME": str(home)})
    assert (home / ".mcpclerk" / "backups" / transaction).exists()


def test_uncertain_post_replace_failure_keeps_undo_transaction(tmp_path, environ):
    config = tmp_path / "client.json"
    config.write_bytes(b"before")
    real_fsync = os.fsync
    calls = 0

    def fail_directory_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 4:  # record, backup directory, temporary config, then containing directory
            raise OSError("simulated durability failure")
        return real_fsync(fd)

    with mock.patch.object(storage.os, "fsync", side_effect=fail_directory_fsync):
        with pytest.raises(storage.StorageError, match="undo transaction") as caught:
            storage.write(config, b"before", b"after", environ)
    assert config.read_bytes() == b"after"
    transaction = str(caught.value).rsplit(" ", 1)[-1]
    assert (Path(environ["MCPCLERK_HOME"]) / "backups" / transaction).exists()
    assert storage.undo(transaction, environ) == str(config)
    assert config.read_bytes() == b"before"


def test_write_and_undo_refuse_concurrent_edits_without_echoing_contents(tmp_path, environ):
    config = tmp_path / "client.json"
    config.write_bytes(b"before-secret")
    with pytest.raises(storage.StorageError, match="changed") as caught:
        storage.write(config, b"other-secret", b"updated", environ)
    assert "secret" not in str(caught.value)

    transaction = storage.write(config, b"before-secret", b"installed", environ)
    config.write_bytes(b"user-edit-secret")
    with pytest.raises(storage.StorageError, match="preserving") as caught:
        storage.undo(transaction, environ)
    assert "user-edit-secret" not in str(caught.value)
    assert config.read_bytes() == b"user-edit-secret"


def test_refuses_symlink_target_and_parent_and_nonregular_file(tmp_path, environ):
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    link = tmp_path / "linked.json"
    link.symlink_to(outside)
    with pytest.raises(storage.StorageError):
        storage.read(link)
    with pytest.raises(storage.StorageError):
        storage.write(link, b"keep", b"replace", environ)
    assert outside.read_bytes() == b"keep"

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    parent_link = tmp_path / "alias"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(storage.StorageError):
        storage.write(parent_link / "client.json", None, b"new", environ)

    with pytest.raises(storage.StorageError):
        storage.read(tmp_path)


def test_only_one_concurrent_write_can_match_same_old_bytes(tmp_path, environ):
    config = tmp_path / "client.json"
    config.write_bytes(b"old")

    def update(value: bytes):
        try:
            return storage.write(config, b"old", value, environ)
        except storage.StorageError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, (b"first", b"second")))
    assert sum(result is not None for result in results) == 1
    assert config.read_bytes() in (b"first", b"second")


def test_backup_parent_symlink_is_refused_before_copying_secrets(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    config = tmp_path / "client.json"
    config.write_bytes(b"private")
    with pytest.raises(storage.StorageError):
        storage.write(config, b"private", b"changed", {"MCPCLERK_HOME": str(alias / "state")})
    assert config.read_bytes() == b"private"
    assert list(outside.iterdir()) == []
