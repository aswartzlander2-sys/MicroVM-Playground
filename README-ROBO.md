# Robo Universal

> **Current Cloudflare implementation:** Robo now talks directly to the Cloudflare API and runs `cloudflared`; Aegis is not required. The older Aegis sections below describe an obsolete implementation and do not apply. The direct connector and DNS/tunnel cleanup have mock tests but have **not** been tested against a real Cloudflare account or the public internet. Do not treat this release as production-verified.
 · MicroVM

A password-protected, command-line machine dashboard built on the **microvm-playground** runtime, with UI inspiration from **alex-aero-workers**. Explore OS entries, launch compatible local images, and manage machines and browser terminals.

## Start

On an Ubuntu/Debian machine, open a terminal in the project directory and run:

```sh
./run.sh
```

On first run, it prompts for an administrator username and a password (at least 12 characters) twice. It stores a salted scrypt hash, not the password, in `~/.local/state/robo-universal/auth.json` (mode 600). It creates a Python virtual environment and installs or updates the Python requirements. If QEMU is missing, it offers to install it; it does not download any guest image at startup. QEMU installation uses `sudo` and apt, and building an image can take a while. Then it starts the dashboard in the background, so closing the terminal does not stop it. The backend itself still uses Python. To reset the login, stop the server and run `./run.sh --reset-login`; restarting invalidates sessions.

The script updates Python dependencies. It does not automatically update the Robo Universal source code or upgrade every OS package; downloads packaged as a ZIP have no trustworthy update source configured. Aegis × Burrow and its Cloudflare setup remain separate and the publishing bridge is experimental. The script does not silently install external services or modify DNS.

Open http://127.0.0.1:64732. Sessions are held in memory, expire after 12 hours and are lost on restart. Run one server process. This is a single-admin login, not a multi-user identity system. Use HTTPS behind a correctly configured trusted proxy for remote access, restrict access at the network layer, and do not expose the server directly to the public internet. Login throttling is per peer IP and process; use additional proxy-side rate limiting. The built-in xterm terminal is used, so all console connections pass through the password gate.

## Real machines and images

The backend is inherited from the upstream project. See `README.md` (original upstream documentation), `setup.sh`, and `tools/` for setup and images. Automatic backend selection may use Docker containers when VMs are unavailable. **Containers are not VMs**: they share the host kernel and provide weaker isolation. For real VMs configure Firecracker with KVM or QEMU, e.g. `MVMP_BACKEND=qemu`; install the appropriate backend and bake or supply compatible images first. Firecracker uses `.ext4` root filesystems; QEMU uses `.qcow2` disks. Place them in the configured `MVMP_STATE_DIR/images` directory. A Linux guest must be prepared for a serial console and login; arbitrary desktop images may not work. Actual supported architectures, guest OS, login configuration and networking depend on the backend and image. Check the host warnings on the VMs tab.

The catalog distinguishes entries that can be launched on the selected backend from discovery-only entries. This is a curated list, **not every Linux distribution**. For container mode, supported upstream images are pulled on first use; those are Linux userlands, not separate kernels. Other distros require appropriate prepared images. Availability and upstream image tags can change.

## DOS

FreeDOS is a free, MS-DOS-compatible project: https://www.freedos.org/download/ . Microsoft publishes historical MS-DOS source code at https://github.com/microsoft/MS-DOS . MS-DOS is not bundled. This runtime's browser terminal attaches to a serial console, while stock DOS normally uses a VGA/keyboard console. **Neither FreeDOS nor MS-DOS is launchable by this project yet**, and a bootable disk plus a QEMU graphical console integration would be needed. A disk image alone is not sufficient. The catalog entries are informational only.

## UI demo (not real machines)

```sh
MVMP_MOCK=1 ./run.sh
```

Mock mode simulates machines and does not run any operating system. The upstream code and tests are retained; see upstream README for full architecture and attribution. Distro logos are from Simple Icons (CC0); trademarks belong to their owners. Letter tiles are used where no logo is bundled.

## QEMU setup and guest files

On a Debian/Ubuntu **host**, `./setup-qemu.sh` interactively installs QEMU and bakes a Debian CLI guest image. It needs sudo and internet access and does not run from the browser. Other host platforms need a manual QEMU installation; see upstream `README.md`. **The guest image enables serial root autologin; older baked images use the initial password `playground`. New images generate a unique template password.** Use only on a trusted host and change the guest password immediately; do not expose guest SSH directly. The QEMU SSH port is forwarded to the host loopback interface only. The QEMU file viewer requires an SSH server and SFTP in the guest, and a guest user/password permitted by sshd. The browser sends those credentials to the server for each file operation; use HTTPS. The guest can start before its SSH service is ready. Only QEMU has file access in this version; mock and Firecracker do not. Saves overwrite existing regular files; download is capped at 16 MiB and editor text at 1 MiB. Changes are inside the disposable VM and are lost on deletion.

## Domains and port publishing — incomplete

