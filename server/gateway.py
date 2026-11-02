"""Wildcard gateway edge: one tunnel per (domain, gate-subdomain) serving every
published port through a single DNS record.

cloudflared forwards every name under *.{gate}.{zone} to this host-side
listener, which reads the Host header, resolves the machine + port, and pumps
raw TCP both ways. Pumping the stream (rather than request/response) keeps
HTTP, WebSockets and anything else the guest serves intact, and hands back
friendly "not reachable" pages instead of Cloudflare's raw 502.

Label grammar mirrors GitHub-codespace-style URLs: {name}-{port}, where the
port is always the label's final '-', digits group.
"""
import asyncio, contextlib, json, os, sys, tempfile
from pathlib import Path

from . import config as C

HEAD_CAP = 64 * 1024
GATEWAYS = C.STATE_DIR / 'robo-gateways.json'
MAPPINGS = C.STATE_DIR / 'robo-gate-published.json'

_servers = {}   # gateway id -> asyncio.Server


def read(path, default):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return default


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.robo-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if Path(tmp).exists(): Path(tmp).unlink()


def load_gates(): return read(GATEWAYS, [])


def save_gates(rows): write(GATEWAYS, rows)


def load_maps(): return read(MAPPINGS, [])


def save_maps(rows): write(MAPPINGS, rows)


def gate_fqdn(g):
    """The bare gateway root: for catch-all gateways (gate=='_') that is the
    zone itself; a burrows gate owns *.{gate}.{zone}."""
    if g['gate'] == '_':
        return '*.' + g['zone']
    return g['gate'] + '.' + g['zone']


class ClientClosed(Exception):
    pass


async def _page(status, title, body):
    html = ("<html><head><meta charset='utf-8'><title>{t}</title></head>"
            "<body style=\"font-family:ui-monospace,monospace;background:#090b0b;"
            "color:#e7e7df;display:grid;place-items:center;min-height:100vh;margin:0\">"
            "<div style='text-align:center;max-width:640px;margin:auto'>"
            "<p style=\"font-size:64px;margin:0;color:#48e08a\">{s}</p>"
            "<h1 style=\"margin:8px 0;font-size:20px\">{t}</h1>"
            "<p style=\"color:#8a8f8b;margin:0;font-size:14px\">{b}</p></div></body></html>"
            ).replace('{t}', title).replace('{s}', str(status)).replace('{b}', body)
    # Cloudflare's edge replaces any 5xx body it receives from a tunnel with
    # its own flat "error code: 502" page — so the friendly diagnostics ride
    # a 200 and carry their meaning inside the page.
    head = ("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            "Content-Length: %d\r\nConnection: close\r\n"
            "Robo-Gateway-Status: %d\r\nCache-Control: no-store\r\n\r\n"
            ) % (len(html.encode()), status)
    return head.encode() + html.encode()


async def _head(reader):
    """The request head, bounded; returns (host, leftover_bytes)."""
    buf = bytearray()
    while b'\r\n\r\n' not in buf and len(buf) < HEAD_CAP:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=30)
        except asyncio.TimeoutError as exc:
            raise ClientClosed('timed out waiting for the request') from exc
        if not chunk:
            raise ClientClosed('connection closed before a request arrived')
        buf.extend(chunk)
    idx = buf.find(b'\r\n\r\n')
    if idx < 0:
        raise ClientClosed('request head exceeded the size limit')
    head = buf[:idx].decode('latin1', 'replace')
    lines = head.split('\r\n')
    host = ''
    for line in lines[1:]:
        name, _, value = line.partition(':')
        if name.strip().lower() == 'host':
            host = value.strip().lower().rstrip('.')
            break
    return host, bytes(buf[idx + 4:])


def _match(host):
    """(gateway, mapping) for a request host. Catch-all gateways (gate=='_')
    own every label directly under the zone; burrows gateways own
    *.{gate}.{zone}."""
    gates = [g for g in load_gates() if g.get('phase') in ('active', 'pending')]
    for g in gates:
        suffix = g['zone'] if g['gate'] == '_' else g['gate'] + '.' + g['zone']
        if host.endswith('.' + suffix) and host != suffix:
            label = host[: -len(suffix) - 1]
            if '.' in label:
                continue          # a deeper name belongs to another gateway
            for m in load_maps():
                if (m.get('zone_id') == g['zone_id'] and m.get('gate') == g['gate']
                        and m.get('sub') == label
                        and m.get('phase', 'active') == 'active'):
                    return g, m
            return g, None       # under our wildcard, but not a published label
    return None, None


