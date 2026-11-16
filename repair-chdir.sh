#!/usr/bin/env bash
# SEKA WEB dev v0.2.3 narrowly scoped repair for systemd 200/CHDIR.
set -Eeuo pipefail
[[ "${EUID}" -eq 0 ]] || { echo 'Run with sudo' >&2; exit 2; }
[[ -f /etc/systemd/system/seka-web-dev.service ]] || { echo 'SEKA WEB dev systemd unit not found' >&2; exit 2; }
[[ -d /opt/seka-web-dev/current ]] || { echo 'SEKA WEB dev application not found' >&2; exit 2; }
id sekawebdev >/dev/null 2>&1 || { echo 'Service account not found' >&2; exit 2; }
chown root:sekawebdev /opt/seka-web-dev
chmod 750 /opt/seka-web-dev
systemctl restart seka-web-dev.service
sleep 2
systemctl is-active --quiet seka-web-dev.service || { systemctl --no-pager status seka-web-dev.service; exit 3; }
curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:547/health || { echo 'Service running but healthcheck failed' >&2; exit 4; }
echo
echo 'SEKA WEB dev service is active. If initial admin bootstrap did not run, it still needs completion.'