The Domains & ports page can **read** active Cloudflare zones with an in-memory token, generate a candidate hostname and show an optional Let's Encrypt DNS-01 command. It does not save the token or modify Cloudflare. It **does not create Cloudflare tunnels, wildcard DNS, HTTPS routing, public/private access controls, or forward VM ports**. Names are plans, not live links. A Cloudflare wildcard DNS entry alone does not cover second-level hostnames with the free edge certificate; Aegis × Burrow documents alternative Workers Custom Domain, relay or paid certificate approaches. The Let's Encrypt command is a starting point for a compatible TLS termination setup, not a tunnel setup command. Never put a DNS API token directly in a shell command; store it in a root-readable-only credentials file.

The terminal opens in a popup. Domains & ports offers an Aegis-powered Cloudflare authorization link and a create-tunnel action; it requires an installed, running Aegis instance. The bridge has not been verified end to end and should not yet be treated as production-ready.

## Aegis bridge (experimental, not yet production-verified)

A QEMU VM's **Ports** button now asks the local Aegis × Burrow service to publish an HTTP guest port. The bridge adds a QEMU loopback-only forward and asks Burrow for a named tunnel with either Aegis-login or public access. Aegis handles Cloudflare DNS, TLS, and HTTPS/WebSocket routing. Aegis-login means Aegis's login, not the Robo password. The guest application must listen on `0.0.0.0`, not only the guest loopback address. The URL is reported by Burrow; DNS and certificates may take time, and the UI does not independently verify reachability.

Install and configure Aegis × Burrow under the **same OS account** as Robo, connect your Cloudflare domain in Aegis, and configure wildcard naming and the Workers Custom Domain option there. Aegis offers its own Cloudflare authorization/token setup. The Robo Cloudflare zone reader is separate and does **not** configure Aegis or create a tunnel. Do not provide Cloudflare tokens in chat. See https://github.com/alexd-aero/aegis-burrow#tunnel-names-two-levels-deep-free-with-nothing-open-at-home for the supported certificate modes and their limitations. Multiple zones and wildcard settings are managed in Aegis; this bridge uses its currently active naming configuration.

By default the bridge uses `~/.local/share/aegis/data/control.sock`; set `ROBO_BURROW_SOCKET` if different. Access to that socket grants Aegis administrative authority; do not make it world-readable. Public access requires explicit confirmation. The bridge journals created tunnels and tries to remove them when the VM disappears or the server restarts. **It has not been tested against a real QEMU VM, Aegis installation, Cloudflare account, or the public internet; do not use it for production yet.** In particular, crash recovery, stale tunnel cleanup, account permissions, TLS provisioning, concurrent lifecycle operations, and security boundaries need end-to-end verification. A server crash may leave an Aegis tunnel temporarily configured; check and remove it in Aegis before reusing its local port. VMs are disposable and not restored on a Robo restart.

The current bake script now creates a fresh random template password at `images/seed-<distro>-initial-password` with mode 600. All clones of that template share it; change it inside each guest. Earlier templates retain their old password. Serial root autologin remains enabled; do not use this template for untrusted workloads. This supersedes the earlier warning about `playground` for **newly baked** images only.

## Image lifecycle and startup

Upstream model, kept: the host keeps **one shared golden image** at `~/.local/state/mvmp/images/golden.qcow2`. The **first machine ever created** bakes it (download of a ~350 MB Debian cloud image plus one guest boot with the standard package set; several minutes, and it must run only once. Deleting a machine does **not** remove the shared golden). Every machine created afterwards is a **copy-on-write overlay** of that golden: creation takes seconds and uses almost no extra disk until you write bytes. Other backend modes have different image behavior. Any extra `.qcow2` dropped into the images directory appears as an additional OS entry in the catalog.

Completed QEMU VMs and images survive a normal dashboard restart and return **stopped**; use Start to boot them. Timed VMs remain subject to their expiration, including after restart. The app offers to enable a systemd user service and user lingering during `./run.sh`; enabling lingering may require sudo. A machine without a working systemd user session cannot use this automatic setup. The service binds `127.0.0.1:64732` by default. The other listed ports are not reserved or opened by the dashboard; QEMU and port publishing may use additional dynamically allocated loopback ports. Port numbers with leading zeroes are decimal (e.g. 06655 = 6655). The experimental publishing limitations above still apply.

The lifecycle has unit tests with a simulated QEMU preparation process; a full real-image boot/download/delete and reboot test is still needed on a host with QEMU and systemd.

## Names and Cloudflare setup

New Debian and Arch QEMU guests get their selected machine name as their cloud-init hostname. Names must be valid DNS labels. Changing an existing machine's name through the API is disabled for QEMU because it would otherwise diverge from the guest hostname. Previously baked VMs do not automatically change hostname.

On Domains & ports, **Connect Cloudflare** asks the local Aegis control socket for Cloudflare's authorization URL. After granting authorization for a zone, **Create tunnel** asks Aegis to create the connector and DNS record. This flow uses Aegis/cloudflared authorization; the optional API-token zone reader is not the same thing and does not create a tunnel. You must install and configure Aegis × Burrow first, and configure Aegis wildcard/Workers Custom Domain support if needed. These actions can modify the chosen Cloudflare zone. Publishing and Cloudflare integration have not been tested against a real account here.

