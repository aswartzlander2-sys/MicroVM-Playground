#!/usr/bin/env python3
"""Bake the golden guest image.

Boots the stock Debian cloud image once with a cloud-init seed that installs a
real userland and sets up autologin on the serial console. The result is the
image every playground is cloned from. cloud-init stays ENABLED in the golden:
each machine attaches its own tiny seed (new instance-id, its own hostname),
so every clone personalises itself on first boot while later boots of the same
machine stay cached no-ops.

    python tools/bake.py                     # fetch + bake, defaults
    python tools/bake.py --packages "git curl jq"
    python tools/bake.py --disk-gb 12 --force
    python tools/bake.py --seed-only --seed-path /tmp/seed.iso --hostname foo

This is the slow step: under TCG software emulation, apt inside an emulated
CPU takes a while. It happens once.
"""
import argparse
import io
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import config as C          # noqa: E402
from server.qemu import find_binary     # noqa: E402

DEBIAN_URL = ("https://cloud.debian.org/images/cloud/bookworm/latest/"
              "debian-12-genericcloud-amd64.qcow2")

# Kept deliberately lean: every package is installed inside an emulated CPU, so
# the bake time is roughly proportional to this list. build-essential and
# python3-pip are the obvious additions once you have a working image --
#   python tools/bake.py --force --packages "... build-essential"
DEFAULT_PACKAGES = ("git curl wget neofetch htop vim nano tmux jq unzip zip "
                    "ca-certificates python3 less tree file "
                    "iputils-ping dnsutils net-tools")

# Arch publishes a cloud image with cloud-init, so the same bake works -- only
# the package names differ. neofetch was dropped from the Arch repositories
# after upstream archived it; fastfetch is the maintained replacement.
ARCH_URL = ("https://geo.mirror.pkgbuild.com/images/latest/"
            "Arch-Linux-x86_64-cloudimg.qcow2")
ARCH_PACKAGES = ("git curl wget fastfetch htop vim nano tmux jq unzip zip "
                 "ca-certificates python less tree file "
                 "iputils bind inetutils net-tools base-devel")

DISTROS = {
    "debian": {"url": DEBIAN_URL, "packages": DEFAULT_PACKAGES,
               "base": "debian-base.qcow2", "out": None},
    "arch":   {"url": ARCH_URL, "packages": ARCH_PACKAGES,
               "base": "arch-base.qcow2", "out": "arch.qcow2"},
}

# cloud-init's NoCloud datasource reads these two files off a volume labelled
# CIDATA. %I below is a systemd specifier, not a Python placeholder.
USER_DATA = """#cloud-config
hostname: __HOSTNAME__
manage_etc_hosts: true
disable_root: false
ssh_pwauth: true

users:
  - name: root
    lock_passwd: false

chpasswd:
  expire: false
  users:
    - name: root
      password: __GUEST_PASSWORD__
      type: text

write_files:
  - path: /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
    permissions: '0644'
    content: |
      [Service]
      ExecStart=
      ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud 115200,38400,9600 %I xterm-256color
      Type=idle

  - path: /etc/profile.d/00-mvmp.sh
    permissions: '0644'
    content: |
      export LANG=C.UTF-8
      export LC_ALL=C.UTF-8
      export TERM=xterm-256color
      export COLORTERM=truecolor
      export PAGER=less
      export LESS="-R"
      export PS1='\\[\\e[1;38;5;48m\\]\\u@\\h\\[\\e[0m\\]:\\[\\e[1;38;5;75m\\]\\w\\[\\e[0m\\]# '
      alias ls='ls --color=auto'
      alias grep='grep --color=auto'
      alias ll='ls -alF --color=auto'
      # A serial line carries no SIGWINCH: ask the terminal where the cursor
      # lands after a huge move, which reveals the real window size.
      mvmp_resize() {
        local old rows cols
        old=$(stty -g 2>/dev/null) || return 0
        stty raw -echo min 0 time 10 2>/dev/null
        printf '\\033[s\\033[999;999H\\033[6n' > /dev/tty
        IFS='[;R' read -r _ rows cols < /dev/tty
        printf '\\033[u' > /dev/tty
        stty "$old" 2>/dev/null
        case "$rows$cols" in *[!0-9]*|"") return 0 ;; esac
        stty rows "$rows" cols "$cols" 2>/dev/null
      }
      mvmp_resize

runcmd:
__MOTD__
  - [ systemctl, daemon-reload ]
  - [ systemctl, enable, "serial-getty@ttyS0.service" ]
  - [ sed, -i, 's/^#*PrintMotd.*/PrintMotd yes/', /etc/ssh/sshd_config ]
__PKGLINES__
  - [ systemctl, mask, "systemd-networkd-wait-online.service" ]
  - [ sh, -c, "echo BAKE-COMPLETE > /dev/ttyS0" ]

power_state:
  mode: poweroff
  timeout: 60
  condition: true
"""

