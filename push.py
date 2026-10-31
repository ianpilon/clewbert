"""Push notifications to the installed phone app: a task finished, or an agent is waiting on you.

Standard Web Push (RFC 8291 encryption, RFC 8292 VAPID signing), so no Google account or app store is involved:
the phone's browser gives us an address to post to, and we post each new piece of news there once.
  DATA_DIR/push/vapid.pem    this server's signing key, made on first start (owner-only)
  DATA_DIR/push/subs.json    the devices that turned notifications on
  DATA_DIR/push/sent.json    the news already pushed, so a restart never sends it again
"""
import base64, hashlib, hmac, json, os, struct, threading, time, urllib.error, urllib.parse, urllib.request
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SUBJECT = 'https://github.com/ianpilon/clewbert'   # who to contact about these pushes, as push services ask


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def unb64(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def raw_public(key):
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def hkdf(salt, ikm, info, n):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b'\x01', hashlib.sha256).digest()[:n]


def encrypt(sub, payload):
    """The message body only the subscribing device can read (aes128gcm, one record)."""
    ua_public, auth = unb64(sub['keys']['p256dh']), unb64(sub['keys']['auth'])
    mine = ec.generate_private_key(ec.SECP256R1())
    as_public = raw_public(mine)
    shared = mine.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = hkdf(auth, shared, b'WebPush: info\x00' + ua_public + as_public, 32)
    salt = os.urandom(16)
    cek = hkdf(salt, ikm, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = hkdf(salt, ikm, b'Content-Encoding: nonce\x00', 12)
    body = AESGCM(cek).encrypt(nonce, payload + b'\x02', None)
    return salt + struct.pack('>IB', 4096, len(as_public)) + as_public + body


class Push:
    def __init__(self, data_dir, news):
        """news() -> {'done': [...], 'waiting': [...]}, each with boardName (the same as GET /api/news)."""
        self.dir = os.path.join(data_dir, 'push')
        os.makedirs(self.dir, exist_ok=True)
        self.news = news
        self.lock = threading.Lock()
        pem = os.path.join(self.dir, 'vapid.pem')
        if not os.path.exists(pem):
            key = ec.generate_private_key(ec.SECP256R1())
            fd = os.open(pem, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        with open(pem, 'rb') as f:
            self.key = serialization.load_pem_private_key(f.read(), None)
        self.public = b64(raw_public(self.key))
        self.subs = self.load('subs.json', [])
        sent = self.load('sent.json', None)
        self.sent = sent if sent is not None else self.keys()   # first start: what is already news is not pushed
        self.save('sent.json', self.sent)
        threading.Thread(target=self.loop, daemon=True).start()

    def load(self, name, default):
        try:
            with open(os.path.join(self.dir, name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def save(self, name, obj):
        path = os.path.join(self.dir, name)
        with open(path + '.tmp', 'w') as f:
            json.dump(obj, f)
        os.replace(path + '.tmp', path)

    def subscribe(self, sub):
        sub = {'endpoint': str(sub['endpoint']), 'keys': {'p256dh': str(sub['keys']['p256dh']), 'auth': str(sub['keys']['auth'])}}
        if not sub['endpoint'].startswith('https://'):
            raise ValueError
        with self.lock:
            self.subs = [s for s in self.subs if s['endpoint'] != sub['endpoint']] + [sub]
            self.save('subs.json', self.subs)

    def unsubscribe(self, endpoint):
        with self.lock:
            self.subs = [s for s in self.subs if s['endpoint'] != endpoint]
            self.save('subs.json', self.subs)

    # ---------- what to say ----------
    @staticmethod
    def key(n, kind):
        return ('q:%s:%s' % (n['id'], hashlib.sha1(str(n.get('question')).encode()).hexdigest()[:10]) if kind == 'waiting'
                else 'd:%s:%s' % (n['id'], n.get('ended')))

    def keys(self):
        news = self.news()
        return [self.key(n, k) for k in ('done', 'waiting') for n in news[k]]

    @staticmethod
    def message(n, kind):
        who = 'The team' if n.get('lead') else (n.get('member') or {}).get('name') or 'The agent'
        short = lambda t, k: ' '.join(str(t or '').split()[:k]) + ('…' if len(str(t or '').split()) > k else '')
        if kind == 'waiting':
            title = '%s is waiting on you' % ((n.get('member') or {}).get('name') or 'An agent')
            body = short(n.get('question'), 40)
        else:
            title = '%s %s' % (who, 'finished' if n['status'] == 'done' else 'stopped')
            body = short(n.get('summary') if n['status'] == 'done' and n.get('summary') else n.get('task'), 40)
        return {'title': title, 'body': body + (' · ' + n['boardName'] if n.get('boardName') else ''),
                'url': './#agents/' + n['id'], 'tag': n['id']}

    # ---------- sending ----------
    def auth(self, endpoint):
        u = urllib.parse.urlsplit(endpoint)
        head = b64(json.dumps({'typ': 'JWT', 'alg': 'ES256'}).encode())
        claims = b64(json.dumps({'aud': '%s://%s' % (u.scheme, u.netloc), 'exp': int(time.time()) + 12 * 3600, 'sub': SUBJECT}).encode())
        r, s = decode_dss_signature(self.key.sign((head + '.' + claims).encode(), ec.ECDSA(hashes.SHA256())))
        return 'vapid t=%s.%s.%s, k=%s' % (head, claims, b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big')), self.public)

    def send(self, msg):
        """Posts one message to every device; a device that unsubscribed (404/410) is forgotten. Returns how many took it."""
        ok = 0
        for sub in list(self.subs):
            req = urllib.request.Request(sub['endpoint'], data=encrypt(sub, json.dumps(msg).encode()), method='POST', headers={
                'Authorization': self.auth(sub['endpoint']), 'Content-Encoding': 'aes128gcm',
                'Content-Type': 'application/octet-stream', 'TTL': '86400', 'Urgency': 'high'})
            try:
                with urllib.request.urlopen(req, timeout=15):
                    ok += 1
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    self.unsubscribe(sub['endpoint'])
            except OSError:
                pass   # no connection right now: this one is missed, the app still shows it
        return ok

    def loop(self):
        while True:
            time.sleep(4)
            try:
                news = self.news()
                fresh = [(n, k) for k in ('done', 'waiting') for n in news[k] if self.key(n, k) not in self.sent]
                for n, k in fresh:
                    self.sent = (self.sent + [self.key(n, k)])[-1000:]
                    self.save('sent.json', self.sent)
                    if self.subs:
                        self.send(self.message(n, k))
            except Exception:
                pass   # never let one bad run stop the notifications
