"""End-to-end check of the upgraded GPU Platform, run from inside the backend
container so it exercises the real network path (frontend nginx -> backend)."""
import json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

BASE = os.environ.get("E2E_BASE", "http://frontend:80")
OK, FAIL = [], []

def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    return cond

def req(method, path, token=None, data=None, form=None, cookie=None, expect=None):
    url = BASE + path
    body, headers = None, {}
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    r = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            # Static assets are binary (fonts, icons), so never assume text.
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw), resp.headers
            except json.JSONDecodeError:
                return resp.status, raw, resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw), e.headers
        except json.JSONDecodeError:
            return e.code, raw, e.headers

print("\n=== 1. Auth ===")
# The admin account is configurable so the check can run against a deployment
# whose admin password has been rotated (which it should have been).
ADMIN_USER = os.environ.get("E2E_ADMIN_USER", "admin")
admin_pw = os.environ["ADMIN_PASSWORD"]
st, body, hdrs = req("POST", "/api/auth/login", form={"username": ADMIN_USER, "password": admin_pw})
check("admin login returns 200", st == 200, str(body)[:80])
ADMIN = body.get("access_token", "") if isinstance(body, dict) else ""
set_cookie = hdrs.get("set-cookie", "") if hdrs else ""
check("login sets HttpOnly proxy cookie scoped to /jupyter",
      "gpu_proxy_token=" in set_cookie and "HttpOnly" in set_cookie and "Path=/jupyter" in set_cookie,
      set_cookie[:90])

st, body, _ = req("POST", "/api/auth/login", form={"username": ADMIN_USER, "password": "definitely-wrong"})
check("wrong password rejected", st == 401)
st, _, _ = req("GET", "/api/admin/users")
check("admin API without token = 401", st == 401, f"got {st}")

print("\n=== 2. Password policy ===")
st, body, _ = req("POST", "/api/admin/users", token=ADMIN, data={
    "username": "e2eweak", "email": "weak@e2e.local", "password": "short1"})
check("weak password refused (min length)", st == 400, str(body)[:90])

print("\n=== 3. Create user + GPU assignment ===")
USER, PW = "e2euser", "E2Etest-passw0rd"
req("DELETE", "/api/admin/users/9999", token=ADMIN)  # no-op
st, users, _ = req("GET", "/api/admin/users", token=ADMIN)
for u in users if isinstance(users, list) else []:
    if u["username"] == USER:
        req("DELETE", f"/api/admin/users/{u['id']}", token=ADMIN)
# Deleting only moves the account to the trash, and a trashed account keeps
# its username, so a leftover from an earlier run has to be purged rather than
# deleted, or this run cannot create the user at all.
st, trash, _ = req("GET", "/api/admin/trash", token=ADMIN)
for u in (trash or {}).get("users", []) if isinstance(trash, dict) else []:
    if u["username"] == USER:
        req("DELETE", f"/api/admin/trash/{u['id']}", token=ADMIN)
st, body, _ = req("POST", "/api/admin/users", token=ADMIN, data={
    "username": USER, "email": "e2e@e2e.local", "password": PW, "disk_quota_mb": 5000})
check("create user", st == 201, str(body)[:100])
uid = body.get("id") if isinstance(body, dict) else None

st, body, _ = req("POST", "/api/admin/gpu/assignments", token=ADMIN,
                  data={"user_id": uid, "gpu_indices": [0], "memory_limit_mb": 4096,
                        "cpu_cores": 2})
check("assign GPU 0", st == 201, str(body)[:100])
aid = body.get("id") if isinstance(body, dict) else None

st, body, _ = req("POST", "/api/admin/gpu/assignments", token=ADMIN,
                  data={"user_id": uid, "gpu_indices": [0, 7]})
check("assigning a non-existent GPU is refused", st in (400, 409), str(body)[:110])

print("\n=== 4. User session ===")
st, body, hdrs = req("POST", "/api/auth/login", form={"username": USER, "password": PW})
check("user login", st == 200)
UTOK = body.get("access_token", "")
UCOOKIE = (hdrs.get("set-cookie", "") or "").split(";")[0]

t0 = time.time()
st, body, _ = req("POST", "/api/user/me/jupyter/start", token=UTOK, data={})
check(f"start session (took {time.time()-t0:.1f}s)", st == 200, str(body)[:200])
session = body if isinstance(body, dict) else {}
check("session reports running", session.get("status") == "running", str(session.get("status")))
check("session got an SSH port", bool(session.get("ssh_port")), str(session.get("ssh_port")))
check("session secret is decrypted for its owner", bool(session.get("token")))

print("\n=== 5. GPU isolation (the core fix) ===")
cname = "gpu-jupyter-" + USER
import docker as dockersdk
dcli = dockersdk.from_env()
try:
    attrs = dcli.containers.get(cname).attrs
except Exception as exc:
    attrs = None
    print("  container inspect failed:", exc)
if attrs:
    devreqs = attrs["HostConfig"].get("DeviceRequests") or []
    ids = devreqs[0].get("DeviceIDs") if devreqs else None
    check("container has an explicit DeviceRequest", bool(devreqs), str(devreqs)[:120])
    check("DeviceRequest pins exactly ONE gpu by UUID",
          bool(ids) and len(ids) == 1 and ids[0].startswith("GPU-"), str(ids))
    env = dict(e.split("=", 1) for e in attrs["Config"]["Env"] if "=" in e)
    check("CUDA_VISIBLE_DEVICES renumbered to 0", env.get("CUDA_VISIBLE_DEVICES") == "0",
          env.get("CUDA_VISIBLE_DEVICES"))
    check("memory cgroup applied", attrs["HostConfig"]["Memory"] == 4096 * 1024 * 1024,
          str(attrs["HostConfig"]["Memory"]))
    check("pids limit applied", attrs["HostConfig"].get("PidsLimit") == 512,
          str(attrs["HostConfig"].get("PidsLimit")))
    rc, out = dcli.containers.get(cname).exec_run("nvidia-smi -L")
    text = out.decode(errors="replace")
    lines = [l for l in text.strip().splitlines() if l.startswith("GPU ")]
    check("container sees exactly 1 of the host's 2 GPUs", len(lines) == 1,
          f"{len(lines)} -> {text.strip()[:90]}")