# First boot of a clone: set the machine's own hostname and root credentials,
# nothing else. The golden already carries the full toolset -- no packages, no
# MOTD rewrite, and deliberately NO power_state: a bake powers off on purpose
# when done, but a clone must stay up after cloud-init finishes.
VM_USER_DATA = """#cloud-config
hostname: __HOSTNAME__
manage_etc_hosts: true
disable_root: false
ssh_pwauth: true

users:
  - name: root
    lock_passwd: false

chpasswd:
  expire: false
  users:
    - name: root
      password: __GUEST_PASSWORD__
      type: text
"""

# Fresh instance-id per machine: it is what makes cloud-init treat a clone's
# first boot as real firstboot (hostname, credentials) without re-running on
# any later boot of the same machine.
META_DATA = "instance-id: __INSTANCE_ID__\nlocal-hostname: __HOSTNAME__\n"

# /etc/motd is displayed with `cat`, which does not interpret escapes -- so the
# escapes have to be real bytes in the file. printf writes them; a cloud-init
# write_files block would store the literal text "\e[38;5;51m" instead.
_MOTD_ROWS = [
    r"",
    r"  \033[38;5;51m╭────────────────────────────────────────────────╮\033[0m",
    r"  \033[38;5;51m│\033[0m\033[1;97m   microvm playground · QEMU · disposable       \033[0m\033[38;5;51m│\033[0m",
    r"  \033[38;5;51m╰────────────────────────────────────────────────╯\033[0m",
    r"",
    r"   \033[90mDebian 12 · UTF-8 · truecolor · full outbound internet\033[0m",
    r"   \033[33m⚠\033[0m  \033[90mThis VM and its disk are destroyed on shutdown.\033[0m",
    r"",
]


def _motd_commands() -> str:
    """runcmd lines that build /etc/motd one printf at a time."""
    out = []
    for i, row in enumerate(_MOTD_ROWS):
        redirect = ">" if i == 0 else ">>"
        out.append("  - printf '%s\\n' %s /etc/motd" % (row, redirect))
    # Debian's dynamic MOTD would print on top of ours.
    out.append("  - rm -f /etc/update-motd.d/10-uname")
    return "\n".join(out)


def log(msg, colour="36"):
    print("\033[1;%sm==>\033[0m %s" % (colour, msg), flush=True)


def _package_runcmd(distro: str, packages: str) -> list[str]:
    """The package-install runcmd lines; empty for a seed that installs nothing."""
    if not packages:
        return []
    if distro == "arch":
        return ["  - pacman -Sy --noconfirm",
                "  - pacman -S --noconfirm --needed " + packages,
                "  - [ pacman, -Sc, --noconfirm ]"]
    return ["  - export DEBIAN_FRONTEND=noninteractive; apt-get update -q",
            "  - export DEBIAN_FRONTEND=noninteractive; apt-get install -y -q "
            "--no-install-recommends " + packages,
            "  - [ apt-get, clean ]"]


