# Operator scripts

| Script | What it does |
|---|---|
| `host_gpu_setup.sh` | Run once as root on the host: driver persistence mode, VRAM preservation, a udev rule keeping `/dev/nvidia*` alive and a systemd timer. Prevents GPUs detaching from idle containers. |
| `host_firewall_setup.sh` | Run as root on a host whose firewall defaults to DROP: allows host→container traffic on the platform's Docker bridge and persists it. Without it the reverse proxy returns **504** even though every container is healthy. `--check` reports, `--with-ssh` also opens the per-user SSH port range, `--revert` undoes it. |
| `e2e_check.py` | End-to-end verification of a *running* stack: auth, throttling, GPU isolation down to the device request, the Jupyter HTTP and WebSocket proxy, telemetry, quotas, accounting, audit and token revocation. |

## Running the end-to-end check

It runs from inside the backend container so it exercises the real network path
(host web server → frontend → backend → user container) and can talk to the
Docker API to inspect what was actually created.

```bash
docker cp scripts/e2e_check.py workspace-gpu-backend-1:/tmp/e2e.py
docker exec -e ADMIN_PASSWORD="$(grep '^ADMIN_PASSWORD=' .env | cut -d= -f2-)" \
    workspace-gpu-backend-1 python /tmp/e2e.py
```

It creates and deletes a temporary `e2euser` account, and briefly starts a real
GPU container for it — safe on a live system, but it does consume a GPU slot for
a few seconds.

## Host firewall

The platform's containers sit on a Docker bridge (`gpu-platform0`, pinned in
`docker-compose.yml` so firewall rules keep matching across recreations). On a
host with `-P OUTPUT DROP`, nothing allows the host to talk to that bridge, so
`docker-proxy` cannot forward a published port to the container:

```bash
sudo bash scripts/host_firewall_setup.sh --check    # report
sudo bash scripts/host_firewall_setup.sh            # add + persist the rule
```

The script adds exactly one rule — `OUTPUT -o gpu-platform0 -j ACCEPT`. It does
**not** open INPUT from the bridge, so user containers still cannot initiate
connections to services running on the host; replies to host-initiated
connections are already covered by the usual `RELATED,ESTABLISHED` rule.
