"""Minimal client for the platform API, used by the experiment scripts.

Standard library only, so the experiments run from the host without a
virtualenv.  Everything goes through the same API an administrator and a user
would use: accounts, assignments and workspaces are created exactly as the
platform creates them, so the limits the experiments probe are the ones a real
user gets.

    export PLATFORM_URL=http://127.0.0.1:8000        # the backend port
    export ADMIN_PASSWORD=...                        # from .env
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("PLATFORM_URL", "http://127.0.0.1:8000").rstrip("/")


def _request(method, path, token=None, json_body=None, form=None, timeout=60):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read()
            return res.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as err:
        body = err.read()
        try:
            return err.code, json.loads(body)
        except ValueError:
            return err.code, body.decode(errors="replace")


def login(username, password):
    status, body = _request("POST", "/api/auth/login",
                            form={"username": username, "password": password})
    if status != 200:
        raise RuntimeError(f"login {username}: {status} {body}")
    return body["access_token"]


def admin_token():
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        path = os.path.expanduser(os.environ.get("ADMIN_PASSWORD_FILE", "~/.gpu_admin_pw"))
        if os.path.exists(path):
            with open(path) as fh:
                password = fh.read().strip()
    if not password:
        raise SystemExit("set ADMIN_PASSWORD, or put it in ~/.gpu_admin_pw")
    return login("admin", password)


def ensure_user(admin, username, password, **assignment):
    """Create *username* (or reset it) and give it exactly *assignment*."""
    status, users = _request("GET", "/api/admin/users", admin)
    existing = next((u for u in users if u["username"] == username), None)
    if existing is None:
        status, body = _request("POST", "/api/admin/users", admin, {
            "username": username, "email": f"{username}@experiment.local",
            "password": password, "full_name": f"Experiment account {username}",
        })
        if status != 201:
            raise RuntimeError(f"create {username}: {status} {body}")
        user_id = body["id"]
    else:
        user_id = existing["id"]
        _request("PUT", f"/api/admin/users/{user_id}", admin, {"is_active": True})
        _request("POST", f"/api/admin/users/{user_id}/reset-password", admin,
                 {"new_password": password})

    if assignment:
        status, rows = _request("GET", "/api/admin/gpu/assignments", admin)
        row = next((r for r in rows if r["user_id"] == user_id), None)
        if row is None:
            status, body = _request("POST", "/api/admin/gpu/assignments", admin,
                                    {"user_id": user_id, **assignment})
        else:
            status, body = _request("PUT", f"/api/admin/gpu/assignments/{row['id']}",
                                    admin, assignment)
        if status not in (200, 201):
            raise RuntimeError(f"assignment {username}: {status} {body}")
    return user_id


def start_workspace(token, timeout=180):
    status, body = _request("POST", "/api/user/me/jupyter/start", token, {}, timeout=timeout)
    if status not in (200, 201):
        raise RuntimeError(f"start: {status} {body}")
    return body


def stop_workspace(token):
    return _request("POST", "/api/user/me/jupyter/stop", token, {})


def deactivate(admin, username):
    status, users = _request("GET", "/api/admin/users", admin)
    user = next((u for u in users if u["username"] == username), None)
    if user:
        _request("PUT", f"/api/admin/users/{user['id']}", admin, {"is_active": False})


def container_id(name):
    out = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", name],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def cgroup_dir(name):
    return f"/sys/fs/cgroup/system.slice/docker-{container_id(name)}.scope"


def read_cgroup(name, filename):
    with open(os.path.join(cgroup_dir(name), filename)) as fh:
        return fh.read()


def wait_running(name, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                             capture_output=True, text=True)
        if out.stdout.strip() == "true":
            return
        time.sleep(1)
    raise RuntimeError(f"{name} did not start")


def sh(container, user, script, timeout=600, detach=False):
    """Run *script* with bash inside *container* as *user*.

    ``docker exec`` joins the container's cgroup, so whatever runs here is held
    to the same limits as the user's own processes.
    """
    cmd = ["docker", "exec"] + (["-d"] if detach else []) + \
          ["-u", user, container, "bash", "-lc", script]
    if detach:
        subprocess.run(cmd, check=True, capture_output=True)
        return None
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return out
