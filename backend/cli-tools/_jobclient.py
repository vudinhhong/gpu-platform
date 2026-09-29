"""Shared helper for the submit / queue / cancel commands.

Talks to the platform on the user's behalf using the credential the platform
placed in their workspace, so nothing has to be typed or remembered.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("PLATFORM_API", "http://backend:8000")
TOKEN_FILES = ("/platform/job-token", "/workspace/.platform/job-token")


def _workspace():
    """Where this workspace is mounted.

    The platform publishes the path; ``HOME`` is the same directory for anyone
    running these commands, and the old fixed path is the last resort for a
    container created before workspaces moved to their home path.
    """
    for value in (os.environ.get("PLATFORM_HOME"), os.environ.get("HOME")):
        if value and os.path.isdir(value):
            return value
    return "/workspace"


WORKSPACE = _workspace()


def die(message, code=1):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def token():
    for path in TOKEN_FILES:
        try:
            with open(path) as fh:
                value = fh.read().strip()
            if value:
                return value
        except OSError:
            continue
    die("this workspace has no job credentials yet. Restart it from the "
        "dashboard and try again")


def call(method, path, payload=None, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("X-Job-Token", token())
    if body:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        die(detail or f"request failed ({exc.code})")
    except urllib.error.URLError as exc:
        die(f"cannot reach the platform: {exc.reason}")


def relative_to_workspace(path):
    """Turn a user-supplied path into one relative to the workspace root."""
    absolute = os.path.realpath(os.path.join(os.getcwd(), path))
    root = os.path.realpath(WORKSPACE)
    if absolute != root and not absolute.startswith(root + os.sep):
        die(f"{path} is outside your workspace")
    return os.path.relpath(absolute, root)


def cwd_relative():
    return relative_to_workspace(".")


STATUS_COLOUR = {
    "queued": "\033[33m", "starting": "\033[36m", "running": "\033[36m",
    "paused": "\033[35m",
    "succeeded": "\033[32m", "failed": "\033[31m",
    "cancelled": "\033[2m", "timeout": "\033[31m",
}


def paint(status_value):
    if not sys.stdout.isatty():
        return status_value
    return f"{STATUS_COLOUR.get(status_value, '')}{status_value}\033[0m"


def duration(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