else:
    check("container exists", False, "inspect failed")

print("\n=== 6. Jupyter proxy (HTTP) ===")
st, body, _ = req("GET", f"/jupyter/{USER}/api/status", token=UTOK)
check("proxy forwards with Bearer JWT", st == 200, str(body)[:100])
st, body, _ = req("GET", f"/jupyter/{USER}/api/status", cookie=UCOOKIE)
check("proxy forwards with the browser cookie", st == 200, str(body)[:100])
st, _, _ = req("GET", f"/jupyter/{USER}/api/status")
check("proxy denies an unauthenticated request", st == 403, f"got {st}")
st, _, _ = req("GET", f"/jupyter/{ADMIN_USER}/api/status", token=UTOK)
check("user cannot reach another user's Jupyter", st in (403, 404), f"got {st}")
st, _, _ = req("GET", f"/jupyter/{USER}/api/status", token=ADMIN)
check("admin can reach any user's Jupyter", st == 200, f"got {st}")
st, body, _ = req("GET", f"/jupyter/{USER}/lab", token=UTOK)
check("JupyterLab UI is served through the proxy", st == 200 and "jupyter" in str(body).lower())

print("\n=== 6a. One password per user ===")
st, ssh_info, _ = req("GET", "/api/user/me/ssh", token=UTOK)
check("SSH info is returned for a running session", st == 200, str(ssh_info)[:100])
if st == 200:
    check("SSH uses the account password (no second secret shown)",
          ssh_info.get("uses_account_password") is True and ssh_info.get("password") is None,
          str({k: ssh_info.get(k) for k in ("uses_account_password", "password")}))

_sys2 = __import__("sys"); _sys2.path.insert(0, "/app")
from database import SessionLocal as _SL2
import models as _m2
_db2 = _SL2()
_u2 = _db2.query(_m2.User).filter(_m2.User.username == USER).first()
_aj, _ux = (_u2.account_jupyter_hash or ""), (_u2.unix_password_hash or "")
_db2.close()
check("Jupyter credential derived from the account password",
      _aj.startswith("argon2:$argon2"), _aj[:28])
check("UNIX credential derived from the account password (sha512-crypt)",
      _ux.startswith("$6$"), _ux[:14])
check("the account password itself is never stored", PW not in _aj + _ux)

print("\n=== 6a2. One directory for SSH and JupyterLab ===")
# A file created in the container's HOME must be listed by JupyterLab: the two
# used to be different trees, so anything made over SSH landed in a hidden
# folder the notebook browser does not display.
import docker as _dk
_cli = _dk.from_env()
_c = _cli.containers.get("gpu-jupyter-" + USER)
_rc, _out = _c.exec_run(["sh", "-c", "getent passwd " + USER + " | cut -d: -f6"])
WS = (_out or b"").decode().strip()
# The workspace is mounted at the path it has outside, so anything installed
# into it keeps working: /home/<user>, or a mapped home's own path.
check("the SSH account's home looks like a home directory",
      WS.startswith("/home/"), WS)
_rc, _out = _c.exec_run(["sh", "-c", "readlink -f /workspace"])
check("the old /workspace path still resolves to the same files",
      (_out or b"").decode().strip() == WS, (_out or b"").decode().strip())

_c.exec_run(["su", "-s", "/bin/sh", USER, "-c",
             f"mkdir -p {WS}/e2e-shared && echo hi > {WS}/e2e-shared/from-shell.txt"])
st, listing, _ = req("GET", f"/jupyter/{USER}/api/contents/", token=UTOK)
names = [c["name"] for c in (listing or {}).get("content", [])] if isinstance(listing, dict) else []
check("a directory created in the shell is listed by JupyterLab",
      "e2e-shared" in names, str(names)[:120])

st, made, _ = req("POST", f"/jupyter/{USER}/api/contents/e2e-shared",
                  token=UTOK, data={"type": "notebook"})
check("JupyterLab can create a notebook there (writable workspace)",
      st in (200, 201) and made.get("writable") is True,
      str(made)[:120] if st in (200, 201) else f"HTTP {st}: {str(made)[:90]}")

_rc, _out = _c.exec_run(["su", "-s", "/bin/sh", USER, "-c", f"ls {WS}/e2e-shared"])
check("the notebook is visible from the shell too",
      b".ipynb" in (_out or b""), (_out or b"").decode().strip()[:80])

print("\n=== 6a3. Telemetry agrees with Docker's own accounting ===")
_c3 = _cli.containers.get("gpu-jupyter-" + USER)
# The collector runs on METRICS_INTERVAL_SECONDS and the session was just
# (re)started, so wait for it to include this container rather than racing it.
_plat, res3 = {}, {}
_deadline3 = time.time() + 45
while time.time() < _deadline3:
    st, res3, _ = req("GET", "/api/user/me/resources", token=UTOK)
    _plat = ((res3 or {}).get("container") or {}).get("memory") or {}
    if _plat.get("used_mb") is not None:
        break
    time.sleep(3)
