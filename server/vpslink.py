"""VPS relay links.

A gate subdomain ({gate}.{zone}) can only publish when a VPS relay carries
its TLS: Cloudflare's free edge certificate covers one wildcard level
(*.{zone}), not two. The relay runs on the user's VPS (Caddy terminating
TLS with its own Let's Encrypt certificates) and dials back into this
dashboard through an `ssh -R` tunnel the link command opens and keeps alive.

This module only owns the link STATE (robo-vps-links.json) and the small
surface the dashboard API needs. The heavy lifting lives in
tools/robo-vps-link, run by the user from a shell.
"""
import json
import secrets
import time
import uuid
from pathlib import Path

from . import config as C
from pathlib import Path

VPS_LINKS = C.STATE_DIR / 'robo-vps-links.json'
COMMAND_NAME = 'robo-vps-link'


def load():
    try:
        return json.loads(VPS_LINKS.read_text())
    except (OSError, ValueError):
        return []


def save(rows):
    import os
    VPS_LINKS.parent.mkdir(parents=True, exist_ok=True)
    tmp = VPS_LINKS.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(rows, indent=1))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(VPS_LINKS)


def linked(zone_id):
    """True while a working relay link covers this zone."""
    return any(zone_id in (r.get('zones') or [])
               for r in load() if r.get('phase') == 'active')


def new_draft(host, user, ssh_port, zone_ids):
    """Register a link the user still has to run the command for."""
    if not host:
        raise ValueError('VPS host or IP is required.')
    if not user:
        user = 'root'
    try:
        ssh_port = int(ssh_port or 22)
    except (TypeError, ValueError):
        ssh_port = 22
    zone_ids = [z for z in (zone_ids or []) if z]
    rows = load()
    same = next((r for r in rows
                 if r.get('host') == host and r.get('user') == user
                 and r.get('phase') in ('draft', 'active')), None)
    if same is not None:
        same['zones'] = sorted(set((same.get('zones') or []) + zone_ids))
        if same.get('phase') != 'active':
            same['note'] = ''
        save(rows)
        return same
    row = {'id': uuid.uuid4().hex[:12],
           'host': host, 'user': user, 'ssh_port': ssh_port,
           'zones': zone_ids, 'gates': [],
           'phase': 'draft', 'created': int(time.time()),
           'token': secrets.token_urlsafe(18), 'note': ''}
    rows.append(row)
    save(rows)
    return row


def refresh(row_id, patch):
    """Apply tool-written state changes (phase transitions, notes)."""
    rows = load()
    row = next((r for r in rows if r.get('id') == row_id), None)
    if row is not None:
        row.update(patch or {})
        save(rows)
    return row


def remove(row_id):
    rows = [r for r in load() if r.get('id') != row_id]
    save(rows)


def view():
    """Dashboard-facing listing: no secrets beyond the host shape."""
    now = int(time.time())
    out = []
    for r in load():
        out.append({'id': r.get('id'), 'host': r.get('host'), 'user': r.get('user'),
                    'ssh_port': r.get('ssh_port'), 'zones': r.get('zones'),
                    'gates': r.get('gates'), 'phase': r.get('phase'),
                    'created': r.get('created'),
                    'note': r.get('note') or '',
                    'age': now - (r.get('created') or now)})
    return out


def command_hint(row, zone_name=None, gate=None):
    """The one shell command for the user's own terminal (runs on this host)."""
    import shlex
    parts = ['python3 tools/robo-vps-link link', shlex.quote(str(row.get('host') or '')),
             '--user', shlex.quote(str(row.get('user') or 'root'))]
    if (row.get('ssh_port') or 22) != 22:
        parts += ['--port', str(row['ssh_port'])]
    if zone_name:
        parts += ['--zone', shlex.quote(zone_name),
                  '--gate', shlex.quote(gate or '')]
    return ' '.join(parts)


def command(row):
    """The one shell command the user runs on this host to wire the link."""
    return ('cd %s && ./.venv/bin/python tools/%s wire %s'
            % (Path(__file__).resolve().parent.parent, COMMAND_NAME, row['id']))
