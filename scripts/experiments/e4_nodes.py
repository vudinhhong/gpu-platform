#!/usr/bin/env python3
"""E4: the platform across nodes, on the two emulated nodes of the dev stack.

The nodes are containers on the same host (docker-compose.emu.yml), each the
backend in ROLE=node with one of the host's GPUs.  What that tests is the
platform's own code across the node boundary: the join, the control channel,
placement, the data port and recovery.  It does not test a real network or a
second kernel, so the network is degraded on purpose with netem.

  placement   the master offers no GPU of its own (JOB_GPU_POOL=-1).  Eight
              jobs of 10 GiB from one account: which node each lands on, how
              long it waited, and whether the GPU it saw is that node's.  Then
              emu1 is drained and three more go only to emu2.
  latency     a workspace on emu1 against one on the master, both reached
              through the master's proxy as a browser would, with netem adding
              DELAY ms to emu1's packets towards the master: HTTP request
              time, kernel round trip, and the time to start the workspace.
  failure     a 120 s job and a workspace on emu1; 30 s in, emu1's agent is
              stopped for DOWN seconds and started again.  Recorded once a
              second from inside the master: whether the node is ready, the
              job's state, and what the proxy returns for the workspace.

    EXP_OUT=out python3 scripts/experiments/e4_nodes.py placement
    EXP_OUT=out python3 scripts/experiments/e4_nodes.py latency --delays 0,1,5,20,50
    EXP_OUT=out python3 scripts/experiments/e4_nodes.py failure --down 15,75,150 --reps 3
"""

import argparse
import json
import os
import statistics as st
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e1_vram as e1  # noqa: E402  (first: points the API client at the dev stack)
import platform_api as api  # noqa: E402

PASSWORD = api.experiment_password()
SUBMITTER = "expn1"     # workspace on the master, no GPU; submits the jobs
REMOTE = "expn2"        # workspace on emu1
LOCAL = "expn3"         # workspace on the master, the baseline
MASTER = "gpu-platform-dev-backend-1"
NODE_CONTAINER = {"emu1": "gpu-platform-dev-node1-1", "emu2": "gpu-platform-dev-node2-1"}
NETEM_IMAGE = "gpu-exp-netem"
EMU = ["bash", "scripts/experiments/emu_nodes.sh"]


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def emu(action, *services):
    subprocess.run(EMU + [action, *services], cwd=e1.ROOT, check=True, capture_output=True)


def nodes(admin):
    _, rows = api._request("GET", "/api/admin/nodes", admin)
    return {n["name"]: n for n in rows}


