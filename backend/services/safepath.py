"""Reading and writing inside a user's workspace without being led out of it.

The backend runs as root and touches files under a directory the user fully
controls.  A plain ``open()`` there is a privilege escalation waiting to happen:
replace ``output.7.out`` with a symlink and the backend will read, or write,
whatever it points at.  Reading ``/proc/self/environ`` that way hands over
``SECRET_KEY``; writing hands over the database.

Two defences, because either alone has a hole:

* **realpath containment** catches a symlinked parent directory, which
  ``O_NOFOLLOW`` does not see;
* **``O_NOFOLLOW``** catches the file being swapped for a link between the
  check and the open.
"""

import errno
import logging
import os
import stat
from typing import Optional

logger = logging.getLogger(__name__)


class UnsafePath(Exception):
    """The path resolved outside the workspace, or is not a regular file."""


def _contained(root: str, candidate: str) -> bool:
    root = os.path.realpath(root)
    return candidate == root or candidate.startswith(root + os.sep)


def resolve(root: str, relative: str) -> str:
    """Absolute path for *relative*, proven to stay inside *root*."""
    candidate = os.path.realpath(os.path.join(os.path.realpath(root), (relative or "").lstrip("/")))
    if not _contained(root, candidate):
        raise UnsafePath(f"{relative!r} resolves outside the workspace")
    return candidate


def read_tail(root: str, relative: str, max_bytes: int) -> tuple:
    """Return ``(bytes, total_size)`` for the end of a file in the workspace.

    Only the last *max_bytes* are read, so the size of the file on disk does
    not decide how much memory the platform uses.
    """
    path = resolve(root, relative)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise UnsafePath(f"{relative!r} is a symbolic link") from exc
        return b"", 0

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePath(f"{relative!r} is not a regular file")
        size = info.st_size
        window = min(size, max(0, max_bytes))
        if window <= 0:
            return b"", size
        os.lseek(fd, size - window, os.SEEK_SET)

        chunks, remaining = [], window
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), size
    finally:
        os.close(fd)


def size(root: str, relative: str) -> int:
    """Size of a file in the workspace, 0 when absent or not a regular file."""
    try:
        path = resolve(root, relative)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, UnsafePath):
        return 0
    try:
        info = os.fstat(fd)
        return info.st_size if stat.S_ISREG(info.st_mode) else 0
    finally:
        os.close(fd)


def write(root: str, relative: str, content: str, mode: int = 0o600,
          owner: Optional[int] = None) -> str:
    """Write *content* to a file in the workspace, refusing to follow links.

    An existing symlink at the target is removed rather than written through:
    the user put it there, and the platform's own file is what belongs at that
    path.
    """
    parent = resolve(root, os.path.dirname(relative) or ".")
    os.makedirs(parent, exist_ok=True)
    path = os.path.join(parent, os.path.basename(relative))

    if os.path.islink(path):
        logger.warning("Refusing to write through a symlink at %s; replacing it", path)
        os.unlink(path)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.write(fd, content.encode())
        os.fchmod(fd, mode)
        if owner is not None:
            os.fchown(fd, owner, owner)
    finally:
        os.close(fd)
    if owner is not None:
        try:
            os.chown(parent, owner, owner)
        except OSError:
            pass
    return path


def remove(root: str, relative: str) -> None:
    """Delete a file in the workspace if it is there (links included)."""
    try:
        parent = resolve(root, os.path.dirname(relative) or ".")
    except UnsafePath:
        return
    path = os.path.join(parent, os.path.basename(relative))
    try:
        os.remove(path)
    except OSError:
        pass
