"""Self-update from GitHub: compare HEAD with origin/main, pull, self-restart.

Only additive and fail-safe: applying refuses when the working tree is dirty
or holds unpushed local commits, and the pull is --ff-only, so nothing is
ever destroyed by an update.
"""
import asyncio, os, shlex, subprocess, sys
from pathlib import Path

from . import config as C

ROOT = Path(__file__).resolve().parent.parent


def _git(*args):
    proc = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), (proc.stderr or proc.stdout).strip()[:300]))
    return proc.stdout.strip()


def current_commit():
    line = _git("log", "-1", "--format=%H%x1f%h%x1f%s")
    sha, short, subject = line.split("\x1f")
    return {"sha": sha, "short": short, "subject": subject}


async def status():
    """What the dashboard runs vs what GitHub offers."""
    try:
        info = await asyncio.to_thread(current_commit)
    except RuntimeError as exc:
        return {"available": False, "error": str(exc)[:160]}
    remote = None
    behind = 0
    error = None
    try:
        def fetch():
            _git("fetch", "-q", "origin", "main")
            return _git("rev-parse", "--short=8", "origin/main")
        remote = await asyncio.to_thread(fetch)
        behind = int(await asyncio.to_thread(
            _git, "rev-list", "--count", "HEAD..origin/main"))
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        error = str(exc)[:160]
    # .git only exists when the checkout is the repo (not a docker copy).
    dirty = ""
    if (ROOT / ".git").exists():
        dirty = await asyncio.to_thread(_git, "status", "--porcelain")
    return {"available": behind > 0, "behind": behind, "current": info,
            "remote_short": remote, "dirty": bool(dirty.strip()), "error": error}


async def apply():
    """Fast-forward to origin/main. Returns True when new code was pulled."""
    def do():
        if _git("status", "--porcelain"):
            raise RuntimeError("The dashboard source has local changes; refusing to overwrite them. Commit or download them first.")
        _git("fetch", "origin", "main")
        if _git("rev-parse", "HEAD") == _git("rev-parse", "origin/main"):
            return False
        _git("pull", "--ff-only", "origin", "main")
        return True
    return await asyncio.to_thread(do)


async def self_restart(delay: float = 2.0):
    """Spawn a fresh copy of this server after this process is gone.

    The state-directory lock is released by our own exit, so the helper waits
    a beat before starting the replacement. Returns the helper's handle.
    """
    cmd = " ".join(shlex.quote(a) for a in sys.argv)
    env = "PYTHONDONTWRITEBYTECODE=1 "
    logfile = C.STATE_DIR / "server.log"
    helper = await asyncio.create_subprocess_exec(
        "/bin/bash", "-c",
        f"sleep {delay}; cd {shlex.quote(str(ROOT))}; "
        f"{env}setsid nohup {shlex.quote(sys.executable)} {cmd} "
        f">> {shlex.quote(str(logfile))} 2>&1 < /dev/null & disown",
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return helper
