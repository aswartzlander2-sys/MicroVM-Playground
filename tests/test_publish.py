"""Direct Cloudflare publication tests: mocked API, connector, QEMU."""
import tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from server import publish

class VM:
    def __init__(self): self.calls=[]
    async def host_forward(self,h,g,remove=False): self.calls.append((h,g,remove))
class Proc:
    returncode=None
    def terminate(self): self.returncode=0
    async def wait(self): return 0
    def kill(self): self.returncode=-9

class PublishingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=(publish.STATE,publish.CONFIG);publish.STATE=Path(self.tmp.name)/'routes.json';publish.CONFIG=Path(self.tmp.name)/'config.json'
        publish.write(publish.CONFIG,{'token':'private-secret','zone':{'id':'zone','name':'example.com','account_id':'account'}})
        self.rec=SimpleNamespace(id='abcd',state='running',vm=VM());self.manager=SimpleNamespace(get=lambda _:self.rec)
        self.calls=[];self.dns=[];self.tunnel=None
    async def asyncTearDown(self):
        publish.STATE,publish.CONFIG=self.old;publish.processes.clear();self.tmp.cleanup()
    async def api(self,method,path,payload=None,params=None,token=None,allow_missing=False):
        self.calls.append((method,path,payload))
        if path.endswith('/dns_records') and method=='GET': return self.dns
        if path.endswith('/dns_records') and method=='POST': self.dns=[{'id':'dns-id','content':payload['content']}];return self.dns[0]
        if '/dns_records/' in path and method=='DELETE': self.dns=[];return {}
        if path.endswith('/cfd_tunnel') and method=='POST': self.tunnel={'id':'tunnel-id','name':payload['name']};return self.tunnel
        if path.endswith('/cfd_tunnel') and method=='GET': return [self.tunnel] if self.tunnel else []
        if path.endswith('/configurations'): return {}
        if path.endswith('/token'): return 'connector-token'
        if method=='DELETE': return {}
        raise AssertionError((method,path))
    async def process(self,*args,**kw):
        self.assertNotIn('connector-token',' '.join(args))
        self.assertEqual(kw['env']['TUNNEL_TOKEN'],'connector-token')
        return Proc()
    async def sleep(self,*args): pass
    async def test_public_create_remove(self):
        with patch.object(publish,'api',self.api),patch.object(publish.shutil,'which',return_value='/bin/cloudflared'),patch.object(publish.asyncio,'create_subprocess_exec',self.process),patch.object(publish.asyncio,'sleep',self.sleep):
            r=await publish.create(self.rec,3000,'hello-3000','public')
            self.assertEqual(r['tunnel']['url'],'https://hello-3000.example.com')
            self.assertEqual(len(await publish.listing()),1)
            self.assertNotIn('private-secret',str(await publish.state()))
            await publish.remove('abcd',3000,self.manager)
            self.assertEqual(publish.load(),[]);self.assertEqual(self.dns,[])
            self.assertTrue(self.rec.vm.calls[-1][2])
    async def test_private_refused(self):
        with self.assertRaises(publish.PublishError): await publish.create(self.rec,3000,'hello','login')
        self.assertEqual(publish.load(),[])
    async def test_dns_collision_no_overwrite(self):
        self.dns=[{'id':'someone-else','content':'other.example'}]
        with patch.object(publish,'api',self.api),patch.object(publish.shutil,'which',return_value='/bin/cloudflared'):
            with self.assertRaises(publish.PublishError): await publish.create(self.rec,3000,'existing','public')
            self.assertEqual(self.dns[0]['id'],'someone-else')
            self.assertFalse(any(m=='POST' for m,p,x in self.calls))

    async def test_failed_dns_write_stops_connector_and_cleans(self):
        async def fail_dns(method,path,payload=None,params=None,token=None,allow_missing=False):
            if method=='POST' and path.endswith('/dns_records'):
                raise publish.PublishError('DNS refused')
            return await self.api(method,path,payload,params,token,allow_missing)
        with patch.object(publish,'api',fail_dns),patch.object(publish.shutil,'which',return_value='/bin/cloudflared'),patch.object(publish.asyncio,'create_subprocess_exec',self.process),patch.object(publish.asyncio,'sleep',self.sleep):
            with self.assertRaises(publish.PublishError): await publish.create(self.rec,3000,'hello-3000','public')
            self.assertEqual(publish.load(),[])
            self.assertEqual(publish.processes,{})
            self.assertTrue(self.rec.vm.calls[-1][2])

    async def test_stale_route_on_restart_is_cleaned(self):
        with patch.object(publish,'api',self.api),patch.object(publish.shutil,'which',return_value='/bin/cloudflared'),patch.object(publish.asyncio,'create_subprocess_exec',self.process),patch.object(publish.asyncio,'sleep',self.sleep):
            await publish.create(self.rec,3000,'hello-3000','public')
            publish.processes.clear() # simulated server restart
            await publish.reconcile(self.manager)
            self.assertEqual(publish.load(),[])
            self.assertEqual(self.dns,[])