## `microvm` command

`./run.sh` installs `microvm` in `/usr/local/bin` when run as root. For a regular user it installs under `~/.local/bin`; if that directory is not on PATH, setup offers to install a link in `/usr/local/bin` with sudo. Existing unrelated `microvm` commands are not overwritten. When a previous login is found on a new installation, setup asks whether to replace it rather than silently using it. Credentials and server state belong to the OS account running the app; run the CLI under that same account.

- `microvm passwd` changes the password while keeping the username.
- `microvm login-reset` changes both username and password.
- `microvm start`, `stop`, `restart`, `status`, and `logs` manage the dashboard.
- `microvm doctor`, `url`, and `help` provide diagnostics and guidance.
- `microvm update` updates Python dependencies; it cannot update the project source from an unconfigured ZIP.

Password changes invalidate existing browser sessions on their next request. Already-open terminal WebSockets should also be closed for immediate revocation. The command does not delete existing VMs when stopping the dashboard.

### On-demand QEMU images and stopping

The launch dialog downloads an official cloud image when you create each VM. Debian 12, Ubuntu 24.04, Arch Linux, Rocky Linux 9 and AlmaLinux 9 have download configurations; other catalog entries are discovery-only and cannot be launched. A real Debian boot was tested; the other images are not yet boot-validated. The download is over HTTPS but is not yet verified against upstream checksums or signatures. First boot can take several minutes, especially on hosts without KVM; QEMU may report running before cloud-init finishes and the terminal prompt appears. The cloud-init serial console logs in as root automatically, so keep dashboard credentials and host access secure. For the file viewer, set a guest root password with `passwd` in its terminal, then use root and that password in the file viewer; the guest SSH forwarding binds to host loopback. SSH daemon availability varies by image.

Run `./kill.sh` as the same user who started the dashboard to stop its user service or its recorded fallback process. This stops the dashboard and its managed running VMs; it does not delete VM disks or stop independently managed Aegis services. The systemd service, if enabled, remains enabled and may start again at the next login or boot. `microvm stop` does the same.

### Current changes and limitations

For new QEMU VMs, the only offered image is the original Debian playground image built by `tools/bake.py`. It is downloaded and baked per VM; first creation can take a long time while packages install. The other image choices in earlier sections are no longer offered for new QEMU VMs. Existing VM disks are not deliberately deleted by this change. The built-in browser terminal opens in a popup.

**Cloudflare publishing is not standalone yet.** The existing Domains & ports integration still requires Aegis × Burrow; it has not been replaced by a direct Cloudflare connector. Do not rely on it for production. The earlier descriptions of multiple on-demand images above are superseded by this section.


## Direct Cloudflare connection (current)

1. Install `cloudflared` on the Ubuntu/Debian host. If it is missing, interactive `./run.sh` offers to add Cloudflare’s signed apt repository and install the package. Alternatively follow https://pkg.cloudflare.com/ . No Aegis service is needed.
2. Sign into Robo over HTTPS or on a trusted local connection. In **Domains & ports**, open **Create Cloudflare token**. Review the prefilled permissions: Account Cloudflare Tunnel Edit, Zone DNS Edit, and Zone Read. Scope the token to the account and zones you intend to manage. Copy the token and paste it into Robo; choose an active zone and connect it.
3. Start a QEMU VM and an HTTP service listening on the guest network (for example `0.0.0.0:3000`). Choose **Ports**, enter the guest port and a unique hostname label, and confirm the public-exposure warning. Robo creates a managed tunnel, runs a local connector and creates a proxied CNAME. The generated label includes the port by default; it can be edited. DNS and edge certificates may take time. Only one-level hostnames directly under the selected zone are supported.
4. Unpublish before disconnecting or replacing the token. Robo removes its Cloudflare records and tunnels when a VM is stopped or deleted, and retries cleanup of journaled resources after a restart. Stopped VMs' routes are **not automatically restored**; republish them after restarting the VM. Existing resources created by the older Aegis integration are not migrated or removed—check Aegis and Cloudflare manually.

The API token is stored as plaintext in the app's state directory in `robo-cloudflare.json` with mode 0600. Anyone with host-user/root access can read it; do not share it or send it over an untrusted HTTP connection. Cloudflare tunnel tokens are passed to child processes in their environment. The endpoint does not disclose the API token back to the browser.

**Public routes only.** There is no Cloudflare Access login or private-access policy in this implementation. Private requests are rejected instead of silently creating a public route. The public route forwards HTTP only; other protocols and automatic certificate management beyond Cloudflare's standard edge certificate are not implemented. A tunnel appearing in the UI does not prove its connector, DNS, TLS, or guest service are healthy. Crash cleanup is best effort: if Cloudflare is unreachable, resources may remain until retry and should be checked manually. Networking/port allocation and cleanup have not been validated under real failure conditions; avoid production use until independently tested. The original per-VM Debian image bake also has not completed a full real boot test in this environment.
