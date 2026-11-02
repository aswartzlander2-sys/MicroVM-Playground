"""Direct Cloudflare Tunnel publication. No Aegis dependency.

Routes are explicitly public. State is journaled before API side effects. On
restart stale routes are cleaned, not automatically rebound to reused ports.
"""
import asyncio, contextlib, json, os, re, shutil, socket, sys, tempfile, time, uuid
from pathlib import Path
import httpx
from . import config as C
from . import gateway

STATE=C.STATE_DIR/'robo-direct-published.json'
CONFIG=C.STATE_DIR/'robo-cloudflare.json'
lock=asyncio.Lock()
processes={}
class PublishError(Exception): pass

def read(path,default):
    try: return json.loads(path.read_text())
    except (OSError,ValueError): return default

def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix='.robo-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as f: json.dump(data,f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def load(): return read(STATE,[])
def save(rows): write(STATE,rows)
def cfg():
    c=read(CONFIG,{})
    if not c.get('token'): raise PublishError('Connect a Cloudflare API token first.')
    return c

def config_zones(c):
    """Every domain the saved token can publish under.

    New configs hold 'zones'; single-zone configs ('zone': {...}) are migrated
    here rather than on disk, so upgrading is additive and reversible.
    """
    if not isinstance(c,dict): return []
    zs=c.get('zones')
    if isinstance(zs,list) and zs:
        return [z for z in zs if isinstance(z,dict) and z.get('id') and z.get('name')]
    z=c.get('zone')
    return [z] if isinstance(z,dict) and z.get('id') and z.get('name') else []

async def api(method,path,payload=None,params=None,token=None,allow_missing=False):
    secret=token or cfg()['token']
    try:
        async with httpx.AsyncClient(base_url='https://api.cloudflare.com/client/v4',timeout=25,follow_redirects=False) as client:
            r=await client.request(method,path,headers={'Authorization':'Bearer '+secret},json=payload,params=params)
        if allow_missing and r.status_code==404: return None
        try: body=r.json()
        except ValueError: body={}
        if r.status_code>=400 or not body.get('success'):
            errs=body.get('errors') or []
            msg='; '.join(str(e.get('message','Cloudflare API error'))[:150] for e in errs[:2])
            raise PublishError('Cloudflare API request failed'+(': '+msg if msg else ' (HTTP %s)'%r.status_code))
        return body.get('result')
    except httpx.HTTPError: raise PublishError('Cannot reach Cloudflare. Check the host network and try again.')

async def zones(token):
    # Copy/paste almost always adds a trailing space or newline; tripping the
    # format check on that made perfectly valid keys look broken.
    token = token.strip() if isinstance(token, str) else token
    if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,500}',token):
        raise PublishError('Enter a valid Cloudflare API token (40+ characters, letters/digits/-/_).')
    result=[]
    # Zone API is paginated; cap protects against unexpectedly large accounts.
    try:
        for page in range(1,11):
            rows=await api('GET','/zones',params={'page':page,'per_page':50,'status':'active'},token=token)
            if not isinstance(rows,list): raise PublishError('Unexpected response from Cloudflare zones API.')
            result.extend({'id':z['id'],'name':z['name'],'account_id':z['account']['id']} for z in rows if z.get('id') and z.get('name') and z.get('account',{}).get('id'))
            if len(rows)<50: break
    except PublishError as exc:
        msg=str(exc)
        if 'failed' in msg and ('Authentication' in msg or 'Invalid request headers' in msg):
            raise PublishError('Cloudflare rejected this token. Verify it is an API Token (not the Global API Key) and is active.')
        if 'failed' in msg and 'Authentication error' in msg:
            raise PublishError('Cloudflare rejected this token as invalid or expired.')
        raise
    return result

async def connect(token,zone_id):
    async with lock:
        if load(): raise PublishError('Remove all published ports before replacing the Cloudflare connection.')
        token = token.strip() if isinstance(token,str) else token
        options=await zones(token)
        zone=next((z for z in options if z['id']==zone_id),None)
        if not zone: raise PublishError('Zone not found or token lacks Zone Read permission.')
        # Multi-domain connection: every zone the token can read stays
        # available in the publishing UI, with the chosen one as the default.
        # Legacy single-zone configs are still honored by config_zones() even
        # if this write never happens.
        ordered=[zone]+[z for z in options if z['id']!=zone_id]
        write(CONFIG,{'token':token,'zones':ordered,'zone':zone})
        return {'connected':True,'zones':ordered,'zone':zone}