def build_seed(path: Path, packages: str, hostname: str = "playground",
               distro: str = "debian", instance_id: str = "mvmp-bake",
               mode: str = "bake") -> None:
    """A tiny ISO9660 volume labelled CIDATA -- cloud-init's NoCloud source.

    mode="bake"  full template: packages, MOTD, completion marker, power off.
    mode="clone" minimal template: hostname + credentials only, stays up.
    """
    import pycdlib

    # Each newly baked template gets a unique password, stored owner-only.
    # All VMs cloned from that template share it until changed in the guest.
    password = secrets.token_urlsafe(24)
    password_file = path.parent / (path.stem + "-initial-password")
    fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f: f.write(password + "\n")
    print("Initial guest password saved in " + str(password_file) + " (mode 600)")
    template = VM_USER_DATA if mode == "clone" else USER_DATA
    user = (template
            .replace("__HOSTNAME__", hostname)
            .replace("__GUEST_PASSWORD__", password)
            .replace("__MOTD__", _motd_commands())
            .replace("__PKGLINES__", "\n".join(_package_runcmd(distro, packages)))
            .encode("utf-8"))
    meta = (META_DATA
            .replace("__HOSTNAME__", hostname)
            .replace("__INSTANCE_ID__", instance_id)
            .encode("utf-8"))

    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="CIDATA")
    for blob, iso_name, name in ((user, "/USERDATA.;1", "user-data"),
                                 (meta, "/METADATA.;1", "meta-data")):
        iso.add_fp(io.BytesIO(blob), len(blob), iso_name,
                   rr_name=name, joliet_path="/" + name)
    iso.write(str(path))
    os.chmod(path, 0o600)
    iso.close()


