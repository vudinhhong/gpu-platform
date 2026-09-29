#!/bin/bash
# Show the resources allocated to this workspace.
#
# The standard tools (free, nproc, df, top) read machine-wide values and cannot
# see the allocation, so this reads it directly.  Nothing here mentions the
# machinery underneath: from the user's seat this workspace IS their machine.

human() {  # bytes -> human readable
    local b=$1
    if [ "$b" = "max" ] || [ -z "$b" ]; then echo "unlimited"; return; fi
    awk -v b="$b" 'BEGIN{
        split("B KiB MiB GiB TiB", u, " "); i=1
        while (b >= 1024 && i < 5) { b /= 1024; i++ }
        printf (i==1 ? "%d %s" : "%.1f %s"), b, u[i]
    }'
}

# Allocations the kernel cannot express (disk space) are published here by the
# platform and kept current, so a change shows up without restarting.
# /platform is the platform's own directory, mounted beside the workspace.
# The older in-workspace path is still read so a container from before the
# change keeps working until it is next recreated.
[ -r /platform/limits.env ] && . /platform/limits.env
[ -r /workspace/.platform/limits.env ] && . /workspace/.platform/limits.env
# Written separately and far more often: hours used move every minute a
# session is open, while the ceilings above only move when an admin edits them.
[ -r /platform/budget.env ] && . /platform/budget.env

# Where this workspace is mounted.  The limits file above names it; $HOME is
# the same thing for anyone running this interactively, and the old fixed path
# is the last resort for a container created before workspaces moved.
WS="${PLATFORM_HOME:-${HOME:-/workspace}}"

CG=/sys/fs/cgroup

printf '\n\033[1mYour workspace\033[0m\n'
printf '%s\n' "----------------------------------------------------------"

# ── RAM ────────────────────────────────────────────────────────────────────
mem_max=$(cat $CG/memory.max 2>/dev/null)
mem_cur=$(cat $CG/memory.current 2>/dev/null)
printf '  %-13s %s used of %s\n' "Memory" "$(human "$mem_cur")" "$(human "$mem_max")"
if [ -r $CG/memory.events ]; then
    oom=$(awk '/^oom_kill /{print $2}' $CG/memory.events 2>/dev/null)
    [ -n "$oom" ] && [ "$oom" != "0" ] && \
        printf '  %-13s \033[33m%s program(s) stopped for running out of memory\033[0m\n' "" "$oom"
fi

# ── CPU ────────────────────────────────────────────────────────────────────
read -r quota period < <(cat $CG/cpu.max 2>/dev/null)
if [ "$quota" = "max" ] || [ -z "$quota" ]; then
    printf '  %-13s %s cores\n' "CPU" "$(nproc)"
else
    printf '  %-13s %s cores\n' "CPU" \
        "$(awk -v q="$quota" -v p="$period" 'BEGIN{c=q/p; printf (c==int(c) ? "%d" : "%.2f"), c}')"
fi

# ── Processes ──────────────────────────────────────────────────────────────
printf '  %-13s %s of %s\n' "Processes" \
    "$(cat $CG/pids.current 2>/dev/null)" "$(cat $CG/pids.max 2>/dev/null)"

# ── Disk speed ─────────────────────────────────────────────────────────────
io=$(cat $CG/io.max 2>/dev/null | head -1)
if [ -n "$io" ]; then
    r=$(echo "$io" | tr ' ' '\n' | awk -F= '/^rbps/{print $2}')
    w=$(echo "$io" | tr ' ' '\n' | awk -F= '/^wbps/{print $2}')
    printf '  %-13s read %s/s, write %s/s\n' "Disk speed" "$(human "$r")" "$(human "$w")"
fi

# ── Disk space ─────────────────────────────────────────────────────────────
used_kb=$(du -sk "$WS" 2>/dev/null | awk '{print $1}')
used_mb=$(( ${used_kb:-0} / 1024 ))
if [ -n "${PLATFORM_DISK_QUOTA_MB:-}" ] && [ "${PLATFORM_DISK_QUOTA_MB}" != "0" ]; then
    pct=$(awk -v u="$used_mb" -v q="$PLATFORM_DISK_QUOTA_MB" 'BEGIN{printf "%.0f", u/q*100}')
    printf '  %-13s %s MB used of %s MB (%s%%)\n' "Disk space" \
        "$used_mb" "$PLATFORM_DISK_QUOTA_MB" "$pct"
    if [ "$pct" -gt 100 ] 2>/dev/null; then
        printf '  %-13s \033[31mOver budget. No GPU and no jobs until you free space.\033[0m\n' ""
    elif [ "$pct" -ge 90 ] 2>/dev/null; then
        printf '  %-13s \033[33mAlmost full. Free some space to keep working.\033[0m\n' ""
    fi
else
    printf '  %-13s %s MB used\n' "Disk space" "$used_mb"
fi

# ── Job budget ─────────────────────────────────────────────────────────────
# The queue's budget, not this workspace's: sitting here costs nothing.  A job
# is charged for what it holds, so an hour on two GPUs is two GPU hours and an
# hour of a four-core job is four CPU hours, busy or idle.
budget_line() {  # label, used, quota
    [ -z "$3" ] && return
    pc=$(awk -v u="$2" -v q="$3" 'BEGIN{printf "%.0f", (q>0 ? u/q*100 : 0)}')
    printf '  %-13s %s of %s used' "$1" "$2" "$3"
    if [ "$pc" -ge 100 ] 2>/dev/null; then
        printf ' \033[31m(spent)\033[0m'
    elif [ "$pc" -ge 90 ] 2>/dev/null; then
        printf ' \033[33m(%s%%)\033[0m' "$pc"
    else
        printf ' (%s%%)' "$pc"
    fi
    printf '\n'
}

if [ -n "${PLATFORM_GPU_HOURS_QUOTA:-}" ] || [ -n "${PLATFORM_CPU_HOURS_QUOTA:-}" ]; then
    budget_line "Job GPU hrs" "${PLATFORM_GPU_HOURS_USED:-0}" "${PLATFORM_GPU_HOURS_QUOTA:-}"
    budget_line "Job CPU hrs" "${PLATFORM_CPU_HOURS_USED:-0}" "${PLATFORM_CPU_HOURS_QUOTA:-}"
    [ -n "${PLATFORM_BUDGET_RESETS:-}" ] && \
        printf '  %-13s refills %s; a job still running when it runs out is\n' \
               "" "$PLATFORM_BUDGET_RESETS" && \
        printf '  %-13s paused, or put back in the queue if it holds a GPU\n' ""
fi

# ── GPU ────────────────────────────────────────────────────────────────────
if command -v nvidia-smi >/dev/null 2>&1; then
    gpus=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null)
    if [ -n "$gpus" ]; then
        first=1
        echo "$gpus" | while read -r g; do
            [ "$first" = 1 ] && printf '  %-13s %s\n' "GPU" "$g" || printf '  %-13s %s\n' "" "$g"
            first=0
        done
    else
        printf '  %-13s none\n' "GPU"
    fi
else
    printf '  %-13s none\n' "GPU"
fi

printf '%s\n' "----------------------------------------------------------"
printf '  \033[2mRun \033[0m\033[1mlimits\033[0m\033[2m to see this again, or \033[0m\033[1msubmit\033[0m\033[2m / \033[0m\033[1mqueue\033[0m\033[2m / \033[0m\033[1mcancel\033[0m\033[2m\n'
printf '  to run a script on a GPU when one is free (\033[0msubmit --help\033[2m).\033[0m\n\n'
