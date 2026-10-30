"""Publishing config: legacy migration, per-zone selection, hostname rules."""
import asyncio, unittest
from unittest import mock

from server import publish


class ZoneConfigTest(unittest.TestCase):
    def test_legacy_single_zone_migrates(self):
        self.assertEqual(
            publish.config_zones({"token": "t",
                                  "zone": {"id": "z1", "name": "a.top", "account_id": "A"}}),
            [{"id": "z1", "name": "a.top", "account_id": "A"}])

    def test_multi_zone_config(self):
        zs = [{"id": "z1", "name": "a.top", "account_id": "A"},
              {"id": "z2", "name": "b.top", "account_id": "A"}]
        self.assertEqual(publish.config_zones({"token": "t", "zones": zs}), zs)

    def test_empty_config(self):
        self.assertEqual(publish.config_zones({}), [])


class FakeVM:
    async def host_forward(self, *args, **kwargs):
        pass


class FakeRec:
    id = "vm1"
    state = "running"
    vm = FakeVM()


class CreateZoneTest(unittest.TestCase):
    """create() picks the right zone/hostname before any Cloudflare API call."""

    def setUp(self):
        self.zones = [{"id": "z1", "name": "a.top", "account_id": "A"},
                      {"id": "z2", "name": "b.top", "account_id": "A"}]
        self.rows = []
        self.saved = []
        self.calls = []

        async def fake_api(method, path, payload=None, params=None,
                           token=None, allow_missing=False):
            self.calls.append((method, path))
            raise publish.PublishError("stop-test")

        self._main = mock.patch.multiple(
            publish, cfg=lambda: {"token": "t", "zones": self.zones},
            load=lambda: self.rows,
            save=self.saved.extend,
            allocate=lambda: 11111,
            api=fake_api,
        )
        which = mock.patch.object(publish.shutil, "which", return_value="/usr/bin/cloudflared")
        self._patches = self._main, which
        self._main.start()
        which.start()

    def tearDown(self):
        self._patches[0].stop()
        self._patches[1].stop()

    def test_default_zone_is_first_connection_zone(self):
        with self.assertRaises(publish.PublishError) as ctx:
            asyncio.run(publish.create(FakeRec(), 8080, "myapp", "public"))
        self.assertIn("stop-test", str(ctx.exception))  # journal reached before API
        row = self.saved[-1]
        self.assertEqual(row["hostname"], "myapp.a.top")
        self.assertEqual(row["zone_id"], "z1")

    def test_explicit_zone_id(self):
        with self.assertRaises(publish.PublishError):
            asyncio.run(publish.create(FakeRec(), 8080, "myapp", "public", "z2"))
        row = self.saved[-1]
        self.assertEqual(row["hostname"], "myapp.b.top")
        self.assertEqual(row["zone_id"], "z2")

    def test_unknown_zone_rejected(self):
        with self.assertRaises(publish.PublishError) as ctx:
            asyncio.run(publish.create(FakeRec(), 8080, "myapp", "public", "zz"))
        self.assertIn("not connected", str(ctx.exception))
        self.assertEqual(self.saved, [])  # nothing journaled

    def test_hostname_unique_across_zones(self):
        self.rows.append({"id": "r0", "vm": "other", "guest_port": 9999,
                          "hostname": "dupe.a.top"})
        with self.assertRaises(publish.PublishError) as ctx:
            asyncio.run(publish.create(FakeRec(), 1234, "dupe", "public", "z1"))
        self.assertIn("choose another subdomain", str(ctx.exception))
        self.assertEqual(self.saved, [])

    def test_same_vm_port_rejected(self):
        self.rows.append({"id": "r0", "vm": "vm1", "guest_port": 8080,
                          "hostname": "x.a.top"})
        with self.assertRaises(publish.PublishError) as ctx:
            asyncio.run(publish.create(FakeRec(), 8080, "fresh", "public", "z1"))
        self.assertIn("already published", str(ctx.exception))

    def test_invalid_label_rejected(self):
        with self.assertRaises(publish.PublishError) as ctx:
            asyncio.run(publish.create(FakeRec(), 8080, "-bad_", "public", "z1"))
        self.assertIn("valid single DNS label", str(ctx.exception))

    def test_empty_label_defaults_to_port_name(self):
        with self.assertRaises(publish.PublishError):
            asyncio.run(publish.create(FakeRec(), 8080, "", "public", "z1"))
        self.assertEqual(self.saved[-1]["sub"], "port-8080")


if __name__ == "__main__":
    unittest.main()
