#!/bin/bash
# GPU Platform, host firewall rules for the platform's Docker bridge.
#
# WHY THIS EXISTS
# ---------------
# On a host with a default-DROP OUTPUT policy (common on hardened servers, and
# invisible until you run your first bridge-networked container), nothing
# allows host-originated packets out to a Docker bridge.  Every other stack on
# such a host usually runs with network_mode: host, so the gap never shows.
#
# The symptom is confusing: the reverse proxy returns **504 Gateway Timeout**,
# not 502.  The connection to 127.0.0.1:<WEB_PORT> succeeds, that is
# docker-proxy listening on loopback, which the `-o lo -j ACCEPT` rule permits
#, but docker-proxy's own connection onward to the container is dropped by the
# OUTPUT policy, so no response header ever comes back.  ICMP keeps working
# (there is usually an ICMP accept rule), which makes the network look healthy.
#
# WHAT THIS ADDS
# --------------
#   OUTPUT -o <bridge> -j ACCEPT
#
# That is all the platform needs.  Return traffic is already covered by the
# existing `--state RELATED,ESTABLISHED` rule in INPUT, so this script
# deliberately does NOT open INPUT from the bridge: user containers run
# untrusted code and must not be able to *initiate* connections to services on
# the host (databases, other stacks, sshd).  Verify after running:
#
#   docker exec <a user container> curl -m5 http://<bridge gateway>/    # must fail
#
# SSH TO USER CONTAINERS
# ----------------------
# Per-user SSH publishes ports SSH_PORT_START..SSH_PORT_END on all interfaces.
# A default-DROP INPUT policy blocks them too.  Pass --with-ssh to open that
# range on the external interface.  Left off by default: exposing a port range
# to the internet is a decision the operator should make deliberately.
#
# USAGE
#   sudo bash scripts/host_firewall_setup.sh              # the required rule
#   sudo bash scripts/host_firewall_setup.sh --with-ssh   # ...plus the SSH range
#   sudo bash scripts/host_firewall_setup.sh --revert     # remove what it added
#   sudo bash scripts/host_firewall_setup.sh --check      # report, change nothing

set -euo pipefail

BRIDGE="${BRIDGE:-gpu-platform0}"     # pinned in docker-compose.yml driver_opts
MODE="apply"
WITH_SSH=false

for arg in "$@"; do
    case "$arg" in
        --with-ssh) WITH_SSH=true ;;
        --revert)   MODE="revert" ;;
        --check)    MODE="check" ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

# Every mode needs root: reading the ruleset requires CAP_NET_ADMIN just as
# changing it does, and a non-root --check would report "missing" for rules
# that are actually present.
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (sudo bash $0 $*)" >&2
    exit 1
fi

# ── Settings from .env ───────────────────────────────────────────────────────
ENV_FILE="$(dirname "$0")/../.env"
get_env() {
    [ -f "$ENV_FILE" ] || { echo "$2"; return; }
    local v
    v="$(grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
    echo "${v:-$2}"
}
SSH_PORT_START="$(get_env SSH_PORT_START 2222)"
SSH_PORT_END="$(get_env SSH_PORT_END 2321)"

# Default route interface, where published ports face the network.
EXT_IF="${EXT_IF:-$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')}"

rule_out=(OUTPUT -o "$BRIDGE" -j ACCEPT)
rule_ssh=(INPUT -i "$EXT_IF" -p tcp -m state --state NEW -m multiport
          --dports "${SSH_PORT_START}:${SSH_PORT_END}" -j ACCEPT)

has_rule() { iptables -C "$@" 2>/dev/null; }

echo "=== GPU Platform host firewall ==="
echo "  bridge            : $BRIDGE"
echo "  external interface: ${EXT_IF:-<unknown>}"
echo "  OUTPUT policy     : $(iptables -S 2>/dev/null | awk '/^-P OUTPUT/{print $3}')"
echo ""

if ! ip link show "$BRIDGE" >/dev/null 2>&1; then
    echo "WARNING: interface '$BRIDGE' does not exist yet."
    echo "         Start the stack first (./deploy.sh), then re-run this script."
    echo "         If you renamed the bridge, pass BRIDGE=<name>."
    [ "$MODE" = "check" ] || exit 1
fi

case "$MODE" in
check)
    if has_rule "${rule_out[@]}"; then
        echo "  [ok]      host -> $BRIDGE is allowed"
    else
        echo "  [MISSING] host -> $BRIDGE is NOT allowed, the reverse proxy will time out (504)"
    fi
    if has_rule "${rule_ssh[@]}"; then
        echo "  [ok]      SSH range ${SSH_PORT_START}-${SSH_PORT_END} open on ${EXT_IF}"
    else
        echo "  [absent]  SSH range ${SSH_PORT_START}-${SSH_PORT_END} closed (use --with-ssh to open)"
    fi
    ;;

apply)
    if has_rule "${rule_out[@]}"; then
        echo "  host -> $BRIDGE already allowed; nothing to do."
    else
        iptables -I "${rule_out[@]}"
        echo "  + iptables -I OUTPUT -o $BRIDGE -j ACCEPT"
    fi

    if [ "$WITH_SSH" = true ]; then
        if [ -z "$EXT_IF" ]; then
            echo "  ! could not determine the external interface; set EXT_IF=<iface> and re-run"
        elif has_rule "${rule_ssh[@]}"; then
            echo "  SSH range already open; nothing to do."
        else
            iptables -I "${rule_ssh[@]}"
            echo "  + iptables -I INPUT -i $EXT_IF -p tcp --dports ${SSH_PORT_START}:${SSH_PORT_END} -j ACCEPT"
        fi
    fi

    echo ""
    echo "Persisting so the rules survive a reboot..."
    if command -v netfilter-persistent >/dev/null 2>&1; then
        netfilter-persistent save && echo "  saved via netfilter-persistent"
    elif [ -d /etc/iptables ]; then
        iptables-save > /etc/iptables/rules.v4 && echo "  saved to /etc/iptables/rules.v4"
    else
        echo "  ! no persistence mechanism found, the rules are live but will be"
        echo "    lost on reboot. Save them however this host normally does."
    fi
    ;;

revert)
    for r in rule_out rule_ssh; do
        declare -n ref=$r
        if has_rule "${ref[@]}"; then
            iptables -D "${ref[@]}"
            echo "  - removed: ${ref[*]}"
        fi
    done
    if command -v netfilter-persistent >/dev/null 2>&1; then
        netfilter-persistent save >/dev/null && echo "  persisted"
    fi
    ;;
esac

echo ""
echo "Done."
