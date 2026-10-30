"""Direct Cloudflare Tunnel publication. No Aegis dependency.

Routes are explicitly public. State is journaled before API side effects. On
restart stale routes are cleaned, not automatically rebound to reused ports.
"""
import asyncio, contextlib, json, os, re, shutil, socket, sys, tempfile, time, uuid
from pathlib import Path
import httpx
from . import config as C

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
    if not binary: raise PublishError('cloudflared is not installed. Install it on the host, then restart Robo Universal.')
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

async def remove(vm,guest_port,manager):
    async with lock:
        rows=load()
        row=next((r for r in rows if r['vm']==vm and r['guest_port']==guest_port),None)
        if not row: raise PublishError('Published port not found.')
        row['phase']='cleanup';save(rows)
        await cleanup(row,manager.get(vm))

async def reconcile(manager):
    async with lock:
        for row in load():
            rec=manager.get(row.get('vm'))
            proc=processes.get(row.get('id'))
            if not rec or rec.state!='running' or row.get('phase')!='active' or not proc or proc.returncode is not None:
                try: await cleanup(row,rec)
                except Exception: pass # retain journal for retry; never silently forget resources

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
    return result

async def close_vm(manager, vm_id):
    async with lock:
        for row in load():
            if row.get('vm') != vm_id: continue
            try: await cleanup(row,manager.get(vm_id))
            except Exception: pass  # connector stopped, retained journal for retry