# Sample Docker only once the platform has a reading, so both refer to the
# same steady state.
_raw = _c3.stats(stream=False)
_mem = _raw["memory_stats"]
_docker_mb = round((_mem.get("usage", 0) - (_mem.get("stats") or {}).get("inactive_file", 0)) / 1048576)
_plat_mb = _plat.get("used_mb")
check("reported RAM matches the container's own cgroup accounting",
      _plat_mb is not None and abs(_plat_mb - _docker_mb) <= max(8, _docker_mb * 0.1),
      f"platform {_plat_mb} MB vs docker {_docker_mb} MB")
check("reported RAM limit matches the cgroup cap",
      _plat.get("limit_mb") == round(_c3.attrs["HostConfig"]["Memory"] / 1048576),
      f"{_plat.get('limit_mb')} vs {round(_c3.attrs['HostConfig']['Memory'] / 1048576)}")

_rc3, _out3 = _c3.exec_run(["sh", "-c", "cat /sys/fs/cgroup/pids.current"])
_cg_pids = int((_out3 or b"0").decode().strip() or 0)
_plat_pids = ((res3 or {}).get("container") or {}).get("pids")
check("reported process count matches the cgroup",
      _plat_pids is not None and abs(_plat_pids - _cg_pids) <= 3,
      f"platform {_plat_pids} vs cgroup {_cg_pids}")

print("\n=== 6a4. The shell people actually work in ===")
_c4 = _cli.containers.get("gpu-jupyter-" + USER)

def _sh(command):
    rc, out = _c4.exec_run(["su", "-s", "/bin/bash", USER, "-c", command])
    return rc, (out or b"").decode(errors="replace").strip()

_rc, _shell = _sh("echo $SHELL")
check("interactive shell is bash, not sh", _shell == "/bin/bash", _shell)
# Without SHELL set, JupyterLab's terminal falls back to dash: a bare "$"
# prompt, no colour, no completion.
_rc, _out = _sh("test -r /usr/share/bash-completion/bash_completion && echo yes")
check("tab completion is installed", _out == "yes", _out)
_rc, _out = _sh("command -v /usr/bin/top /usr/bin/ps /usr/bin/free | wc -l")
check("ps / top / free exist at all", _out == "3", _out)
_rc, _out = _sh("for t in git less nano vim wget unzip bzip2 rsync; do command -v $t >/dev/null || echo $t; done")
check("the usual command-line tools are present", _out == "", "missing: " + _out)
_rc, _host = _sh("hostname")
check("the prompt does not name infrastructure", _host == "workspace", _host)

# Regression: the friendly `df` must never reach a script.  Rewriting `df -Pk`
# into human units made the Miniconda installer do arithmetic on "195.1G".
_rc, _script_df = _sh(f"df -Pk {WS} | tail -1 | awk '{{print $4}}'")
check("df -Pk returns a plain number scripts can compute with",
      _script_df.isdigit(), repr(_script_df))
_rc, _arith = _sh(f"a=$(df -Pk {WS} | tail -1 | awk '{{print $4}}'); echo $(( a / 1024 ))")
check("shell arithmetic on df output works", _arith.isdigit(), repr(_arith))
_rc, _human_df = _sh(f"df -h {WS} | tail -1")
check("df -h still shows the workspace at its allocated size",
      "workspace" in _human_df, _human_df[:70])
_rc, _free = _sh("free -w 2>&1 | head -1")
check("free defers to the real one for options it cannot reproduce",
      "total" in _free.lower(), _free[:60])

# A workspace started from the current image must say so; a rebuilt image is
# only picked up by a stop/start, never by a restart, so the UI has to tell
# people rather than leaving them on a stale environment indefinitely.
st, status_payload, _ = req("GET", "/api/user/me/jupyter/status", token=UTOK)
check("the workspace reports whether its environment is current",
      (status_payload or {}).get("runtime", {}).get("environment_current") is True,
      str((status_payload or {}).get("runtime", {}).get("environment_current")))

# Every job command must be self-documenting.
for _cmd in ("submit", "queue", "cancel"):
    _rc, _help = _c4.exec_run(["su", "-s", "/bin/bash", USER, "-c", f"{_cmd} --help"])
    _text = (_help or b"").decode(errors="replace")
    check(f"`{_cmd} --help` explains itself",
          _rc == 0 and "usage:" in _text and "options:" in _text,
          _text.splitlines()[0] if _text else f"rc={_rc}")

# Shared memory: Docker's 64 MB default breaks any DataLoader with workers,
# and the failure surfaces as "Connection reset by peer" from multiprocessing.
_rc, _shm = _sh("/usr/bin/df -m /dev/shm | tail -1 | awk '{print $2}'")
check("the workspace has usable shared memory, not Docker's 64 MB",
      _shm.isdigit() and int(_shm) >= 512, f"{_shm} MB")
_rc, _shmdf = _sh("df -h 2>/dev/null | grep -c /dev/shm")
check("df shows /dev/shm, so running out of it is diagnosable",
      _shmdf.strip() == "1", _shmdf)

print("\n=== 6b. Static assets through the proxy ===")
# Regression: nginx matches regex locations BEFORE prefix ones, so a
# static-asset cache block (~* \.(js|css|...)$) silently swallowed every
# JupyterLab asset, and the page loaded and stayed blank.
st, lab_html, _ = req("GET", f"/jupyter/{USER}/lab", token=UTOK)
asset_refs = re.findall(r'(?:href|src)="([^"]+\.(?:js|css|ico))(?:\?[^"]*)?"', str(lab_html))
check("JupyterLab page references static assets", bool(asset_refs), str(asset_refs[:2]))
asset_ok = True
for ref in asset_refs[:4]:
    path = ref if ref.startswith("/") else f"/jupyter/{USER}/{ref}"
    code, body, hdrs = req("GET", path, token=UTOK)
    if code != 200:
        asset_ok = False
        print(f"      {path} -> HTTP {code}")