def _running_rec(mapping):
    from . import manager
    rec = manager.lookup(mapping.get('vm', ''))
    if rec is None or rec.state != 'running':
        return None
    return rec


async def _ensure_forward(mapping, rec):
    """(Re)create the loopback forward when the machine booted after publish."""
    if rec is None or not hasattr(rec.vm, 'host_forward'):
        return
    with contextlib.suppress(Exception):
        await rec.vm.host_forward(mapping['host_port'], mapping['guest_port'])


def _host_of(head):
    """The Host header from a buffered request head ('' when absent)."""
    idx = head.find(b'\r\n\r\n')
    text = head[:idx if idx >= 0 else len(head)].decode('latin1', 'replace')
    lines = text.split('\r\n')
    for line in lines[1:]:
        name, _, value = line.partition(':')
        if name.strip().lower() == 'host':
            return value.strip().lower().rstrip('.')
    return ''


class _Edge(asyncio.Protocol):
    """One side of a relayed connection: the client edge (accepted by the
    broker listener) or the guest edge (opened towards the machine's host
    forward). Queued bytes keep the exchange ordered; the broker hands the
    guest its first response before switching this edge into a raw pump, so
    guest ports that answer nothing produce a friendly page, never a reset.

    Written as an asyncio.Protocol because Python 3.14's loop.create_server
    no longer drives coroutine-function callbacks (uvicorn-style protocol
    factories are the only path that accepts here).
    """

    def __init__(self, broker=None, client=False):
        self.broker = broker
        self.client = client
        self.tr = None
        self.other = None
        self.bq = bytearray()
        self._ev = asyncio.Event()
        self.lost = False
        self.task = None

    def connection_made(self, transport):
        self.tr = transport
        if self.client and self.broker is not None:
            self.task = asyncio.get_running_loop().create_task(self.broker.serve(self))

    def data_received(self, data):
        self.bq.extend(data)
        self._ev.set()

    def eof_received(self):
        self.lost = True
        self._ev.set()
        return False

    def connection_lost(self, exc):
        self.lost = True
        self._ev.set()
        other, self.other = self.other, None
        if other is not None:
            with contextlib.suppress(Exception):
                other.tr.close()

    async def read(self):
        """The peer's next bytes; b'' once the peer is done."""
        while not self.bq:
            if self.lost:
                return b''
            self._ev.clear()
            await self._ev.wait()
        out = bytes(self.bq)
        self.bq.clear()
        return out


