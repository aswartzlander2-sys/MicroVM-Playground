#!/usr/bin/env bash
# Stop Robo Universal without deleting VM disks or unrelated processes.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/bin/microvm" stop
