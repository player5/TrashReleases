# TrashReleases — SEKA WEB development distribution

Public distribution for **experimental SEKA WEB**, not a production release.

Install (Ubuntu):

```bash
curl -fsSL https://raw.githubusercontent.com/player5/TrashReleases/main/install.sh -o /tmp/seka-web-install.sh && sudo bash /tmp/seka-web-install.sh
```

Default LAN port 547. Configurable: `sudo env SEKA_WEB_PORT=548 bash /tmp/seka-web-install.sh`.

**Security caveat:** development SQLite prototype, no HTTPS, no PostgreSQL/jobs runtime. Install only in an isolated/test LAN. This repository contains text source files and scripts; no binary releases, secrets or archives.

Source project & design: https://github.com/player5/SEKA_WEB
