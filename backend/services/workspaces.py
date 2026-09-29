"""Where a user's workspace lives, and who owns it.

By default the platform creates and owns ``JUPYTER_DATA_DIR/<username>``.  An
administrator can instead point a user at a directory that already exists on
this machine, typically their own home, when the platform account and the
host account are the same person.

That case is different in three ways that all have to be handled together:

* **The files are already owned by someone.**  The container account must be
  created with *that* uid and gid.  Chowning the directory to the platform's
  usual uid would take a person's own files away from them on the host.
* **The platform must not write into it.**  Its bookkeeping files, the job
  token, the limits file, the SSH key it installs, go to a separate directory
  mounted beside the workspace, so nothing the platform does can overwrite a
  real ``~/.ssh/authorized_keys`` or clutter someone's home.
* **The mapping cannot be inferred.**  Whether platform user *alice* is the
  same person as host user *alice* is a judgement, and guessing it wrong hands
  that host account to whoever registered the name first.  An administrator
  sets it explicitly.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# Platform bookkeeping is mounted here, outside the workspace, so a mapped home
# is never written into.
PLATFORM_DIR = "/platform"

# Where a workspace appears inside the container.  It has to look like a home
# directory, because that is what everything installed into one assumes: conda
# writes its absolute prefix into ``.bashrc``, into every wrapper script's
# shebang and into its own ``conda-meta``, and pip does the same for
# ``--user`` installs.  Mounting a real home at ``/workspace`` left all of
# those pointing at paths that no longer existed.
CONTAINER_HOME_ROOT = "/home"

# Fallback uid when a directory's owner cannot be read.
DEFAULT_UID = 1000


def _safe_name(username: str) -> str:
    """The container account's name, mirrors container_manager.sanitize_username."""
    import re

    return re.sub(r"[^a-z0-9_-]", "_", username.lower())[:28]


@dataclass
class Workspace:
    """Everything the container layer needs to mount a user's files."""

    username: str
    host_path: str        # as the Docker daemon sees it
    backend_path: str     # as this process sees it
    uid: int
    gid: int
    mapped: bool          # True when it is a pre-existing directory

    @property
    def container_path(self) -> str:
        """Where this workspace is mounted inside the container.

        A mapped home keeps the *same absolute path* it has on this machine,
        so everything already installed in it, a conda prefix, a virtualenv,
        a ``pip install --user`` script, keeps working unchanged.  A workspace
        the platform created gets ``/home/<username>``, which is what a Linux
        account is expected to look like and what ``~`` expands to anyway.
        """
        if self.mapped:
            return self.host_path
        return os.path.join(CONTAINER_HOME_ROOT, _safe_name(self.username))

    @property
    def platform_host_path(self) -> str:
        """Host directory holding the platform's own files for this user."""
        return os.path.join(
            settings.JUPYTER_DATA_HOST_DIR or settings.JUPYTER_DATA_DIR,
            self.username, ".platform",
        )

    @property
    def platform_backend_path(self) -> str:
        return os.path.join(settings.JUPYTER_DATA_DIR, self.username, ".platform")


def validate_home_path(path: str) -> str:
    """Check an administrator-supplied mapping, or raise HTTP 400.

    Deliberately strict about *where*: a mapping must sit under
    ``HOME_MOUNT_ROOT``, so a mistyped path cannot mount ``/`` or ``/etc`` into
    somebody's shell.
    """
    from fastapi import HTTPException, status

    def bad(detail: str):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

    path = (path or "").strip().rstrip("/")
    if not path:
        return ""
    if not path.startswith("/"):
        raise bad("Give an absolute path, for example /home/alice.")

    root = os.path.realpath(settings.HOME_MOUNT_ROOT or "/home")
    resolved = os.path.realpath(path)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise bad(f"The directory must be under {root}.")
    if resolved == root:
        raise bad(f"{root} itself is not a workspace. Name a directory inside it.")

    if not os.path.isdir(resolved):
        raise bad(
            f"{path} is not visible to the platform. It must exist on the host "
            f"and {root} must be mounted into the backend "
            "(see docker-compose.homes.yml)."
        )

    info = os.stat(resolved)
    if info.st_uid == 0:
        raise bad(
            f"{path} is owned by root. A workspace has to belong to the person "
            "using it, otherwise nothing in it is writable."
        )
    if info.st_uid < 1000:
        raise bad(
            f"{path} belongs to a system account (uid {info.st_uid}). "
            "Map a real user's directory."
        )
    return resolved


def for_user(user) -> Workspace:
    """Resolve where this user's files live and which account owns them."""
    mapped_path = (getattr(user, "home_path", None) or "").strip()

    if mapped_path and os.path.isdir(mapped_path):
        try:
            info = os.stat(mapped_path)
            uid, gid = info.st_uid, info.st_gid
        except OSError as exc:
            logger.warning(
                "Cannot stat mapped home %s for %r (%s); falling back to the "
                "platform-owned workspace", mapped_path, user.username, exc,
            )
        else:
            # The host path and the backend path are the same string: the
            # backend mounts HOME_MOUNT_ROOT at the same location, so a
            # mapping means the same thing on both sides.
            return Workspace(
                username=user.username, host_path=mapped_path,
                backend_path=mapped_path, uid=uid, gid=gid, mapped=True,
            )
    elif mapped_path:
        logger.warning(
            "Mapped home %s for %r does not exist; using the platform workspace",
            mapped_path, user.username,
        )

    host_root = settings.JUPYTER_DATA_HOST_DIR or settings.JUPYTER_DATA_DIR
    return Workspace(
        username=user.username,
        host_path=os.path.join(host_root, user.username),
        backend_path=os.path.join(settings.JUPYTER_DATA_DIR, user.username),
        uid=DEFAULT_UID, gid=DEFAULT_UID, mapped=False,
    )


def for_username(username: str) -> Workspace:
    """Resolve a workspace when only the username is in hand.

    Callers deep in the container layer often have the name and not the row.
    Doing the lookup here means none of them has to decide what to do when it
    fails, they get the platform-owned workspace, which is the safe answer.
    """
    from database import SessionLocal

    db = SessionLocal()
    try:
        import models

        user = db.query(models.User).filter(models.User.username == username).first()
        if user is not None:
            return for_user(user)
    except Exception as exc:  # noqa: BLE001 (never fail a write on a lookup)
        logger.debug("Could not resolve workspace for %r: %s", username, exc)
    finally:
        db.close()

    host_root = settings.JUPYTER_DATA_HOST_DIR or settings.JUPYTER_DATA_DIR
    return Workspace(
        username=username,
        host_path=os.path.join(host_root, username),
        backend_path=os.path.join(settings.JUPYTER_DATA_DIR, username),
        uid=DEFAULT_UID, gid=DEFAULT_UID, mapped=False,
    )


def owner_uid(username: str) -> int:
    """The uid that owns this user's workspace.

    Resolved here rather than passed by each caller: every helper that writes a
    platform file needs it, and one that forgets produces a file the user
    cannot read, which is how the job commands silently stopped working for a
    mapped workspace.
    """
    return for_username(username).uid


def backend_path_for(username: str, home_path: Optional[str] = None) -> str:
    """Backend-visible workspace path, for code that only has a username."""
    if home_path and os.path.isdir(home_path):
        return home_path
    return os.path.join(settings.JUPYTER_DATA_DIR, username)
