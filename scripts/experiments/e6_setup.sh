#!/bin/bash
# E6: what it takes to set up the platform, or Slurm with JupyterHub, on a
# fresh machine, step by step, up to a first notebook and a first batch job.
#
#   sudo bash e6_setup.sh platform --src gpu-platform-src.tar.gz
#   sudo bash e6_setup.sh platform --repo https://github.com/vudinhhong/gpu-platform.git --ref v1.3.0
#   sudo bash e6_setup.sh slurm
#
# Run each on its own fresh Ubuntu 22.04 or 24.04 machine (a VM restored from
# the same snapshot is best): the script installs system packages, starts
# services and creates a local account "e6user", and the second stack would
# find the first one's downloads.  A GPU with its driver already installed is
# used if present; without one both stacks are set up CPU-only.
#
# Each step is timed and the bytes received over the network are counted.
# The configuration an administrator has to write is counted too: for Slurm
# with JupyterHub that is every file below that the script writes on the
# administrator's behalf; the platform generates its own.
#
# Output, in the current directory: e6_<stack>_<host>_<time>.json and a
# .tar.gz of the logs.  Bring both back.

set -uo pipefail

STACK="${1:-}"
shift || true
SRC=""; REPO=""; REF=""; YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --src)  SRC="$(realpath "$2")"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        --ref)  REF="$2"; shift 2 ;;
        --yes)  YES=1; shift ;;
        *) echo "unknown argument $1" >&2; exit 2 ;;
    esac
done
case "$STACK" in platform|slurm) ;; *)
    sed -n 2,23p "$0"; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 2; }
if [ "$STACK" = platform ] && [ -z "$SRC" ] && [ -z "$REPO" ]; then
    echo "platform needs --src TARBALL or --repo URL [--ref REF]" >&2; exit 2
fi
if [ "$YES" != 1 ]; then
    echo "This installs packages and services on THIS machine; use a scratch VM."
    read -r -p "Type yes to go on: " answer
    [ "$answer" = yes ] || exit 1
fi

. /etc/os-release
HOST="$(hostname -s)"
STAMP="$(date +%Y%m%dT%H%M%S)"
OUT="$PWD/e6_${STACK}_${HOST}_${STAMP}"
mkdir -p "$OUT/logs"
STEPS="$OUT/steps.tsv"
: > "$STEPS"
CONFIG="$OUT/config.tsv"          # file, lines an administrator writes
: > "$CONFIG"
export DEBIAN_FRONTEND=noninteractive
T_START=$(date +%s.%N)

GPUS=0
if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
    GPUS=$(nvidia-smi -L | wc -l)
fi

rx_bytes() {
    awk -F'[: ]+' 'NR>2 && $2 != "lo" {s += $3} END {print s+0}' /proc/net/dev
}

# step NAME KIND COMMAND...   KIND: prerequisite | install | configure | start | verify
step() {
    local name="$1" kind="$2"; shift 2
    local log="$OUT/logs/$(printf '%02d' "$(wc -l < "$STEPS")")_${name}.log"
    local t0 rx0 rc t1 rx1
    echo ">>> $name"
    t0=$(date +%s.%N); rx0=$(rx_bytes)
    ( set -e; "$@" ) > "$log" 2>&1
    rc=$?
    t1=$(date +%s.%N); rx1=$(rx_bytes)
    printf '%s\t%s\t%s\t%d\t%d\n' "$name" "$kind" "$(awk "BEGIN {printf \"%.1f\", $t1 - $t0}")" "$rc" \
        "$(( (rx1 - rx0) / 1048576 ))" >> "$STEPS"
    if [ $rc -ne 0 ]; then
        echo "    failed (rc $rc), see $log"; tail -20 "$log"
        declare -F "collect_${STACK}_logs" >/dev/null && "collect_${STACK}_logs"
        finish "failed at $name"
        exit 1
    fi
}

# authored PATH: count the lines an administrator would have written
authored() {
    printf '%s\t%d\n' "$1" "$(grep -cvE '^\s*(#|$)' "$1")" >> "$CONFIG"
}