def fetch_base(dest: Path, url: str) -> None:
    if not dest.exists():
        log("downloading the Debian cloud image (~350 MB)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Per-process temp name: two concurrent first-time bakes must not race.
        tmp = dest.parent / ("%s.%d.part" % (dest.name, os.getpid()))
        with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        tmp.replace(dest)
    log("base image ready at %s (%.0f MB)" % (dest, dest.stat().st_size / 2**20))


def main() -> int:
    ap = argparse.ArgumentParser(description="bake the golden playground image")
    ap.add_argument("--hostname", default="playground")
    ap.add_argument("--disk-gb", type=int, default=8)
    ap.add_argument("--mem-mib", type=int, default=2048)
    ap.add_argument("--vcpus", type=int, default=max(2, min(4, os.cpu_count() or 2)))
    ap.add_argument("--distro", choices=sorted(DISTROS), default="debian",
                    help="debian is the default image; arch is offered alongside it")
    ap.add_argument("--packages", default=None)
    ap.add_argument("--url", default=None)
    ap.add_argument("--seed-only", action="store_true",
                    help="write a per-VM seed ISO and exit -- no bake, no QEMU")
    ap.add_argument("--seed-path", default=None,
                    help="with --seed-only: where to write the seed ISO")
    ap.add_argument("--instance-id", default="mvmp-bake",
                    help="cloud-init instance-id; per-VM seeds use the VM id")
    ap.add_argument("--mode", choices=("bake", "clone"), default="clone",
                    help="clone seeds are minimal (stay up); bake seeds power off when done")
    ap.add_argument("--out", default=None,
                    help="output filename inside the images directory")
    ap.add_argument("--timeout", type=int, default=5400)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    import re
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", args.hostname):
        ap.error("hostname must be a lowercase DNS label (1–32 letters, numbers and hyphens)")
    spec = DISTROS[args.distro]
    if args.packages is None:
        args.packages = spec["packages"]

    if args.seed_only:
        # Per-VM seed: no packages (the golden already has the toolset), the
        # machine's own hostname and a fresh instance-id.
        path = Path(args.seed_path) if args.seed_path else C.IMAGES_DIR / "seed-custom.iso"
        path.parent.mkdir(parents=True, exist_ok=True)
        build_seed(path, "", args.hostname, args.distro, instance_id=args.instance_id, mode=args.mode)
        log("seed written to %s" % path, "32")
        return 0

    if args.url is None:
        args.url = spec["url"]

    qemu = find_binary(C.QEMU_SYSTEM)
    qemu_img = find_binary("qemu-img")
    if not qemu or not qemu_img:
        print("QEMU not found. Run tools/get-qemu.ps1 first.", file=sys.stderr)
        return 1

    # The downloaded base lives in a shared cache (MVMP_BASE_CACHE): one copy
    # serves every VM; only the golden images are per-VM. golden/seed stay in
    # IMAGES_DIR, which the manager pins to each VM's own directory.
    C.BASE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    C.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    base = C.BASE_CACHE_DIR / spec["base"]
    # Non-default distros are written beside the golden image under their own
    # name. The catalogue lists every qcow2 it finds, so a baked arch.qcow2
    # simply appears in the OS dropdown with no further wiring.
    out_name = args.out or spec["out"]
    golden = (C.IMAGES_DIR / out_name) if out_name else C.BASE_IMAGE
    seed = C.IMAGES_DIR / ("seed-%s.iso" % args.distro)
    serial_log = C.IMAGES_DIR / ("bake-%s-serial.log" % args.distro)

    if golden.exists() and not args.force:
        log("%s image already built at %s (use --force to rebuild)"
            % (args.distro, golden), "32")
        return 0

    fetch_base(base, args.url)

    log("preparing a %d GB working copy" % args.disk_gb)
    # Baked beside the final name and renamed only once the guest reports
    # BAKE-COMPLETE: a bake that is cancelled, crashes or is still running never
    # leaves a half-built golden where every new machine would clone it.
    final = golden
    golden = final.with_name(final.name + ".partial")
    golden.unlink(missing_ok=True)
    shutil.copyfile(base, golden)
    subprocess.run([qemu_img, "resize", "-q", str(golden), "%dG" % args.disk_gb], check=True)

    log("building the cloud-init seed")
    build_seed(seed, args.packages, args.hostname, args.distro)

    log("baking: booting the guest to install %d packages" % len(args.packages.split()))
    log("this is the slow part -- TCG emulates every instruction. Grab a coffee.", "33")

    argv = [
        qemu, "-display", "none", "-machine", "q35",
        "-accel", "tcg,thread=multi" if args.vcpus > 1 else "tcg",
        "-cpu", "max", "-smp", str(args.vcpus), "-m", str(args.mem_mib),
        "-drive", "file=%s,if=virtio,format=qcow2,cache=writeback" % golden,
        "-drive", "file=%s,if=virtio,format=raw,readonly=on" % seed,
        "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0",
        "-object", "rng-builtin,id=rng0", "-device", "virtio-rng-pci,rng=rng0",
        "-serial", "file:%s" % serial_log,
        "-no-reboot",
    ]

    t0 = time.monotonic()
    proc = subprocess.Popen(argv)
    last = 0
    try:
        while proc.poll() is None:
            time.sleep(5)
            elapsed = time.monotonic() - t0
            if elapsed > args.timeout:
                proc.kill()
                print("\nbake timed out after %d s" % args.timeout, file=sys.stderr)
                return 1
            if serial_log.exists():
                size = serial_log.stat().st_size
                if size != last:
                    last = size
                    tail = serial_log.read_bytes()[-400:].decode("utf-8", "replace")
                    marker = [l for l in tail.splitlines() if l.strip()]
                    note = marker[-1][:90] if marker else ""
                    print("\r    %4ds  %6d KB  %s" % (elapsed, size // 1024, note.ljust(90)[:90]),
                          end="", flush=True)
    except KeyboardInterrupt:
        proc.kill()
        return 130

    print()
    took = time.monotonic() - t0
    body = serial_log.read_bytes().decode("utf-8", "replace") if serial_log.exists() else ""
    if "BAKE-COMPLETE" not in body:
        print("\033[1;31mbake did not report completion\033[0m -- see %s" % serial_log,
              file=sys.stderr)
        return 1

    seed.unlink(missing_ok=True)
    os.replace(golden, final)
    golden = final
    log("golden image ready in %.0f s: %s (%.0f MB on disk)"
        % (took, golden, golden.stat().st_size / 2**20), "32")
    return 0


if __name__ == "__main__":
    sys.exit(main())