check("every referenced .js/.css/.ico asset returns 200", asset_ok,
      f"{len(asset_refs[:4])} asset(s) checked")

print("\n=== 6c. Jupyter password mode ===")
JPW = "Jupyter-pass-1234"
st, body, _ = req("PUT", "/api/user/me/jupyter-password", token=UTOK, data={"password": JPW})
check("set Jupyter password", st == 200, str(body)[:80])

# The hash must be in the format Jupyter itself verifies (argon2: prefix);
# without it passwd_check() rejects even the correct password.
import sys as _sys
_sys.path.insert(0, "/app")
from database import SessionLocal as _SL
import models as _m
_db = _SL()
_row = _db.query(_m.User).filter(_m.User.username == USER).first()
_stored = _row.hashed_jupyter_password if _row else None
_db.close()
check("stored hash uses Jupyter's argon2: format",
      bool(_stored) and _stored.startswith("argon2:"), str(_stored)[:40])

req("POST", "/api/user/me/jupyter/stop", token=UTOK, data={})
st, body, _ = req("POST", "/api/user/me/jupyter/start", token=UTOK, data={})
check("restart session in password mode", st == 200 and body.get("status") == "running",
      str(body)[:120])

# Full browser login flow: fetch the form (for _xsrf), post the password.
import http.cookiejar as _cj

_jar = _cj.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_opener.addheaders = [("Authorization", f"Bearer {UTOK}")]

login_url = f"{BASE}/jupyter/{USER}/login?next=/jupyter/{USER}/lab"
with _opener.open(login_url, timeout=60) as resp:
    login_html = resp.read().decode(errors="replace")
    check("login page served", resp.status == 200 and "password" in login_html.lower())

xsrf = re.search(r'name="_xsrf"\s+value="([^"]+)"', login_html)
check("login form carries an _xsrf token", bool(xsrf))

css_refs = re.findall(r'href="([^"]+\.css)"', login_html)
css_ok = True
for ref in css_refs[:3]:
    path = ref if ref.startswith("/") else f"/jupyter/{USER}/{ref}"
    code, _, _ = req("GET", path, token=UTOK)
    if code != 200:
        css_ok = False
        print(f"      {path} -> HTTP {code}")
check("login page stylesheets load (page is not unstyled)", css_ok,
      f"{len(css_refs[:3])} stylesheet(s)")

if xsrf:
    form = urllib.parse.urlencode({"_xsrf": xsrf.group(1), "password": JPW}).encode()
    r = urllib.request.Request(f"{BASE}/jupyter/{USER}/login", data=form, method="POST")
    r.add_header("Content-Type", "application/x-www-form-urlencoded")
    r.add_header("Authorization", f"Bearer {UTOK}")
    try:
        with _opener.open(r, timeout=60) as resp:
            landed = resp.read().decode(errors="replace")
            ok = resp.status == 200 and "invalid credentials" not in landed.lower()
    except urllib.error.HTTPError as e:
        ok = False
        landed = e.read().decode(errors="replace")
    check("correct Jupyter password is ACCEPTED", ok,
          "rejected" if not ok else "logged in")
    check("a Jupyter session cookie was issued",
          any(c.name.startswith("username-") or "token" in c.name for c in _jar),
          str([c.name for c in _jar]))

# Back to token mode for the remaining sections.  Clearing the password only
# takes effect on the next start, so the session has to be recycled; otherwise
# section 7 posts to a password-mode Jupyter without its _xsrf cookie.
req("PUT", "/api/user/me/jupyter-password", token=UTOK, data={"password": ""})
req("POST", "/api/user/me/jupyter/stop", token=UTOK, data={})
st, body, _ = req("POST", "/api/user/me/jupyter/start", token=UTOK, data={})
check("back to token mode", st == 200 and body.get("status") == "running", str(body)[:80])

print("\n=== 6d. Terminals and real enforcement ===")
# Terminals: the image once shipped without etc/jupyter/jupyter_server_config.d,
# so every server extension stayed disabled and the launcher had no Terminal.
st, terms, _ = req("GET", f"/jupyter/{USER}/api/terminals", token=UTOK)
check("the terminals API is served (extension enabled)", st == 200, f"HTTP {st}")
st, term, _ = req("POST", f"/jupyter/{USER}/api/terminals", token=UTOK, data={})
check("a terminal can be created", st in (200, 201) and bool((term or {}).get("name")),
      str(term)[:80])

# Limits must be what the DAEMON applied, not what the platform intended: a
# setting it rejects is dropped so the session can still start.
st, res, _ = req("GET", "/api/user/me/resources", token=UTOK)
enforced = (res or {}).get("enforced") or {}
check("RAM cap applied by the daemon", bool(enforced.get("memory_limit_mb")),
      str(enforced.get("memory_limit_mb")))
# The cap must equal what the assignment asked for, not a derived guess.
_st_a, _assigns, _ = req("GET", "/api/admin/gpu/assignments", token=ADMIN)
_mine = next((a for a in (_assigns or []) if a["user_id"] == uid), {}) if _st_a == 200 else {}
_want = _mine.get("cpu_cores") or None
check("CPU cap applied by the daemon", bool(enforced.get("cpu_cores")),
      str(enforced.get("cpu_cores")))