finish() {
    local outcome="$1"
    python3 - "$OUT" "$STACK" "$outcome" "$GPUS" "$T_START" <<'PY'
import json, os, platform, subprocess, sys, time
out, stack, outcome, gpus, t_start = sys.argv[1:]

def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except Exception:
        return ""

steps = []
for line in open(os.path.join(out, "steps.tsv")):
    name, kind, sec, rc, mib = line.rstrip("\n").split("\t")
    steps.append({"step": name, "kind": kind, "seconds": float(sec),
                  "rc": int(rc), "downloaded_mib": int(mib)})
config = []
for line in open(os.path.join(out, "config.tsv")):
    path, n = line.rstrip("\n").split("\t")
    config.append({"file": path, "lines": int(n)})
extra = {}
p = os.path.join(out, "extra.json")
if os.path.exists(p):
    extra = json.load(open(p))
mem = int(open("/proc/meminfo").read().split()[1]) // 1024
result = {
    "stack": stack, "outcome": outcome,
    "machine": {
        "os": sh(". /etc/os-release; echo $PRETTY_NAME"), "kernel": platform.release(),
        "cpu": sh("lscpu | sed -n 's/^Model name: *//p'"), "cores": os.cpu_count(),
        "memory_mib": mem, "gpus": int(gpus),
        "gpu_names": sh("nvidia-smi --query-gpu=name --format=csv,noheader"),
        "driver": sh("nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1"),
        "cgroup": sh("stat -fc %T /sys/fs/cgroup"),
    },
    "steps": steps,
    "total_seconds": round(time.time() - float(t_start), 1),
    "steps_by_kind": {k: sum(1 for s in steps if s["kind"] == k)
                      for k in sorted({s["kind"] for s in steps})},
    "downloaded_mib": sum(s["downloaded_mib"] for s in steps),
    "config_written_by_hand": config,
    "config_lines": sum(c["lines"] for c in config),
    **extra,
}
json.dump(result, open(out + ".json", "w"), indent=1)
print(json.dumps({k: result[k] for k in ("stack", "outcome", "total_seconds",
                                         "downloaded_mib", "config_lines")}))
PY
    tar -czf "$OUT.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")"
    echo "Results: $OUT.json and $OUT.tar.gz"
}

extra() {   # extra KEY JSONVALUE: add a field to the result
    python3 - "$OUT/extra.json" "$1" "$2" <<'PY'
import json, os, sys
p, k, v = sys.argv[1:]
d = json.load(open(p)) if os.path.exists(p) else {}
d[k] = json.loads(v)
json.dump(d, open(p, "w"))
PY
}

wait_for() {    # wait_for SECONDS COMMAND...
    local limit="$1"; shift
    local t=0
    until "$@"; do
        t=$((t + 3)); [ $t -ge "$limit" ] && return 1; sleep 3
    done
}

apt_base() {
    apt-get update
    apt-get install -y --no-install-recommends curl ca-certificates python3 gnupg
}

# ════════════════════════════════════════════════════════════════════════
# The platform
# ════════════════════════════════════════════════════════════════════════

install_docker() {
    if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
        echo "already installed: $(docker --version)"; return 0
    fi
    curl -fsSL https://get.docker.com | sh
}

install_nvidia_toolkit() {
    [ "$GPUS" -gt 0 ] || { echo "no GPU, skipped"; return 0; }
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update && apt-get install -y nvidia-container-toolkit
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker
}

PLAT=/opt/e6/gpu-platform
get_platform() {
    rm -rf "$PLAT"; mkdir -p "$PLAT"
    if [ -n "$SRC" ]; then
        tar -xzf "$SRC" -C "$PLAT" --strip-components=1
    else
        apt-get install -y --no-install-recommends git
        git clone "$REPO" "$PLAT"
        [ -z "$REF" ] || git -C "$PLAT" checkout "$REF"
    fi
}

deploy_platform() {
    cd "$PLAT"
    if [ "$GPUS" -gt 0 ]; then
        HTTP_MODE=host ./deploy.sh
    else
        HTTP_MODE=host FORCE_CPU_ONLY=1 ./deploy.sh
    fi
}