def wait_ready(admin, names, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ns = nodes(admin)
        if all(ns.get(n, {}).get("ready") for n in names):
            return ns
        time.sleep(2)
    raise SystemExit(f"nodes {names} did not become ready")


def master(**env):
    """Restart the dev master with *env* on top of its settings."""
    e1.restart_backend(dict(os.environ, **env))


def gpu_uuid(index):
    return subprocess.run(["nvidia-smi", "-i", str(index), "--query-gpu=uuid",
                           "--format=csv,noheader"], capture_output=True, text=True,
                          check=True).stdout.strip()


def submit(user, script, mib, name):
    path = f"e4/{name}.sh"
    api.sh(f"{e1.PREFIX}jupyter-{user}", user,
           f"mkdir -p ~/e4 && cat > ~/{path} <<'SH'\n{script}\nSH")
    res = api.sh(f"{e1.PREFIX}jupyter-{user}", user,
                 f"cd ~ && submit {path} --gpu-memory {mib} --name {name}")
    return int(res.stdout.split("job ")[1].split()[0])


def job(token, jid, output=False):
    _, j = api._request("GET", f"/api/jobs/{jid}?output={'true' if output else 'false'}", token)
    return j


def wait_done(token, jid, timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = job(token, jid)
        if j["status"] in ("succeeded", "failed", "cancelled", "timeout"):
            return job(token, jid, output=True)
        time.sleep(2)
    raise SystemExit(f"job {jid} did not finish")


def node_id(admin, name):
    return nodes(admin)[name]["id"]


# --------------------------------------------------------------------------
# placement

def placement(admin, args, out):
    emu("up")
    ns = wait_ready(admin, ["emu1", "emu2"])
    master(JOB_GPU_POOL="-1", JOB_MAX_RUNNING_PER_USER="12")
    tok = api.login(SUBMITTER, PASSWORD)
    uuids = {"emu1": gpu_uuid(0), "emu2": gpu_uuid(1)}
    by_id = {n["id"]: name for name, n in ns.items()}
    script = ("nvidia-smi --query-gpu=uuid --format=csv,noheader\n"
              f"sleep {args.seconds}")

    def batch(label, count):
        ids = [submit(SUBMITTER, script, args.mib, f"e4p-{label}-{i}") for i in range(count)]
        rows = []
        for jid in ids:
            j = wait_done(tok, jid)
            seen = (j.get("output_tail") or "").strip().splitlines()
            node = by_id.get(j["node_id"], "master")
            rows.append({"job": jid, "node": node, "gpus": j["gpus"], "status": j["status"],
                         "waited_s": j["waited_seconds"], "runtime_s": j.get("runtime_seconds"),
                         "saw": seen[0] if seen else None,
                         "right_gpu": bool(seen) and seen[0] == uuids.get(node)})
        return rows

    first = batch("both", args.jobs)
    api._request("POST", f"/api/admin/nodes/{ns['emu1']['id']}/drain", admin)
    drained = batch("drained", 3)
    api._request("POST", f"/api/admin/nodes/{ns['emu1']['id']}/resume", admin)
    res = {"measured": now(), "request_mib": args.mib, "job_seconds": args.seconds,
           "free_mib": {"emu1": e1.free_mib(0), "emu2": e1.free_mib(1)},
           "both_nodes": first, "emu1_drained": drained}
    (out / "e4_placement.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


# --------------------------------------------------------------------------
# latency

# Runs inside the master's backend container: the platform's own address,
# reached the way the web front end reaches it.
_CLIENT = r'''
import asyncio, json, statistics, sys, time, uuid
import httpx, websockets
s = json.load(sys.stdin)
BASE = "http://127.0.0.1:8000"

async def main():
    out = {}
    async with httpx.AsyncClient(timeout=60) as c:
        kernels = {}
        for name, t in s["targets"].items():
            h = {"Authorization": "Bearer " + t["token"]}
            for _ in range(60):
                try:
                    r = await c.get(BASE + t["base"] + "api/status", headers=h)
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2)
            r = await c.post(BASE + t["base"] + "api/kernels", headers=h, json={"name": "python3"})
            r.raise_for_status()
            kernels[name] = r.json()["id"]
        http = {n: [] for n in s["targets"]}
        for _ in range(s["n"]):
            for name, t in s["targets"].items():
                t0 = time.perf_counter()
                r = await c.get(BASE + t["base"] + "api/status",
                                headers={"Authorization": "Bearer " + t["token"]})
                assert r.status_code == 200, (name, r.status_code)
                http[name].append((time.perf_counter() - t0) * 1000)
        conns = {}
        for name, t in s["targets"].items():
            url = (BASE + t["base"]).replace("http://", "ws://") + f"api/kernels/{kernels[name]}/channels"
            conns[name] = await websockets.connect(
                url, extra_headers={"Authorization": "Bearer " + t["token"]}, max_size=None)
        ws = {n: [] for n in s["targets"]}
        session = uuid.uuid4().hex
        for i in range(s["n"] + 5):
            for name, conn in conns.items():
                mid = uuid.uuid4().hex
                msg = {"header": {"msg_id": mid, "msg_type": "execute_request", "username": "e4",
                                  "session": session, "version": "5.3", "date": ""},
                       "parent_header": {}, "metadata": {}, "channel": "shell", "buffers": [],
                       "content": {"code": "1+1", "silent": False, "store_history": False,
                                   "user_expressions": {}, "allow_stdin": False}}
                t0 = time.perf_counter()
                await conn.send(json.dumps(msg))
                while True:
                    m = json.loads(await conn.recv())
                    if m.get("msg_type") == "execute_reply" and m.get("parent_header", {}).get("msg_id") == mid:
                        break
                if i >= 5:
                    ws[name].append((time.perf_counter() - t0) * 1000)
        for conn in conns.values():
            await conn.close()
        for name, t in s["targets"].items():
            await c.delete(BASE + t["base"] + f"api/kernels/{kernels[name]}",
                           headers={"Authorization": "Bearer " + t["token"]})
    print(json.dumps({"http_ms": http, "ws_rtt_ms": ws}))

asyncio.run(main())
'''


def in_master(script, spec, timeout=1800):
    res = subprocess.run(["docker", "exec", "-i", MASTER, "python", "-c", script],
                         input=json.dumps(spec), capture_output=True, text=True,
                         timeout=timeout)
    if res.returncode != 0:
        raise SystemExit(f"client in the master failed: {res.stderr[-2000:]}")
    return json.loads(res.stdout.strip().splitlines()[-1])


def container_ip(name):
    return subprocess.run(["docker", "inspect", "-f",
                           "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
                          capture_output=True, text=True, check=True).stdout.strip()


def netem_image():
    if subprocess.run(["docker", "image", "inspect", NETEM_IMAGE], capture_output=True).returncode:
        subprocess.run(["docker", "build", "-q", "-t", NETEM_IMAGE, "-"], check=True,
                       capture_output=True, text=True,
                       input="FROM alpine:latest\nRUN apk add --no-cache iproute2 iproute2-tc\n")


def set_delay(node, ms):
    """Delay what *node* sends to the master by *ms*; 0 removes the delay.

    Only packets to the master's address are delayed, so the node's own
    traffic to its containers is not: the added round trip is *ms*.
    """
    cmd = "tc qdisc del dev eth0 root 2>/dev/null; true"
    if ms:
        cmd += (f"; tc qdisc add dev eth0 root handle 1: prio"
                f" && tc qdisc add dev eth0 parent 1:3 handle 30: netem delay {ms}ms"
                f" && tc filter add dev eth0 protocol ip parent 1:0 prio 1 u32"
                f" match ip dst {container_ip(MASTER)}/32 flowid 1:3")
    subprocess.run(["docker", "run", "--rm", "--net", f"container:{NODE_CONTAINER[node]}",
                    "--cap-add", "NET_ADMIN", "--label", "exp.runner=e4", NETEM_IMAGE,
                    "sh", "-c", cmd], check=True, capture_output=True)


def ping_ms(node):
    """Round trip from the master to *node*, as a TCP connect to its data port."""
    spec = {"host": NODE_CONTAINER[node]}
    script = r'''
import json, socket, statistics, sys, time
s = json.load(sys.stdin); xs = []
for _ in range(20):
    t0 = time.perf_counter(); c = socket.create_connection((s["host"], 8000), timeout=10)
    xs.append((time.perf_counter() - t0) * 1000); c.close(); time.sleep(0.05)
print(json.dumps(statistics.median(xs)))
'''
    return in_master(script, spec, timeout=120)


def summary(xs):
    xs = sorted(xs)
    return {"n": len(xs), "median": round(st.median(xs), 3),
            "p95": round(xs[int(0.95 * (len(xs) - 1))], 3), "mean": round(st.mean(xs), 3)}


def latency(admin, args, out):
    netem_image()
    emu("up", "node1")
    wait_ready(admin, ["emu1"])
    api.ensure_user(admin, REMOTE, PASSWORD, gpu_indices=args.remote_gpus,
                    node_id=node_id(admin, "emu1"), memory_limit_mb=4096, cpu_cores=2)
    toks = {REMOTE: api.login(REMOTE, PASSWORD), LOCAL: api.login(LOCAL, PASSWORD)}
    rows = []
    try:
        for d in [int(x) for x in args.delays.split(",")]:
            set_delay("emu1", d)
            starts = {REMOTE: [], LOCAL: []}
            for _ in range(args.start_reps):
                for user in (REMOTE, LOCAL):
                    api.stop_workspace(toks[user])
                    time.sleep(3)
                    t0 = time.perf_counter()
                    api.start_workspace(toks[user], timeout=600)
                    starts[user].append(round(time.perf_counter() - t0, 3))
            spec = {"n": args.n, "targets": {
                "remote": {"token": toks[REMOTE], "base": f"/jupyter/{REMOTE}/"},
                "local": {"token": toks[LOCAL], "base": f"/jupyter/{LOCAL}/"}}}
            r = in_master(_CLIENT, spec)
            row = {"delay_ms": d, "connect_ms": round(ping_ms("emu1"), 3),
                   "http_ms": {k: summary(v) for k, v in r["http_ms"].items()},
                   "ws_rtt_ms": {k: summary(v) for k, v in r["ws_rtt_ms"].items()},
                   "start_s": {"remote": starts[REMOTE], "local": starts[LOCAL]},
                   # Kernel round trips fall in two groups, a few ms and about
                   # 40 ms more, directly as well as through the proxy; the
                   # samples are kept so that the groups can be counted.
                   "ws_rtt_raw": {k: [round(x, 2) for x in v] for k, v in r["ws_rtt_ms"].items()}}
            rows.append(row)
            print(json.dumps(row), flush=True)
            with open(out / "e4_latency.jsonl", "a") as fh:
                fh.write(json.dumps(row) + "\n")
    finally:
        set_delay("emu1", 0)
        for user in (REMOTE, LOCAL):
            api.stop_workspace(toks[user])
    (out / "e4_latency.json").write_text(json.dumps(
        {"measured": now(), "n": args.n, "rows": rows}, indent=1))


# --------------------------------------------------------------------------
# failure

_WATCH = r'''
import json, sys, time
import httpx
s = json.load(sys.stdin)
BASE = "http://127.0.0.1:8000"
rows = []
t_end = time.time() + s["seconds"]
with httpx.Client(timeout=5) as c:
    while time.time() < t_end:
        t = time.time(); row = {"t": t}
        try:
            ns = c.get(BASE + "/api/admin/nodes", headers={"Authorization": "Bearer " + s["admin"]}).json()
            n = next(x for x in ns if x["id"] == s["node_id"])
            row["ready"] = n["ready"]
        except Exception as e:
            row["ready"] = None
        try:
            j = c.get(BASE + f"/api/jobs/{s['job']}?output=false",
                      headers={"Authorization": "Bearer " + s["job_token"]}).json()
            row["job"] = j["status"]
        except Exception:
            row["job"] = None
        try:
            r = c.get(BASE + s["ws_base"] + "api/status",
                      headers={"Authorization": "Bearer " + s["ws_token"]})
            row["proxy"] = r.status_code
        except Exception:
            row["proxy"] = None
        rows.append(row)
        time.sleep(max(0, 1 - (time.time() - t)))
print(json.dumps(rows))
'''


def first(rows, pred, after=0.0):
    return next((r["t"] for r in rows if r["t"] >= after and pred(r)), None)


def failure(admin, args, out):
    emu("up", "node1")
    wait_ready(admin, ["emu1"])
    master(JOB_GPU_POOL="-1")
    emu1 = node_id(admin, "emu1")
    api.ensure_user(admin, REMOTE, PASSWORD, gpu_indices=args.remote_gpus, node_id=emu1,
                    memory_limit_mb=4096, cpu_cores=2)
    jtok, wtok = api.login(SUBMITTER, PASSWORD), api.login(REMOTE, PASSWORD)
    api.start_workspace(wtok, timeout=600)
    done = set()
    for f in out.parent.glob("*/e4_failure.jsonl"):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            done.add((r["down_s"], r["rep"]))
    script = 'for i in $(seq 1 120); do echo "tick $i $(date +%s.%N)"; sleep 1; done'
    try:
        for rep in range(args.reps):
            for down in [int(x) for x in args.down.split(",")]:
                if (down, rep) in done:
                    continue
                jid = submit(SUBMITTER, script, 1024, f"e4f-{down}-r{rep}")
                while True:
                    j = job(jtok, jid)
                    if j["status"] == "running":
                        break
                    if j["status"] in ("succeeded", "failed", "cancelled", "timeout"):
                        raise SystemExit(f"job {jid} ended before it ran: {j.get('message')}")
                    time.sleep(1)
                watch = subprocess.Popen(
                    ["docker", "exec", "-i", MASTER, "python", "-c", _WATCH],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                watch.stdin.write(json.dumps({
                    "seconds": 30 + down + 150, "admin": admin, "node_id": emu1, "job": jid,
                    "job_token": jtok, "ws_base": f"/jupyter/{REMOTE}/", "ws_token": wtok}))
                watch.stdin.close()
                time.sleep(30)
                t_stop = time.time()
                subprocess.run(["docker", "stop", "-t", "5", NODE_CONTAINER["emu1"]],
                               check=True, capture_output=True)
                time.sleep(max(0, t_stop + down - time.time()))
                t_start = time.time()
                subprocess.run(["docker", "start", NODE_CONTAINER["emu1"]],
                               check=True, capture_output=True)
                rows = json.loads(watch.stdout.read())
                watch.wait()
                j = wait_done(jtok, jid, timeout=600)
                ticks = [l.split() for l in (j.get("output_tail") or "").splitlines()
                         if l.startswith("tick ")]
                actual = (float(ticks[-1][2]) - float(ticks[0][2])) if len(ticks) > 1 else None
                not_ready = first(rows, lambda r: r["ready"] is False, t_stop)
                ready_again = first(rows, lambda r: r["ready"] is True, t_start)
                bad_proxy = first(rows, lambda r: r["proxy"] != 200, t_stop)
                good_proxy = first(rows, lambda r: r["proxy"] == 200, t_start)
                rec = {
                    "down_s": down, "rep": rep, "job": jid,
                    "node_not_ready_after_s": round(not_ready - t_stop, 1) if not_ready else None,
                    "node_ready_after_restart_s": round(ready_again - t_start, 1) if ready_again else None,
                    "proxy_failing_after_s": round(bad_proxy - t_stop, 1) if bad_proxy else None,
                    "proxy_codes_while_down": sorted({r["proxy"] for r in rows
                                                      if t_stop + 2 <= r["t"] <= t_start}, key=str),
                    "proxy_back_after_restart_s": round(good_proxy - t_start, 1) if good_proxy else None,
                    "job_states_seen": sorted({r["job"] for r in rows if r["job"]}),
                    "job_status": j["status"], "job_message": j.get("message"),
                    "ticks": len(ticks), "ran_s": round(actual, 1) if actual else None,
                    "recorded_runtime_s": j.get("runtime_seconds"),
                }
                print(json.dumps(rec), flush=True)
                with open(out / "e4_failure.jsonl", "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
                with open(out / f"e4_failure_timeline_{down}_r{rep}.json", "w") as fh:
                    json.dump({"t_stop": t_stop, "t_start": t_start, "rows": rows}, fh)
                wait_ready(admin, ["emu1"])
    finally:
        api.stop_workspace(wtok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("part", choices=["placement", "latency", "failure"])
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--mib", type=int, default=10240)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--delays", default="0,1,5,20,50")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--start-reps", type=int, default=3)
    ap.add_argument("--down", default="15,75,150")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--remote-gpus", default="0",
                    help="GPUs of emu1 for the remote workspace; empty for none")
    args = ap.parse_args()
    args.remote_gpus = [int(x) for x in args.remote_gpus.split(",") if x.strip()]
    out = Path(os.environ.get("EXP_OUT") or ".")
    out.mkdir(parents=True, exist_ok=True)

    admin = api.admin_token()
    for user, gpus in ((SUBMITTER, []), (LOCAL, [])):
        api.ensure_user(admin, user, PASSWORD, gpu_indices=gpus, memory_limit_mb=4096, cpu_cores=2)
    sub = api.login(SUBMITTER, PASSWORD)
    api.start_workspace(sub)
    api.wait_running(f"{e1.PREFIX}jupyter-{SUBMITTER}")
    try:
        {"placement": placement, "latency": latency, "failure": failure}[args.part](admin, args, out)
    finally:
        api.stop_workspace(sub)
        master()
        emu("down")


if __name__ == "__main__":
    main()