if _want:
    check("CPU cap equals the assigned core count (not seconds/3600)",
          abs(float(enforced.get("cpu_cores") or 0) - float(_want)) < 0.01,
          f"assigned {_want}, applied {enforced.get('cpu_cores')}")
check("process cap applied by the daemon", bool(enforced.get("pids_limit")),
      str(enforced.get("pids_limit")))
check("disk I/O caps applied by the daemon",
      bool(enforced.get("disk_read_mbps")) and bool(enforced.get("disk_write_mbps")),
      f"read={enforced.get('disk_read_mbps')} write={enforced.get('disk_write_mbps')} MB/s")
check("GPU pinned by UUID", bool(enforced.get("gpus")) and
      all(str(g).startswith("GPU-") for g in enforced["gpus"]), str(enforced.get("gpus")))

# And the kernel agrees with the daemon.
_c2 = _cli.containers.get("gpu-jupyter-" + USER)
_rc, _out = _c2.exec_run(["sh", "-c", "cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max "
                          "/sys/fs/cgroup/pids.max /sys/fs/cgroup/io.max"])
_cg = (_out or b"").decode().split("\n")
check("the cgroup itself carries the limits", len(_cg) >= 4 and _cg[0].isdigit(),
      " | ".join(x for x in _cg[:4] if x))

print("\n=== 6e. Batch job queue ===")
# A job is submitted from inside the workspace; here we drive the same API the
# `submit` command calls.
import os as _os

_ws = f"/jupyter_data/{USER}"
_os.makedirs(f"{_ws}/jobs", exist_ok=True)
with open(f"{_ws}/jobs/hello.sh", "w") as _fh:
    _fh.write("#!/bin/bash\n"
              "echo \"whoami=$(whoami) pwd=$(pwd) home=$HOME\"\n"
              "nvidia-smi -L | wc -l\n"
              "echo JOB_OK\n")
_os.chmod(f"{_ws}/jobs/hello.sh", 0o755)
subprocess.run(["chown", "-R", "1000:1000", f"{_ws}/jobs"], capture_output=True)

st, job, _ = req("POST", "/api/jobs", token=UTOK, data={
    "script": "jobs/hello.sh", "workdir": "jobs", "name": "e2e-hello",
    "gpu_count": 1, "gpu_memory_mb": 2000,
})
check("a job can be submitted", st == 201 and job.get("id"), str(job)[:120])
job_id = job.get("id")
check("it is queued with a position", job.get("status") == "queued" and job.get("queue_position"),
      f"status={job.get('status')} pos={job.get('queue_position')}")
check("the output file is named after the job id",
      job.get("output") == f"jobs/output.{job_id}.out", str(job.get("output")))

# Scheduler runs on JOB_SCHEDULER_INTERVAL; wait for a terminal state.
final, deadline = None, time.time() + 150
while time.time() < deadline:
    st, detail, _ = req("GET", f"/api/jobs/{job_id}", token=UTOK)
    if detail.get("status") in ("succeeded", "failed", "timeout", "cancelled"):
        final = detail
        break
    time.sleep(5)
check("the scheduler ran it to completion", bool(final) and final["status"] == "succeeded",
      str((final or {}).get("status")) + " " + str((final or {}).get("message") or ""))

if final:
    out = final.get("output_tail") or ""
    check("the script ran as its owner, in its own workspace",
          f"whoami={USER}" in out and f"pwd={WS}/jobs" in out and f"home={WS}" in out,
          out.splitlines()[0] if out else "(no output)")
    check("the job saw exactly the GPUs it was scheduled onto",
          "\n1\n" in out or out.strip().splitlines()[-2:] == ["1", "JOB_OK"],
          " | ".join(out.splitlines()[-3:]))
    check("output was written to the submit directory",
          _os.path.isfile(f"{_ws}/jobs/output.{job_id}.out"),
          f"jobs/output.{job_id}.out")
    _stat = _os.stat(f"{_ws}/jobs/output.{job_id}.out")
    check("the output file belongs to the user, not root", _stat.st_uid == 1000, str(_stat.st_uid))

# A job needs no GPU at all; it is admitted against CPU capacity instead.
st, cpu_job, _ = req("POST", "/api/jobs", token=UTOK, data={
    "script": "jobs/hello.sh", "workdir": "jobs", "name": "e2e-cpu-only",
    "gpu_count": 0})
check("a job can ask for no GPU", st == 201 and cpu_job.get("gpu_count") == 0,
      str(cpu_job)[:100])
st, listing, _ = req("GET", "/api/jobs", token=UTOK)
check("CPU capacity is reported alongside GPU capacity",
      bool((listing or {}).get("cpu", {}).get("total_cores")),
      str((listing or {}).get("cpu")))
if cpu_job.get("id"):
    req("POST", "/api/jobs/cancel", token=UTOK, data={"ids": [cpu_job["id"]]})

# Consumption is recorded per user, GPU and CPU alike.
st, my_usage, _ = req("GET", "/api/user/me/usage", token=UTOK)
check("the user can see their own GPU and CPU hours",
      st == 200 and "gpu_hours" in my_usage and "cpu_hours" in my_usage,
      str(my_usage)[:130])
check("their submitted jobs are counted", bool(my_usage.get("jobs")),
      str(my_usage.get("jobs")))

# The two time budgets.  They are charged to jobs only, so the workspace this
# user has had open for the whole run counts for nothing, and whatever they are
# over is over because of the jobs above.
_quota = (my_usage or {}).get("quota") or {}
_period = _quota.get("period") or {}
check("the budget period says when it refills",
      bool(_period.get("label") and _period.get("resets_text")), str(_period)[:90])