# Everything a new administrator does next, through the same API the web
# interface calls: sign in, create a user, open their notebook, run a job.
verify_platform() {
    cd "$PLAT"
    local port bind
    port=$(sed -n 's/^WEB_PORT=//p' .env | tail -1); bind=$(sed -n 's/^WEB_BIND=//p' .env | tail -1)
    BASE="http://${bind:-127.0.0.1}:${port:-7080}" GPUS="$GPUS" \
    ADMIN_PASSWORD="$(sed -n 's/^ADMIN_PASSWORD=//p' .env | tail -1)" \
    python3 - "$OUT/extra.json" <<'PY'
import json, os, sys, time, urllib.parse, urllib.request
BASE, GPUS = os.environ["BASE"], int(os.environ["GPUS"])

def call(method, path, token=None, body=None, form=None, ok=(200, 201)):
    data, headers = None, {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    if code not in ok:
        raise SystemExit(f"{method} {path}: {code} {raw[:300]!r}")
    return json.loads(raw) if raw and raw[:1] in b"[{" else raw

marks = {}
t0 = time.time()
for _ in range(100):
    try:
        call("GET", "/api/health")
        break
    except Exception:
        time.sleep(3)
admin = call("POST", "/api/auth/login",
             form={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]})["access_token"]
pw = "E6-first-user-1"
user = call("POST", "/api/admin/users", admin, {
    "username": "e6user", "email": "e6user@example.org", "password": pw,
    "full_name": "E6 user"})
call("POST", "/api/admin/gpu/assignments", admin,
     {"user_id": user["id"], "gpu_indices": [], "memory_limit_mb": 4096, "cpu_cores": 2})
tok = call("POST", "/api/auth/login", form={"username": "e6user", "password": pw})["access_token"]
marks["user_ready_s"] = round(time.time() - t0, 1)

t1 = time.time()
call("POST", "/api/user/me/jupyter/start", tok, {})
for _ in range(200):
    try:
        call("GET", "/jupyter/e6user/api/status", tok)
        break
    except BaseException:
        time.sleep(3)
else:
    raise SystemExit("JupyterLab never answered")
marks["notebook_ready_s"] = round(time.time() - t1, 1)

# The script is saved through JupyterLab, as a user would.
script = "nvidia-smi -L\nnproc\n" if GPUS else "nproc\nfree -m\n"
call("PUT", "/jupyter/e6user/api/contents/hello.sh", tok,
     {"type": "file", "format": "text", "content": script})
t2 = time.time()
body = {"script": "hello.sh", "name": "hello"}
body.update({"gpu_memory_mb": 2000} if GPUS else {"gpu_count": 0})
job = call("POST", "/api/jobs", tok, body)
for _ in range(400):
    j = call("GET", f"/api/jobs/{job['id']}", tok)
    if j["status"] in ("succeeded", "failed", "cancelled", "timeout"):
        break
    time.sleep(3)
marks["first_job_s"] = round(time.time() - t2, 1)
marks["first_job_status"] = j["status"]
if j["status"] != "succeeded":
    raise SystemExit(f"job ended {j['status']}: {j.get('message')}")
d = json.load(open(sys.argv[1])) if os.path.exists(sys.argv[1]) else {}
d["checks"] = marks
json.dump(d, open(sys.argv[1], "w"))
print(marks)
PY
}

platform_versions() {
    extra versions "$(python3 -c "import json,subprocess as s
v=lambda c: s.run(c,shell=True,capture_output=True,text=True).stdout.strip()
print(json.dumps({'docker': v('docker --version'), 'compose': v('docker compose version --short'),
 'source': '${SRC:+tarball}${REPO:+$REPO@${REF:-default}}'}))")"
}

run_platform() {
    step apt-base prerequisite apt_base
    step docker prerequisite install_docker
    step nvidia-container-toolkit prerequisite install_nvidia_toolkit
    step get-source install get_platform
    step deploy install deploy_platform
    step first-user-notebook-job verify verify_platform
    platform_versions
}

