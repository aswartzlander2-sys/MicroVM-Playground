"""VM registry: backend selection, slot allocation, lifecycle, TTL reaping."""
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config as C
from . import net
from . import ttyd as _ttyd
from . import catalog
from .models import TTL_CHOICES, CreateVM, HostInfo, VMView

ADJECTIVES = ["brisk", "amber", "quiet", "lucid", "nimble", "vivid", "candid",
              "stellar", "arctic", "ember", "cobalt", "swift"]
NOUNS = ["otter", "falcon", "cinder", "harbor", "quartz", "vector", "lantern",
         "meadow", "cipher", "pylon", "beacon", "mantis"]


def _friendly_name() -> str:
    return "%s-%s" % (ADJECTIVES[uuid.uuid4().int % len(ADJECTIVES)],
                      NOUNS[uuid.uuid4().int // 13 % len(NOUNS)])


def has_kvm() -> bool:
    return os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK)


def _firecracker_ready() -> bool:
    # Firecracker's tap networking needs root: picking it for a normal user
    # only fails later, at the first launch.
    root = hasattr(os, "geteuid") and os.geteuid() == 0
    return (root and has_kvm() and Path(C.FIRECRACKER_BIN).exists()
            and C.KERNEL_PATH.exists() and C.BASE_ROOTFS.exists())


def _container_ready() -> bool:
    if os.name == "nt":          # needs a POSIX pty
        return False
    from .container import has_docker, image_present
    return has_docker() and image_present()


def _qemu_ready() -> bool:
    return C.find_qemu_binary(C.QEMU_SYSTEM) is not None


def resolve_backend() -> str:
    """Pick the best backend that is actually ready to run something.

    Order is isolation first, then speed:

      firecracker  a real VM at ~125 ms -- but needs /dev/kvm, which neither
                   Codespaces nor Windows provides
      container    native speed and millisecond startup, shared kernel
      qemu         a real VM anywhere, but software-emulated without KVM/WHPX

    Readiness matters as much as capability: a backend whose images are missing
    is not a candidate, because selecting it would only produce errors later.
    Mock is never selected automatically -- silently falling back to a
    simulation is how you end up thinking you have a machine when you do not.
    """
    if C.MOCK:
        return "mock"
    choice = C.BACKEND
    if choice in ("firecracker", "qemu", "container", "mock"):
        return choice

    if _firecracker_ready():
        return "firecracker"
    if _container_ready():
        return "container"
    if _qemu_ready():
        return "qemu"
    # Nothing is provisioned yet. Name the one the host could actually support
    # so host_info() can explain what to install.
    if os.name != "nt" and shutil.which(C.DOCKER_BIN):
        return "container"
    return "qemu"


@dataclass
class Record:
    id: str
    name: str
    spec: CreateVM
    slot: int
    state: str = "starting"
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    error: Optional[str] = None
    vm: object = None
    task: object = None
    destroying: bool = False

    def view(self) -> VMView:
        return VMView(
            id=self.id, name=self.name, state=self.state,
            vcpus=self.spec.vcpus, mem_mib=self.spec.mem_mib, disk_gb=self.spec.disk_gb,
            ip=getattr(self.vm, "ip", None), gateway=getattr(self.vm, "gateway", None),
            created_at=self.created_at, boot_ms=getattr(self.vm, "boot_ms", None),
            started_at=getattr(self.vm, "started_at", None),
            expires_at=self.expires_at, error=self.error,
            terminal_url=self.terminal_url,
            image=self.spec.image, image_label=self.image_label,
            progress=self._progress(),
            # Stopping only makes sense for something that is not on a timer:
            # a VM that will be destroyed at expiry gains nothing from being
            # paused, and pausing it would silently outlive its own deadline.
            can_suspend=self.spec.ttl == "never",
        )

    def _progress(self):
        if self.state != "starting": return None
        try:
            info = json.loads((C.VMS_DIR / self.id / "progress.json").read_text())
            return {"message": str(info.get("message", "Preparing…"))[:200], "percent": info.get("percent")}
        except (OSError, ValueError, TypeError):
            return {"message": "Building the original Debian image; package installation can take a long time. See prepare.log if it fails.", "percent": None}

    image_label: Optional[str] = None

    @property
    def terminal_url(self) -> Optional[str]:
        """Same-origin path to this playground's ttyd, when ttyd is serving.

        A path rather than a URL on purpose: the proxy keeps everything on the
        one port, which is what makes it reachable through Codespaces port
        forwarding.
        """
        from . import ttyd
        sess = ttyd.get(self.id)
        return sess.base_path + "/" if sess and sess.alive else None


class Manager:
    def __init__(self, backend: Optional[str] = None):
        self.backend = backend or resolve_backend()
        self.mock = self.backend == "mock"
        self._vms: dict[str, Record] = {}
        self._slots: set[int] = set()
        self._lock = asyncio.Lock()
        self._bake_lock = asyncio.Lock()
        self._reaper: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ probe
    def min_disk_gb(self) -> int:
        """A playground disk can never be smaller than the image it clones."""
        if self.backend == "container":
            return C.DISK_MIN_GB      # containers share the host filesystem
        if self.backend == "qemu":
            return C.base_image_gb()
        if self.backend == "firecracker" and C.BASE_ROOTFS.exists():
            gb = -(-C.BASE_ROOTFS.stat().st_size // (1024 ** 3))
            return max(C.DISK_MIN_GB, int(gb))
        return C.DISK_MIN_GB

    def host_info(self) -> HostInfo:
        problems: list[str] = []
        notes: list[str] = []
        kvm = has_kvm()
        root = hasattr(os, "geteuid") and os.geteuid() == 0
        fc_version = qemu_version = accel = None

        if self.backend == "mock":
            problems.append("Mock mode: VMs are simulated. No kernel, no filesystem, no network.")

        elif self.backend == "container":
            from .container import docker_version, has_docker, image_present
            if not has_docker():
                problems.append("Docker is not available or its daemon is not running.")
            else:
                qemu_version = docker_version()
                if not image_present():
                    problems.append("Playground image %s is not built. Run: "
                                    "docker build -t %s docker/"
                                    % (C.CONTAINER_IMAGE, C.CONTAINER_IMAGE))
            accel = "native"
            notes.append("Containers run directly on the host CPU: native speed and "
                         "millisecond startup. They share the host kernel, so this is "
                         "weaker isolation than a VM -- fine for scratch work, not for "
                         "code you actively distrust.")
            if C.CONTAINER_POOL:
                notes.append("A warm pool of %d keeps launches instant."
                             % C.CONTAINER_POOL)

        elif self.backend == "qemu":
            from .qemu import detect_accelerator, find_binary
            qemu_bin = find_binary(C.QEMU_SYSTEM)
            if not qemu_bin:
                problems.append("QEMU not found. Run tools/get-qemu.ps1 (no admin rights needed).")
            else:
                with contextlib.suppress(Exception):
                    out = subprocess.run([qemu_bin, "--version"], capture_output=True,
                                         text=True, timeout=15)
                    qemu_version = (out.stdout or out.stderr).strip().splitlines()[0]
                accel = detect_accelerator(qemu_bin)
                if accel == "tcg":
                    notes.append("Running under TCG software emulation: correct but slow, "
                                 "roughly 5-10x slower than hardware acceleration. "
                                 "Boots take tens of seconds.")
                else:
                    notes.append("Hardware acceleration active via %s." % accel)
            notes.append("The first machine bakes the shared Debian image (a few minutes); after that, machines start in seconds from copy-on-write clones.")

        else:  # firecracker
            if net.icmp_state() is False:
                notes.append("This host cannot send ICMP -- ping will not work inside "
                             "a playground, and no firewall rule can change that. "
                             "TCP (apt, curl, git, ssh) is unaffected. Common on "
                             "Azure and therefore in Codespaces.")
            if not kvm:
                problems.append("/dev/kvm is missing or not writable -- Firecracker cannot start.")
            if not Path(C.FIRECRACKER_BIN).exists():
                problems.append("firecracker binary not found at " + str(C.FIRECRACKER_BIN))
            else:
                with contextlib.suppress(Exception):
                    out = subprocess.run([C.FIRECRACKER_BIN, "--version"],
                                         capture_output=True, text=True, timeout=5)
                    fc_version = (out.stdout or out.stderr).strip().splitlines()[0]
            if not C.KERNEL_PATH.exists():
                problems.append("guest kernel missing at " + str(C.KERNEL_PATH))
            if not C.BASE_ROOTFS.exists():
                problems.append("base rootfs missing at " + str(C.BASE_ROOTFS))
            if not root:
                problems.append("not running as root -- tap devices and NAT rules will fail.")
            for tool in ("ip", "iptables", "resize2fs"):
                if shutil.which(tool) is None:
                    problems.append("missing host tool: " + tool)
            accel = "kvm" if kvm else None

        if os.name != "nt" and C.USE_TTYD != "off" and not _ttyd.available():
            notes.append("ttyd is not installed, so the built-in terminal is the only "
                         "option. Install it for the second choice: "
                         "sudo bash tools/get-ttyd.sh")

        return HostInfo(
            mode=self.backend,
            kvm=kvm, firecracker=fc_version, qemu=qemu_version, accel=accel,
            kernel=self.backend != "firecracker" or C.KERNEL_PATH.exists(),
            rootfs=self.backend != "firecracker" or C.BASE_ROOTFS.exists(),
            image=self.backend != "qemu" or bool(C.find_qemu_binary(C.QEMU_SYSTEM)),
            root=root, max_vms=C.MAX_VMS,
            limits={
                "vcpus": [C.VCPU_MIN, C.VCPU_MAX],
                "mem_mib": [C.MEM_MIN_MIB, C.MEM_MAX_MIB],
                "disk_gb": [self.min_disk_gb(), C.DISK_MAX_GB],
            },
            defaults={
                "vcpus": C.DEFAULT_VCPUS, "mem_mib": C.DEFAULT_MEM_MIB,
                "disk_gb": max(C.DEFAULT_DISK_GB, self.min_disk_gb()),
            },
            problems=problems, notes=notes,
            terminal="ttyd" if _ttyd.available() else "builtin",
            ttyd=_ttyd.version(),
        )

    # ----------------------------------------------------------------- create
    def _alloc_slot(self) -> int:
        for s in range(1, C.MAX_VMS + 1):
            if s not in self._slots:
                self._slots.add(s)
                return s
        raise RuntimeError("no free slots (MVMP_MAX_VMS=%d)" % C.MAX_VMS)

    def _make_seed(self, vm_id: str, name: str, distro: str = "debian") -> Optional[str]:
        """This machine's own cloud-init seed: name is the real guest hostname.

        A fresh instance-id makes cloud-init run exactly once on the clone's
        first boot (set hostname, credentials) and never again afterwards.
        """
        from tools.bake import build_seed
        seed = C.VMS_DIR / vm_id / "seed.iso"
        seed.parent.mkdir(parents=True, exist_ok=True)
        build_seed(seed, "", name, distro, instance_id=vm_id, mode="clone")
        return str(seed)

    def _make_vm(self, vm_id: str, slot: int, spec: CreateVM, name: str):
        disk = max(spec.disk_gb, self.min_disk_gb())
        img = catalog.resolve(self.backend, spec.image)
        if self.backend == "mock":
            from .mock import MockVM
            return MockVM(vm_id, slot, spec.vcpus, spec.mem_mib, disk, name)
        if self.backend == "container":
            from .container import ContainerVM
            return ContainerVM(vm_id, slot, spec.vcpus, spec.mem_mib, disk, name, img)
        if self.backend == "qemu":
            from .qemu import QemuVM
            seed = None
            if C.BASE_IMAGE.exists():
                seed = self._make_seed(vm_id, name,
                                       "arch" if "arch" in (spec.image or "") else "debian")
            return QemuVM(vm_id, slot, spec.vcpus, spec.mem_mib, disk, name, img, seed=seed)
        from .firecracker import FirecrackerVM
        return FirecrackerVM(vm_id, slot, spec.vcpus, spec.mem_mib, disk, name, img)

    def _image_label(self, image_id: str) -> Optional[str]:
        return catalog.label_for(self.backend, image_id)

    async def create(self, spec: CreateVM) -> Record:
        async with self._lock:
            if len(self._vms) >= C.MAX_VMS:
                raise RuntimeError("playground limit reached (%d)" % C.MAX_VMS)
            slot = self._alloc_slot()

        vm_id = uuid.uuid4().hex[:12]
        name = spec.name or _friendly_name()
        ttl = TTL_CHOICES[spec.ttl]
        rec = Record(id=vm_id, name=name, spec=spec, slot=slot,
                     expires_at=(time.time() + ttl) if ttl else None)
        self._vms[vm_id] = rec

        rec.image_label = self._image_label(spec.image)
        if self.backend == "qemu" and not C.BASE_IMAGE.exists() and (spec.image or catalog.DEFAULT_ID) in catalog.GOLDEN_IDS:
            # The shared golden image is missing: bake it once in the
            # background, then boot a clone. Every later machine is instant.
            rec.task = asyncio.create_task(self._prepare_golden(rec))
            return rec
        try:
            rec.vm = self._make_vm(vm_id, slot, spec, name)
            await rec.vm.start()
            # Deleted while it was starting: destroy() already let go of the
            # record and its slot, so this VM must not live on unseen.
            if self._vms.get(vm_id) is not rec or rec.destroying:
                with contextlib.suppress(Exception):
                    await rec.vm.stop(graceful=False)
                if self.backend == "qemu":
                    await asyncio.to_thread(shutil.rmtree, C.VMS_DIR / vm_id, True)
                return rec
            rec.state = "running"
            self._save_record(rec)
            # ttyd serves the terminal where it exists; the built-in console
            # websocket stays as the fallback (and as ttyd's own transport).
            from . import ttyd
            await asyncio.to_thread(ttyd.start_for, vm_id, C.RUNTIME_PORT)
        except Exception as exc:
            rec.state = "error"
            rec.error = str(exc)
            with contextlib.suppress(Exception):
                if rec.vm is not None:
                    await rec.vm.stop(graceful=False)
            # The slot stays with the errored record until it is deleted:
            # destroy() frees it, exactly once.
        return rec

    async def _prepare_golden(self, rec: Record) -> None:
        """Bake the shared golden image once, then boot a clone of it.

        This mirrors the upstream flow: the multi-minute package install runs
        exactly once on this host; every machine created afterwards clones the
        resulting golden image through a copy-on-write overlay and starts in
        seconds.
        """
        import signal, sys
        from .qemu import QemuVM
        vm_dir = C.VMS_DIR / rec.id
        vm_dir.mkdir(parents=True, exist_ok=True)
        marker = vm_dir / ".robo-provisioning"
        marker.touch(mode=0o600, exist_ok=True)
        env = os.environ.copy()
        # Bake into the server's own images directory so the golden lands at
        # C.BASE_IMAGE; downloaded base images go into a shared cache subfolder
        # so they never show up as separate OS entries in the catalog.
        env["MVMP_STATE_DIR"] = str(C.STATE_DIR)
        env["MVMP_BASE_CACHE"] = str(C.STATE_DIR / "images" / "base")
        env.pop("MVMP_IMAGE", None)
        # A fixed 8 GB: the golden's size becomes every later machine's minimum disk.
        args = [sys.executable, str(Path(__file__).resolve().parent.parent / "tools" / "bake.py"),
                "--distro", "debian", "--disk-gb", "8", "--mem-mib", str(max(1024, rec.spec.mem_mib)), "--vcpus", str(rec.spec.vcpus), "--out", "golden.qcow2"]
        proc = None
        # _progress() reads this file to report real status on the machine card.
        progress_file = vm_dir / "progress.json"
        def _write_progress() -> None:
            tmp = vm_dir / "progress.json.tmp"
            tmp.write_text(json.dumps({"phase": "golden", "message": "Building the shared Debian image once (this runs only for the first machine) — after this every new machine starts in seconds.", "percent": None}))
            os.replace(tmp, progress_file)
        await asyncio.to_thread(_write_progress)
        partial = C.BASE_IMAGE.with_name(C.BASE_IMAGE.name + ".partial")
        try:
            # One bake at a time: a second machine created meanwhile waits for
            # this one's golden instead of starting its own over the same files.
            async with self._bake_lock:
                if C.BASE_IMAGE.exists():
                    result = 0
                else:
                    with (vm_dir / "prepare.log").open("wb") as log:
                        proc = await asyncio.create_subprocess_exec(*args, env=env, stdout=log,
                                stderr=asyncio.subprocess.STDOUT, start_new_session=(os.name != "nt"))
                        result = await proc.wait()
            if result:
                detail = (vm_dir / "prepare.log").read_text(errors="replace")[-1200:].strip()
                raise RuntimeError("Image preparation failed: " + (detail or "see prepare.log"))
            if not C.BASE_IMAGE.exists():
                raise RuntimeError("Image preparation produced no golden image at %s (see prepare.log)" % C.BASE_IMAGE)
            C._image_gb_cache = None   # the golden just appeared; re-probe its virtual size
            disk = max(rec.spec.disk_gb, self.min_disk_gb())
            seed = self._make_seed(rec.id, rec.name, "debian")
            rec.vm = QemuVM(rec.id, rec.slot, rec.spec.vcpus, rec.spec.mem_mib,
                            disk, rec.name, str(C.BASE_IMAGE), seed=seed)
            await rec.vm.start()
            if self._vms.get(rec.id) is not rec or rec.destroying:
                with contextlib.suppress(Exception): await rec.vm.stop(graceful=False)
                return
            rec.state = "running"
            self._save_record(rec)
            marker.unlink(missing_ok=True)
            progress_file.unlink(missing_ok=True)
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    if os.name != "nt": os.killpg(proc.pid, signal.SIGTERM)
                    else: proc.terminate()
                try: await asyncio.wait_for(proc.wait(), 5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        if os.name != "nt": os.killpg(proc.pid, signal.SIGKILL)
                        else: proc.kill()
                    await proc.wait()
            if rec.vm is not None:
                with contextlib.suppress(Exception): await rec.vm.stop(graceful=False)
            with contextlib.suppress(OSError): partial.unlink(missing_ok=True)
            await asyncio.to_thread(shutil.rmtree, vm_dir, True)
            raise
        except Exception as exc:
            rec.state = "error"
            rec.error = str(exc)
            if rec.vm is not None:
                with contextlib.suppress(Exception): await rec.vm.stop(graceful=False)
            # A failed bake must not leave a half-built golden behind (bake.py
            # only renames a finished one into place; a failed boot keeps it).
            with contextlib.suppress(OSError): partial.unlink(missing_ok=True)
            (vm_dir / "progress.json").unlink(missing_ok=True)
            # The slot stays with the errored record; destroy() frees it once.
        finally:
            rec.task = None

    def _save_record(self, rec: Record) -> None:
        if self.backend != "qemu":
            return
        directory = C.VMS_DIR / rec.id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "record.json"
        tmp = directory / "record.json.tmp"
        data = {"id": rec.id, "name": rec.name, "spec": rec.spec.model_dump(),
                "created_at": rec.created_at, "expires_at": rec.expires_at}
        with open(tmp, "w", encoding="utf-8") as f:
            os.chmod(tmp, 0o600)
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _restore_qemu(self) -> None:
        """Re-register completed per-VM disks as stopped, without redownloading."""
        from .qemu import QemuVM
        if not C.VMS_DIR.exists(): return
        for directory in C.VMS_DIR.iterdir():
            try:
                if not directory.is_dir() or len(directory.name) != 12 or any(c not in "0123456789abcdef" for c in directory.name): continue
                marker = directory / ".robo-provisioning"
                record = directory / "record.json"
                if marker.is_file() and not record.exists():
                    shutil.rmtree(directory)
                    continue
                if marker.is_file() and record.is_file(): marker.unlink(missing_ok=True)
                data = json.loads(record.read_text())
                if data.get("id") != directory.name: continue
                spec = CreateVM.model_validate(data["spec"])
                # Prefer the shared golden; older VMs keep their private image.
                image = C.BASE_IMAGE if C.BASE_IMAGE.is_file() else directory / "provision" / "images" / "golden.qcow2"
                if not image.is_file() or not (directory / "disk.qcow2").is_file(): continue
                slot = self._alloc_slot()
                rec = Record(id=directory.name, name=data["name"], spec=spec, slot=slot,
                             state="stopped", created_at=float(data["created_at"]), expires_at=data.get("expires_at"))
                rec.image_label = self._image_label(spec.image)
                rec.vm = QemuVM(rec.id, slot, spec.vcpus, spec.mem_mib, max(8, spec.disk_gb), rec.name, str(image))
                self._vms[rec.id] = rec
            except (OSError, ValueError, KeyError, RuntimeError, TypeError):
                continue  # leave unrecognized data alone; never delete user files

    # ------------------------------------------------------- rename / suspend
    async def rename(self, vm_id: str, name: str) -> Optional[Record]:
        rec = self._vms.get(vm_id)
        if rec is None:
            return None
        if self.backend == "qemu" and name != rec.name:
            raise RuntimeError("Renaming a guest is not supported; its name is its hostname")
        rec.name = name
        # qemu keeps the name as the guest's cloud-init-set hostname (fixed at
        # boot); other backends treat it purely as a label.
        with contextlib.suppress(Exception):
            if rec.vm is not None:
                rec.vm.name = name
        if rec.vm is not None and rec.state in ("running", "stopped"):
            self._save_record(rec)
        return rec

    async def suspend(self, vm_id: str) -> Optional[Record]:
        rec = self._vms.get(vm_id)
        if rec is None or rec.vm is None:
            return None
        if rec.state != "running":
            return rec
        if not rec.view().can_suspend:
            raise RuntimeError("only playgrounds with auto-destroy set to 'never' "
                               "can be stopped -- one on a timer would outlive it")
        rec.state = "stopping"
        from . import publish
        await publish.close_vm(self, vm_id)
        from . import ttyd
        with contextlib.suppress(Exception):
            await asyncio.to_thread(ttyd.stop_for, vm_id)
        try:
            await rec.vm.suspend()
            rec.state = "stopped"
            self._save_record(rec)
        except Exception as exc:
            rec.state = "error"
            rec.error = str(exc)
        return rec

    async def resume(self, vm_id: str) -> Optional[Record]:
        rec = self._vms.get(vm_id)
        if rec is None or rec.vm is None:
            return None
        if rec.state == "running":
            return rec
        rec.state = "starting"
        rec.error = None
        try:
            await rec.vm.resume()
            rec.state = "running"
            self._save_record(rec)
            from . import ttyd
            await asyncio.to_thread(ttyd.start_for, vm_id, C.RUNTIME_PORT)
        except Exception as exc:
            rec.state = "error"
            rec.error = str(exc)
            # a half-started VM keeps its disk lock and ports: stop it
            with contextlib.suppress(Exception):
                await rec.vm.stop(graceful=False)
        return rec

    # ---------------------------------------------------------------- destroy
    async def destroy(self, vm_id: str) -> bool:
        rec = self._vms.get(vm_id)
        if rec is None:
            return False
        # Twice at once (the reaper and a click, a double click): the second
        # waits for nothing and frees nothing -- one slot is freed exactly once.
        if rec.destroying:
            return True
        rec.destroying = True
        rec.state = "stopping"
        from . import publish
        await publish.close_vm(self, vm_id)
        task = rec.task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await task
        from . import ttyd
        with contextlib.suppress(Exception):
            await asyncio.to_thread(ttyd.stop_for, vm_id)
        with contextlib.suppress(Exception):
            if rec.vm is not None:
                await rec.vm.stop()
        if self.backend == "qemu":
            await asyncio.to_thread(shutil.rmtree, C.VMS_DIR / vm_id, True)
        self._vms.pop(vm_id, None)
        self._slots.discard(rec.slot)
        return True

    def get(self, vm_id: str) -> Optional[Record]:
        return self._vms.get(vm_id)

    def list(self) -> list[VMView]:
        for rec in self._vms.values():
            if rec.state == "running" and rec.vm is not None and not rec.vm.alive:
                rec.state = "stopped"
        return [r.view() for r in sorted(self._vms.values(), key=lambda r: r.created_at)]

    # ----------------------------------------------------------------- reaper
    async def start_reaper(self) -> None:
        if self.backend == "qemu": self._restore_qemu()
        self._reaper = asyncio.create_task(self._reap_loop())
        if self.backend == "container":
            from .container import cleanup_orphans, pool
            await cleanup_orphans()      # containers a previous run left behind
            pool().refill_soon()
        if self.backend == "firecracker":
            # Off the startup path: the probe costs a couple of seconds and
            # nothing needs the answer until someone asks why ping fails.
            asyncio.create_task(asyncio.to_thread(net.probe_icmp))

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(10)
                now = time.time()
                expired = [r.id for r in self._vms.values()
                           if r.expires_at and now >= r.expires_at]
                for vm_id in expired:
                    await self.destroy(vm_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        from . import ttyd
        with contextlib.suppress(Exception):
            ttyd.stop_all()
        for vm_id, rec in list(self._vms.items()):
            if self.backend == "qemu" and rec.state in ("running", "stopped") and rec.vm:
                if rec.state == "running":
                    from . import publish
                    await publish.close_vm(self, vm_id)
                    with contextlib.suppress(Exception): await rec.vm.suspend()
                # Completed disks persist across app restarts and are restored stopped.
                self._save_record(rec)
            else:
                await self.destroy(vm_id)
        if self.backend == "container":
            from .container import pool
            await pool().drain()         # do not leak the warm pool