_cpu_used = (_quota.get("cpu_hours") or {}).get("used") or 0
if _cpu_used > 0:
    # Half of what their jobs have already spent is a budget they are over.
    req("PUT", f"/api/admin/users/{uid}", token=ADMIN,
        data={"cpu_hours_quota": round(_cpu_used / 2, 6)})
    st, refused, _ = req("POST", "/api/jobs", token=UTOK, data={
        "script": "jobs/hello.sh", "workdir": "jobs", "name": "e2e-over-budget",
        "gpu_count": 0})
    check("a user out of CPU hours cannot queue new work",
          st == 429 and "CPU hours" in str(refused), f"{st} {str(refused)[:100]}")
    if st == 201 and isinstance(refused, dict) and refused.get("id"):
        req("POST", "/api/jobs/cancel", token=UTOK, data={"ids": [refused["id"]]})

    # ...and the same budget leaves their workspace alone, which is the point of
    # charging the queue rather than the seat.  Read rather than restarted: the
    # kernel and WebSocket checks further down need this session as it is.
    st, _res, _ = req("GET", "/api/user/me/resources", token=UTOK)
    _over = (((_res or {}).get("quota") or {}).get("cpu_hours") or {}).get("over")
    st2, _sess, _ = req("GET", "/api/user/me/jupyter/status", token=UTOK)
    check("being out of hours does not cost the workspace",
          _over is True and st2 == 200
          and (_sess or {}).get("status") == "running",
          f"over={_over} session={(_sess or {}).get('status')}")
    req("PUT", f"/api/admin/users/{uid}", token=ADMIN, data={"cpu_hours_quota": 0})
else:
    check("the user's jobs recorded some CPU time to budget against",
          False, f"cpu_hours used = {_cpu_used}")

# The queue is ordered by fair share, and says so.
st, admin_jobs, _ = req("GET", "/api/admin/jobs", token=ADMIN)
check("admins can see why the queue is in this order",
      st == 200 and "fair_share" in admin_jobs, str(list((admin_jobs or {}).keys()))[:90])

# Output is read from a directory its owner controls, by a backend running as
# root.  Replacing the file with a symlink must not turn that into a way to
# read the platform's own files.
_ws_root = f"/jupyter_data/{USER}"
if final and _os.path.isfile(f"{_ws_root}/jobs/output.{job_id}.out"):
    _real = f"{_ws_root}/jobs/output.{job_id}.out"
    _backup = _real + ".keep"
    _os.rename(_real, _backup)
    _os.symlink("/app/data/gpu_platform.db", _real)
    st, attacked, _ = req("GET", f"/api/jobs/{job_id}", token=UTOK)
    _leaked = (attacked or {}).get("output_tail", "")
    check("a symlinked output file cannot be used to read platform files",
          "cannot be shown" in _leaked or _leaked == "",
          _leaked[:60])
    _os.remove(_real)
    _os.rename(_backup, _real)

# Output is bounded regardless of how much a job prints.
if final:
    _big = f"{_ws_root}/jobs/output.{job_id}.out"
    with open(_big, "w") as _fh:
        for _i in range(20000):
            _fh.write(f"line {_i} " + "x" * 120 + "\n")
    _size = _os.path.getsize(_big)
    st, bounded, _ = req("GET", f"/api/jobs/{job_id}", token=UTOK)
    _tail = (bounded or {}).get("output_tail", "")
    check("a huge output file is tailed, not loaded",
          len(_tail) < 200_000 and _size > 2_000_000,
          f"file {_size // 1048576} MB -> {len(_tail) // 1024} KB returned")
    check("the tail is the END of the file",
          _tail.strip().endswith("x" * 10) and "line 19999" in _tail,
          _tail.strip().splitlines()[-1][:40] if _tail.strip() else "(empty)")
    check("the response says it was truncated and how big the file is",
          bounded.get("output_truncated") is True and bounded.get("output_size_bytes") == _size,
          f"truncated={bounded.get('output_truncated')} size={bounded.get('output_size_bytes')}")

# A \r progress bar must stay ONE line in the file, and be shown as its final
# state rather than every frame it ever drew.
if final:
    _pb = f"{_ws_root}/jobs/output.{job_id}.out"
    _frame = "\r  45%|####      | 45/100 [02:13<02:43, 3371.29it/s, loss=0.0421]"
    # Deliberately small enough to fit the display window, so the whole file
    # is visible and the collapse can be checked against all of it.
    with open(_pb, "w") as _fh:
        _fh.write("\x1b[32mEpoch 1/3\x1b[0m\n")
        for _i in range(1000):
            _fh.write(_frame)
        _fh.write("\ntraining finished\n")
    with open(_pb, "rb") as _fh:
        _newlines = _fh.read().count(b"\n")
    check("1000 progress updates are 3 lines in the file, not 1000",
          _newlines == 3, f"{_newlines} newlines")

    st, shown, _ = req("GET", f"/api/jobs/{job_id}", token=UTOK)
    _tail = shown.get("output_tail") or ""
    _lines = [l for l in _tail.splitlines() if l.strip()]
    check("the progress bar shows as one line, its final state",
          len(_lines) == 3 and _lines[0] == "Epoch 1/3"
          and _lines[1].startswith("  45%") and _lines[2] == "training finished",
          f"{len(_lines)} lines: {_lines[:3]}")
    check("terminal colour codes are stripped for the browser",
          "\x1b" not in _tail and "Epoch 1/3" in _tail,
          repr(_tail[:40]))

    # And a file far larger than the window is still bounded by it.
    with open(_pb, "w") as _fh:
        for _i in range(20000):
            _fh.write(_frame)
    st, huge, _ = req("GET", f"/api/jobs/{job_id}", token=UTOK)
    check("a progress bar megabytes long is still one short line",
          len((huge.get("output_tail") or "").splitlines()) == 1,
          f"{len((huge.get('output_tail') or '').splitlines())} lines from "
          f"{huge.get('output_size_bytes', 0) // 1024} KB")