# ════════════════════════════════════════════════════════════════════════
# Slurm with JupyterHub (batchspawner)
# ════════════════════════════════════════════════════════════════════════

DBPASS="$(head -c 12 /dev/urandom | base64 | tr -dc A-Za-z0-9)"
HUB=/opt/e6/jupyterhub
HUBTOKEN="$(head -c 24 /dev/urandom | base64 | tr -dc A-Za-z0-9)"

install_slurm_packages() {
    apt-get install -y slurm-wlm slurmdbd mariadb-server munge
}

configure_database() {
    systemctl enable --now mariadb
    cat > "$OUT/slurm_db.sql" <<EOF
CREATE DATABASE IF NOT EXISTS slurm_acct_db;
CREATE USER IF NOT EXISTS 'slurm'@'localhost' IDENTIFIED BY '${DBPASS}';
GRANT ALL ON slurm_acct_db.* TO 'slurm'@'localhost';
EOF
    mysql < "$OUT/slurm_db.sql"
    authored "$OUT/slurm_db.sql"
    sed -i "s/${DBPASS}/<password>/" "$OUT/slurm_db.sql"
}

configure_slurm() {
    mkdir -p /etc/slurm /var/spool/slurmctld /var/spool/slurmd /var/log/slurm
    chown slurm:slurm /var/spool/slurmctld /var/log/slurm
    [ -s /etc/munge/munge.key ] || mungekey -c -f
    chown munge:munge /etc/munge/munge.key
    systemctl enable --now munge

    local node mem
    node=$(slurmd -C | head -1)
    # Leave the system some memory, or the node drains as soon as it is short.
    mem=$(echo "$node" | sed -n 's/.*RealMemory=\([0-9]*\).*/\1/p')
    node=$(echo "$node" | sed "s/RealMemory=[0-9]*/RealMemory=$((mem * 95 / 100))/")
    local gres="" gresline=""
    if [ "$GPUS" -gt 0 ]; then gres="GresTypes=gpu"; gresline=" Gres=gpu:$GPUS"; fi
    cat > /etc/slurm/slurm.conf <<EOF
ClusterName=e6
SlurmctldHost=$(hostname -s)
AuthType=auth/munge
SlurmUser=slurm
ProctrackType=proctrack/cgroup
TaskPlugin=task/cgroup,task/affinity
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory
$gres
StateSaveLocation=/var/spool/slurmctld
SlurmdSpoolDir=/var/spool/slurmd
SlurmctldLogFile=/var/log/slurm/slurmctld.log
SlurmdLogFile=/var/log/slurm/slurmd.log
AccountingStorageType=accounting_storage/slurmdbd
AccountingStorageHost=localhost
AccountingStorageEnforce=associations,limits
JobAcctGatherType=jobacct_gather/cgroup
PriorityType=priority/multifactor
PriorityWeightFairshare=10000
PriorityDecayHalfLife=7-0
DefMemPerCPU=1024
${node}${gresline} State=UNKNOWN
PartitionName=main Nodes=ALL Default=YES MaxTime=INFINITE State=UP
EOF
    authored /etc/slurm/slurm.conf
    cat > /etc/slurm/cgroup.conf <<EOF
CgroupPlugin=autodetect
ConstrainCores=yes
ConstrainRAMSpace=yes
ConstrainDevices=yes
EOF
    authored /etc/slurm/cgroup.conf
    if [ "$GPUS" -gt 0 ]; then
        : > /etc/slurm/gres.conf
        for i in $(seq 0 $((GPUS - 1))); do
            echo "Name=gpu File=/dev/nvidia$i" >> /etc/slurm/gres.conf
        done
        authored /etc/slurm/gres.conf
    fi
    cat > /etc/slurm/slurmdbd.conf <<EOF
AuthType=auth/munge
DbdHost=localhost
SlurmUser=slurm
StorageType=accounting_storage/mysql
StorageHost=localhost
StorageUser=slurm
StoragePass=${DBPASS}
StorageLoc=slurm_acct_db
LogFile=/var/log/slurm/slurmdbd.log
EOF
    chown slurm:slurm /etc/slurm/slurmdbd.conf
    chmod 600 /etc/slurm/slurmdbd.conf
    authored /etc/slurm/slurmdbd.conf
}

