"""Atomic, reversible updates to native client configuration files."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping


class StorageError(ValueError):
    """A configuration update cannot be performed safely."""


class _ReplaceUncertain(StorageError):
    pass


def _target(path: str | os.PathLike[str], *, create_parent: bool = False) -> tuple[str, str, str]:
    full = os.path.abspath(os.fspath(path))
    parent, name = os.path.split(full)
    if not name or name in (".", ".."):
        raise StorageError("configuration path is invalid")
    current = os.path.sep
    for part in Path(parent).parts[1:]:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if not create_parent:
                raise
            try:
                os.mkdir(current, 0o700)
                info = os.lstat(current)
            except FileExistsError:
                info = os.lstat(current)
            except OSError as error:
                raise StorageError("configuration parent cannot be created safely") from error
        except OSError as error:
            raise StorageError("configuration parent is unavailable") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise StorageError("configuration parent must contain no symlinks")
    return full, parent, name


def _open_parent(parent: str) -> int:
    try:
        return os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise
    except OSError as error:
        raise StorageError("configuration parent cannot be opened safely") from error


def _read_at(directory: int, name: str) -> tuple[bytes | None, int | None]:
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
    except FileNotFoundError:
        return None, None
    except OSError as error:
        raise StorageError("configuration file cannot be opened safely") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StorageError("configuration path is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(), stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def read(path: str | os.PathLike[str]) -> bytes | None:
    """Read a regular config file, returning None when it does not exist."""
    try:
        _, parent, name = _target(path)
        directory = _open_parent(parent)
        try:
            return _read_at(directory, name)[0]
        finally:
            os.close(directory)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StorageError("configuration file cannot be read safely") from error


def _state_home(environ: Mapping[str, str]) -> str:
    requested = environ.get("MCPCLERK_HOME") or os.path.join(environ.get("HOME") or str(Path.home()), ".mcpclerk")
    home = Path(requested).expanduser()
    if not home.is_absolute():
        home = Path(environ.get("HOME") or Path.home()) / home
    home = os.path.abspath(home)
    _target(os.path.join(home, "backups", ".receipt"), create_parent=True)
    try:
        os.chmod(home, 0o700)
        os.chmod(os.path.join(home, "backups"), 0o700)
    except OSError as error:
        raise StorageError("MCPClerk state directory is unavailable") from error
    return home


@contextmanager
def _locked(home: str):
    try:
        fd = os.open(os.path.join(home, "storage.lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as error:
        raise StorageError("MCPClerk lock file is unavailable") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StorageError("MCPClerk lock file is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as error:
            raise StorageError("MCPClerk lock could not be acquired") from error
        yield
    finally:
        os.close(fd)


def _atomic_replace(directory: int, name: str, data: bytes, mode: int) -> None:
    temp_name = ".mcpclerk-" + secrets.token_hex(12)
    fd = os.open(temp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600,
                 dir_fd=directory)
    replaced = False
    try:
        os.fchmod(fd, mode)
        stream = os.fdopen(fd, "wb")
        fd = -1
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, name, src_dir_fd=directory, dst_dir_fd=directory)
        replaced = True
        os.fsync(directory)
    except OSError as error:
        if replaced:
            raise _ReplaceUncertain("configuration replacement may have succeeded") from error
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name, dir_fd=directory)
        except FileNotFoundError:
            pass


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_record(backup_dir: str, transaction: str, metadata: dict, before: bytes) -> str:
    record_path = os.path.join(backup_dir, transaction)
    payload = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode() + b"\n" + before
    fd = os.open(record_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(backup_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return record_path


def write(path: str | os.PathLike[str], expected: bytes | None, updated: bytes,
          environ: Mapping[str, str] = os.environ) -> str:
    """Replace one config file and return an opaque id that can undo this write."""
    if (expected is not None and not isinstance(expected, bytes)) or not isinstance(updated, bytes):
        raise TypeError("expected and updated must be bytes (expected may be None)")
    full, parent, name = _target(path, create_parent=True)
    home = _state_home(environ)
    backup_dir = os.path.join(home, "backups")
    with _locked(home):
        directory = _open_parent(parent)
        transaction = secrets.token_hex(16)
        record_path = ""
        try:
            before, mode = _read_at(directory, name)
            if before != expected:
                raise StorageError("configuration changed since it was read")
            before = expected or b""
            metadata = {"path": full, "existed": expected is not None, "mode": mode,
                        "before": _digest(before), "after": _digest(updated)}
            record_path = _write_record(backup_dir, transaction, metadata, before)
            current, _ = _read_at(directory, name)
            if current != expected:
                raise StorageError("configuration changed during update")
            _atomic_replace(directory, name, updated, mode if mode is not None else 0o600)
            return transaction
        except Exception as error:
            if record_path and not isinstance(error, _ReplaceUncertain):
                try:
                    os.unlink(record_path)
                except OSError:
                    pass
            if isinstance(error, _ReplaceUncertain):
                raise StorageError(f"configuration update may have succeeded; undo transaction {transaction}") from error
            if isinstance(error, OSError):
                raise StorageError("configuration update could not be completed") from error
            raise
        finally:
            os.close(directory)


def _record(backup_dir: str, transaction: str) -> tuple[str, dict, bytes]:
    if not isinstance(transaction, str) or len(transaction) != 32 or any(c not in "0123456789abcdef" for c in transaction):
        raise StorageError("transaction id is invalid")
    path = os.path.join(backup_dir, transaction)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise StorageError("transaction record is unsafe")
            payload = stream.read()
        header, before = payload.split(b"\n", 1)
        metadata = json.loads(header)
        if not isinstance(metadata, dict):
            raise ValueError
        return path, metadata, before
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise StorageError("transaction record is unavailable or malformed") from error


def undo(transaction: str, environ: Mapping[str, str] = os.environ) -> str:
    """Restore the exact previous bytes, but only if the installed bytes are unchanged."""
    home = _state_home(environ)
    backup_dir = os.path.join(home, "backups")
    with _locked(home):
        record_path, metadata, before = _record(backup_dir, transaction)
        full, parent, name = _target(metadata.get("path", ""))
        if metadata.get("before") != _digest(before):
            raise StorageError("transaction backup failed integrity check")
        directory = _open_parent(parent)
        try:
            current, _ = _read_at(directory, name)
            if current is None or _digest(current) != metadata.get("after"):
                raise StorageError("configuration changed since update; preserving it")
            if metadata.get("existed"):
                if not isinstance(metadata.get("mode"), int):
                    raise StorageError("transaction mode is invalid")
                _atomic_replace(directory, name, before, metadata["mode"])
            else:
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
            os.unlink(record_path)
            return full
        except OSError as error:
            raise StorageError("configuration undo could not be completed") from error
        finally:
            os.close(directory)