class _Broker:
    """Per-request routing for one *.{gate}.{zone} listener."""

    def __init__(self, gate):
        self.gate = gate

    def __call__(self):
        return _Edge(self, client=True)      # protocol factory for create_server

    async def serve(self, ce):
        """Resolve and relay one accepted edge until both sides are done."""
        loop = asyncio.get_running_loop()
        try:
            head = bytearray()
            while b'\r\n\r\n' not in head and len(head) < HEAD_CAP:
                chunk = await asyncio.wait_for(ce.read(), 30)
                if not chunk:
                    if not head:
                        raise ClientClosed('page', 400, 'Bad request',
                                           'No request reached this address.')
                    break
                head.extend(chunk)
            if b'\r\n\r\n' not in head:
                raise ClientClosed('page', 431, 'Request too large',
                                   'The request head exceeded the gateway limit.')
            host = _host_of(head)
            gw, mapping = _match(host)
            if gw is None:
                raise ClientClosed('page', 404, 'Not routed',
                                   'This address is not a Robo Universal gateway.')
            if gw.get('id') != self.gate['id']:
                raise ClientClosed('page', 404, 'Not routed',
                                   'This address is not served by this gateway.')
            if mapping is None:
                raise ClientClosed('page', 404, 'Nothing published here',
                                   '“%s” is not currently published on this gateway.'
                                   % _safe(host))
            rec = _running_rec(mapping)
            if rec is None:
                raise ClientClosed('page', 502, 'Machine not reachable',
                                   'The machine this service belongs to is stopped or deleted.')
            # Forward the head AT ONCE, carrying only the bytes that already
            # arrived past it (an early request body). Waiting here for body
            # bytes that may never come would strand every simple GET.
            idx = head.find(b'\r\n\r\n')
            pending = bytes(head[idx + 4:])
            head = bytes(head[:idx + 4])
            # Lazy host forward: created again after a machine restart, so a
            # publish outlives stops, reboots and server restarts. A port with
            # nothing listening answers as a friendly notice, never a raw 500.
            ge = None
            for attempt in range(2):
                try:
                    # create_connection returns (transport, protocol); the
                    # protocol is the guest edge that carries .tr and .read().
                    _tr, proto = await loop.create_connection(
                        lambda: _Edge(), '127.0.0.1', mapping['host_port'])
                    ge = proto
                    break
                except OSError:
                    if attempt == 0:
                        with contextlib.suppress(Exception):
                            await _ensure_forward(mapping, rec)
            if ge is None:
                raise ClientClosed('page', 502, 'Service unreachable',
                                   '“%s” is published but the machine’s port forward is not answering.'
                                   % _safe(host))
            ge.other = ce; ce.other = ge
            # Everything the client already sent (head + any early body
            # bytes) rides to the guest first.
            ge.tr.write(head + pending)
            # The guest's first response decides the page. A silent guest
            # port gives the friendly notice; a live one starts streaming.
            try:
                first = await asyncio.wait_for(ge.read(), 20)
            except asyncio.TimeoutError:
                raise ClientClosed('page', 504, 'Gateway timed out',
                                   'The published service did not answer in time.')
            if not first:
                raise ClientClosed('page', 502, 'Service unreachable',
                                   '“%s” is published but the service on guest port %s did not respond.'
                                   % (_safe(host), mapping['guest_port']))
            ce.tr.write(first)
            await asyncio.gather(
                self._shovel(ce, ge), self._shovel(ge, ce), return_exceptions=True)
        except ClientClosed as page:
            if page.reply:
                with contextlib.suppress(Exception):
                    ce.tr.write(await _page(*page.reply))
        except Exception as exc:
            # A routing bug must surface as a page, never as a raw reset; its
            # cause lands in the server log where it can be hunted down.
            print('gateway internal error serving:', repr(exc), file=sys.stderr, flush=True)
            with contextlib.suppress(Exception):
                ce.tr.write(await _page(500, 'Gateway error',
                                        'The gateway hit an internal error serving this address.'))
        finally:
            with contextlib.suppress(Exception):
                ce.tr.close()

    async def _shovel(self, src, dst):
        try:
            while True:
                data = await src.read()
                if not data:
                    break
                if dst.tr is None:
                    break
                dst.tr.write(data)
        except Exception:
            pass
        with contextlib.suppress(Exception):
            if dst.tr is not None:
                dst.tr.write_eof()


def _safe(text):
    return ''.join(c for c in str(text) if c.isprintable())[:120]


class ClientClosed(Exception):
    def __init__(self, *page):
        super().__init__(page[0] if page else '')
        self.reply = page[1:] or None


async def start_listeners():
    """Rebind every configured gateway's broker (idempotent across restarts)."""
    loop = asyncio.get_running_loop()
    for g in load_gates():
        if g.get('phase') not in ('active', 'pending'):
            continue
        if g['id'] in _servers:
            continue
        srv = await loop.create_server(_Broker(g), '127.0.0.1', g.get('broker_port') or 0)
        _servers[g['id']] = srv


async def stop_listeners():
    for srv in _servers.values():
        with contextlib.suppress(Exception):
            srv.close()
            await srv.wait_closed()
    _servers.clear()


async def stop_listener(gate, zone, zone_id):
    """Close one gateway's broker listener (by identity, not by content)."""
    for gid, srv in list(_servers.items()):
        row = next((g for g in load_gates() if g['id'] == gid), None)
        if row and (row.get('gate') == gate and row.get('zone') == zone
                    and row.get('zone_id') == zone_id):
            with contextlib.suppress(Exception):
                srv.close()
                await srv.wait_closed()
            _servers.pop(gid, None)