start_slurm() {
    systemctl enable --now slurmdbd
    wait_for 120 sacctmgr -n list cluster
    sacctmgr -i add cluster e6 || true
    sacctmgr -i add account research cluster=e6 || true
    id e6user >/dev/null 2>&1 || useradd -m -s /bin/bash e6user
    echo "e6user:E6-first-user-1" | chpasswd
    sacctmgr -i add user e6user account=research || true
    systemctl enable --now slurmctld
    systemctl enable --now slurmd
    # A node that registers before slurmctld has its configuration comes up
    # down; the usual first-day fix.
    sleep 10
    scontrol update nodename="$(hostname -s)" state=resume 2>/dev/null || true
    wait_for 120 sh -c "sinfo -h -o %t | grep -qE '^(idle|mix)'"
}

install_jupyterhub() {
    apt-get install -y --no-install-recommends python3-venv nodejs npm
    npm install -g configurable-http-proxy
    python3 -m venv "$HUB"
    "$HUB/bin/pip" install --upgrade pip
    "$HUB/bin/pip" install jupyterhub jupyterlab batchspawner
}

configure_jupyterhub() {
    mkdir -p /etc/jupyterhub
    cat > /etc/jupyterhub/jupyterhub_config.py <<EOF
c.JupyterHub.bind_url = 'http://0.0.0.0:8000'
c.JupyterHub.hub_ip = '127.0.0.1'
c.JupyterHub.db_url = 'sqlite:////var/lib/jupyterhub/jupyterhub.sqlite'
c.JupyterHub.cookie_secret_file = '/var/lib/jupyterhub/cookie_secret'
c.JupyterHub.authenticator_class = 'pam'
c.Authenticator.allowed_users = {'e6user'}
c.Authenticator.admin_users = {'root'}
c.JupyterHub.spawner_class = 'batchspawner.SlurmSpawner'
c.Spawner.default_url = '/lab'
c.Spawner.http_timeout = 120
c.SlurmSpawner.start_timeout = 300
c.SlurmSpawner.req_prologue = 'export PATH=${HUB}/bin:\$PATH'
# srun looks the command up on the compute side, where the prologue's PATH
# does not reach.
c.SlurmSpawner.batchspawner_singleuser_cmd = '${HUB}/bin/batchspawner-singleuser'
c.Spawner.cmd = ['${HUB}/bin/jupyterhub-singleuser']
c.SlurmSpawner.req_partition = 'main'
c.SlurmSpawner.req_memory = '4G'
c.SlurmSpawner.req_nprocs = '2'
c.SlurmSpawner.req_runtime = '8:00:00'
c.JupyterHub.services = [{'name': 'e6-check', 'api_token': open('/etc/jupyterhub/e6_token').read().strip()}]
c.JupyterHub.load_roles = [{'name': 'e6-check', 'services': ['e6-check'],
                            'scopes': ['admin:users', 'admin:servers', 'access:servers']}]
EOF
    printf '%s\n' "$HUBTOKEN" > /etc/jupyterhub/e6_token
    chmod 600 /etc/jupyterhub/e6_token
    authored /etc/jupyterhub/jupyterhub_config.py
    mkdir -p /var/lib/jupyterhub
    cat > /etc/systemd/system/jupyterhub.service <<EOF
[Unit]
Description=JupyterHub
After=network.target slurmctld.service

[Service]
User=root
Environment=PATH=${HUB}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WorkingDirectory=/var/lib/jupyterhub
ExecStart=${HUB}/bin/jupyterhub -f /etc/jupyterhub/jupyterhub_config.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
    authored /etc/systemd/system/jupyterhub.service
    systemctl daemon-reload
    systemctl enable --now jupyterhub
    # Role and service names need three characters or more.
    wait_for 120 curl -sf http://127.0.0.1:8000/hub/api \
        || { journalctl -u jupyterhub --no-pager | tail -40; return 1; }
}

