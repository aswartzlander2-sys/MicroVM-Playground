"""Fail-closed password gate for HTTP and WebSocket traffic."""
import hashlib, hmac, json, os, secrets, time
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from urllib.parse import urlsplit
from starlette.responses import JSONResponse
from . import credentials

_sessions = {}  # opaque random tokens, intentionally invalidated at restart
_attempts = defaultdict(deque)
TTL = 12 * 60 * 60

class AuthMiddleware:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        headers = {k.decode('latin1').lower(): v.decode('latin1') for k,v in scope.get('headers', [])}
        path, method = scope['path'], scope.get('method', 'GET')
        cfg = credentials.load()
        password = os.environ.get('ROBO_PASSWORD', '') if cfg is None else ''
        configured = bool(cfg or password)
        stamp = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode() if cfg else password.encode()).hexdigest() if configured else ''
        cookie = SimpleCookie()
        try: cookie.load(headers.get('cookie',''))
        except Exception: pass
        token = cookie['robo_session'].value if 'robo_session' in cookie else ''
        now = time.time()
        for key, session_info in list(_sessions.items()):
            if session_info[0] < now: _sessions.pop(key, None)
        session = _sessions.get(token)
        signed = bool(configured and session and session[0] > now and hmac.compare_digest(session[1], stamp))
        async def reply(response): return await response(scope, receive, send)
        # Prevent cross-site browser requests, including websocket hijacking.
        origin = headers.get('origin')
        host = headers.get('host', '')
        if origin and urlsplit(origin).netloc.lower() != host.lower():
            if scope['type'] == 'websocket': return await send({'type':'websocket.close','code':1008})
            return await reply(JSONResponse({'detail':'Origin not allowed'}, status_code=403))
        if path == '/api/auth/session' and method == 'GET':
            return await reply(JSONResponse({'signedIn': signed, 'configured': configured}))
        if path == '/api/auth/login' and method == 'POST':
            if not configured: return await reply(JSONResponse({'detail':'Run ./run.sh to configure your login.'}, status_code=503))
            ip = (scope.get('client') or ('unknown',0))[0]
            # Not a defaultdict access: succeeded logins must not leave a permanent
            # per-IP entry behind (unbounded memory growth guard).
            attempts = _attempts.get(ip)
            if attempts is None: attempts = _attempts[ip] = deque()
            while attempts and attempts[0] < now - 600: attempts.popleft()
            if len(attempts) >= 8: return await reply(JSONResponse({'detail':'Too many attempts. Try again in 10 minutes.'}, status_code=429))
            body = b''
            while True:
                msg = await receive(); body += msg.get('body',b'')
                if len(body) > 4096: return await reply(JSONResponse({'detail':'Request too large'}, status_code=413))
                if not msg.get('more_body'): break
            try:
                body_json = json.loads(body)
                supplied = str(body_json.get('password',''))
                username = str(body_json.get('username',''))
            except Exception: supplied = ''; username = ''
            # Scrypt is deliberately expensive; keep it off the event loop.
            import asyncio
            if cfg is None:
                # hmac.compare_digest only supports ASCII str; encode so any
                # valid UTF-8 login compares (and never raises) in constant time.
                valid = (hmac.compare_digest(username.encode('utf-8'), os.environ.get('ROBO_USERNAME', 'admin').encode('utf-8'))
                         and hmac.compare_digest(supplied.encode('utf-8'), password.encode('utf-8')))
            else:
                valid = await asyncio.to_thread(credentials.verify, cfg, username, supplied)
            if not valid:
                attempts.append(now)
                return await reply(JSONResponse({'detail':'Incorrect username or password.'}, status_code=401))
            attempts.clear()
            # Successful login: keep failed-attempt memory only for IPs that need it.
            if not attempts: _attempts.pop(ip, None)
            token = secrets.token_urlsafe(32); _sessions[token] = (now + TTL, stamp)
            response = JSONResponse({'ok':True})
            response.set_cookie('robo_session', token, max_age=TTL, httponly=True, samesite='strict', secure=(scope.get('scheme') == 'https'))
            return await reply(response)
        if path == '/api/auth/logout' and method == 'POST':
            _sessions.pop(token, None); response = JSONResponse({'ok':True}); response.delete_cookie('robo_session'); return await reply(response)
        protected = path.startswith('/api/') or path.startswith('/terminal/')
        if protected and not signed:
            if scope['type'] == 'websocket': return await send({'type':'websocket.close','code':1008})
            return await reply(JSONResponse({'detail':'Sign in required'}, status_code=401))
        return await self.app(scope, receive, send)
