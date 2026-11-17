# A stand-in for a fresh Ubuntu machine for E6: the distribution with systemd
# as PID 1 and nothing else.  e6_container.sh runs it privileged, with its
# own Docker data directory, so the platform's set-up starts from no images.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        systemd systemd-sysv dbus sudo iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl mask systemd-logind getty.target console-getty.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