verify_slurm() {
    HUBTOKEN="$HUBTOKEN" GPUS="$GPUS" python3 - "$OUT/extra.json" <<'PY'
import json, os, subprocess, sys, time, urllib.request
TOKEN, GPUS = os.environ["HUBTOKEN"], int(os.environ["GPUS"])
BASE = "http://127.0.0.1:8000"

def call(method, path, body=None, ok=(200, 201, 202)):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "token " + TOKEN,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    if code not in ok:
        raise SystemExit(f"{method} {path}: {code} {raw[:300]!r}")
    return json.loads(raw) if raw and raw[:1] in b"[{" else raw

marks = {}
t0 = time.time()
call("POST", "/hub/api/users/e6user", ok=(201, 409))
marks["user_ready_s"] = round(time.time() - t0, 1)
t1 = time.time()
call("POST", "/hub/api/users/e6user/server")
for _ in range(200):
    u = call("GET", "/hub/api/users/e6user")
    if (u.get("servers") or {}).get("", {}).get("ready"):
        break
    time.sleep(3)
else:
    raise SystemExit("notebook server never became ready")
call("GET", "/user/e6user/api/status")
marks["notebook_ready_s"] = round(time.time() - t1, 1)
# The notebook holds cores and memory but no GPU, as the platform's workspace
# in this test does; the batch job asks for the GPU.
script = "nvidia-smi -L; nproc" if GPUS else "nproc; free -m"
gres = "--gres=gpu:1 " if GPUS else ""
t2 = time.time()
jid = subprocess.run(["sudo", "-u", "e6user", "-i", "sh", "-c",
                      f"sbatch --parsable {gres}--mem=2G -c1 -o hello.%j.out --wrap='{script}'"],
                     capture_output=True, text=True, check=True).stdout.strip()
state = ""
for _ in range(400):
    state = subprocess.run(["sacct", "-X", "-n", "-P", "-j", jid, "-o", "State"],
                           capture_output=True, text=True).stdout.strip()
    if state and state.split()[0] not in ("PENDING", "RUNNING", "COMPLETING", "CONFIGURING"):
        break
    time.sleep(3)
marks["first_job_s"] = round(time.time() - t2, 1)
marks["first_job_status"] = state
if not state.startswith("COMPLETED"):
    pend = subprocess.run(["squeue", "-j", jid, "-h", "-o", "%r"], capture_output=True, text=True).stdout
    raise SystemExit(f"job {jid} ended {state!r} {pend}")
d = json.load(open(sys.argv[1])) if os.path.exists(sys.argv[1]) else {}
d["checks"] = marks
json.dump(d, open(sys.argv[1], "w"))
print(marks)
PY
}

slurm_versions() {
    extra versions "$(python3 -c "import json,subprocess as s
v=lambda c: s.run(c,shell=True,capture_output=True,text=True).stdout.strip()
print(json.dumps({'slurm': v('sinfo -V'), 'mariadb': v('mysql -V'),
 'jupyterhub': v('$HUB/bin/jupyterhub --version'),
 'batchspawner': v('$HUB/bin/pip show batchspawner | sed -n s/^Version:.//p'),
 'node': v('node --version')}))")"
}

collect_slurm_logs() {
    cp -r /var/log/slurm "$OUT/logs/slurm" 2>/dev/null || true
    journalctl -u jupyterhub --no-pager > "$OUT/logs/jupyterhub.journal" 2>/dev/null || true
}

run_slurm() {
    step apt-base prerequisite apt_base
    step slurm-packages install install_slurm_packages
    step database configure configure_database
    step slurm-config configure configure_slurm
    step slurm-start start start_slurm
    step jupyterhub-packages install install_jupyterhub
    step jupyterhub-config configure configure_jupyterhub
    step first-user-notebook-job verify verify_slurm
    slurm_versions
    collect_slurm_logs
}

# ════════════════════════════════════════════════════════════════════════

"run_$STACK"
finish ok