async def disconnect():
    async with lock:
        if load(): raise PublishError('Remove all published ports before disconnecting.')
        CONFIG.unlink(missing_ok=True)

def allocate():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); return s.getsockname()[1]

async def state():
    c=read(CONFIG,{})
    zs=[dict(z,wildcard=await zone_wildcard(z)) for z in config_zones(c)]
    return {'connected':bool(c.get('token')), 'zone':(zs[0] if zs else None), 'zones':zs, 'cloudflared':bool(shutil.which('cloudflared')), 'pending_cleanup':sum(1 for r in load() if r.get('phase')!='active')}

_DOH='https://cloudflare-dns.com/dns-query'

async def doh_answers(name):
    """Public A answers for a name via 1.1.1.1's JSON DoH endpoint."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r=await client.get(_DOH,params={'name':name,'type':'A'},headers={'accept':'application/dns-json'})
        body=r.json()
        return [a.get('data') for a in body.get('Answer',[]) if a.get('type')==1 and a.get('data')]
    except Exception:
        return []

def _ensure_cloudflared():
    """Best-effort install of cloudflared when the host lacks it.

    Order: existing binary on PATH, the user-local xdg binary, then a one-time
    download from Cloudflare's GitHub releases into ~/.local/bin. Nothing on
    the system is overwritten; if every attempt fails the publish errors and
    the reason lands on the request.
    """
    found=shutil.which('cloudflared')
    if found: return found
    local=Path.home()/'.local'/ 'bin' / 'cloudflared'
    if local.exists() and os.access(local,os.X_OK): return str(local)
    machine={'x86_64':'amd64','aarch64':'arm64','armv7l':'arm','armv6':'arm'}.get(
        os.uname().machine,'amd64')
    url='https://github.com/cloudflare/cloudflared/releases/latest/download/'\
        'cloudflared-linux-%s'%machine
    try:
        local.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with httpx.stream('GET',url,timeout=180,follow_redirects=True) as resp:
            resp.raise_for_status()
            tmp=local.with_suffix('.part')
            with open(tmp,'wb') as f:
                for chunk in resp.iter_bytes(1<<16): f.write(chunk)
        os.chmod(tmp,0o755)
        os.replace(tmp,local)
        return str(local)
    except Exception:
        with contextlib.suppress(OSError): (local.parent / 'cloudflared.part').unlink()
        return None

async def _wild_answers(zone):
    """Answers a WILDCARD/resolver would give for any name in this zone.

    A random never-published label is resolved publicly: whatever comes back
    is what the catch-all record (Aegis relay, NAT, parked host) serves, and
    exactly what a freshly published name must NOT resolve to for 'ready' to
    be truthful.
    """
    probe='wildcheck-'+uuid.uuid4().hex[:12]+'.'+zone['name']
    return frozenset(a for a in await doh_answers(probe) if a)

_wild_cache={}  # zone_id -> (timestamp, wildcard exists)

async def zone_wildcard(zone):
    """True when this zone has a catch-all record answering unknown names."""
    hit=_wild_cache.get(zone['id'])
    if hit and time.time()-hit[0]<600: return hit[1]
    try:
        answers=await _wild_answers(zone)
        flag=bool(answers)
    except Exception:
        flag=hit[1] if hit else False
    _wild_cache[zone['id']]=(time.time(),flag)
    return flag

async def wait_dns(hostname,forbidden=frozenset(),timeout=25):
    """Resolve the new CNAME from the host before calling it 'ready'.

    Between the Cloudflare API returning 2xx and the record actually being
    served, a fresh lookup returns NXDOMAIN -- exactly what a browser shows
    as DNS_PROBE_FINISHED_NXDOMAIN when a link is opened too early. Polling
    here (and reporting 'provisioning' until it resolves) turns that dead
    window into visible status instead of a broken link.

    'forbidden' is the zone catch-all's answer set: a wildcard domain makes
    every name resolve instantly, so accepting any answer would mistake the
    other service (e.g. Aegis relay 404) for a working route. The name only
    counts as ready when it answers with something the wildcard cannot
    produce -- or with anything at all when the zone has no catch-all.
    """
    loop=asyncio.get_running_loop()
    deadline=loop.time()+timeout
    while loop.time()<deadline:
        addrs=set()
        try:
            info=await loop.getaddrinfo(hostname,443)
            addrs|={i[4][0] for i in info}
        except (socket.gaierror,OSError):
            pass
        addrs|=set(await doh_answers(hostname))
        usable={a for a in addrs if a and (not forbidden or a not in forbidden)}
        if usable: return True
        await asyncio.sleep(2.5)
    return False

async def create(rec,guest_port,sub,access,zone_id=None):
    if not 1<=guest_port<=65535: raise PublishError('Port must be between 1 and 65535')
    if not sub:
        # Leaving the label empty is not an error: the port itself is a fine,
        # collision-free name (port-3000.robo-universe.top).
        sub = 'port-%d' % guest_port
    sub = sub.strip().lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',sub): raise PublishError('Name must be a valid single DNS label (1–63 characters).')
    if access!='public': raise PublishError('Private access is not implemented; no route was published.')
    if rec.state!='running' or not hasattr(rec.vm,'host_forward'): raise PublishError('A running QEMU machine is required.')
    binary=shutil.which('cloudflared')
    if not binary:
        binary=_ensure_cloudflared()
        if not binary: raise PublishError('cloudflared could not be installed automatically. Put it on PATH and retry.')
    if not sys.platform.startswith('linux'): raise PublishError('Direct publishing currently requires Linux.')
    async with lock:
        if rec.state!='running': raise PublishError('The machine is no longer running.')
        c=cfg(); zones_all=config_zones(c)
        if not zones_all: raise PublishError('This connection has no usable domain. Reconnect Cloudflare.')
        zone=next((z for z in zones_all if z.get('id')==zone_id),None)
        if zone is None and zone_id:
            raise PublishError('That domain is not connected to this dashboard. Reconnect Cloudflare or pick a connected domain.')
        # Default: the connection's primary (first-chosen) domain.
        if zone is None: zone=zones_all[0]
        rows=load()
        if any(r['vm']==rec.id and r['guest_port']==guest_port for r in rows): raise PublishError('That guest port is already published or awaiting cleanup.')
        hostname=sub+'.'+zone['name']
        # Uniqueness is per-hostname, so the same label cannot be double-booked
        # within a zone or clash with a sibling zone of the same account.
        if any(r.get('hostname')==hostname for r in rows): raise PublishError('That DNS name is already published; choose another subdomain.')
        hostport=allocate(); identifier=uuid.uuid4().hex
        row={'id':identifier,'vm':rec.id,'guest_port':guest_port,'host_port':hostport,'sub':sub,'hostname':hostname,'access':'public','zone_id':zone['id'],'account_id':zone['account_id'],'name':'robo-'+identifier,'phase':'pending'}
        rows.append(row);save(rows)
        try:
            # Catch-all awareness: a zone answering unknown names (e.g. the
            # Aegis relay wildcard) makes every name resolve BEFORE this
            # record exists, so naive readiness checks go false-ready and
            # the browser hits the catch-all instead of this machine.
            wilds=await api('GET',f"/zones/{zone['id']}/dns_records",params={'name':'*.'+zone['name'],'per_page':5}) or []
            wildcard=bool(wilds)
            row['wildcard']=wildcard;save(rows)
            forbidden=set()
            if wildcard:
                for w in wilds:
                    if not w.get('proxied') and w.get('type')=='A' and w.get('content'): forbidden.add(w['content'])
                forbidden|=set(await _wild_answers(zone))
                row['wild_answers']=sorted(forbidden);save(rows)
        except Exception:
            try: await cleanup(row,rec)
            except Exception: pass
            raise
        try:
            # Refuse to replace existing records, even records with a different type.
            existing=await api('GET',f"/zones/{zone['id']}/dns_records",params={'name':hostname})
            if existing: raise PublishError('That DNS name already exists; choose another name.')
            await rec.vm.host_forward(hostport,guest_port)
            tunnel=await api('POST',f"/accounts/{row['account_id']}/cfd_tunnel",{'name':row['name'],'config_src':'cloudflare'})
            row['tunnel_id']=tunnel['id'];save(rows)
            base=f"/accounts/{row['account_id']}/cfd_tunnel/{row['tunnel_id']}"
            await api('PUT',base+'/configurations',{'config':{'ingress':[{'hostname':hostname,'service':f'http://127.0.0.1:{hostport}'},{'service':'http_status:404'}]}})
            connector_token=await api('GET',base+'/token')
            env=os.environ.copy();env['TUNNEL_TOKEN']=connector_token
            wrapper=str(Path(__file__).resolve().parent.parent/'tools'/'cloudflared_child.py')
            proc=await asyncio.create_subprocess_exec(sys.executable,wrapper,binary,env=env,stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            processes[row['id']]=proc
            await asyncio.sleep(1)
            if proc.returncode is not None: raise PublishError('cloudflared exited immediately. Check its installation and Cloudflare tunnel configuration.')
            dns=await api('POST',f"/zones/{zone['id']}/dns_records",{'type':'CNAME','name':hostname,'content':row['tunnel_id']+'.cfargotunnel.com','proxied':True,'ttl':1})
            row['dns_id']=dns['id'];save(rows)
            # Prove the record answers with OUR route before advertising the
            # link as ready; otherwise the browser reports NXDOMAIN -- or,
            # before this check, silently landed on a catch-all 404.
            dns_ready=await wait_dns(hostname,forbidden=frozenset(forbidden),timeout=(60 if wildcard else 25))
            row.update(dns_checked=dns_ready,phase='active');save(rows)
            return {'vm':rec.id,'guest_port':guest_port,'host_port':hostport,'tunnel':{'url':'https://'+hostname,'domain':zone['name'],'sub':sub,'access':'public','wildcard':wildcard,'status':'ready' if dns_ready else 'provisioning'}}
        except Exception:
            try: await cleanup(row,rec)
            except Exception: pass
            raise

async def cleanup(row,rec=None):
    p=processes.pop(row.get('id'),None)
    if p and p.returncode is None:
        p.terminate()
        try: await asyncio.wait_for(p.wait(),5)
        except asyncio.TimeoutError: p.kill(); await p.wait()
    if rec and hasattr(rec.vm,'host_forward'):
        with contextlib.suppress(Exception): await rec.vm.host_forward(row['host_port'],row['guest_port'],remove=True)
    account=row['account_id'];zone=row['zone_id'];tid=row.get('tunnel_id')
    # Recover resource IDs if the API completed but its response was lost.
    if not tid:
        matches=await api('GET',f'/accounts/{account}/cfd_tunnel',params={'name':row['name'],'is_deleted':'false'})
        match=next((x for x in matches or [] if x.get('name')==row['name']),None)
        if match: tid=match['id']
    if tid:
        records=await api('GET',f'/zones/{zone}/dns_records',params={'name':row['hostname'],'type':'CNAME'})
        for record in records or []:
            if record.get('content','').rstrip('.')==tid+'.cfargotunnel.com':
                await api('DELETE',f"/zones/{zone}/dns_records/{record['id']}",allow_missing=True)
        base=f'/accounts/{account}/cfd_tunnel/{tid}'
        await api('DELETE',base+'/connections',allow_missing=True)
        await api('DELETE',base,allow_missing=True)
    save([r for r in load() if r.get('id')!=row.get('id')])

async def close_vm(manager, vm_id):
    """All routes for one VM are torn down (machine delete/shutdown)."""
    async with lock:
        for row in load():
            if row.get('vm') != vm_id: continue
            try: await cleanup(row,manager.get(vm_id))
            except Exception: pass  # connector stopped, journal kept for retry
        # Gate mappings only hold a wildcard-route entry: dropping the mapping
        # is enough; the shared tunnel + DNS record stay for the rest.
        maps=gateway.load_maps()
        keep=[m for m in maps if m.get('vm')!=vm_id]
        if len(keep)!=len(maps): gateway.save_maps(keep)

async def reconcile(manager):
    async with lock:
        for row in load():
            rec=manager.get(row.get('vm'))
            proc=processes.get(row.get('id'))
            if not rec or rec.state!='running' or row.get('phase')!='active' or not proc or proc.returncode is not None:
                try: await cleanup(row,rec)
                except Exception: pass # retain journal for retry; never silently forget resources
        await _reconcile_gates()

async def listing():
    rows=load();mutated=False;result=[]
    for r in rows:
        sub=r.get('sub','')
        host=r.get('hostname','')
        domain=host[len(sub)+1:] if sub and host.startswith(sub+'.') else host
        status=r.get('phase','')
        if status=='active':
            if not r.get('dns_checked'):
                # Lazy re-check: once the name actually answers (not with the
                # catch-all's answer), flip provisioning -> ready.
                forbid=set(r.get('wild_answers') or [])
                if await wait_dns(host,frozenset(forbid),timeout=1.2):
                    r['dns_checked']=True;mutated=True
            status='ready' if r.get('dns_checked') else 'provisioning'
        result.append({**r,'tunnel':{'url':'https://'+host,'domain':domain,'sub':sub,'status':status,'wildcard':bool(r.get('wildcard')),'access':r.get('access','public')}})
    if mutated: save(rows)
    result.extend(await _gate_listing())
    return result



async def remove(vm,guest_port,manager):
    """Unpublish a port. Tries the legacy direct route first, then gate maps."""
    async with lock:
        rows=load()
        row=next((r for r in rows if r['vm']==vm and r['guest_port']==guest_port),None)
        if row is not None:
            row['phase']='cleanup';save(rows)
            await cleanup(row,manager.get(vm))
            return
        await _gate_unpublish(vm,guest_port)


# ─────────────────────────── wildcard gateways ───────────────────────────
# ONE cloudflare tunnel + ONE wildcard record per (domain, root-subdomain).
# Every published port rides that wildcard as a label (github-codespace
# style): {label}.{gate}.{zone} -> cloudflared -> local broker -> guest port.
# Publishing becomes instant (no per-port DNS records); the address stays
# stable while the machine is down (the broker explains instead of raw 502).

gate_processes={}   # gateway id -> cloudflared subprocess

def _label_ok(text):
    return bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',text))

VPS_LINKS=C.STATE_DIR/'robo-vps-links.json'

def _vps_linked(zone_id):
    """True while a working VPS relay link covers this zone (the burrows
    gate path, which terminates TLS off-Cloudflare)."""
    from . import vpslink
    return vpslink.linked(zone_id)

def _host_under(zone, gate, sub):
    return (sub + '.' + zone) if gate == '_' else (sub + '.' + gate + '.' + zone)

def _display_domain(zone, gate):
    return ('' if gate == '_' else gate + '.') + zone

def _gate_binary():
    binary=shutil.which('cloudflared') or _ensure_cloudflared()
    if not binary: raise PublishError('cloudflared could not be installed automatically. Put it on PATH and retry.')
    return binary

def manager_rec(vm_id):
    from . import manager
    return manager.lookup(vm_id)

async def gates_state():
    """Setup info for the UI: every zone's configured gates + live status.

    The catch-all ('_': labels directly under the domain) is always offered;
    brand new publish names may exist nowhere yet -- the Domains page builds
    them explicitly, and the ports modal builds them right before publishing.
    """
    c=read(CONFIG,{})
    out=[]
    for z in config_zones(c):
        gates=[]
        grows=gateway.load_gates()
        for name in (z.get('gates') or []):
            row=next((g for g in grows if g.get('zone_id')==z['id'] and g.get('gate')==name),None)
            proc=gate_processes.get(row['id']) if row else None
            gates.append({'name':name,'default':z.get('default_gate')==name,
                          'id':row['id'] if row else None,
                          'status':('unbuilt' if not row else
                                    ('connected' if proc and proc.returncode is None else 'down'))})
        if not any(g['name']=='_' for g in gates):
            row=next((g for g in grows if g.get('zone_id')==z['id'] and g.get('gate')=='_'),None)
            proc=gate_processes.get(row['id']) if row else None
            gates.insert(0,{'name':'_','default':z.get('default_gate','') in ('',None),
                            'id':row['id'] if row else None,
                            'status':('unbuilt' if not row else
                                      ('connected' if proc and proc.returncode is None else 'down'))})
        out.append({**z,'gates':gates})
    return {'zones':out,'connected':bool(c.get('token'))}

async def zones_update(default_zone_id,gates_by_zone,default_gate_by_zone):
    """Save per-domain defaults and gateway labels from the Domains tab.

    Pure bookkeeping: nothing here touches DNS or tunnels. Creating or
    deleting wildcard gateways is a separate explicit action.
    """
    async with lock:
        c=cfg(); zones_all=config_zones(c)
        for z in zones_all:
            z['default']=z['id']==default_zone_id
            z['default_gate']=str(default_gate_by_zone.get(z['id'],'') or '')
            known=set(z.get('gates') or [])
            z['gates']=sorted(known | {str(g) for g in gates_by_zone.get(z['id'],[]) if _label_ok(str(g))})
        c['zones']=zones_all
        write(CONFIG,c)
        return await gates_state()

async def ensure_gate(zone_id,gate):
    """Idempotently stand up a wildcard gateway for a domain.

    gate=='_' is the zone catch-all: one record *.{zone} -> tunnel, and every
    published port becomes a label directly under the domain. Anything else
    is a burrows gate ({label}.{gate}.{zone}) which Cloudflare's free cert
    does not cover at the edge -- those need a linked VPS relay to terminate
    TLS, so they are refused until one is linked.

    Creates (or reuses) the shared cfd_tunnel, points ONE wildcard CNAME at
    it, starts the local broker listener and the cloudflared connector. If all
    of that already exists, this is a no-op -- a retry or a server restart
    never duplicates DNS rows or tunnels.
    """
    gate=(gate or '').strip().lower()
    catch_all = gate == '_'
    if not catch_all and not _label_ok(gate): raise PublishError('Gateway name must be a single DNS label (letters, digits, dashes).')
    async with lock:
        c=cfg(); zones_all=config_zones(c)
        zone=next((z for z in zones_all if z.get('id')==zone_id),None)
        if zone is None: raise PublishError('That domain is not connected. Reconnect Cloudflare first.')
        if not catch_all and not _vps_linked(zone['id']):
            raise PublishError("A gate subdomain needs a linked VPS relay — Cloudflare's free edge certificate only covers one level (labels directly under the domain). Add a VPS on the VPS panel first, or use the zone catch-all.")
        rows=gateway.load_gates()
        g=next((x for x in rows if x.get('zone_id')==zone_id and x.get('gate')==gate),None)
        if g and g.get('phase')=='active':
            # Rebind the broker if this process has no listener yet (a server
            # restart re-registers every gate only on the next reconcile tick
            # otherwise -- the gate would 502/timeout until then).
            await gateway.start_listeners()
            return await _gate_view(g)
        wildcard_fqdn=('*.'+zone['name']) if catch_all else ('*.'+gate+'.'+zone['name'])
        tunnel_name=('robo-zone-%s-%s'%(zone['name'],uuid.uuid4().hex[:8]) if catch_all
                     else 'robo-gate-%s-%s'%(gate,uuid.uuid4().hex[:8]))
        try:
            binary=_gate_binary()
            if g is None:
                g={'id':uuid.uuid4().hex,'zone_id':zone['id'],'zone':zone['name'],
                   'gate':gate,'account_id':zone['account_id'],'phase':'pending'}
                rows.append(g)
            g['phase']='pending'
            broker_port=g.get('broker_port') or allocate()
            tunnel_id=g.get('tunnel_id')
            if not tunnel_id:
                tunnel=await api('POST',f"/accounts/{zone['account_id']}/cfd_tunnel",
                                 {'name':tunnel_name,'config_src':'cloudflare'})
                tunnel_id=tunnel['id']
                await api('PUT',f"/accounts/{zone['account_id']}/cfd_tunnel/{tunnel_id}/configurations",
                          {'config':{'ingress':[
                              {'hostname':wildcard_fqdn,'service':'http://127.0.0.1:%d'%broker_port},
                              {'service':'http_status:404'}]}})
            g['tunnel_id']=tunnel_id;g['broker_port']=broker_port
            gateway.save_gates(rows)
            # The single DNS row the whole gateway needs. Reuse our own;
            # never overwrite someone else's record, and for the catch-all
            # make that crystal clear -- the zone's every name rides it.
            record=next((r for r in await api('GET',f"/zones/{zone['id']}/dns_records",
                        params={'name':wildcard_fqdn,'per_page':5}) or []),None)
            if record and record.get('content','').rstrip('.')!=tunnel_id+'.cfargotunnel.com':
                if catch_all:
                    raise PublishError("'%s' already has a catch-all record pointing somewhere else (%s). Latitude: this domain's labels are served by another destination; removing that record would take it down."
                                       %(wildcard_fqdn, record.get('type','record')))
                raise PublishError("'%s' already has a different wildcard record; choose another gateway name or remove that record."%wildcard_fqdn)
            if not record:
                dns=await api('POST',f"/zones/{zone['id']}/dns_records",
                              {'type':'CNAME','name':wildcard_fqdn,'content':tunnel_id+'.cfargotunnel.com','proxied':True,'ttl':1})
                g['dns_id']=dns['id']
            else:
                g['dns_id']=record['id']
            gateway.save_gates(rows)
            proc=gate_processes.get(g['id'])
            if not proc or proc.returncode is not None:
                token=await api('GET',f"/accounts/{zone['account_id']}/cfd_tunnel/{tunnel_id}/token")
                env=os.environ.copy();env['TUNNEL_TOKEN']=token
                wrapper=str(Path(__file__).resolve().parent.parent/'tools'/'cloudflared_child.py')
                proc=await asyncio.create_subprocess_exec(sys.executable,wrapper,binary,env=env,
                                                          stdin=asyncio.subprocess.DEVNULL,
                                                          stdout=asyncio.subprocess.DEVNULL,
                                                          stderr=asyncio.subprocess.DEVNULL)
                gate_processes[g['id']]=proc
            await gateway.start_listeners()
            g['phase']='active';gateway.save_gates(rows)
            return await _gate_view(g)
        except PublishError:
            gateway.save_gates(rows)   # pending row kept: reconciliation retries
            raise
        except Exception as exc:
            gateway.save_gates(rows)
            raise PublishError(str(exc)[:220])

async def _gate_view(g):
    proc=gate_processes.get(g['id'])
    return {'id':g['id'],'zone':g['zone'],'zone_id':g['zone_id'],'gate':g['gate'],
            'wildcard':('*.'+g['zone']) if g['gate']=='_' else ('*.'+g['gate']+'.'+g['zone']),
            'broker_port':g.get('broker_port'),
            'status':('connected' if proc and proc.returncode is None else g.get('phase','pending'))}

async def remove_gate(gate_id):
    """Tear down one gateway: its maps, wildcard record, tunnel, connector."""
    async with lock:
        rows=gateway.load_gates()
        g=next((x for x in rows if x['id']==gate_id),None)
        if g is None: raise PublishError('Gateway not found.')
        g['phase']='removing';gateway.save_gates(rows)
        zid,zname,gt=g['zone_id'],g['zone'],g['gate']
        maps=gateway.load_maps()
        gateway.save_maps([m for m in maps if not (m.get('zone_id')==zid and m.get('gate')==gt)])
        try:
            proc=gate_processes.pop(g['id'],None)
            if proc and proc.returncode is None:
                proc.terminate()
                try: await asyncio.wait_for(proc.wait(),5)
                except asyncio.TimeoutError: proc.kill(); await proc.wait()
            tid=g.get('tunnel_id')
            if tid:
                wcard=('*.'+zname) if gt=='_' else ('*.'+gt+'.'+zname)
                for record in await api('GET',f"/zones/{zid}/dns_records",params={'name':wcard,'per_page':5}) or []:
                    if record.get('content','').rstrip('.')==tid+'.cfargotunnel.com':
                        await api('DELETE',f"/zones/{zid}/dns_records/{record['id']}",allow_missing=True)
                base=f"/accounts/{g['account_id']}/cfd_tunnel/{tid}"
                await api('DELETE',base+'/connections',allow_missing=True)
                await api('DELETE',base,allow_missing=True)
        except Exception:
            pass    # partial teardown; retrying remove_gate finishes the job
        await gateway.stop_listener(gt,zname,zid)
        fresh=[x for x in gateway.load_gates() if x['id']!=gate_id]
        gateway.save_gates(fresh)
        # Drop the label from the zone's configured list, and its default.
        c=read(CONFIG,{})
        for z in config_zones(c):
            if z['id']==zid:
                z['gates']=[x for x in (z.get('gates') or []) if x!=gt]
                if z.get('default_gate')==gt: z['default_gate']=''
        if c.get('zones'): write(CONFIG,c)

async def gate_publish(rec,guest_port,sub,zone_id,gate_name):
    """Publish one port through a wildcard gateway: zero per-port DNS rows."""
    if not 1<=guest_port<=65535: raise PublishError('Port must be between 1 and 65535')
    if rec.state!='running' or not hasattr(rec.vm,'host_forward'): raise PublishError('A running QEMU machine is required.')
    async with lock:
        c=cfg(); zones_all=config_zones(c)
        zone=next((z for z in zones_all if z.get('id')==zone_id),None)
        if zone is None: raise PublishError('Pick one of the connected domains.')
        grow=gateway.load_gates()
        g=next((x for x in grow if x.get('zone_id')==zone_id and x.get('gate')==gate_name and x.get('phase')=='active'),None)
        if g is None: raise PublishError('Set up the wildcard gateway for this domain first (Domains tab).')
        sub=(sub or '').strip().lower()
        if not sub:
            sub='%s-%d'%((rec.name or 'vm').lower().replace('_','-'),guest_port)
        if not _label_ok(sub): raise PublishError('Publish name must be a single DNS label (letters, digits, dashes).')
        host=_host_under(zone['name'], g['gate'], sub)
        maps=gateway.load_maps()
        kind=[m for m in maps if m['hostname']==host and m.get('phase','active')=='active']
        if kind:
            e=kind[0]
            if e.get('vm')==rec.id and e.get('guest_port')==guest_port:
                return _map_view(e,g)
            raise PublishError("'%s' is already published by another service; pick another name."%host)
        row={'id':uuid.uuid4().hex,'vm':rec.id,'guest_port':guest_port,
             'host_port':allocate(),'sub':sub,'hostname':host,'zone':zone['name'],
             'zone_id':zone['id'],'gate':g['gate'],'access':'public','phase':'active',
             'created_at':time.time()}
        maps.append(row);gateway.save_maps(maps)
        try:
            await rec.vm.host_forward(row['host_port'],guest_port)
        except Exception as exc:
            _save_maps_without(row)
            raise PublishError('QEMU rejected the port forward: %s'%str(exc)[:180])
        return _map_view(row,g)

def _save_maps_without(row):
    gateway.save_maps([m for m in gateway.load_maps() if m.get('id')!=row.get('id')])

def _map_view(m,g):
    return {'vm':m['vm'],'guest_port':m['guest_port'],'host_port':m['host_port'],
            'gateway':g['gate'],
            'tunnel':{'url':'https://'+m['hostname'],'domain':_display_domain(m['zone'],g['gate']),
                      'sub':m['sub'],'access':'public','status':'ready','wildcard':True}}

def _gate_unpublish(vm,guest_port):
    """Remove gate maps (+ their forwards) for one guest port."""
    maps=gateway.load_maps()
    rows=[m for m in maps if m.get('vm')==vm and m.get('guest_port')==guest_port]
    if not rows: raise PublishError('Published port not found.')

    async def _drop():
        rec=manager_rec(vm)
        if rec is not None and hasattr(rec.vm,'host_forward'):
            for row in rows:
                with contextlib.suppress(Exception):
                    await rec.vm.host_forward(row['host_port'],row['guest_port'],remove=True)
        gateway.save_maps([m for m in maps if m not in rows])

    return _drop()

async def _reconcile_gates():
    """Keep every gateway's connector and broker alive across restarts."""
    for g in gateway.load_gates():
        if g.get('phase') not in ('active','pending') or g.get('phase')=='removing': continue
        proc=gate_processes.get(g['id'])
        if not proc or proc.returncode is not None:
            try:
                binary=_gate_binary()
                token=await api('GET',f"/accounts/{g['account_id']}/cfd_tunnel/{g['tunnel_id']}/token")
                env=os.environ.copy();env['TUNNEL_TOKEN']=token
                wrapper=str(Path(__file__).resolve().parent.parent/'tools'/'cloudflared_child.py')
                proc=await asyncio.create_subprocess_exec(sys.executable,wrapper,binary,env=env,
                                                          stdin=asyncio.subprocess.DEVNULL,
                                                          stdout=asyncio.subprocess.DEVNULL,
                                                          stderr=asyncio.subprocess.DEVNULL)
                gate_processes[g['id']]=proc
            except Exception: continue
    await gateway.start_listeners()

async def _gate_listing():
    """Gate-mapped publishes, in the same shape the ports modal already reads."""
    result=[]
    for m in gateway.load_maps():
        if m.get('phase','active')!='active': continue
        rec=manager_rec(m['vm'])
        up=bool(rec and rec.state=='running')
        result.append({**m,'tunnel':{'url':'https://'+m['hostname'],
            'domain':_display_domain(m['zone'],m['gate']),'sub':m['sub'],
            'status':('ready' if up else 'connected-machine-down'),
            'access':m.get('access','public'),'wildcard':True,'gateway':m['gate']}})
    return result

async def close_listeners():
    await gateway.stop_listeners()
