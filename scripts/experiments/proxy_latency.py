#!/usr/bin/env python3
"""What the authenticated Jupyter proxy costs, against reaching the server directly.

Three paths to the same JupyterLab server, in an experiment account's workspace:

    direct    host -> container:8888 (Jupyter's own token; no platform in the path)
    backend   host -> backend:8000 /jupyter/... (platform JWT, the proxy)
    web       host -> frontend nginx -> backend -> container (what a browser uses)

Measured on each path, interleaved so host load affects all three alike:

    http_ms        GET api/status, N requests
    ws_rtt_ms      kernel execute_request "1+1" -> execute_reply over the
                   kernel WebSocket, N round trips on one connection
    download_mbs   one 256 MiB file through the files endpoint

    python3 scripts/experiments/proxy_latency.py --n 300 --out paper/data/proxy.json
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime

import websocket  # websocket-client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_api as api  # noqa: E402

USER = "expc"
PASSWORD = "Experiment-pass-1"


def env_of(container, key):
    out = subprocess.run(["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}",
                          container], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return None


def ip_of(container):
    return subprocess.run(["docker", "inspect", "-f",
                           "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container],
                          capture_output=True, text=True, check=True).stdout.strip()


def get(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def post(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={**headers, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def ws_rtt(ws_url, headers, n):
    ws = websocket.create_connection(ws_url, header=[f"{k}: {v}" for k, v in headers.items()],
                                     timeout=30)
    session = uuid.uuid4().hex
    out = []
    try:
        for _ in range(n + 5):
            msg_id = uuid.uuid4().hex
            msg = {"header": {"msg_id": msg_id, "msg_type": "execute_request", "username": USER,
                              "session": session, "version": "5.3", "date": ""},
                   "parent_header": {}, "metadata": {}, "channel": "shell", "buffers": [],
                   "content": {"code": "1+1", "silent": False, "store_history": False,
                               "user_expressions": {}, "allow_stdin": False}}
            t0 = time.perf_counter()
            ws.send(json.dumps(msg))
            while True:
                reply = json.loads(ws.recv())
                if (reply.get("msg_type") == "execute_reply"
                        and reply.get("parent_header", {}).get("msg_id") == msg_id):
                    break
            out.append((time.perf_counter() - t0) * 1000)
    finally:
        ws.close()
    return out[5:]                       # drop warm-up


def summary(xs):
    xs = sorted(xs)
    return {"n": len(xs), "median": statistics.median(xs), "p95": xs[int(0.95 * (len(xs) - 1))],
            "mean": statistics.mean(xs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--web", default="http://127.0.0.1:7080", help="the frontend's published address")
    ap.add_argument("--out", default="proxy.json")
    args = ap.parse_args()

    admin = api.admin_token()
    api.ensure_user(admin, USER, PASSWORD, gpu_indices=[], memory_limit_mb=4096, cpu_cores=4)
    token = api.login(USER, PASSWORD)
    api.start_workspace(token)
    container = f"gpu-jupyter-{USER}"
    api.wait_running(container)
    jtoken = env_of(container, "JUPYTER_TOKEN")
    base = env_of(container, "JUPYTER_BASE_URL") or f"/jupyter/{USER}/"
    ip = ip_of(container)

    paths = {
        "direct": (f"http://{ip}:8888{base}", {"Authorization": f"token {jtoken}"}),
        "backend": (f"{api.URL}{base}", {"Authorization": f"Bearer {token}"}),
        "web": (f"{args.web.rstrip('/')}{base}", {"Authorization": f"Bearer {token}"}),
    }

    # wait for Jupyter itself
    deadline = time.time() + 120
    while True:
        try:
            if get(paths["direct"][0] + "api/status", paths["direct"][1])[0] == 200:
                break
        except Exception:  # noqa: BLE001
            if time.time() > deadline:
                raise
            time.sleep(2)

    api.sh(container, USER, "head -c 268435456 /dev/zero > ~/proxybench.bin")

    try:
        http = {p: [] for p in paths}
        for _ in range(args.n):
            for p, (url, hdr) in paths.items():
                t0 = time.perf_counter()
                status, _ = get(url + "api/status", hdr)
                assert status == 200, (p, status)
                http[p].append((time.perf_counter() - t0) * 1000)

        kernel = post(paths["direct"][0] + "api/kernels", paths["direct"][1], {"name": "python3"})
        ws = {}
        for p, (url, hdr) in paths.items():
            ws_url = url.replace("http://", "ws://") + f"api/kernels/{kernel['id']}/channels"
            ws[p] = ws_rtt(ws_url, hdr, args.n)

        download = {}
        for p, (url, hdr) in paths.items():
            rates = []
            for _ in range(3):
                t0 = time.perf_counter()
                _, body = get(url + "files/proxybench.bin", hdr, timeout=300)
                rates.append(len(body) / (time.perf_counter() - t0) / 1e6)
            download[p] = {"runs": rates, "median": statistics.median(rates)}
    finally:
        api.sh(container, USER, "rm -f ~/proxybench.bin")
        api.stop_workspace(token)
        api.deactivate(admin, USER)

    result = {
        "measured": datetime.now().astimezone().isoformat(timespec="seconds"),
        "n": args.n,
        "http_ms": {p: summary(v) for p, v in http.items()},
        "ws_rtt_ms": {p: summary(v) for p, v in ws.items()},
        "download_mbs": download,
    }
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1)
    for k in ("http_ms", "ws_rtt_ms"):
        print(k, {p: round(v["median"], 2) for p, v in result[k].items()},
              "p95", {p: round(v["p95"], 2) for p, v in result[k].items()})
    print("download_mbs", {p: round(v["median"], 1) for p, v in download.items()})


if __name__ == "__main__":
    main()