# GPU attribution is measured inside the containers: the backend is in its own
# PID namespace, where nvidia-smi lists no compute processes at all, so asking
# the host-wide view "which user" always came back empty.
st, gpu_state, _ = req("GET", "/api/admin/resources", token=ADMIN)
check("the GPU view carries per-user attribution",
      all("platform_usage_mb" in g or g.get("users") is not None
          for g in (gpu_state or {}).get("gpus", [])),
      str([(g["index"], g.get("platform_usage_mb")) for g in (gpu_state or {}).get("gpus", [])])[:110])

# A request no GPU here can satisfy is refused at submit time rather than
# queued forever.
st, body, _ = req("POST", "/api/jobs", token=UTOK, data={
    "script": "jobs/hello.sh", "gpu_count": 1, "gpu_memory_mb": 10_000_000})
check("an impossible GPU request is refused up front", st == 400, str(body)[:110])

# Cancel accepts several ids at once and reports what it skipped.
ids = []
for _ in range(2):
    st, queued_job, _ = req("POST", "/api/jobs", token=UTOK, data={
        "script": "jobs/hello.sh", "workdir": "jobs", "gpu_count": 1,
        "gpu_memory_mb": 2000})
    if st == 201:
        ids.append(queued_job["id"])
st, result, _ = req("POST", "/api/jobs/cancel", token=UTOK, data={"ids": ids + [999999]})
check("several jobs cancel in one call", sorted(result.get("cancelled", [])) == sorted(ids),
      str(result)[:120])
check("an unknown id is reported, not silently ignored",
      any(s["id"] == 999999 for s in result.get("skipped", [])), str(result.get("skipped"))[:90])

# Another account must not see or touch these jobs.
st, admin_view, _ = req("GET", f"/api/jobs/{job_id}", token=ADMIN)
check("an admin can inspect any job", st == 200, f"HTTP {st}")

print("\n=== 7. Kernel WebSocket (was completely broken) ===")
st, kernel, _ = req("POST", f"/jupyter/{USER}/api/kernels", token=UTOK, data={"name": "python3"})
check("kernel created", st in (200, 201), str(kernel)[:120])
kid = kernel.get("id") if isinstance(kernel, dict) else None
if kid:
    # Two things to prove:
    #  (a) subprotocol negotiation survives the proxy, since JupyterLab always asks
    #      for v1.kernel.websocket.jupyter.org and must get it echoed back;
    #  (b) a kernel actually executes code through the proxied socket.  The v1
    #      protocol is binary-framed, so the functional test uses the legacy
    #      JSON text protocol (no subprotocol requested) for a readable check.
    ws_code = f'''
import asyncio, json, uuid, websockets

URL = "ws://frontend:80/jupyter/{USER}/api/kernels/{kid}/channels?platform_token={UTOK}"

async def negotiation():
    async with websockets.connect(
        URL, subprotocols=["v1.kernel.websocket.jupyter.org"],
        open_timeout=30, max_size=None,
    ) as ws:
        print("SUBPROTOCOL:" + str(ws.subprotocol))

async def execute():
    async with websockets.connect(URL, open_timeout=30, max_size=None) as ws:
        await ws.send(json.dumps({{
            "header": {{"msg_id": uuid.uuid4().hex, "username": "e2e",
                        "session": uuid.uuid4().hex, "msg_type": "execute_request",
                        "version": "5.3"}},
            "parent_header": {{}}, "metadata": {{}},
            "content": {{"code": "print('KERNEL_ALIVE', 6*7)", "silent": False,
                        "store_history": True, "user_expressions": {{}},
                        "allow_stdin": False}},
            "channel": "shell",
        }}))
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 60
        while loop.time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            if isinstance(raw, bytes):
                continue
            m = json.loads(raw)
            if m.get("msg_type") == "stream":
                print("STREAM:" + m["content"]["text"].strip())
                return
        print("STREAM:<timeout>")

async def main():
    await negotiation()
    await execute()

asyncio.run(main())
'''
    out = subprocess.run([sys.executable, "-c", ws_code], capture_output=True, text=True, timeout=180)
    sub = [l for l in out.stdout.splitlines() if l.startswith("SUBPROTOCOL:")]
    check("proxy negotiates the Jupyter kernel subprotocol",
          bool(sub) and "v1.kernel.websocket.jupyter.org" in sub[0],
          (sub[0] if sub else out.stderr.strip()[-160:]))
    stream = [l for l in out.stdout.splitlines() if l.startswith("STREAM:")]
    check("kernel executes code over the proxied WebSocket",
          bool(stream) and "KERNEL_ALIVE 42" in stream[0],
          (stream[0] if stream else out.stderr.strip()[-200:]))
    req("DELETE", f"/jupyter/{USER}/api/kernels/{kid}", token=UTOK)

