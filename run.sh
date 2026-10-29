#!/usr/bin/env bash
# One command for first-run setup, Python dependency updates and start.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
# Install a usable command without replacing a different microvm executable.
as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }
cli_source="$HERE/bin/microvm"
local_dir="$HOME/.local/bin"
mkdir -p "$local_dir"
install_link() {
  local target="$1"
  if [ -L "$target" ] && [ "$(readlink -f "$target")" = "$cli_source" ]; then return 0; fi
  if [ -e "$target" ] || [ -L "$target" ]; then
    echo "  Not replacing existing $target (it belongs to another installation)." >&2
    return 1
  fi
  if [ -w "$(dirname "$target")" ]; then ln -s "$cli_source" "$target"
  else as_root ln -s "$cli_source" "$target"; fi
}
installed_cli=""
if [ "$(id -u)" = 0 ]; then
  # /root/.local/bin is frequently absent from root's PATH; use the normal
  # system-wide executable directory, but never overwrite another program.
  mkdir -p /usr/local/bin
  if install_link /usr/local/bin/microvm; then installed_cli=/usr/local/bin/microvm; fi
else
  if install_link "$local_dir/microvm"; then installed_cli="$local_dir/microvm"; fi
  case ":$PATH:" in
    *":$local_dir:"*) ;;
    *)
      if [ -t 0 ] && command -v sudo >/dev/null; then
        echo "  $local_dir is not on PATH in this shell."
        read -r -p '  Install microvm in /usr/local/bin too (requires sudo)? [Y/n] ' cli_answer
        if [[ "${cli_answer:-y}" =~ ^[Yy]$ ]] && install_link /usr/local/bin/microvm; then
          installed_cli=/usr/local/bin/microvm
        fi
      fi
      ;;
  esac
fi
if [ -n "$installed_cli" ]; then
  echo "  Command installed: $installed_cli"
  if [ "$(id -u)" != 0 ] && [ "$installed_cli" = "$local_dir/microvm" ]; then
    case ":$PATH:" in *":$local_dir:"*) ;; *) echo "  To use it now: export PATH=\"$local_dir:\$PATH\"" ;; esac
  fi
else
  echo "  Use $cli_source; an existing microvm command was not replaced." >&2
fi
echo '  Robo Universal · setup and start'
command -v python3 >/dev/null || { echo 'Python 3 is required. Install python3 first.' >&2; exit 1; }
if [ ! -x .venv/bin/python ]; then
  echo '  Creating the Python environment…'
  if ! python3 -m venv .venv; then
    if command -v apt-get >/dev/null && [ -t 0 ]; then
      echo '  Python venv is missing. Installing python3-venv requires sudo.'
      as_root apt-get update && as_root apt-get install -y python3-venv
      python3 -m venv .venv
    else echo '  Install python3-venv and try again.' >&2; exit 1; fi
  fi
fi
PY="$HERE/.venv/bin/python"
if [[ " ${*:-} " == *" --reset-login "* ]]; then
  "$PY" -m server.credentials --reset
  echo '  Restart any running server to invalidate existing sessions.'
  exit 0
fi
# A previous installation under the same OS account may already have an
# auth file. On this install's first run, ask instead of silently reusing it.
setup_marker="$HERE/.microvm-setup-complete"
auth_file=$("$PY" -c 'from server.credentials import auth_path; print(auth_path())')
if [ ! -e "$setup_marker" ] && [ -e "$auth_file" ] && [ -t 0 ]; then
  echo "  An existing login was found at $auth_file."
  echo '  It may belong to an earlier Robo Universal installation.'
  read -r -p '  Set a new username and password for this installation? [Y/n] ' auth_answer
  if [[ "${auth_answer:-y}" =~ ^[Yy]$ ]]; then
    "$PY" -m server.credentials --reset
  else
    "$PY" -m server.credentials
    echo '  Keeping the existing login.'
  fi
else
  "$PY" -m server.credentials
fi
# Only write the marker after login setup succeeds.
touch "$setup_marker"
# Upgrades the app's Python dependencies, without changing unrelated system packages.
echo '  Installing/updating Python dependencies…'
"$PY" -m pip install --upgrade -r requirements.txt
backend="${MVMP_BACKEND:-qemu}"
case " $* " in *" --mock "*) backend=mock ;; esac
if [[ "${MVMP_MOCK:-}" =~ ^(1|true|yes|on)$ ]]; then backend=mock; fi
if [ "$backend" = qemu ]; then
  if ! command -v qemu-system-x86_64 >/dev/null || ! command -v qemu-img >/dev/null; then
    if command -v apt-get >/dev/null && [ -t 0 ]; then
      echo '  QEMU is needed for real VMs. Ubuntu will ask for sudo to install it.'
      read -r -p '  Install QEMU now? [Y/n] ' answer
      if [[ "${answer:-y}" =~ ^[Yy]$ ]]; then
        as_root apt-get update
        as_root apt-get install -y qemu-system-x86 qemu-utils
      else echo '  QEMU not installed; VM launches will be unavailable.'; fi
    else echo '  QEMU is missing. Install qemu-system-x86 and qemu-utils for real VMs.'; fi
  fi
  export MVMP_BACKEND=qemu
