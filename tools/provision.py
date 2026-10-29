#!/usr/bin/env python3
"""Prepare one disposable QEMU cloud image, without booting a build VM."""
import argparse, json, os, secrets, sys, time, urllib.request
from pathlib import Path
import pycdlib
import io

IMAGES = {
 'debian': ('Debian 12', 'https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2'),
 'ubuntu': ('Ubuntu 24.04', 'https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img'),
 'arch': ('Arch Linux', 'https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2'),
 'rocky': ('Rocky Linux 9', 'https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2'),
 'almalinux': ('AlmaLinux 9', 'https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2'),
}
MAX_SIZE=8*1024**3

def progress(directory, phase, message, percent=None):
    tmp=directory/'progress.tmp'; tmp.write_text(json.dumps({'phase':phase,'message':message,'percent':percent}),encoding='utf8'); os.replace(tmp,directory/'progress.json')

def seed_iso(path, hostname, vm_id):
    # The cloud image receives its own metadata. Serial autologin exposes root
    # only through the dashboard's protected console, not a public TCP port.
    user=f'''#cloud-config
hostname: {hostname}
manage_etc_hosts: true
ssh_pwauth: true
disable_root: false
write_files:
  - path: /etc/ssh/sshd_config.d/01-robo-guest.conf
    permissions: '0644'
    content: |
      PermitRootLogin yes
      PasswordAuthentication yes
  - path: /etc/systemd/system/serial-getty@ttyS0.service.d/override.conf
    permissions: '0644'
    content: |
      [Service]
      ExecStart=
      ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud 115200,38400,9600 %I xterm-256color
      Type=idle
runcmd:
  - [ sh, -c, 'systemctl restart ssh || systemctl restart sshd || true' ]
  - [ systemctl, daemon-reload ]
  - [ systemctl, enable, 'serial-getty@ttyS0.service' ]
  - [ systemctl, restart, 'serial-getty@ttyS0.service' ]
'''
    meta=f'instance-id: robo-{vm_id}\nlocal-hostname: {hostname}\n'
    iso=pycdlib.PyCdlib();iso.new(interchange_level=3,joliet=3,rock_ridge='1.09',vol_ident='CIDATA')
    for text,iso_name,filename in ((user,'/USERDATA.;1','user-data'),(meta,'/METADATA.;1','meta-data')):
        data=text.encode();iso.add_fp(io.BytesIO(data),len(data),iso_name,rr_name=filename,joliet_path='/'+filename)
    iso.write(str(path));iso.close();os.chmod(path,0o600)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--distro',required=True,choices=IMAGES);ap.add_argument('--hostname',required=True);ap.add_argument('--vm-id',required=True);ap.add_argument('--vm-dir',required=True);args=ap.parse_args()
    root=Path(args.vm_dir);dest=root/'provision'/'images';dest.mkdir(parents=True,exist_ok=True)
    image=dest/'golden.qcow2';tmp=dest/'image.part';name,url=IMAGES[args.distro]
    try:
        progress(root,'download',f'Downloading {name}…',0)
        request=urllib.request.Request(url,headers={'User-Agent':'RoboUniversal/1'})
        with urllib.request.urlopen(request,timeout=45) as response, tmp.open('wb') as out:
            if not response.geturl().startswith('https://'):raise RuntimeError('Refusing a non-HTTPS image redirect')
            total=int(response.headers.get('Content-Length') or 0)
            if total>MAX_SIZE: raise RuntimeError('Image is larger than the 8 GiB limit')
            received=0;last=0
            while True:
                buf=response.read(1024*1024)
                if not buf: break
                received+=len(buf)
                if received>MAX_SIZE: raise RuntimeError('Image exceeds the 8 GiB limit')
                out.write(buf)
                if time.monotonic()-last>1:
                    progress(root,'download',f'Downloading {name}: {received//1048576} MiB',min(100,int(received*100/total)) if total else None);last=time.monotonic()
            if not received or (total and received != total):raise RuntimeError('Download was incomplete')
        tmp.replace(image)
        progress(root,'prepare','Preparing guest configuration…')
        seed_iso(dest/'seed.iso',args.hostname,args.vm_id)
        progress(root,'boot','Starting QEMU; the guest may take a few minutes to boot…')
        return 0
    except Exception as e:
        tmp.unlink(missing_ok=True)
        print('Image preparation failed:',str(e),file=sys.stderr,flush=True)
        return 1
if __name__=='__main__':sys.exit(main())
