"""The guest network posture: isolated by default, inbound forwards still on."""
import unittest

from server.qemu import QemuVM


class ArgvNetTest(unittest.TestCase):
    def make(self, internet: bool) -> QemuVM:
        vm = QemuVM("abc123", 3, 1, 256, 8, name="test", internet=internet)
        vm.accel = "kvm"
        vm.ssh_port = 21203
        return vm

    def argv(self, net: str) -> str:
        # _argv is pure string building: no disk, no subprocess.
        i = self.argv_index(net)
        return " ".join(self.argv_list(net)[i:i + 2])

    def argv_list(self, net: str):
        return self.make(net)._argv("qemu-system-x86_64", 30001, 30002)

    def argv_index(self, net: str) -> int:
        return next(i for i, a in enumerate(self.argv_list(net)) if a == "-netdev")

    def test_isolated_by_default(self):
        argv = " ".join(self.argv_list(False))
        self.assertIn("restrict=on", argv)

    def test_internet_opt_in(self):
        argv = " ".join(self.argv_list(True))
        self.assertNotIn("restrict=on", argv)
        self.assertIn("user,id=net0", argv)

    def test_management_forward_survives_isolation(self):
        argv = " ".join(self.argv_list(False))
        self.assertIn("hostfwd=tcp:127.0.0.1:21203-:22", argv)


if __name__ == "__main__":
    unittest.main()
