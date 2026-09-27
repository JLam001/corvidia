"""Private local discovery of a running stand service; never logs its token."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat


def default_session_path(port: int = 8080) -> Path:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be an integer in [1, 65535]")
    return Path.home() / ".local" / "state" / "corvidia" / f"stand-{port}.json"


@contextmanager
def _private_directory(path: Path, *, create: bool):
    """Pin the checked directory so a path replacement cannot redirect writes."""
    parent = path.parent
    if create:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(parent, flags)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PermissionError("session directory must be owned by this user and not writable by others")
        if create:
            os.fchmod(fd, 0o700)
        elif info.st_mode & 0o077:
            raise PermissionError("session directory must be private")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _session_lock(directory_fd: int, name: str):
    # Serializes replace/remove across cooperating service instances. Without
    # this lock an old process could remove a new process's session after checking
    # the old token. This file contains no secret and is deliberately retained.
    fd = os.open(f".{name}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("session lock must be a private file owned by this user")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def write_session(path, *, url: str, token: str, pid: int) -> Path:
    """Atomically publish {version, url, token, pid} in an owned 0600 file."""
    path = Path(path).expanduser().absolute()
    if not path.name or path.name in (".", ".."):
        raise ValueError("session path must name a file")
    if not isinstance(url, str) or not url or not isinstance(token, str) or not token:
        raise ValueError("session URL and token must be nonempty strings")
    if type(pid) is not int or pid <= 0:
        raise ValueError("session pid must be a positive integer")
    payload = (json.dumps({"version": 1, "url": url, "token": token, "pid": pid}) + "\n").encode()
    with _private_directory(path, create=True) as directory_fd, _session_lock(directory_fd, path.name):
        try:
            existing = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()):
            raise PermissionError("existing session must be a regular file owned by this user")
        temporary = f".{path.name}.{secrets.token_hex(12)}.tmp"
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
    return path


def remove_session(path, token: str) -> bool:
    """Remove only this service's record; missing/unreadable records are ignored.

    The final pathname is never followed if it is a symlink. False means no
    matching regular file was removed, including any unreadable/malformed record.
    """
    try:
        path = Path(path).expanduser().absolute()
        with _private_directory(path, create=False) as directory_fd, _session_lock(directory_fd, path.name):
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                    return False
                raw = stream.read(8193)
            if len(raw) > 8192:
                return False
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("token") != token:
                return False
            # Also refuse an uncooperative replacement during the read.
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                return False
            os.unlink(path.name, dir_fd=directory_fd)
            return True
    except (OSError, ValueError, TypeError):
        return False