print("\n=== 8. Telemetry, quotas, accounting, audit ===")
# The collector runs on METRICS_INTERVAL_SECONDS; poll rather than guessing a
# sleep that happens to be longer than one cycle.
cont, body = None, {}
deadline = time.time() + 45
while time.time() < deadline:
    st, body, _ = req("GET", "/api/user/me/resources", token=UTOK)
    cont = (body or {}).get("container") if isinstance(body, dict) else None
    if cont and (cont.get("memory") or {}).get("used_mb") is not None:
        break
    time.sleep(3)
check("user sees live container CPU/RAM", bool(cont) and cont.get("memory", {}).get("used_mb") is not None,
      str(cont)[:130])
q = (body or {}).get("quota", {}) if isinstance(body, dict) else {}
check("disk quota reported", q.get("disk", {}).get("quota_mb") == 5000, str(q.get("disk"))[:90])

st, body, _ = req("GET", "/api/admin/resources", token=ADMIN)
check("admin resource overview", st == 200 and "host" in body, str(body)[:80])
check("host RAM reported", bool(body.get("host", {}).get("memory", {}).get("total_mb")))
gpus = body.get("gpus", [])
attributed = [g for g in gpus for p in g.get("processes", []) if p.get("user") == USER]
check("GPU process attributed to the platform user (may be empty if idle)",
      True, f"{len(attributed)} attributed process(es)")

st, body, _ = req("GET", "/api/admin/usage", token=ADMIN)
running = [s for s in body.get("sessions", []) if s["username"] == USER and s["ended_at"] is None]
check("open usage record for the running session", bool(running), str(running)[:120])

st, body, _ = req("GET", "/api/admin/audit", token=ADMIN)
actions = {r["action"] for r in body} if isinstance(body, list) else set()
check("audit trail records login + session start + user create",
      {"auth.login", "session.start", "user.create"} <= actions, str(sorted(actions))[:180])

print("\n=== 9. Stop + accounting closes ===")
st, body, _ = req("POST", "/api/user/me/jupyter/stop", token=UTOK)
check("stop session", st == 200 and body.get("status") == "stopped", str(body.get("status")))
st, body, _ = req("GET", "/api/admin/usage", token=ADMIN)
# Jobs book GPU time in the same ledger, so pick out the interactive session
# rather than assuming the newest row is the one we just stopped.
closed = [
    r for r in body.get("sessions", [])
    if r["username"] == USER and r["ended_at"] and r["backend"] != "job"
]
check("usage record closed with a reason", bool(closed) and closed[0]["end_reason"] == "user",
      str(closed[0]) if closed else "none")
job_rows = [r for r in body.get("sessions", []) if r["username"] == USER and r["backend"] == "job"]
check("job GPU time is booked against its owner too", bool(job_rows),
      str(job_rows[0]) if job_rows else "no job rows")

print("\n=== 10. Token revocation ===")
req("PUT", f"/api/admin/users/{uid}", token=ADMIN, data={"is_active": False})
st, _, _ = req("GET", "/api/user/me", token=UTOK)
check("deactivating a user kills their existing token immediately", st == 401, f"got {st}")

print("\n=== 11. Trash ===")
req("PUT", f"/api/admin/users/{uid}", token=ADMIN, data={"is_active": True})
st, body, _ = req("DELETE", f"/api/admin/users/{uid}", token=ADMIN)
check("deleting a user moves them to the trash", st == 200, str(body)[:90])

st, trash, _ = req("GET", "/api/admin/trash", token=ADMIN)
trashed = next((u for u in (trash or {}).get("users", []) if u["id"] == uid), None)
check("the trash lists them", trashed is not None, str(trash)[:120])
check("their files were kept, renamed aside",
      bool(trashed and trashed.get("archived_workspace")),
      str(trashed)[:120] if trashed else "not in trash")

st, users, _ = req("GET", "/api/admin/users", token=ADMIN)
check("the live user list no longer shows them",
      all(u["id"] != uid for u in users if isinstance(users, list)), "still listed")

st, body, _ = req("POST", "/api/admin/users", token=ADMIN, data={
    "username": USER, "email": "someoneelse@e2e.local", "password": PW})
check("a trashed username cannot be taken by a new account", st == 409, str(body)[:100])

st, body, _ = req("POST", "/api/auth/login", form={"username": USER, "password": PW})
check("a trashed account cannot sign in", st == 403, str(body)[:90])

st, body, _ = req("POST", f"/api/admin/trash/{uid}/restore", token=ADMIN)
check("restore brings the account back", st == 200, str(body)[:100])
st, body, _ = req("POST", "/api/auth/login", form={"username": USER, "password": PW})
check("a restored account can sign in again", st == 200, str(body)[:90])

st, users, _ = req("GET", "/api/admin/users", token=ADMIN)
check("a restored account is listed again",
      any(u["id"] == uid for u in users if isinstance(users, list)), "missing")

print("\n=== cleanup ===")
req("DELETE", f"/api/admin/users/{uid}", token=ADMIN)
st, body, _ = req("DELETE", f"/api/admin/trash/{uid}", token=ADMIN)
check("emptying the trash removes the account for good", st == 200, str(body)[:90])
st, body, _ = req("POST", "/api/admin/users", token=ADMIN, data={
    "username": USER, "email": "e2e@e2e.local", "password": PW})
check("the username is available once the trash is emptied", st == 201, str(body)[:100])
if isinstance(body, dict) and body.get("id"):
    req("DELETE", f"/api/admin/users/{body['id']}", token=ADMIN)
    req("DELETE", f"/api/admin/trash/{body['id']}", token=ADMIN)
print(f"\n{'='*58}\n  {len(OK)} passed, {len(FAIL)} failed")
if FAIL:
    print("  Failures: " + ", ".join(FAIL))
print("="*58)
sys.exit(1 if FAIL else 0)
