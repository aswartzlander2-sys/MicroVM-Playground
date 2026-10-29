"""Local single-admin credentials. Passwords are salted and scrypt-hashed."""
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path


def auth_path():
    root = Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state'))
    return Path(os.environ.get('ROBO_AUTH_FILE', str(root/'robo-universal'/'auth.json'))).expanduser()


def load():
    try:
        cfg = json.loads(auth_path().read_text(encoding='utf-8'))
        if not isinstance(cfg.get('username'), str) or not cfg['username'] or not re.fullmatch(r'[0-9a-f]{32}',cfg.get('salt','')) or not re.fullmatch(r'[0-9a-f]{64}',cfg.get('hash','')):
            return None
        return cfg
    except (OSError, ValueError, TypeError): return None


def verify(cfg, username, password):
    if not cfg or not isinstance(username,str) or not isinstance(password,str) or len(username)>128 or len(password)>1024: return False
    try:
        digest = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(cfg['salt']), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(username.encode('utf-8'),cfg['username'].encode('utf-8')) & hmac.compare_digest(digest,bytes.fromhex(cfg['hash']))
    except (ValueError,UnicodeError): return False


def setup(reset=False, password_only=False):
    path=auth_path()
    if path.exists() and not reset:
        if not load(): raise RuntimeError('Credentials file exists but is invalid: '+str(path))
        print('  Login already configured.')
        return
    if not sys.stdin.isatty(): raise RuntimeError('First-run login setup needs an interactive terminal. Run ./run.sh in a terminal.')
    existing = load() if password_only else None
    if password_only and not existing: raise RuntimeError('No valid administrator login exists. Run ./run.sh first.')
    print('\n' + ('Set a new administrator password.' if password_only else 'Create your Robo Universal administrator login.'))
    if existing:
        username = existing['username']
        print('  Username: ' + username)
    else:
        while True:
            username=input('  Username: ').strip()
            if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',username): break
            print('  Use 1–64 letters, numbers, dots, underscores or hyphens.')
    while True:
        pw=getpass.getpass('  Password (at least 12 characters): ')
        if len(pw)<12 or len(pw)>1024:
            print('  Use a long, unique password of 12–1024 characters.'); continue
        confirm=getpass.getpass('  Repeat password: ')
        if not hmac.compare_digest(pw.encode(),confirm.encode()): print('  Passwords do not match; try again.'); continue
        break
    salt=secrets.token_bytes(16)
    digest=hashlib.scrypt(pw.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix='.auth-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as f:
            json.dump({'username':username,'salt':salt.hex(),'hash':digest.hex()},f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    print('  Login saved securely to '+str(path))

if __name__=='__main__':
    try: setup('--reset' in sys.argv or '--passwd' in sys.argv, '--passwd' in sys.argv)
    except (RuntimeError, EOFError, KeyboardInterrupt, OSError) as exc:
        print('\nSetup stopped: '+str(exc),file=sys.stderr); sys.exit(1)
