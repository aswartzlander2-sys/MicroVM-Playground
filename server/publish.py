"""Direct Cloudflare Tunnel publication. No Aegis dependency.

Routes are explicitly public. State is journaled before API side effects. On
restart stale routes are cleaned, not automatically rebound to reused ports.
"""
import asyncio, contextlib, json, os, re, shutil, socket, sys, tempfile, uuid
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
    if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,500}',token): raise PublishError('Enter a valid Cloudflare API token.')
    result=[]
    # Zone API is paginated; cap protects against unexpectedly large accounts.
    for page in range(1,11):
        rows=await api('GET','/zones',params={'page':page,'per_page':50,'status':'active'},token=token)
        if not isinstance(rows,list): raise PublishError('Unexpected response from Cloudflare zones API.')
        result.extend({'id':z['id'],'name':z['name'],'account_id':z['account']['id']} for z in rows if z.get('id') and z.get('name') and z.get('account',{}).get('id'))
        if len(rows)<50: break
    return result

async def connect(token,zone_id):
    async with lock:
        if load(): raise PublishError('Remove all published ports before replacing the Cloudflare connection.')
        options=await zones(token)
        zone=next((z for z in options if z['id']==zone_id),None)
        if not zone: raise PublishError('Zone not found or token lacks Zone Read permission.')
        write(CONFIG,{'token':token,'zone':zone})
        return {'connected':True,'zone':zone}

async def disconnect():
    async with lock:
        if load(): raise PublishError('Remove all published ports before disconnecting.')
        CONFIG.unlink(missing_ok=True)

def allocate():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); return s.getsockname()[1]

async def state():
    c=read(CONFIG,{})
    return {'connected':bool(c.get('token')), 'zone':c.get('zone'), 'cloudflared':bool(shutil.which('cloudflared')), 'pending_cleanup':sum(1 for r in load() if r.get('phase')!='active')}

async def create(rec,guest_port,sub,access):
    if not 1<=guest_port<=65535: raise PublishError('Port must be between 1 and 65535')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',sub): raise PublishError('Name must be a valid single DNS label (1–63 characters).')
    if access!='public': raise PublishError('Private access is not implemented; no route was published.')
    if rec.state!='running' or not hasattr(rec.vm,'host_forward'): raise PublishError('A running QEMU machine is required.')
    binary=shutil.which('cloudflared')
    if not binary: raise PublishError('cloudflared is not installed. Install it on the host, then restart Robo Universal.')
    if not sys.platform.startswith('linux'): raise PublishError('Direct publishing currently requires Linux.')
    async with lock:
        if rec.state!='running': raise PublishError('The machine is no longer running.')
        c=cfg(); zone=c['zone']; rows=load()
        if any(r['vm']==rec.id and r['guest_port']==guest_port for r in rows): raise PublishError('That guest port is already published or awaiting cleanup.')
        hostname=sub+'.'+zone['name']; hostport=allocate(); identifier=uuid.uuid4().hex
        row={'id':identifier,'vm':rec.id,'guest_port':guest_port,'host_port':hostport,'sub':sub,'hostname':hostname,'access':'public','zone_id':zone['id'],'account_id':zone['account_id'],'name':'robo-'+identifier,'phase':'pending'}
        rows.append(row);save(rows)
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
            row['dns_id']=dns['id'];row['phase']='active';save(rows)
            return {'vm':rec.id,'guest_port':guest_port,'host_port':hostport,'tunnel':{'url':'https://'+hostname,'access':'public','status':'provisioning'}}
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
    return [{**r,'tunnel':{'url':'https://'+r['hostname'],'status':r['phase'],'access':'public'}} for r in load()]

async def close_vm(manager, vm_id):
    async with lock:
        for row in load():
            if row.get('vm') != vm_id: continue
            try: await cleanup(row,manager.get(vm_id))
            except Exception: pass  # connector stopped, retained journal for retry
