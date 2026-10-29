"""Bounded SFTP file operations via the running QEMU guest's loopback forward."""
import stat
import paramiko

MAX_TEXT = 1024 * 1024
MAX_DOWNLOAD = 16 * 1024 * 1024

class FileError(Exception): pass

def handle(port, data):
    if not isinstance(data, dict):
        raise FileError('Bad request body')
    action = data.get('action')
    path = data.get('path', '/')
    user = data.get('username', '')
    password = data.get('password', '')
    if not isinstance(path, str) or not path.startswith('/') or '\x00' in path or len(path) > 4096:
        raise FileError('Use an absolute guest path')
    if not isinstance(user, str) or not user or len(user) > 128 or not isinstance(password, str) or len(password) > 1024:
        raise FileError('Enter your guest SSH username and password')
    if action not in ('list','read','write','download'):
        raise FileError('Unsupported operation')
    client = paramiko.SSHClient()
    # Loopback-only QEMU port is owned by the selected VM; each connection is
    # ephemeral. Do not use this policy for a remote or user-specified host.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect('127.0.0.1', port=int(port), username=user, password=password,
                       timeout=6, banner_timeout=6, auth_timeout=6,
                       look_for_keys=False, allow_agent=False)
        with client.open_sftp() as sftp:
            if action == 'list':
                entries = sftp.listdir_attr(path)
                if len(entries)>2000: raise FileError('Directory has more than 2,000 entries')
                return {'path': path, 'entries': [{'name': a.filename, 'directory': stat.S_ISDIR(a.st_mode), 'size': a.st_size, 'symlink': stat.S_ISLNK(a.st_mode)} for a in sorted(entries, key=lambda x:(not stat.S_ISDIR(x.st_mode),x.filename.lower()))]}
            if action in ('read','download'):
                limit = MAX_TEXT if action == 'read' else MAX_DOWNLOAD
                with sftp.open(path, 'rb') as f: content = f.read(limit+1)
                if len(content)>limit: raise FileError('File exceeds size limit (%s MiB)' % (limit//1024//1024))
                if action == 'download': return content
                try: return {'content':content.decode('utf-8')}
                except UnicodeDecodeError: raise FileError('Not UTF-8 text. Download the file instead.')
            content = data.get('content')
            if not isinstance(content,str): raise FileError('Content must be text')
            encoded=content.encode('utf-8')
            if len(encoded)>MAX_TEXT: raise FileError('Text file exceeds 1 MiB')
            # Refuse to create files or follow links; saves are explicit overwrites.
            info=sftp.lstat(path)
            if not stat.S_ISREG(info.st_mode): raise FileError('Only regular files can be edited')
            with sftp.open(path,'wb') as f: f.write(encoded)
            return {'ok':True}
    except FileError: raise
    except paramiko.AuthenticationException: raise FileError('Guest SSH credentials rejected')
    except (OSError, paramiko.SSHException) as e: raise FileError('Cannot access guest over SSH: ' + str(e)[:200])
    finally: client.close()
