"""Test per-VM image ownership without downloading or starting a hypervisor."""
import asyncio, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from server import catalog
from server.manager import Manager
from server.models import CreateVM
from server import config as C

class Proc:
    returncode = 0
    async def wait(self): return 0

class Lifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.old=C.VMS_DIR; C.VMS_DIR=Path(self.temp.name)/'vms'; C.VMS_DIR.mkdir()
    async def asyncTearDown(self): C.VMS_DIR=self.old; self.temp.cleanup()
    async def test_catalog_requires_no_disk_image(self):
        self.assertEqual([x['id'] for x in catalog.list_images('qemu')], ['debian'])
        self.assertIn('debian', [x['id'] for x in catalog.list_images('qemu')])
        self.assertFalse(any(C.VMS_DIR.iterdir()))
    async def test_create_delete_and_restore(self):
        m=Manager(backend='qemu')
        async def bake(*args,**kw):
            root=Path(kw['env']['MVMP_STATE_DIR'])/'images';root.mkdir(parents=True);(root/'golden.qcow2').write_bytes(b'disk'); return Proc()
        async def start(vm): vm.dir.mkdir(parents=True,exist_ok=True);vm.overlay.write_bytes(b'overlay')
        async def suspend(vm): pass
        async def stop(vm,graceful=True):
            import shutil;shutil.rmtree(vm.dir,ignore_errors=True)
        with patch('asyncio.create_subprocess_exec',bake),patch('server.qemu.QemuVM.start',start),patch('server.qemu.QemuVM.suspend',suspend),patch('server.qemu.QemuVM.stop',stop):
            rec=await m.create(CreateVM(image='debian',ttl='never'))
            self.assertEqual(rec.state,'starting'); await rec.task
            self.assertEqual(rec.state,'running'); self.assertTrue((C.VMS_DIR/rec.id/'provision/images/golden.qcow2').exists())
            await m.shutdown();self.assertTrue((C.VMS_DIR/rec.id/'disk.qcow2').exists())
            restored=Manager(backend='qemu');restored._restore_qemu();self.assertEqual(restored.get(rec.id).state,'stopped')
            await restored.destroy(rec.id);self.assertFalse((C.VMS_DIR/rec.id).exists())
