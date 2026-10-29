#!/usr/bin/env bash
# An explicit, interactive installer. No sudo action happens from the web UI.
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v apt-get >/dev/null; then echo 'This helper supports Debian/Ubuntu hosts. Install qemu-system-x86, qemu-utils, python3-venv and pip manually on other systems.' >&2; exit 1; fi
printf '%s\n' 'This installs QEMU and builds a Debian cloud-image template (network download; may take a long time).' 'The guest uses serial root autologin. A unique template password is saved in the images directory with mode 600; change it in each VM. Run only on a trusted host.'
read -r -p 'Proceed? [y/N] ' answer
[[ "$answer" =~ ^[Yy]$ ]] || exit 0
sudo apt-get update
sudo apt-get install -y qemu-system-x86 qemu-utils python3-venv python3-pip
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tools/bake.py
printf '\n%s\n' 'Done. Set ROBO_PASSWORD and run: MVMP_BACKEND=qemu ./run.sh'
