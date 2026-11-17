#!/bin/bash
# Start MariaDB, munge, slurmdbd, slurmctld and slurmd; register the users
# u1..uN with MaxJobs (the platform's per-user running cap).
set -e
# Slurm 23.11 needs a cgroup v2 tree it can write (cgroup.conf): move this
# container's processes out of the root, which may then delegate controllers,
# and give slurmd the system.slice it would get from systemd.
CG=/sys/fs/cgroup
mkdir -p $CG/init $CG/system.slice
for p in $(cat $CG/cgroup.procs); do echo "$p" > $CG/init/cgroup.procs 2>/dev/null || true; done
for c in $(cat $CG/cgroup.controllers); do echo "+$c" > $CG/cgroup.subtree_control 2>/dev/null || true; done
for c in $(cat $CG/cgroup.controllers); do echo "+$c" > $CG/system.slice/cgroup.subtree_control 2>/dev/null || true; done
sed -i "s/__MEM__/${E3_MEM_MIB:-46000}/" /etc/slurm/slurm.conf
mysqld_safe >/dev/null 2>&1 &
for _ in $(seq 60); do mysqladmin ping >/dev/null 2>&1 && break; sleep 1; done
mysql -e "CREATE DATABASE IF NOT EXISTS slurm_acct_db;
          CREATE USER IF NOT EXISTS 'slurm'@'localhost' IDENTIFIED BY 'e3-local-only';
          GRANT ALL ON slurm_acct_db.* TO 'slurm'@'localhost';"
[ -s /etc/munge/munge.key ] || mungekey -c -f
chown munge:munge /etc/munge/munge.key /run/munge
su -s /bin/sh munge -c munged
slurmdbd
for _ in $(seq 60); do sacctmgr -n list cluster >/dev/null 2>&1 && break; sleep 1; done
sacctmgr -i add cluster e3 >/dev/null 2>&1 || true
sacctmgr -i add account e3 cluster=e3 >/dev/null 2>&1 || true
for i in $(seq 1 16); do
    sacctmgr -i add user u$i account=e3 maxjobs=${E3_MAX_RUNNING:-2} >/dev/null 2>&1 || true
done
slurmctld
slurmd
touch /ready
exec sleep infinity
