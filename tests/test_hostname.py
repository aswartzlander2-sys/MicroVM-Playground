import io, tempfile, unittest
from pathlib import Path
import pycdlib
from tools.bake import build_seed
from server.models import CreateVM

class HostnameTest(unittest.TestCase):
    def test_guest_cloud_init_uses_machine_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso_path=Path(tmp)/'seed.iso';build_seed(iso_path,'git','brisk-otter','debian')
            iso=pycdlib.PyCdlib();iso.open(str(iso_path));user=io.BytesIO();meta=io.BytesIO()
            iso.get_file_from_iso_fp(user,rr_path='/user-data');iso.get_file_from_iso_fp(meta,rr_path='/meta-data');iso.close()
            self.assertIn(b'hostname: brisk-otter',user.getvalue())
            self.assertIn(b'local-hostname: brisk-otter',meta.getvalue())
            self.assertNotIn(b'passwd, -d, root',user.getvalue())
    def test_invalid_hostname_is_rejected(self):
        with self.assertRaises(ValueError):CreateVM(name='bad_name')
        self.assertEqual(CreateVM(name='My-Server').name,'my-server')