fi
if [ "$backend" = firecracker ] && [ "$(id -u)" != 0 ]; then
  echo '  Firecracker needs root for tap networking; choose QEMU or configure Firecracker separately.' >&2; exit 1
fi
if ! command -v cloudflared >/dev/null; then
  echo '  Cloudflare publishing needs cloudflared from Cloudflare’s signed apt repository.'
  if command -v apt-get >/dev/null && [ -t 0 ]; then
    read -r -p '  Add the Cloudflare apt repository and install cloudflared? [Y/n] ' cf_answer
    if [[ "${cf_answer:-y}" =~ ^[Yy]$ ]]; then
      cf_key=$(mktemp); cf_list=$(mktemp)
      trap 'rm -f "$cf_key" "$cf_list"' EXIT
      "$PY" -c 'import urllib.request,sys; urllib.request.urlretrieve("https://pkg.cloudflare.com/cloudflare-main.gpg",sys.argv[1])' "$cf_key"
      printf '%s\n' 'deb [signed-by=/usr/share/keyrings/robo-cloudflare.gpg] https://pkg.cloudflare.com/cloudflared any main' > "$cf_list"
      as_root install -m 0644 "$cf_key" /usr/share/keyrings/robo-cloudflare.gpg
      as_root install -m 0644 "$cf_list" /etc/apt/sources.list.d/robo-cloudflared.list
      as_root apt-get update
      as_root apt-get install -y cloudflared
      rm -f "$cf_key" "$cf_list"; trap - EXIT
    fi
  fi
fi
# A user service runs the already-installed environment, never this interactive script.
if [ -t 0 ] && command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  service="$HOME/.config/systemd/user/robo-universal.service"
  if [ ! -f "$service" ]; then
    read -r -p '  Start Robo Universal automatically at boot? [Y/n] ' answer
    if [[ "${answer:-y}" =~ ^[Yy]$ ]]; then
      if command -v loginctl >/dev/null; then
        echo '  Enabling boot startup for this user may ask for sudo.'
        if ! as_root loginctl enable-linger "$(id -un)"; then
          echo '  Could not enable linger; the service may start only after you log in.' >&2
        fi
      fi
      mkdir -p "$(dirname "$service")"
      # systemd unit paths are quoted rather than interpreted by a shell.
      escaped_here=${HERE//\/\\}; escaped_here=${escaped_here//\"/\\\"}
      cat > "$service" <<EOF
[Unit]
Description=Robo Universal dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory="$escaped_here"
Environment=MVMP_BACKEND=qemu
Environment=MVMP_PORT=45952
ExecStart="$escaped_here/.venv/bin/python" "$escaped_here/run_server.py" --host 127.0.0.1 --port 45952
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
      systemctl --user daemon-reload
      systemctl --user enable robo-universal.service
      echo '  Boot startup enabled.'
    fi
  fi
fi
# Launch as a service when available: no terminal is needed afterwards.
if [ "$backend" = qemu ] && [ -f "$HOME/.config/systemd/user/robo-universal.service" ] && systemctl --user show-environment >/dev/null 2>&1; then
  if ! systemctl --user is-active --quiet robo-universal.service; then
    systemctl --user start robo-universal.service
  fi
  echo '  Running in the background at http://127.0.0.1:45952'
  echo '  Logs: journalctl --user -u robo-universal -f'
  exit 0
fi
# Fallback for hosts without a user service: detach from the terminal. This
# stays running when the terminal closes, but does not provide boot startup.
state="${XDG_STATE_HOME:-$HOME/.local/state}/robo-universal"
mkdir -p "$state"
pidfile="$state/server.pid"
if [ -f "$pidfile" ]; then
  oldpid=$(cat "$pidfile" 2>/dev/null || true)
  if [[ "$oldpid" =~ ^[0-9]+$ ]] && kill -0 "$oldpid" 2>/dev/null && [ -r "/proc/$oldpid/cmdline" ] && tr '\0' ' ' < "/proc/$oldpid/cmdline" | grep -Fq "$HERE/run_server.py"; then
    echo '  Robo Universal is already running in the background.'
    exit 0
  fi
fi
export MVMP_BACKEND="$backend"
nohup "$PY" "$HERE/run_server.py" "$@" >"$state/server.log" 2>&1 < /dev/null &
pid=$!
echo "$pid" > "$pidfile"
sleep 1
if ! kill -0 "$pid" 2>/dev/null; then
  echo "  Startup failed. See $state/server.log" >&2
  rm -f "$pidfile"
  exit 1
fi
echo "  Running in the background (PID $pid). Open http://127.0.0.1:45952"
echo "  Logs: $state/server.log"
echo '  Boot startup requires the systemd user service.'

