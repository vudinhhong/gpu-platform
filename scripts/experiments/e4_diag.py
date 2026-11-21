#!/usr/bin/env python3
"""Why requests to a workspace on the master slowed with a node's delay (E4).

In two of four E4 latency runs, requests through the proxy to the workspace
on the master took up to 11 ms at 20-50 ms of added node delay, against
2 ms at none.  This repeats that set-up and separates the possible causes:

* every request is recorded with its start time, so that slow ones can be
  matched to anything periodic;
* a probe calls /api/health every 10 ms meanwhile: it does nothing but return,
  so when it is slow the backend's event loop was busy;
* three orders: the two workspaces alternately (as in E4), the master's only,
  the node's only.  If the master's alone are fast, the cost comes from the
  request to the node just before;
* each set is measured right after the workspaces start and again a minute
  later, in case it is work left over from the start.

    EXP_OUT=out python3 scripts/experiments/e4_diag.py --delays 0,50,20,50,0,50
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e4_nodes as e4  # noqa: E402
import platform_api as api  # noqa: E402

_CLIENT = r'''
import asyncio, json, sys, time
import httpx
s = json.load(sys.stdin)
BASE = "http://127.0.0.1:8000"

async def main():
    probe, reqs, stop = [], [], asyncio.Event()
    async with httpx.AsyncClient(timeout=60) as c, httpx.AsyncClient(timeout=60) as pc:
        async def prober():
            while not stop.is_set():
                t0 = time.perf_counter(); w = time.time()
                await pc.get(BASE + "/api/health")
                probe.append((round(w, 4), round((time.perf_counter() - t0) * 1000, 3)))
                await asyncio.sleep(0.01)
        for name, t in s["targets"].items():
            h = {"Authorization": "Bearer " + t["token"]}
            for _ in range(60):
                try:
                    if (await c.get(BASE + t["base"] + "api/status", headers=h)).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2)
        task = asyncio.create_task(prober())
        for mode, names in (("interleaved", ["remote", "local"]), ("local_only", ["local"]),
                            ("remote_only", ["remote"])):
            for _ in range(s["n"]):
                for name in names:
                    t = s["targets"][name]
                    t0 = time.perf_counter(); w = time.time()
                    r = await c.get(BASE + t["base"] + "api/status",
                                    headers={"Authorization": "Bearer " + t["token"]})
                    assert r.status_code == 200, (name, r.status_code)
                    reqs.append((mode, name, round(w, 4), round((time.perf_counter() - t0) * 1000, 3)))
        stop.set(); await task
    print(json.dumps({"requests": reqs, "probe": probe}))

asyncio.run(main())
'''


def stats(xs):
    if not xs:
        return None
    xs = sorted(xs)
    pick = lambda q: round(xs[int(q * (len(xs) - 1))], 2)  # noqa: E731
    return {"n": len(xs), "median": pick(0.5), "p90": pick(0.9), "max": round(xs[-1], 2),
            "over_5ms": sum(x > 5 for x in xs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delays", default="0,50,20,50,0,50")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--settle", type=int, default=60)
    ap.add_argument("--remote-gpus", default="0")
    args = ap.parse_args()
    out = Path(os.environ.get("EXP_OUT", "."))
    out.mkdir(parents=True, exist_ok=True)

    args.remote_gpus = [int(x) for x in args.remote_gpus.split(",") if x.strip()]
    admin = api.admin_token()
    for user in (e4.SUBMITTER, e4.LOCAL):
        api.ensure_user(admin, user, e4.PASSWORD, gpu_indices=[], memory_limit_mb=4096, cpu_cores=2)
    # As in E4: the submitter's workspace on the master runs throughout.
    sub = api.login(e4.SUBMITTER, e4.PASSWORD)
    api.start_workspace(sub)
    e4.netem_image()
    e4.emu("up", "node1")
    e4.wait_ready(admin, ["emu1"])
    api.ensure_user(admin, e4.REMOTE, e4.PASSWORD, gpu_indices=args.remote_gpus,
                    node_id=e4.node_id(admin, "emu1"), memory_limit_mb=4096, cpu_cores=2)
    toks = {u: api.login(u, e4.PASSWORD) for u in (e4.REMOTE, e4.LOCAL)}
    spec = {"n": args.n, "targets": {
        "remote": {"token": toks[e4.REMOTE], "base": f"/jupyter/{e4.REMOTE}/"},
        "local": {"token": toks[e4.LOCAL], "base": f"/jupyter/{e4.LOCAL}/"}}}
    try:
        for i, d in enumerate(int(x) for x in args.delays.split(",")):
            e4.set_delay("emu1", d)
            for user in (e4.REMOTE, e4.LOCAL):
                api.stop_workspace(toks[user])
                time.sleep(3)
                api.start_workspace(toks[user], timeout=600)
            for phase in ("after_start", "settled"):
                if phase == "settled":
                    time.sleep(args.settle)
                raw = e4.in_master(_CLIENT, spec)
                row = {"step": i, "delay_ms": d, "phase": phase, "measured": e4.now(),
                       "summary": {f"{m}/{n}": stats([x[3] for x in raw["requests"]
                                                       if x[0] == m and x[1] == n])
                                   for m, n in (("interleaved", "remote"), ("interleaved", "local"),
                                                ("local_only", "local"), ("remote_only", "remote"))},
                       "probe": stats([x[1] for x in raw["probe"]]),
                       **raw}
                print(json.dumps({k: v for k, v in row.items() if k not in ("requests", "probe")}
                                 | {"probe": row["probe"]}), flush=True)
                with open(out / "e4_diag.jsonl", "a") as fh:
                    fh.write(json.dumps(row) + "\n")
    finally:
        e4.set_delay("emu1", 0)
        for user in (e4.REMOTE, e4.LOCAL):
            api.stop_workspace(toks[user])
        api.stop_workspace(sub)
        e4.master()
        e4.emu("down")


if __name__ == "__main__":
    main()
