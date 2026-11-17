# Single-host Slurm for E3: slurmctld, slurmd and slurmdbd (with MariaDB) in
# one container, so fair share and per-user limits work as on a cluster.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        slurm-wlm slurmdbd mariadb-server munge python3 \
    && rm -rf /var/lib/apt/lists/*
RUN for i in $(seq 1 16); do useradd -m -u $((2000 + i)) u$i; done
COPY slurm.conf slurmdbd.conf cgroup.conf /etc/slurm/
COPY start.sh /start.sh
RUN chmod 600 /etc/slurm/slurmdbd.conf && chown slurm:slurm /etc/slurm/slurmdbd.conf \
    && mkdir -p /var/spool/slurmctld /var/spool/slurmd /var/log/slurm /run/munge \
    && chown -R slurm:slurm /var/spool/slurmctld /var/log/slurm && chmod +x /start.sh
CMD ["/start.sh"]
