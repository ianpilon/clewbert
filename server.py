#!/usr/bin/env python3
"""Clewbert server: serves index.html and keeps boards + images as plain files.

Usage: server.py PORT DATA_DIR
Binds 127.0.0.1 only; Cloudflare Access sits in front of it on the tunnel.
  DATA_DIR/boards/<id>.json   one board (name, items, view)
  DATA_DIR/images/<sha>.jpg   pasted screenshots, content-addressed, never rewritten
  DATA_DIR/files/<sha>.<ext>  any other dropped file (video, audio, text, documents...), content-addressed
  DATA_DIR/thumbs/<image>.jpg 400 px copies of the images, made on first ask, for the board list previews
  DATA_DIR/uploads/           files still arriving in pieces (Cloudflare caps one request at 100 MB)
  DATA_DIR/trash/             deleted boards are moved here, not erased
  DATA_DIR/text/              the words read out of each image and PDF (see talk.py)
  DATA_DIR/talks/<id>.json    the voice conversation for a board (<board id>), a space (s<space id>),
                              the unsorted boards (unsorted) or all boards (all)
  DATA_DIR/briefs/<id>.json   the AI's brief of a whole board (see briefs.py)
  DATA_DIR/spaces.json        the spaces (folders) boards are sorted into; a board's "space" names one, or none;
                              a space with "parent" is a sub-space of that top-level space (one level only)
  DATA_DIR/runs/<id>.json     agent tasks: each one fills a frame on its board (see agents/runner.py)
  DATA_DIR/push/              the phones that get notifications, and what was already pushed (see push.py)
"""
import base64, concurrent.futures, gzip, hashlib, html, json, mimetypes, os, queue, re, secrets, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import ssl
if not ssl.get_default_verify_paths().cafile and os.path.exists('/etc/ssl/cert.pem'):
    # the python.org Python ships without certificates until "Install Certificates" is run: use the Mac's own list
    ssl._create_default_https_context = lambda: ssl.create_default_context(cafile='/etc/ssl/cert.pem')
import guide, models, talk
import briefs
try:
    import push   # phone notifications; needs the cryptography package, everything else runs without it
except ImportError:
    push = None
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools'))
import miro_import
import arrange
from agents import runner as agents

PORT = int(sys.argv[1])
DATA = os.path.abspath(os.path.expanduser(sys.argv[2]))
models.SETTINGS = os.path.join(DATA, 'settings.json')   # which model does which job, and outside keys (owner-only file)
HERE = os.path.dirname(os.path.abspath(__file__))
BOARDS, IMAGES, TRASH, TALKS, FILES, UPLOADS, THUMBS = (os.path.join(DATA, d) for d in ('boards', 'images', 'trash', 'talks', 'files', 'uploads', 'thumbs'))
for d in (BOARDS, IMAGES, TRASH, TALKS, FILES, UPLOADS, THUMBS):
    os.makedirs(d, exist_ok=True)

MAX_BODY = 25 * 1024 * 1024
BOARD_ID = re.compile(r'^[a-z0-9]{6,40}$')
IMAGE_NAME = re.compile(r'^[0-9a-f]{64}\.(jpg|png|pdf)$')
FILE_NAME = re.compile(r'^[0-9a-f]{64}\.[a-z0-9]{1,10}$')
UPLOAD_ID = re.compile(r'^[0-9a-f]{12}$')
MAX_FILE = 4 * 1024 ** 3
PLAIN_TEXT = set('txt md markdown csv tsv json xml yaml yml log ini toml py js ts jsx tsx css sh swift go rs java c h cpp rb sql srt vtt'.split())
TEXTUTIL = set('doc docx rtf rtfd odt html htm webarchive wordml'.split())   # macOS textutil turns these into plain text
for _t, _e in (('audio/mp4', '.m4a'), ('video/x-matroska', '.mkv'), ('audio/flac', '.flac'), ('text/markdown', '.md')):
    mimetypes.add_type(_t, _e)
for _u in os.listdir(UPLOADS):   # pieces of uploads that never finished
    if time.time() - os.path.getmtime(os.path.join(UPLOADS, _u)) > 86400:
        os.remove(os.path.join(UPLOADS, _u))
INLINE = ('video/', 'audio/', 'image/', 'application/pdf')   # shown in the browser; anything else downloads
LOCK = threading.Lock()
IMPORTS = {}   # import job id -> {state, done, total, board, name, message}; kept in memory, the page polls it


def run_import(job, link, space, crops=None):
    j = IMPORTS[job]

    def progress(done, total):
        j.update(done=done, total=total)
    try:
        r = miro_import.import_board(link, DATA, space=space, progress=progress, crops=crops)
        for name in r['images']:
            READER.add(name)   # read every picture and PDF so the board can be talked to
        BRIEFER.now(r['id'])   # then the whole board, for its brief
        j.update(state='done', board=r['id'], name=r['name'], brought=r['brought'], skipped=r['skipped'])
    except miro_import.ImportError_ as e:
        j.update(state='error', message=str(e))
    except Exception as e:
        j.update(state='error', message='The import stopped (%s). Try again in a moment.' % type(e).__name__)


COMPLETE_BACKUPS = os.path.join(DATA, 'backups', 'before-complete')


def complete_layers(items, added):
    """New items above the imported frames and backdrops, under everything else; connectors and drawings on top.
    Then whole-number layers again (CSS ignores 129.5)."""
    floor = max([i.get('z', 0) for i in items if i.get('type') == 'area' and i not in added] + [0])
    ceil = min([i.get('z', 0) for i in items if i.get('type') != 'area' and i not in added and i.get('z', 0) > floor] + [floor + 1])
    top = max([i.get('z', 0) for i in items if i not in added] + [0])
    low = [a for a in added if a['type'] not in ('arrow', 'ink')]
    for n, a in enumerate(low):
        a['z'] = floor + (ceil - floor) * (n + 1) / (len(low) + 1)
    for n, a in enumerate([a for a in added if a['type'] in ('arrow', 'ink')], 1):
        a['z'] = top + n
    for n, i in enumerate(sorted(items, key=lambda i: i.get('z', 0)), 1):
        i['z'] = n


def complete_board(bid, req):
    """Add what the Miro import missed and fix what it drew wrong (tools/miro_complete.py). Items carry `miro`, their
    Miro source, and one already on the board is never added again. Pictures are fetched in the background (a job)."""
    os.makedirs(COMPLETE_BACKUPS, exist_ok=True)
    backup = os.path.join(COMPLETE_BACKUPS, bid + '.json')
    if not os.path.exists(backup):
        shutil.copy(board_path(bid), backup)
    move, patch = req.get('move') or {}, req.get('patch') or {}
    out = {}

    def change(board):
        items = board.get('items') or []
        have = {i.get('miro') for i in items if i.get('miro')}
        imported = [i for i in items if i.get('type') in ('img', 'note', 'area')]
        has_ids = any(i.get('miro') for i in imported) or len(imported) < 5   # boards imported before items kept their Miro id get no
        # missing-item adds (they would come twice); a board the import left (nearly) empty does
        add = [a for a in req.get('add') or [] if isinstance(a, dict) and a.get('miro') not in have
               and a.get('type') in ('note', 'area', 'link', 'ink', 'arrow', 'emoji') and (has_ids or not a.get('missing'))]
        out['images'] = [m for m in req.get('images') or [] if isinstance(m, dict) and m.get('miro') not in have and has_ids]
        blanked = moved = restyled = 0
        if req.get('blank_shape_titles'):
            for i in items:
                if i.get('type') == 'area' and i.get('text') == 'Shape':
                    i['text'] = ''
                    blanked += 1
        for i in items:   # what the import drew wrong (text size, see-through fill, alignment)
            p = patch.get(i.get('miro') or '')
            if isinstance(p, dict) and i.get('type') == 'arrow' and p.get('shape') in ('elbow', 'curve'):
                i['shape'] = p['shape']
                restyled += 1
            elif isinstance(p, dict) and i.get('type') in ('note', 'area'):
                for k in ('fs', 'color', 'align'):
                    if k in p and not (i['type'] == 'area' and k != 'color'):
                        i[k] = p[k]
                restyled += 1
            m = move.get(i.get('miro') or '')
            if isinstance(m, list) and len(m) == 4 and (abs(i['x'] - m[0]) > 3 or abs(i['y'] - m[1]) > 3):
                i['x'], i['y'], i['w'], i['h'] = (float(v) for v in m)
                moved += 1
        ids = {i['miro']: i['id'] for i in items if i.get('miro')}
        ids.update({a['miro']: a['id'] for a in add})
        added = []
        for a in add:   # a connector joins the items it joins in Miro, or stays the line Miro drew
            if a['type'] == 'arrow':
                f, t = ids.get(a.pop('from_miro', None)), ids.get(a.pop('to_miro', None))
                fb = a.pop('fallback', None)
                if f and t and f != t:
                    a['from'], a['to'] = f, t
                elif fb:
                    a = fb
                else:
                    continue
            a.pop('missing', None)
            added.append(a)
        items.extend(added)
        complete_layers(items, added)
        board['items'] = items
        out.update(added=len(added), moved=moved, restyled=restyled, blank_titles=blanked)
        return out
    edit_board(bid, change)
    live_send(bid, {'kind': 'saved', 'updated': read_board(bid)['updated']}, '')   # open copies fetch it
    images = out.pop('images')
    if images:
        job = secrets.token_hex(6)
        IMPORTS[job] = {'state': 'running', 'done': 0, 'total': len(images)}
        threading.Thread(target=run_complete_images, args=(job, bid, str(req.get('miro_board') or ''), images), daemon=True).start()
        out['images_job'], out['images'] = job, len(images)
    return out


def run_complete_images(job, bid, miro_board, images):
    """The pictures and stamps the import dropped (SVG icons, failed downloads), with their crops."""
    j = IMPORTS[job]
    store, tmp, got = miro_import.Store(DATA), tempfile.mkdtemp(prefix='miro-complete-'), []

    def one(m):
        try:
            if m.get('url'):
                if not re.match(r'^https://([a-z0-9-]+\.)*(mirostatic\.com|miro\.com)/', m['url']):
                    return
                data = miro_import.get(m['url'], auth=False, raw=True)
            else:
                i = miro_import.api('boards/%s/images/%s' % (urllib.parse.quote(miro_board), urllib.parse.quote(m['miro'])))
                data = miro_import.resource(i['data']['imageUrl'])
            if m.get('crop'):
                data = miro_import.cut(data, m['crop'], tmp)
            src = store.picture(data, miro_import.ext_of(data), tmp)
            got.append({'type': 'img', 'src': src, 'x': float(m['x']), 'y': float(m['y']), 'w': float(m['w']), 'h': float(m['h']),
                        'miro': m['miro'], 'id': 'c' + secrets.token_hex(5)})
        except Exception:
            pass
        j['done'] = j.get('done', 0) + 1
    try:
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            list(pool.map(one, images))
        shutil.rmtree(tmp, ignore_errors=True)

        def put(board):
            items = board.get('items') or []
            have = {i.get('miro') for i in items if i.get('miro')}
            new = [g for g in got if g['miro'] not in have]
            items.extend(new)
            complete_layers(items, new)
            board['items'] = items
            return len(new)
        n = edit_board(bid, put)
        live_send(bid, {'kind': 'saved', 'updated': read_board(bid)['updated']}, '')
        for g in got:
            READER.add(g['src'].rsplit('/', 1)[1])
        j.update(state='done', added=n)
    except Exception as e:
        j.update(state='error', message='The picture step stopped (%s).' % type(e).__name__)


def run_complete_files(job, bid, miro_board, wanted):
    """Miro documents as file cards (or PDF pictures, or video), embeds as link cards (tools/miro_files.py)."""
    j = IMPORTS[job]
    got = []

    def one(m):
        try:
            box = {'x': float(m['x']), 'y': float(m['y']), 'w': float(m['w']), 'h': float(m['h']), 'miro': str(m['miro']),
                   'id': 'c' + secrets.token_hex(5)}
            if m.get('kind') == 'link':
                got.append({'type': 'link', 'url': str(m['url']), **box,
                            'unfurl': {'title': str(m.get('title') or ''), 'desc': str(m.get('desc') or ''), 'img': str(m.get('img') or ''), 'pub': ''}})
                return
            if not str(m.get('doc', '')).startswith('https://api.miro.com/'):
                return
            data = miro_import.resource(m['doc'])
            part = os.path.join(UPLOADS, 'miro-' + secrets.token_hex(8))
            with open(part, 'wb') as f:
                f.write(data)
            title = os.path.basename(str(m.get('title') or 'file'))[:200]
            r = finish_upload(part, title)
            if r['kind'] == 'pdf':
                got.append({'type': 'img', 'src': r['src'], 'url': r['url'], 'label': title, **box})
            elif r['kind'] in ('video', 'audio'):
                got.append({'type': 'media', 'kind': r['kind'], 'src': r['src'], 'label': title, **box})
            else:
                got.append({'type': 'file', 'label': title, 'src': r['src'], 'size': r['size'], 'preview': r.get('preview') or '', **box})
        except Exception:
            pass
        j['done'] = j.get('done', 0) + 1
    try:
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            list(pool.map(one, wanted))

        def put(board):
            items = board.get('items') or []
            have = {i.get('miro') for i in items if i.get('miro')}
            new = [g for g in got if g['miro'] not in have]
            items.extend(new)
            complete_layers(items, new)
            board['items'] = items
            return len(new)
        n = edit_board(bid, put)
        live_send(bid, {'kind': 'saved', 'updated': read_board(bid)['updated']}, '')
        j.update(state='done', added=n)
    except Exception as e:
        j.update(state='error', message='The file step stopped (%s).' % type(e).__name__)


def run_recrop(job, bid, miro_board, crops):
    """Fix the cropped pictures of a board imported before its crops were known (tools/miro_recrop.py)."""
    j = IMPORTS[job]
    try:
        cut = miro_import.recut(miro_board, crops, DATA)

        def swap(board):
            n = 0
            for i in board.get('items') or []:
                if i.get('type') == 'img' and i.get('miro') in cut:
                    i['src'] = cut[i['miro']]
                    n += 1
            return n
        n = edit_board(bid, swap)
        live_send(bid, {'kind': 'saved', 'updated': read_board(bid)['updated']}, '')   # open copies fetch the new pictures
        for src in cut.values():
            READER.add(src.rsplit('/', 1)[1])
        j.update(state='done', fixed=n)
    except Exception as e:
        j.update(state='error', message='The crop fix stopped (%s).' % type(e).__name__)


READER = talk.Reader(IMAGES, os.path.join(DATA, 'text'), os.path.join(HERE, 'bin', 'textof'))
FINDER = talk.Finder(BOARDS, READER)
threading.Thread(target=FINDER.fresh, daemon=True).start()   # the first search would wait ~8 s to read every board


def board_path(bid):
    return os.path.join(BOARDS, bid + '.json')


# ---------- link previews: read from the page's own tags, here (no outside service, no daily limit) ----------
PRIVATE_NET = re.compile(r'^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.|::1$|fc|fd|fe80)')


def unfurl(url):
    """{title, desc, img, pub} of a web page from its og:/twitter: tags and <title>; refuses addresses on your own network."""
    host = urllib.parse.urlsplit(url).hostname or ''
    if not re.match(r'^https?://', url) or not host or any(PRIVATE_NET.match(a[4][0]) for a in socket.getaddrinfo(host, None)):
        raise ValueError
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                                               '(KHTML, like Gecko) Chrome/130.0 Safari/537.36', 'Accept-Language': 'en'})
    with urllib.request.urlopen(req, timeout=8) as f:
        if 'html' not in (f.headers.get('Content-Type') or ''):
            return {}
        page = f.read(800000).decode(f.headers.get_content_charset() or 'utf-8', 'replace')
    meta = {}
    for m in re.finditer(r'<meta\s[^>]*>', page, re.I):
        k = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)', m.group(0), re.I)
        v = re.search(r'content\s*=\s*"([^"]*)"|content\s*=\s*\'([^\']*)\'', m.group(0), re.I)
        if k and v:
            meta.setdefault(k.group(1).lower(), html.unescape(v.group(1) if v.group(1) is not None else v.group(2)).strip())
    t = re.search(r'<title[^>]*>(.*?)</title>', page, re.I | re.S)
    img = meta.get('og:image') or meta.get('twitter:image') or ''
    return {'title': (meta.get('og:title') or meta.get('twitter:title') or (html.unescape(t.group(1)).strip() if t else ''))[:300],
            'desc': (meta.get('og:description') or meta.get('description') or meta.get('twitter:description') or '')[:500],
            'img': urllib.parse.urljoin(url, img) if img else '', 'pub': meta.get('og:site_name') or ''}


# ---------- live: every open copy of a board hears what another copy does (moves as they happen, saves when done) ----------
LIVE, LIVE_LOCK = {}, threading.Lock()   # board id -> {queue: client id}


def live_send(bid, msg, sender):
    with LIVE_LOCK:
        listeners = list((LIVE.get(bid) or {}).items())
    for q, client in listeners:
        if client != sender:
            try:
                q.put_nowait(msg)
            except queue.Full:   # a copy that stopped reading: it catches up from the next save
                pass


def read_board(bid):
    with open(board_path(bid)) as f:
        return json.load(f)


def write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def talk_path(scope):
    return os.path.join(TALKS, scope + '.json')


SPACES = os.path.join(DATA, 'spaces.json')


def read_spaces():
    try:
        with open(SPACES) as f:
            return json.load(f)
    except FileNotFoundError:
        return {'spaces': []}


def all_boards():
    out = []
    for fn in os.listdir(BOARDS):
        if fn.endswith('.json'):
            try:
                out.append(read_board(fn[:-5]))
            except (OSError, ValueError):
                pass
    return out


def read_talk(scope):
    try:
        with open(talk_path(scope)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {'turns': []}


def now_ms():
    return int(time.time() * 1000)


def file_words(path, ext):
    """Plain text of a dropped document, for the card preview and for the voice; '' when it has none."""
    try:
        if ext in PLAIN_TEXT:
            with open(path, 'rb') as f:
                return f.read(2_000_000).decode('utf-8', 'replace')
        if ext in TEXTUTIL:
            r = subprocess.run(['textutil', '-convert', 'txt', '-stdout', path], capture_output=True, timeout=120)
            return r.stdout.decode('utf-8', 'replace') if r.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ''


def finish_upload(part, filename):
    """A fully arrived upload becomes a content-addressed file; PDFs join the images so they get a first-page picture."""
    h = hashlib.sha256()
    with open(part, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    with open(part, 'rb') as f:
        head = f.read(8)
    ext = (os.path.splitext(filename)[1][1:].lower() or 'bin')
    if not re.match(r'^[a-z0-9]{1,10}$', ext):
        ext = 'bin'
    size = os.path.getsize(part)
    if head[:5] == b'%PDF-':
        name = h.hexdigest() + '.pdf'
        with open(part, 'rb') as f:
            data = f.read()
        os.remove(part)
        path = os.path.join(IMAGES, name)
        if not os.path.exists(path):
            with open(path + '.tmp', 'wb') as f:
                f.write(data)
            os.replace(path + '.tmp', path)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                src = miro_import.Store(DATA).picture(data, 'pdf', tmp)
            except (OSError, subprocess.CalledProcessError, ValueError):
                src = None
        READER.add(name)
        if src:
            READER.add(src.rsplit('/', 1)[1])
        return {'kind': 'pdf', 'src': src, 'url': 'api/images/' + name, 'size': size}
    name = h.hexdigest() + '.' + ext
    path = os.path.join(FILES, name)
    if os.path.exists(path):
        os.remove(part)
    else:
        os.replace(part, path)
    mime = mimetypes.guess_type('x.' + ext)[0] or 'application/octet-stream'
    words = READER.words(name)
    if not os.path.exists(READER.path(name)):
        words = file_words(path, ext)
        with open(READER.path(name) + '.tmp', 'w') as f:
            f.write(words)
        os.replace(READER.path(name) + '.tmp', READER.path(name))
    kind = 'video' if mime.startswith('video/') else 'audio' if mime.startswith('audio/') else 'file'
    return {'kind': kind, 'src': 'api/files/' + name, 'mime': mime, 'size': size,
            'preview': re.sub(r'[ \t]+', ' ', words[:600]).strip()}


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body=b'', ctype='application/json', headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        headers = dict(headers or {})
        if (len(body) > 1400 and ctype.startswith(('application/json', 'text/')) and 'gzip' in (self.headers.get('Accept-Encoding') or '')):
            body = gzip.compress(body, 5)   # boards are mostly repeated JSON keys: about a third of the size over the wire
            headers['Content-Encoding'] = 'gzip'
            headers['Vary'] = 'Accept-Encoding'
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_BODY:
            raise OverflowError
        return self.rfile.read(n)

    def parts(self):
        return [p for p in self.path.split('?')[0].split('/') if p]

    def talk_scope(self, parts):
        if len(parts) == 3 and parts[:2] == ['api', 'talk'] and (parts[2] in ('all', 'unsorted') or BOARD_ID.match(parts[2])):
            return parts[2]
        return None

    def space_id(self, parts):
        if len(parts) == 3 and parts[:2] == ['api', 'spaces'] and BOARD_ID.match(parts[2]):
            return parts[2]
        return None

    def board_id(self, parts):
        if len(parts) == 3 and parts[:2] == ['api', 'boards'] and BOARD_ID.match(parts[2]):
            return parts[2]
        return None

    def handle_one(self, fn):
        try:
            fn(self.parts())
        except OverflowError:
            self.reply(413, {'error': 'too large'})
        except (ValueError, KeyError, TypeError):
            self.reply(400, {'error': 'bad request'})

    def do_GET(self):
        self.handle_one(self.get)

    do_HEAD = do_GET

    def do_POST(self):
        self.handle_one(self.post)

    def do_PUT(self):
        self.handle_one(self.put)

    def do_DELETE(self):
        self.handle_one(self.delete)

    def live(self, bid):
        """Server-sent events for one open board: what other copies of it do, until the page goes away."""
        client = (urllib.parse.parse_qs(self.path.partition('?')[2]).get('client') or [''])[0][:40]
        q = queue.Queue(maxsize=500)
        with LIVE_LOCK:
            LIVE.setdefault(bid, {})[q] = client
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        try:
            self.wfile.write(b': open\n\n')
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=20)
                except queue.Empty:
                    msg = None   # a comment every 20 s keeps the connection (and Cloudflare) from timing out
                self.wfile.write(b': ping\n\n' if msg is None else ('data: %s\n\n' % json.dumps(msg)).encode())
                self.wfile.flush()
        except OSError:   # the page closed or moved to another board
            pass
        finally:
            with LIVE_LOCK:
                (LIVE.get(bid) or {}).pop(q, None)

    def get(self, parts):
        if parts in ([], ['index.html']):
            with open(os.path.join(HERE, 'index.html'), 'rb') as f:
                return self.reply(200, f.read(), 'text/html; charset=utf-8', {'Cache-Control': 'no-cache'})
        if parts in (['manifest.webmanifest'], ['sw.js']):   # what makes the phone install this as an app (see sw.js)
            with open(os.path.join(HERE, parts[0]), 'rb') as f:
                return self.reply(200, f.read(), 'application/manifest+json' if parts[0].endswith('manifest') else 'text/javascript',
                                  {'Cache-Control': 'no-cache'})
        if len(parts) == 2 and parts[0] == 'icons' and re.match(r'^[a-z0-9-]{1,40}\.png$', parts[1]):
            try:
                with open(os.path.join(HERE, 'icons', parts[1]), 'rb') as f:
                    return self.reply(200, f.read(), 'image/png', {'Cache-Control': 'max-age=86400'})
            except FileNotFoundError:
                return self.reply(404, {'error': 'no such icon'})
        if len(parts) == 3 and parts[:2] == ['api', 'live'] and BOARD_ID.match(parts[2]):
            return self.live(parts[2])
        if parts == ['api', 'unfurl']:
            url = (urllib.parse.parse_qs(self.path.partition('?')[2]).get('url') or [''])[0][:2000]
            try:
                return self.reply(200, unfurl(url))
            except (OSError, ValueError, UnicodeError):
                return self.reply(200, {})   # no preview: the page shows the plain link card
        if parts == ['api', 'search']:   # the search bar, on every keystroke: matches only, no model
            qs = urllib.parse.parse_qs(self.path.partition('?')[2])
            here = (qs.get('board') or [''])[0]
            return self.reply(200, FINDER.search((qs.get('q') or [''])[0][:200], here if BOARD_ID.match(here) else None))
        if parts == ['api', 'session']:   # the ears ask this who is calling, with the caller's cookie
            who = talk.access_user(self.headers.get('Cookie'))
            return self.reply(200, {'user': {'id': 'clewbert', 'name': who}}) if who else self.reply(401, {'error': 'not signed in'})
        if len(parts) == 3 and parts[:2] == ['api', 'brief'] and BOARD_ID.match(parts[2]):
            return self.reply(200, BRIEFER.view(parts[2]))
        if parts == ['api', 'settings']:
            return self.reply(200, dict(models.public(), local=local_models(), startup=local_models(False)))
        if parts == ['api', 'settings', 'local']:   # the models your own machines serve now, to pick from
            found = own_models()
            return self.reply(200, {'models': found, 'startup': real_names(local_models(False)), 'local': real_names(local_models())})
        if len(parts) == 4 and parts[:3] == ['api', 'settings', 'models'] and parts[3] in models.PROVIDERS:
            try:
                return self.reply(200, {'models': models.list_models(parts[3])})
            except (OSError, ValueError) as e:
                return self.reply(200, {'models': [], 'error': str(e)[:200]})
        if parts == ['api', 'setup']:   # the welcome screen shows until a model answers
            return self.reply(200, {'ready': bool(models.choice('voice') or models.local('voice') or talk.MODEL_URL),
                                    'speech': (models.speech() or [None])[0] if not talk.TTS_URL else 'local'})
        if parts == ['api', 'voices']:
            try:
                return self.reply(200, talk.voices() if talk.TTS_URL else models.voices() if models.speech() else {'voices': []})
            except OSError:
                return self.reply(503, {'error': 'voice service unavailable'})
        if parts == ['api', 'spaces']:
            return self.reply(200, read_spaces())
        if len(parts) == 3 and parts[:2] == ['api', 'agents'] and BOARD_ID.match(parts[2]):   # the page polls this
            try:
                items = [i for i in read_board(parts[2]).get('items') or [] if i.get('agent')]
            except FileNotFoundError:
                return self.reply(404, {'error': 'no such board'})
            return self.reply(200, {'runs': RUNNER.view(parts[2]), 'items': items})
        if len(parts) == 3 and parts[:2] == ['api', 'runs'] and BOARD_ID.match(parts[2]):
            return self.reply(200, RUNNER.steps(parts[2]) or {'error': 'no such task'})
        if parts == ['api', 'tasks']:   # the phone app's Agents screen: every task on every board
            names = board_names()
            return self.reply(200, {'runs': [dict(r, boardName=names[r['board']]) for r in RUNNER.view() if r['board'] in names]})
        if parts == ['api', 'push', 'key']:
            return self.reply(200, {'key': PUSH.public}) if PUSH else self.reply(503, NO_PUSH)
        if len(parts) == 3 and parts[:2] == ['api', 'teams'] and BOARD_ID.match(parts[2]):
            return self.reply(200, dict(RUNNER.team(parts[2]), starters=agents.STARTERS))
        if parts == ['api', 'news']:   # what the person has not been told yet, on every board (the voice and notifications)
            return self.reply(200, named_news())
        if len(parts) == 3 and parts[:2] == ['api', 'import'] and parts[2] in IMPORTS:
            return self.reply(200, IMPORTS[parts[2]])
        if parts == ['api', 'boards']:
            if 'light=1' in self.path:   # the board list: each card without its items (they come from api/previews)
                return self.reply(200, board_cards())
            out = all_boards()
            out.sort(key=lambda b: b.get('updated', 0), reverse=True)
            return self.reply(200, out)
        if parts == ['api', 'previews']:   # the biggest items of the boards whose cards just scrolled into view
            ids = (urllib.parse.parse_qs(self.path.partition('?')[2]).get('ids') or [''])[0].split(',')[:40]
            out = {}
            for bid in ids:
                if BOARD_ID.match(bid):
                    try:
                        out[bid] = light(read_board(bid))['items']
                    except (OSError, ValueError):
                        pass
            if ids == [BOARDSHOT_SEEN[0]] and ids[0] in out:
                BOARDSHOT_SEEN[1] = True   # the board's picture is being taken, and the page got its items
            return self.reply(200, out)
        if self.talk_scope(parts):
            return self.reply(200, read_talk(self.talk_scope(parts)))
        bid = self.board_id(parts)
        if bid:
            try:
                return self.reply(200, read_board(bid))
            except FileNotFoundError:
                return self.reply(404, {'error': 'no such board'})
        if len(parts) == 3 and parts[:2] == ['api', 'images'] and IMAGE_NAME.match(parts[2]):
            try:
                with open(os.path.join(IMAGES, parts[2]), 'rb') as f:
                    data = f.read()
            except FileNotFoundError:
                return self.reply(404, {'error': 'no such image'})
            ctype = {'jpg': 'image/jpeg', 'png': 'image/png', 'pdf': 'application/pdf'}[parts[2].rsplit('.', 1)[1]]
            return self.reply(200, data, ctype, {'Cache-Control': 'private, max-age=31536000, immutable'})
        if len(parts) == 3 and parts[:2] == ['api', 'view'] and re.match(r'^[0-9a-f]{64}\.pdf$', parts[2]):
            # a PDF in its own tab, titled with its name on the board (the file's own title is often "Untitled")
            name = (urllib.parse.parse_qs(self.path.partition('?')[2]).get('name') or ['PDF'])[0][:200]
            page = ('<!doctype html><meta charset="utf-8"><title>%s</title><style>html,body{margin:0;height:100%%;background:#525659}'
                    'iframe{border:0;width:100%%;height:100%%;display:block}</style><iframe src="../images/%s" title="%s"></iframe>'
                    % (html.escape(name), parts[2], html.escape(name)))
            return self.reply(200, page.encode(), 'text/html; charset=utf-8', {'Cache-Control': 'no-cache'})
        if len(parts) == 3 and parts[:2] == ['api', 'demos'] and re.match(r'^r[0-9a-f]{12}-[a-z0-9-]{1,40}\.html$', parts[2]):
            # a page an agent wrote: sealed off from the canvas (its own blank origin: no cookies, no access to our data)
            try:
                with open(os.path.join(DATA, 'demos', parts[2]), 'rb') as f:
                    return self.reply(200, f.read(), 'text/html; charset=utf-8', {
                        'Cache-Control': 'no-cache', 'Content-Security-Policy':
                        "sandbox allow-scripts allow-forms allow-pointer-lock allow-modals; connect-src 'none'"})
            except FileNotFoundError:
                return self.reply(404, {'error': 'no such demo'})
        if len(parts) == 3 and parts[:2] == ['api', 'shots'] and re.match(r'^r[0-9a-f]{12}\.jpg$', parts[2]):   # an agent's browser view
            try:
                with open(os.path.join(DATA, 'shots', parts[2]), 'rb') as f:
                    return self.reply(200, f.read(), 'image/jpeg', {'Cache-Control': 'no-cache'})
            except FileNotFoundError:
                return self.reply(404, {'error': 'no screenshot yet'})
        if len(parts) == 3 and parts[:2] == ['api', 'boardshots'] and parts[2].endswith('.jpg') and BOARD_ID.match(parts[2][:-4]):
            # a board's picture for its card; the page asks with ?u=<updated>, so a current one is kept by the browser
            path = os.path.join(BOARDSHOTS, parts[2])
            try:
                with open(path, 'rb') as f:
                    data = f.read()
                fresh = os.stat(path).st_mtime >= os.stat(board_path(parts[2][:-4])).st_mtime
            except OSError:
                # the page draws the preview itself; never kept (Cloudflare otherwise has the browser keep a 404 for 4 h)
                return self.reply(404, {'error': 'no picture yet'}, headers={'Cache-Control': 'no-store'})
            return self.reply(200, data, 'image/jpeg', {'Cache-Control': 'private, max-age=31536000, immutable' if fresh else 'no-store'})
        if len(parts) == 3 and parts[:2] == ['api', 'thumbs'] and IMAGE_NAME.match(parts[2]) and not parts[2].endswith('.pdf'):
            src, thumb = os.path.join(IMAGES, parts[2]), os.path.join(THUMBS, parts[2] + '.jpg')
            if not os.path.exists(thumb):
                if not os.path.exists(src):
                    return self.reply(404, {'error': 'no such image'})
                tmp = thumb + '.' + secrets.token_hex(4) + '.jpg'
                r = subprocess.run(['sips', '-Z', '400', '-s', 'format', 'jpeg', '-s', 'formatOptions', '70', src, '--out', tmp],
                                   capture_output=True)
                if r.returncode or not os.path.exists(tmp):
                    return self.send_file(src)   # could not shrink it: the full picture still shows
                os.replace(tmp, thumb)
            with open(thumb, 'rb') as f:
                return self.reply(200, f.read(), 'image/jpeg', {'Cache-Control': 'private, max-age=31536000, immutable'})
        if len(parts) == 3 and parts[:2] == ['api', 'files'] and FILE_NAME.match(parts[2]):
            return self.send_file(os.path.join(FILES, parts[2]))
        self.reply(404, {'error': 'not found'})

    def send_file(self, path):
        """Streams a dropped file, with byte ranges so videos can seek. Web pages and scripts come back as plain text."""
        try:
            f = open(path, 'rb')
        except FileNotFoundError:
            return self.reply(404, {'error': 'no such file'})
        with f:
            size = os.fstat(f.fileno()).st_size
            ext = path.rsplit('.', 1)[1]
            ctype = mimetypes.guess_type('x.' + ext)[0] or 'application/octet-stream'
            if ext in PLAIN_TEXT or ext in ('html', 'htm', 'svg', 'xhtml'):
                ctype = 'text/plain; charset=utf-8'
            start, end, code = 0, size - 1, 200
            m = re.match(r'bytes=(\d*)-(\d*)$', self.headers.get('Range') or '')
            if m and size and (m.group(1) or m.group(2)):
                if m.group(1):
                    start, end = int(m.group(1)), min(int(m.group(2) or size - 1), size - 1)
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header('Content-Range', 'bytes */%d' % size)
                    self.send_header('Content-Length', '0')
                    return self.end_headers()
                code = 206
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'private, max-age=31536000, immutable')
            if not ctype.startswith(INLINE) and not ctype.startswith('text/plain'):
                self.send_header('Content-Disposition', 'attachment')
            if code == 206:
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
            self.end_headers()
            if self.command == 'HEAD':
                return
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    block = f.read(min(1 << 20, left))
                    if not block:
                        break
                    self.wfile.write(block)
                    left -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass   # the video player stopped this range to ask for another

    def post(self, parts):
        if parts[:2] == ['api', 'push'] and not PUSH:
            return self.reply(503, NO_PUSH)
        if parts == ['api', 'push', 'subscribe']:   # this phone wants notifications
            PUSH.subscribe(json.loads(self.read_body()))
            return self.reply(201, {'ok': True})
        if parts == ['api', 'push', 'unsubscribe']:
            PUSH.unsubscribe(str(json.loads(self.read_body()).get('endpoint') or ''))
            return self.reply(200, {'ok': True})
        if parts == ['api', 'push', 'test']:   # the Agents screen's "Send a test": proves the whole path works
            return self.reply(200, {'sent': PUSH.send({'title': 'Notifications are on', 'body': 'Agents will tell you here when they finish or need you.',
                                                         'url': './#agents', 'tag': 'test'})})
        if parts == ['api', 'settings', 'servers']:   # add or remove one of your own model servers, from the Settings card
            return self.reply(200, own_server(json.loads(self.read_body())))
        if len(parts) == 4 and parts[:3] == ['api', 'settings', 'test'] and parts[3] in models.JOBS:
            return self.reply(200, models.test(parts[3]) if models.choice(parts[3]) else local_test(parts[3]))
        if parts == ['api', 'uploads']:   # a dropped file arrives in pieces: start, PUT each piece in order, then done
            up = secrets.token_hex(6)
            open(os.path.join(UPLOADS, up), 'wb').close()
            return self.reply(201, {'up': up})
        if len(parts) == 4 and parts[:2] == ['api', 'uploads'] and UPLOAD_ID.match(parts[2]) and parts[3] == 'done':
            part = os.path.join(UPLOADS, parts[2])
            if not os.path.exists(part):
                return self.reply(404, {'error': 'no such upload'})
            name = str(json.loads(self.read_body() or b'{}').get('name') or '')
            return self.reply(201, finish_upload(part, name))
        if parts == ['api', 'boards']:
            req = json.loads(self.read_body() or b'{}')
            t = now_ms()
            board = {'id': secrets.token_hex(6), 'name': str(req.get('name') or 'Untitled board')[:200],
                     'created': t, 'updated': t, 'items': [], 'view': None, 'space': None}
            with LOCK:
                if req.get('space') in {x['id'] for x in read_spaces()['spaces']}:
                    board['space'] = req['space']
                write_json(board_path(board['id']), board)
            return self.reply(201, board)
        if len(parts) == 4 and parts[:2] == ['api', 'boards'] and parts[3] == 'files' and BOARD_ID.match(parts[2]):
            if not os.path.exists(board_path(parts[2])):
                return self.reply(404, {'error': 'no such board'})
            req = json.loads(self.read_body() or b'{}')
            wanted = [m for m in req.get('items') or [] if isinstance(m, dict) and m.get('miro')]
            job = secrets.token_hex(6)
            IMPORTS[job] = {'state': 'running', 'done': 0, 'total': len(wanted)}
            threading.Thread(target=run_complete_files, args=(job, parts[2], str(req.get('miro_board') or ''), wanted), daemon=True).start()
            return self.reply(202, {'job': job})
        if len(parts) == 4 and parts[:2] == ['api', 'boards'] and parts[3] == 'pictures' and BOARD_ID.match(parts[2]):
            if not os.path.exists(board_path(parts[2])):   # tools/miro_mockups.py: pictures of what Miro draws, by Miro id
                return self.reply(404, {'error': 'no such board'})
            got, tmp = [], tempfile.mkdtemp(prefix='miro-pictures-')
            for m in json.loads(self.read_body() or b'{}').get('items') or []:
                try:
                    data = base64.b64decode(m['png'])
                    if miro_import.ext_of(data) != 'png':
                        continue
                    got.append({'type': 'img', 'src': miro_import.Store(DATA).picture(data, 'png', tmp), 'miro': str(m['miro']),
                                'x': float(m['x']), 'y': float(m['y']), 'w': float(m['w']), 'h': float(m['h']), 'id': 'c' + secrets.token_hex(5)})
                except (KeyError, ValueError, TypeError, subprocess.CalledProcessError):
                    continue
            shutil.rmtree(tmp, ignore_errors=True)

            def put(board):
                items = board.get('items') or []
                have = {i.get('miro') for i in items if i.get('miro')}
                new = [g for g in got if g['miro'] not in have]
                items.extend(new)
                complete_layers(items, new)
                board['items'] = items
                return len(new)
            n = edit_board(parts[2], put)
            live_send(parts[2], {'kind': 'saved', 'updated': read_board(parts[2])['updated']}, '')
            return self.reply(200, {'added': n})
        if len(parts) == 4 and parts[:2] == ['api', 'boards'] and parts[3] == 'dedupe' and BOARD_ID.match(parts[2]):
            if not os.path.exists(board_path(parts[2])):   # [new id, old id]: drop the copy, the old item takes its Miro id
                return self.reply(404, {'error': 'no such board'})
            pairs = [p for p in json.loads(self.read_body() or b'{}').get('pairs') or [] if isinstance(p, list) and len(p) == 2]

            def dedupe(board):
                items = board.get('items') or []
                by = {i['id']: i for i in items}
                gone = set()
                for new, old in pairs:
                    if new in by and old in by and by[new].get('miro') and not by[old].get('miro'):
                        by[old]['miro'] = by[new]['miro']
                        gone.add(new)
                board['items'] = [i for i in items if i['id'] not in gone]
                return len(gone)
            n = edit_board(parts[2], dedupe)
            live_send(parts[2], {'kind': 'saved', 'updated': read_board(parts[2])['updated']}, '')
            return self.reply(200, {'removed': n})
        if len(parts) == 4 and parts[:2] == ['api', 'boards'] and parts[3] == 'adopt' and BOARD_ID.match(parts[2]):
            if not os.path.exists(board_path(parts[2])):   # tools/miro_adopt.py: give old items their Miro id
                return self.reply(404, {'error': 'no such board'})
            pairs = (json.loads(self.read_body() or b'{}').get('map') or {})
            os.makedirs(COMPLETE_BACKUPS, exist_ok=True)
            backup = os.path.join(COMPLETE_BACKUPS, parts[2] + '.json')
            if not os.path.exists(backup):
                shutil.copy(board_path(parts[2]), backup)

            def adopt(board):
                have = {i.get('miro') for i in board.get('items') or [] if i.get('miro')}
                n = 0
                for i in board.get('items') or []:
                    k = pairs.get(i.get('id'))
                    if k and not i.get('miro') and k not in have:
                        i['miro'] = str(k)
                        have.add(k)
                        n += 1
                return n
            return self.reply(200, {'adopted': edit_board(parts[2], adopt)})
        if len(parts) == 4 and parts[:2] == ['api', 'boards'] and parts[3] == 'complete' and BOARD_ID.match(parts[2]):
            if not os.path.exists(board_path(parts[2])):
                return self.reply(404, {'error': 'no such board'})
            return self.reply(200, complete_board(parts[2], json.loads(self.read_body() or b'{}')))
        if len(parts) == 4 and parts[:2] == ['api', 'boards'] and parts[3] == 'recrop' and BOARD_ID.match(parts[2]):
            req = json.loads(self.read_body() or b'{}')
            if not os.path.exists(board_path(parts[2])):
                return self.reply(404, {'error': 'no such board'})
            crops = {str(k): v for k, v in (req.get('crops') or {}).items()
                     if isinstance(v, list) and len(v) >= 4 and all(isinstance(n, (int, float)) and n >= 0 for n in v[:4])}
            job = secrets.token_hex(6)
            IMPORTS[job] = {'state': 'running'}
            threading.Thread(target=run_recrop, args=(job, parts[2], miro_import.board_id_from(req.get('miro')), crops),
                             daemon=True).start()
            return self.reply(202, {'job': job})
        if parts == ['api', 'import']:   # New board > Import from Miro: start it, the page polls /api/import/<job>
            req = json.loads(self.read_body() or b'{}')
            try:
                miro_import.board_id_from(req.get('link'))
            except miro_import.ImportError_ as e:
                return self.reply(400, {'error': str(e)})
            space = req.get('space') if req.get('space') in {x['id'] for x in read_spaces()['spaces']} else None
            job = secrets.token_hex(6)
            IMPORTS[job] = {'state': 'running', 'done': 0, 'total': 0}
            crops = {str(k): v for k, v in (req.get('crops') or {}).items()   # from tools/miro_crops.py: id -> [x, y, w, h]
                     if isinstance(v, list) and len(v) >= 4 and all(isinstance(n, (int, float)) and n >= 0 for n in v[:4])}
            threading.Thread(target=run_import, args=(job, str(req.get('link')), space, crops), daemon=True).start()
            return self.reply(202, {'job': job})
        if parts == ['api', 'spaces']:
            req = json.loads(self.read_body() or b'{}')
            name = str(req.get('name') or '').strip()[:80]
            if not name:
                raise ValueError
            space = {'id': secrets.token_hex(6), 'name': name, 'created': now_ms()}
            with LOCK:
                sp = read_spaces()
                if req.get('parent'):   # a sub-space: one level only, so its parent must be a top-level space
                    if not any(x['id'] == req['parent'] and not x.get('parent') for x in sp['spaces']):
                        return self.reply(404, {'error': 'no such space'})
                    space['parent'] = req['parent']
                sp['spaces'].append(space)
                write_json(SPACES, sp)
            return self.reply(201, space)
        if parts == ['api', 'images']:
            data = self.read_body()
            if data[:3] == b'\xff\xd8\xff':
                ext = 'jpg'
            elif data[:8] == b'\x89PNG\r\n\x1a\n':
                ext = 'png'
            elif data[:5] == b'%PDF-':
                ext = 'pdf'
            else:
                return self.reply(415, {'error': 'only JPEG, PNG or PDF'})
            name = hashlib.sha256(data).hexdigest() + '.' + ext
            path = os.path.join(IMAGES, name)
            with LOCK:
                if not os.path.exists(path):
                    with open(path + '.tmp', 'wb') as f:
                        f.write(data)
                    os.replace(path + '.tmp', path)
            READER.add(name)
            return self.reply(201, {'src': 'api/images/' + name})
        if len(parts) == 3 and parts[:2] == ['api', 'agents'] and BOARD_ID.match(parts[2]):
            req = json.loads(self.read_body())
            task = str(req.get('task') or '').strip()
            if not task:
                raise ValueError
            try:
                r = RUNNER.start(parts[2], task, [str(x) for x in req.get('selected') or []][:500], req.get('anchor'),
                                 'one' if req.get('mode') == 'one' else 'team')
            except FileNotFoundError:
                return self.reply(404, {'error': 'no such board'})
            return self.reply(201, {'run': r['id'], 'frame': r['frame']})
        if parts == ['api', 'voicelog']:   # the page reports what happened to the microphone (it dropped out, came back)
            info = json.loads(self.read_body()[:4000] or b'{}')
            print('%s voice %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), json.dumps(info)[:600]), flush=True)
            return self.reply(204)
        if parts == ['api', 'news', 'told']:
            body = json.loads(self.read_body())
            RUNNER.told([str(x) for x in body.get('runs') or []][:200], [str(x) for x in body.get('questions') or []][:200])
            return self.reply(200, {'ok': True})
        if len(parts) == 4 and parts[:2] == ['api', 'runs'] and BOARD_ID.match(parts[2]) and parts[3] in ('answer', 'stop', 'follow', 'steer'):
            if parts[3] == 'steer':
                return self.reply(200, {'ok': bool(RUNNER.steer(parts[2], str(json.loads(self.read_body()).get('text') or '')))})
            if parts[3] == 'stop':
                return self.reply(200, {'ok': RUNNER.stop(parts[2])})
            if parts[3] == 'follow':
                return self.reply(200, {'ok': RUNNER.follow_up(parts[2], str(json.loads(self.read_body()).get('text') or ''))})
            text = str(json.loads(self.read_body()).get('text') or '').strip()
            if not text:
                raise ValueError
            return self.reply(200, {'ok': RUNNER.answer(parts[2], text)})
        if len(parts) == 3 and parts[:2] == ['api', 'guide'] and BOARD_ID.match(parts[2]):   # onboarding: the guide builds a first team
            req, bid = json.loads(self.read_body()), parts[2]
            try:
                board = read_board(bid)
            except FileNotFoundError:
                return self.reply(404, {'error': 'no such board'})
            card = guide.tidy(req.get('card')) if isinstance(req.get('card'), dict) else None
            try:
                out = guide.turn(str(req.get('event') or 'reply'), str(req.get('text') or ''), card, req.get('history') or [])
            except models.NoModel:
                return self.reply(503, {'error': 'no model', 'message': 'No model is set up yet. Open Settings (the gear on the home page) and pick one.'})
            except (OSError, ValueError, KeyError):
                return self.reply(503, {'error': 'the model did not answer'})
            RUNNER.set_team(bid, guide.members(out['card'], agents.STARTERS['lead']['role']))   # the board's team is the one on the card
            if out['run']:
                files = [i['id'] for i in board.get('items') or [] if str(i.get('url') or '').endswith('.pdf') or str(i.get('label') or '').endswith('.md')]
                r = RUNNER.start(bid, guide.TASK, files, None, 'team')
                out['started'] = {'run': r['id'], 'frame': r['frame']}
            return self.reply(200, out)
        if parts == ['api', 'ask']:
            req = json.loads(self.read_body())
            question = str(req.get('question') or '').strip()[:500]
            if not question:
                raise ValueError
            spaces = {x['id']: x for x in read_spaces()['spaces']}
            space = str(req.get('space') or '')
            if req.get('board'):
                talk_id = str(req['board'])
            elif space == 'unsorted' or space in spaces:
                talk_id = 'unsorted' if space == 'unsorted' else 's' + space
            else:
                talk_id = 'all'
            # follow-ups use the last turns of THIS conversation (board, space or all), kept on disk
            # since (off a board): the mic was opened then, or it is a search-bar question: older turns are not history
            since = req.get('since') if isinstance(req.get('since'), (int, float)) else 0
            history = [{'q': t['q'], 'a': t['a'], 'did': t.get('did'), 't': t.get('t', 0)} for t in read_talk(talk_id)['turns'] if not t['a'].startswith(NOT_MOVED) and t.get('t', 0) >= since] if BOARD_ID.match(talk_id) or talk_id in ('all', 'unsorted') else []
            # carry: the voice brought the user here mid-conversation ("open the Research board"); what was said just before counts
            carry = [{'q': str(c['q'])[:500], 'a': str(c['a'])[:2000], 'did': None, 't': c['t']} for c in (req.get('carry') or [])[:6]
                     if isinstance(c, dict) and c.get('q') and c.get('a') and isinstance(c.get('t'), (int, float))]
            have = {(h['t'], h['q']) for h in history}
            history = sorted(history + [c for c in carry if (c['t'], c['q']) not in have], key=lambda h: h['t'])[-3:]
            if req.get('board'):
                try:
                    boards = [read_board(str(req['board']))] if BOARD_ID.match(str(req['board'])) else []
                except FileNotFoundError:
                    boards = []
                if not boards:
                    return self.reply(404, {'error': 'no such board'})
                scope = boards[0].get('name') or 'this board'
            elif talk_id == 'all':
                boards, scope = all_boards(), 'all'
            else:
                sid = None if space == 'unsorted' else space
                within = {sid} | {x['id'] for x in spaces.values() if sid and x.get('parent') == sid}   # a parent space includes its sub-spaces' boards
                boards = [b for b in all_boards() if (b.get('space') if b.get('space') in spaces else None) in within]
                scope = 'space:' + ('Unsorted' if sid is None else spaces[sid]['name'])
            try:
                # the map of all spaces and boards goes with every question, so the layout itself can be talked about
                everything = all_boards()
                here = ('inside the board "%s"' % scope if req.get('board') else 'on the list of all boards' if talk_id == 'all'
                        else 'in the %s space' % scope[6:] if talk_id != 'unsorted' else 'on the list of boards not in any space')
                if req.get('board'):
                    sp = spaces.get(boards[0].get('space'))
                    here += ' (in the %s space)' % sp['name'] if sp else ' (not in any space)'
                layout = talk.layout_entry(list(spaces.values()), everything, {b['id']: BRIEFER.load(b['id']) for b in everything}, here, detail=not req.get('board'),
                                           question=question, history=history)
                names = {b['id']: b.get('name', '') for b in boards}
                waiting = [w for w in RUNNER.news()['waiting'] if w['board'] in names]   # questions on the boards in scope
                tasks = [{'board': t['board'], 'boardName': names.get(t['board'], ''), 'item': t['frame'], 'run': t['id'], 'frame': '',
                          'ended': t.get('ended'), 'text': RUNNER.progress(t)} for t in sorted(RUNNER.recent(set(names)), key=lambda t: t['created'])][-6:]
                result = talk.answer(question, history, boards, scope, READER, {b['id']: BRIEFER.load(b['id']) for b in boards}, layout,
                                     waiting, tasks, RUNNER.team_text(boards[0]['id']) if req.get('board') else '', req.get('seen'), FINDER)
                edit = result.pop('edit', None)
                shown_card = result.pop('shown_card', None)
                did = {'card': shown_card} if shown_card else None   # "the card below that" next time means this card
                if edit and req.get('board'):   # the voice moves cards itself; what it says comes from what really moved
                    result['answer'], result['edited'] = do_edit(boards[0]['id'], edit)
                    if result['edited']:   # the model sees, in later turns, that its reply did this (not only said it)
                        did = ({'move_card': {'from': edit.get('from'), 'to': edit.get('to')}} if edit['kind'] == 'move_card'
                               else {edit['kind'] if edit['kind'] != 'undo' else 'undo_edit': True})
                elif arrange.CLAIM.search(result.get('answer') or ''):
                    result['answer'] = NOT_MOVED + " Tell me which card and where it should go, and I'll move it."

                task, mode = result.pop('task', None), result.pop('task_mode', 'team')
                if task and req.get('board'):   # "have the team / an agent ...": a task frame on this board
                    sel, about = task_items(boards[0], result.get('sources') or [], read_talk(talk_id)['turns'])
                    r = RUNNER.start(boards[0]['id'], task + about, sel, None, mode)
                    result['started'] = {'run': r['id'], 'frame': r['frame']}
                card = result.pop('web_card', None)   # a live web page on the board, if the site lets itself be shown there
                if card and req.get('board'):
                    url = card if card.startswith('http') else 'https://' + card
                    result['web'] = {'url': url, 'frameable': frameable(url)}
                mate = result.pop('add_teammate', None)   # "add a developer to the team"
                if mate and req.get('board'):
                    result['team'] = add_teammate(boards[0]['id'], mate)
                turn = result.pop('steer', None)   # {'run', 'text'}: direction for a running task, to its Lead
                if turn and RUNNER.steer(turn['run'], turn['text']):
                    result['steered'] = turn['run']
                more = result.pop('follow_up', None)   # {'run', 'text'}: more work on a task that ended
                if more and RUNNER.follow_up(more['run'], more['text']):
                    result['followed'] = more['run']
                said_back = result.pop('agent_answer', None)   # {'run', 'text'}: the user answered one of the waiting questions
                if said_back and said_back['text'] and RUNNER.answer(said_back['run'], said_back['text']):
                    result['answered'] = said_back['run']
            except models.NoModel:
                return self.reply(503, {'error': 'no model', 'message': 'No model is set up yet. Open Settings (the gear on the home page) and pick one.'})
            except (OSError, ValueError, KeyError):
                return self.reply(503, {'error': 'the model did not answer'})
            turn = {'q': question, 'a': result['answer'], 'sources': result['sources'], 'checked': result.get('checked'), 't': now_ms(),
                    **({'did': did} if did else {})}
            with LOCK:
                conv = read_talk(talk_id)
                conv['turns'] = (conv['turns'] + [turn])[-500:]
                write_json(talk_path(talk_id), conv)
            return self.reply(200, {**result, 'turn': turn})
        if parts == ['api', 'setup']:
            req = json.loads(self.read_body())
            if req.get('provider') == 'local':   # a model server on your own network, by its address
                return self.reply(200, local_setup(str(req.get('url') or '')))
            return self.reply(200, models.setup(str(req.get('provider') or ''), str(req.get('key') or '')))
        if parts == ['api', 'hear']:   # one spoken question as WAV, when hearing runs on the OpenAI or xAI key
            if not models.speech():
                return self.reply(503, {'error': 'no speech provider'})
            try:
                return self.reply(200, {'text': models.hear(self.read_body())})
            except OSError as e:
                return self.reply(502, {'error': str(e)[:300]})
        if len(parts) == 3 and parts[:2] == ['api', 'live'] and BOARD_ID.match(parts[2]):   # a move as it happens, or "I have control"
            req = json.loads(self.read_body())
            if req.get('kind') not in ('move', 'claim'):
                raise ValueError
            live_send(parts[2], {k: req.get(k) for k in ('kind', 'client', 'device', 'items')}, str(req.get('client') or ''))
            return self.reply(200, {'ok': True})
        if parts == ['api', 'speak']:
            req = json.loads(self.read_body())
            text, voice = str(req.get('text') or '').strip()[:2000], str(req.get('voice') or '').strip()
            if not text or not voice:
                raise ValueError
            try:
                upstream = talk.speak(text, voice) if talk.TTS_URL else models.speak(text, voice) if models.speech() else None
            except OSError:
                upstream = None
            if upstream is None:
                return self.reply(503, {'error': 'voice service unavailable'})
            with upstream:   # stream the speech through as it is made (no length; the connection closes at the end)
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('X-Sample-Rate', upstream.headers.get('X-Sample-Rate', '24000') if talk.TTS_URL else str(models.RATE))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    while True:
                        chunk = upstream.read1(16384) if hasattr(upstream, 'read1') else upstream.read(16384)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass   # the listener cut the answer off
            return
        self.reply(404, {'error': 'not found'})

    def put(self, parts):
        if len(parts) == 3 and parts[:2] == ['api', 'uploads'] and UPLOAD_ID.match(parts[2]):
            part = os.path.join(UPLOADS, parts[2])
            if not os.path.exists(part):
                return self.reply(404, {'error': 'no such upload'})
            data = self.read_body()
            if os.path.getsize(part) + len(data) > MAX_FILE:
                os.remove(part)
                raise OverflowError
            with open(part, 'ab') as f:
                f.write(data)
            return self.reply(200, {'size': os.path.getsize(part)})
        if len(parts) == 4 and parts[:2] == ['api', 'boards'] and parts[3] == 'space' and BOARD_ID.match(parts[2]):
            space = json.loads(self.read_body()).get('space')
            with LOCK:
                if space is not None and space not in {x['id'] for x in read_spaces()['spaces']}:
                    return self.reply(404, {'error': 'no such space'})
                try:
                    board = read_board(parts[2])
                except FileNotFoundError:
                    return self.reply(404, {'error': 'no such board'})
                board['space'] = space
                write_json(board_path(parts[2]), board)
            return self.reply(200, {'ok': True})
        if parts == ['api', 'spaces', 'order']:   # the list order the user dragged them into
            ids = json.loads(self.read_body()).get('ids')
            if not isinstance(ids, list):
                raise ValueError
            with LOCK:
                sp = read_spaces()
                pos = {str(x): i for i, x in enumerate(ids)}
                sp['spaces'].sort(key=lambda x: pos.get(x['id'], len(pos)))   # spaces missing from the list keep their place at the end
                write_json(SPACES, sp)
            return self.reply(200, sp)
        sid = self.space_id(parts)
        if sid:
            name = str(json.loads(self.read_body()).get('name') or '').strip()[:80]
            if not name:
                raise ValueError
            with LOCK:
                sp = read_spaces()
                hit = [x for x in sp['spaces'] if x['id'] == sid]
                if not hit:
                    return self.reply(404, {'error': 'no such space'})
                hit[0]['name'] = name
                write_json(SPACES, sp)
            return self.reply(200, hit[0])
        if parts == ['api', 'settings']:
            return self.reply(200, dict(models.save(json.loads(self.read_body())), local=local_models(), startup=local_models(False)))
        if len(parts) == 3 and parts[:2] == ['api', 'teams'] and BOARD_ID.match(parts[2]):
            members = json.loads(self.read_body()).get('members')
            if not isinstance(members, list):
                raise ValueError
            return self.reply(200, RUNNER.set_team(parts[2], [m for m in members if isinstance(m, dict)]))
        bid = self.board_id(parts)
        if not bid:
            return self.reply(404, {'error': 'not found'})
        req = json.loads(self.read_body())
        if not isinstance(req.get('items'), list):
            raise ValueError
        with LOCK:
            try:
                board = read_board(bid)
            except FileNotFoundError:
                return self.reply(404, {'error': 'no such board'})
            board['name'] = str(req.get('name') or 'Untitled board')[:200]
            # agents add items while the page is open: keep theirs that this page has not seen yet (and not deleted),
            # and a task frame keeps the size the agent grew it to
            sent = {i.get('id'): i for i in req['items'] if isinstance(i, dict)}
            gone = set(req.get('deleted') or [])
            for i in board.get('items') or []:
                if i.get('agent') and i['id'] not in sent and i['id'] not in gone:
                    req['items'].append(i)
                elif i.get('run') and i['id'] in sent:
                    sent[i['id']]['h'] = max(sent[i['id']].get('h', 0), i.get('h', 0))
            # a page that has not seen a move made here yet sends the item at its old place: keep the move
            moves = {k: v for k, v in (MOVED.get(bid) or {}).items() if time.time() - v[2] < 900}
            MOVED[bid] = moves
            kept = False
            for k, (olds, new, _) in moves.items():
                it = sent.get(k)
                if it and any(abs(it.get('x', 0) - ox) < .5 and abs(it.get('y', 0) - oy) < .5 for ox, oy in olds):
                    it.update(x=new['x'], y=new['y'], z=new.get('z', it.get('z')), w=new.get('w', it.get('w')), h=new.get('h', it.get('h')))
                    kept = True
            board['items'] = req['items']
            board['view'] = req.get('view')
            board['updated'] = now_ms()
            write_json(board_path(bid), board)
        BRIEFER.touch(bid)   # read the changed parts again once the edits settle
        live_send(bid, {'kind': 'saved', 'updated': board['updated']}, self.headers.get('X-Client') or '')
        self.reply(200, {'updated': board['updated'], **({'refresh': True} if kept else {})})   # kept: this page should fetch the board

    def delete(self, parts):
        sid = self.space_id(parts)
        if sid:   # deleting a space never deletes boards: they become unsorted
            with LOCK:
                sp = read_spaces()
                if not any(x['id'] == sid for x in sp['spaces']):
                    return self.reply(404, {'error': 'no such space'})
                sp['spaces'] = [x for x in sp['spaces'] if x['id'] != sid]
                for x in sp['spaces']:   # its sub-spaces move up to the top level
                    if x.get('parent') == sid:
                        del x['parent']
                write_json(SPACES, sp)
                for b in all_boards():
                    if b.get('space') == sid:
                        b['space'] = None
                        write_json(board_path(b['id']), b)
                if os.path.exists(talk_path('s' + sid)):
                    os.replace(talk_path('s' + sid), os.path.join(TRASH, 'talk-s%s-%d.json' % (sid, now_ms())))
            return self.reply(200, {'ok': True})
        scope = self.talk_scope(parts)
        if scope:   # "Clear": the conversation is kept in trash, like a deleted board
            with LOCK:
                if os.path.exists(talk_path(scope)):
                    os.replace(talk_path(scope), os.path.join(TRASH, 'talk-%s-%d.json' % (scope, now_ms())))
            return self.reply(200, {'ok': True})
        bid = self.board_id(parts)
        if not bid:
            return self.reply(404, {'error': 'not found'})
        with LOCK:
            try:
                os.replace(board_path(bid), os.path.join(TRASH, '%s-%d.json' % (bid, now_ms())))
                BRIEFER.forget(bid)
            except FileNotFoundError:
                return self.reply(404, {'error': 'no such board'})
            if os.path.exists(talk_path(bid)):
                os.replace(talk_path(bid), os.path.join(TRASH, 'talk-%s-%d.json' % (bid, now_ms())))
        self.reply(200, {'ok': True})


def frameable(url):
    """Can a page be shown inside ours? Many sites forbid it (X-Frame-Options, or CSP frame-ancestors)."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh) Chrome/130 Safari/537.36'})
        with urllib.request.urlopen(req, timeout=8) as r:
            xfo = (r.headers.get('X-Frame-Options') or '').lower()
            csp = (r.headers.get('Content-Security-Policy') or '').lower()
    except (OSError, ValueError):
        return False
    fa = re.search(r'frame-ancestors([^;]*)', csp)
    return not (xfo in ('deny', 'sameorigin') or (fa and '*' not in fa.group(1).split()))


def add_teammate(bid, mate):
    """A teammate added by voice: a starter role when the name matches one, else the role as said."""
    members = RUNNER.team(bid)['members']
    name = ' '.join(str(mate.get('name') or '').split())[:40]
    if any(m['name'].lower() == name.lower() for m in members):
        return {'added': None, 'members': [m['name'] for m in members]}
    starter = next((dict(v, id=k) for k, v in agents.STARTERS.items() if v['name'].lower() == name.lower()), None)
    new = starter or {'id': 'm' + secrets.token_hex(3), 'name': name.title(), 'color': '#475569', 'role': str(mate.get('role') or '')[:800]}
    team = RUNNER.set_team(bid, members + [new])
    return {'added': new['name'], 'members': [m['name'] for m in team['members']]}


CARDS, CARDS_LOCK = {}, threading.Lock()   # board id -> (file time, card): the list reads only boards that changed


def board_cards():
    """Every board's card for the board list, newest first: light() without the items. With 700 boards, reading every
    file (60 MB) on each visit took 0.65 s and sent 5 MB; this sends about 100 KB and reads only changed boards."""
    with CARDS_LOCK:
        now = {}
        for fn in os.listdir(BOARDS):
            if fn.endswith('.json'):
                try:
                    now[fn[:-5]] = os.stat(os.path.join(BOARDS, fn)).st_mtime
                except OSError:
                    pass
        for bid in set(CARDS) - set(now):
            del CARDS[bid]
        for bid, mtime in now.items():
            if bid not in CARDS or CARDS[bid][0] != mtime:
                try:
                    card = light(read_board(bid))
                except (OSError, ValueError):
                    continue
                del card['items']
                CARDS[bid] = (mtime, card)
        return sorted((c for _, c in CARDS.values()), key=lambda b: b.get('updated') or 0, reverse=True)


def board_names():
    return {c['id']: c.get('name') or '' for c in board_cards()}


def named_news():
    """RUNNER.news() with each item's board name; items on deleted boards are left out."""
    names = board_names()
    news = RUNNER.news()
    for k in ('done', 'waiting'):
        news[k] = [dict(n, boardName=names[n['board']]) for n in news[k] if n['board'] in names]
    return news


def light(b, keep=150):
    """A board as its card needs it: name, space, dates, item count, the outline of all items, the biggest items."""
    items = b.get('items') or []
    out = {k: b.get(k) for k in ('id', 'name', 'space', 'created', 'updated')}
    out['count'] = len(items)
    if items:
        out['bounds'] = [min(i['x'] for i in items), min(i['y'] for i in items),
                         max(i['x'] + i['w'] for i in items), max(i['y'] + i['h'] for i in items)]
    out['items'] = sorted(items, key=lambda i: -(i.get('w', 0) * i.get('h', 0)))[:keep]
    return out


def task_items(board, sources, turns):
    """What a voice task is about: the items its answer was about, else the card shown in the last 15 minutes, each with
    the card stacked with it. "Analyze the full tweet" went out with nothing selected, so the Lead searched the board
    for "tweet" and the team studied another post. Returns (item ids, words naming the card for the task text)."""
    items = board.get('items') or []
    ids = [x['item'] for x in sources if x.get('item') and x.get('board') == board['id'] and x.get('kind') not in ('frame layout', 'frame summary', 'frame gist', 'board brief')]
    if not ids:
        last = next((t for t in reversed(turns) if (t.get('did') or {}).get('card')), None)
        if last and now_ms() - last.get('t', 0) < 15 * 60 * 1000:
            c = last['did']['card']
            frame = next((i for i in items if i['id'] == c.get('frame')), None)
            cs = arrange.cards(board, frame) if frame else []
            if 1 <= (c.get('place') or 0) <= len(cs):
                ids = [i['id'] for i in cs[c['place'] - 1]]
    for st in talk.stacks([i for i in items if i.get('type') not in ('area', 'arrow')]):   # a note brings its link card, and back
        if any(i['id'] in ids for i in st):
            ids += [i['id'] for i in st if i['id'] not in ids]
    words = [' '.join(talk.item_words(i, READER)[1].split()[:30]) for i in items if i['id'] in ids]
    return ids, (' (This is about the selected card: %s)' % ' | '.join(words) if words else '')


def local_models(picked=True):
    """Each job's model on your own machines: [(model, address)] in the order tried; the one picked in Settings first,
    else as the app was started with (builders keep their startup models as fallbacks)."""
    start = {'voice': [(talk.MODEL, talk.MODEL_URL)] if talk.MODEL_URL else [],   # none until set up
             'agents': [(agents.MODEL, agents.MODEL_URL)] if agents.MODEL_URL else [],
             'builders': [(m, u) for u, m in agents.CODE_MODELS],
             'vision': [(agents.VISION_MODEL, agents.VISION_MODEL_URL)] if agents.VISION_MODEL_URL else []}
    if not picked:
        return start
    out = {}
    for job, ms in start.items():
        picked = models.local(job)
        out[job] = ([(picked[1], picked[0])] + ([m for m in ms if m != (picked[1], picked[0])] if job == 'builders' else [])) if picked else ms
    return out


def real_names(jobs):
    """The same lists, each name as the model that really answers it (a server can take one model's name and answer
    with another), once own_models has asked."""
    def name(m, u):
        a = (OWN_SEEN.get((re.sub(r'/v1(/.*)?$', '', u.rstrip('/')), m)) or (None,))[0]
        return '%s (answered by %s)' % (m, a) if a and a != m else m
    return {j: [(name(m, u), u) for m, u in ms] for j, ms in jobs.items()}


def own_server(req):
    """{add: address} or {remove: address}. An address is added only when it serves models (an OpenAI-style /v1/models,
    as vLLM, llama.cpp, Ollama and LM Studio do); "192.168.1.20:8000" or ".../v1/chat/completions" both work."""
    url = str(req.get('add') or req.get('remove') or '').strip()
    url = re.sub(r'/v1(/.*)?$', '', url.rstrip('/'))
    if url and not re.match(r'^https?://', url):
        url = 'http://' + url
    have = models.load().get('servers') or []
    if req.get('remove'):   # jobs that ran on one of its models go back to the startup setting
        jobs = {j: {'provider': 'local', 'model': ''} for j, c in (models.load().get('jobs') or {}).items()
                if c.get('provider') == 'local' and str(c.get('url') or '').startswith(url + '/')}
        return {'ok': True, 'servers': models.save({'servers': [u for u in have if u != url], 'jobs': jobs})['servers']}
    if not re.match(r'^https?://[\w.-]+(:\d+)?$', url):
        return {'ok': False, 'error': 'That does not look like an address. Type it like http://192.168.1.20:8000'}
    try:
        with urllib.request.urlopen(url + '/v1/models', timeout=6) as f:
            names = [m['id'] for m in json.load(f).get('data') or [] if m.get('id')]
    except (OSError, ValueError) as e:
        return {'ok': False, 'error': 'Nothing answered at %s (%s). Check the address and that the model server is running.'
                % (url, str(getattr(e, 'reason', e))[:80])}
    if not names:
        return {'ok': False, 'error': '%s answered, but serves no models.' % url}
    return {'ok': True, 'models': names, 'url': url,
            'servers': models.save({'servers': have + ([url] if url not in have else [])})['servers']}


def local_setup(url):
    """The welcome screen's "Your own": add the server, run every job on its first model (vision only on a model that
    reads pictures, by its name), and prove it answers; on failure the jobs are left as they were."""
    r = own_server({'add': url})
    if not r['ok']:
        return r
    m, where = r['models'][0], r['url'] + '/v1/chat/completions'
    before = {j: dict(c) for j, c in (models.load().get('jobs') or {}).items()}
    jobs = {j: {'provider': 'local', 'model': m, 'url': where} for j in ('voice', 'agents', 'builders')}
    if re.search(r'vl|vision|llava|gemma-3|qwen2\.5-vl', m, re.I):
        jobs['vision'] = dict(jobs['voice'])
    models.save({'jobs': jobs})
    t = local_test('voice')
    if not t['ok']:
        models.save({'jobs': {j: before.get(j) or {'provider': 'local', 'model': ''} for j in jobs}})
        return {'ok': False, 'error': '%s lists %s, but it did not answer a question: %s' % (r['url'], m, t['error'])}
    return {'ok': True, 'model': m, 'models': r['models'], 'speech': False}


OWN_SEEN = {}   # (address, model name) -> (the model that really answers, when asked)


def own_models():
    """Every model your own machines serve, from each model server the app knows (its startup addresses and any added in
    settings), as [{url, model}]. Each name is asked once (kept 30 minutes): a name answered by another model is left out,
    since a server can take one model's name and answer with another, and the card would show the wrong model."""
    import concurrent.futures
    bases = []
    for u in [talk.MODEL_URL, agents.MODEL_URL, agents.VISION_MODEL_URL] + [u for u, _ in agents.CODE_MODELS] + (models.load().get('servers') or []):
        b = re.sub(r'/v1(/.*)?$', '', str(u or '').rstrip('/'))
        if b.startswith('http') and b not in bases:
            bases.append(b)

    def names(b):
        try:
            with urllib.request.urlopen(b + '/v1/models', timeout=4) as f:
                return [(b, m['id']) for m in json.load(f).get('data') or [] if m.get('id')]
        except (OSError, ValueError):
            return []

    def real(bm):
        b, m = bm
        if bm not in OWN_SEEN or time.time() - OWN_SEEN[bm][1] > 1800:
            body = json.dumps({'model': m, 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 1,
                               'chat_template_kwargs': {'enable_thinking': False}}).encode()
            try:
                with urllib.request.urlopen(urllib.request.Request(b + '/v1/chat/completions', body, {'Content-Type': 'application/json'}), timeout=20) as f:
                    OWN_SEEN[bm] = (json.load(f).get('model') or m, time.time())
            except (OSError, ValueError):
                OWN_SEEN[bm] = (None, time.time())
        return OWN_SEEN[bm][0]
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        found = [x for xs in ex.map(names, bases) for x in xs]
        answers = list(ex.map(real, found))
    return [{'url': b + '/v1/chat/completions', 'model': m} for (b, m), a in zip(found, answers) if a == m]


def local_test(job):
    """One tiny question to each of the job's own machines: which answered, in the Settings card's words."""
    tried = []
    for model, url in local_models()[job]:
        body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'Reply with the single word: ready'}],
                           'max_tokens': 20, 'chat_template_kwargs': {'enable_thinking': False}}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body, {'Content-Type': 'application/json'}), timeout=60) as f:
                reply = (json.load(f)['choices'][0]['message'].get('content') or '').strip()
            return {'ok': True, 'reply': re.sub(r'<think>.*?</think>', '', reply, flags=re.S).strip()[:80], 'model': model,
                    'skipped': tried}
        except (OSError, ValueError, KeyError, IndexError) as e:
            why = getattr(e, 'reason', e)
            tried.append('%s did not answer: %s' % (model, 'connection refused, its machine is not serving it' if 'refused' in str(why)
                                                    else 'it took too long' if 'timed out' in str(why) else str(why)[:80]))
    return {'ok': False, 'error': '; '.join(tried) if tried else 'no model is set for this job'}


NOT_MOVED = "I haven't moved anything."   # such turns stay out of the model's history: it read one back word for word


def do_edit(bid, edit):
    """A voice move, sort or undo on one board. Returns (what to say, whether the board changed)."""
    if edit['kind'] == 'sort_frame':   # the model call happens before the lock: it takes a few seconds
        board = read_board(bid)
        frame = next((i for i in board['items'] if i['id'] == edit['frame']), None)
        if not frame:
            return 'I could not find that frame.', False
        groups, err = arrange.themes(board, frame, READER)
        if err:
            return err, False
        ids = [[i['id'] for i in s] for _, g in groups for s in g]

    def change(board):
        if edit['kind'] == 'undo':
            what = arrange.undo(DATA, bid, board)
            return ('Done. I put back %s.' % what) if what else 'There is nothing to put back.', bool(what)
        frame = next((i for i in board['items'] if i['id'] == edit['frame']), None)
        if not frame:
            return 'I could not find that frame.', False
        before = json.loads(json.dumps(board))
        title = frame.get('text') or 'that'
        if edit['kind'] == 'move_card':
            card, err = arrange.move(board, frame, edit.get('from'), edit.get('to'), READER)
            if err:
                return err, False
            arrange.keep_undo(DATA, bid, before, 'the card you moved')
            return 'Done. "%s" is now in place %s of the %s frame.' % (card, edit['to'], title), True
        by_id = {i['id']: i for i in board['items']}   # the sort, with the cards as they are now (edits may have come in)
        now = [(t, [[by_id[k] for k in ks if k in by_id] for ks in ids_g]) for (t, _), ids_g in zip(groups, regroup(ids, groups))]
        arrange.lay(board, frame, [(t, [s for s in g if s]) for t, g in now])
        arrange.keep_undo(DATA, bid, before, 'the frame as it was before the sort')
        return 'Done. I sorted the %s frame into %d themes: %s. Say "put it back" to undo it.' % (
            title, len(groups), ', '.join(t for t, _ in groups)), True
    return edit_board(bid, change)


def regroup(ids, groups):
    """The flat list of card ids cut back into the groups' sizes."""
    out, k = [], 0
    for _, g in groups:
        out.append(ids[k:k + len(g)])
        k += len(g)
    return out


MOVED = {}   # board -> {item: ([old places], new item, when)}: moves made here, kept when a page still showing an old place saves


def edit_board(bid, fn):
    """Change a stored board under the lock (agents and the voice write this way); returns what fn returns. Items it
    moves are remembered for a while, and every open copy of the board is told to fetch it."""
    with LOCK:
        board = read_board(bid)
        before = {i['id']: (i['x'], i['y']) for i in board.get('items') or [] if 'x' in i}
        out = fn(board)
        board['updated'] = now_ms()
        write_json(board_path(bid), board)
        moved = {i['id']: i for i in board.get('items') or [] if i['id'] in before and (i['x'], i['y']) != before[i['id']]}
        if moved:
            mine = MOVED.setdefault(bid, {})
            for k, i in moved.items():   # moved again (sort, put back, sort): a page can still show any of the earlier places
                olds = mine[k][0] if k in mine and time.time() - mine[k][2] < 900 else []
                mine[k] = ([p for p in olds if p != (i['x'], i['y'])] + [before[k]], dict(i), time.time())
    if moved:
        live_send(bid, {'kind': 'saved', 'updated': board['updated']}, '')
    return out


BRIEFER = briefs.Briefer(DATA, READER, read_board, lambda: [b['id'] for b in sorted(all_boards(), key=lambda b: len(b.get('items') or []))])   # small boards first: they are ready in minutes

RUNNER = agents.Runner(DATA, type('Store', (), {'read_board': staticmethod(read_board), 'edit_board': staticmethod(edit_board)}),
                       READER, BRIEFER.load, BRIEFER.touch)

def seed_examples():
    """A brand-new data folder starts with the example boards, in an Examples space. Once only: deleted ones stay deleted."""
    mark = os.path.join(DATA, 'examples-added')
    if os.path.exists(mark) or os.listdir(BOARDS):
        return
    src = os.path.join(HERE, 'examples')
    for sub, dest in (('images', IMAGES), ('files', FILES), ('text', READER.text)):   # the PDF, Markdown file and picture the Start here board shows, already read
        folder = os.path.join(src, 'files', sub)
        for fn in os.listdir(folder) if os.path.isdir(folder) else []:
            if not os.path.exists(os.path.join(dest, fn)):
                shutil.copyfile(os.path.join(folder, fn), os.path.join(dest, fn))
    for fn in sorted(os.listdir(src)) if os.path.isdir(src) else []:
        if fn.endswith('.json'):
            with open(os.path.join(src, fn)) as f:
                board = json.load(f)
            write_json(board_path(board['id']), board)
    sp = read_spaces()
    if not any(x['id'] == 'examples' for x in sp['spaces']):
        sp['spaces'].append({'id': 'examples', 'name': 'Examples', 'created': now_ms()})
        write_json(SPACES, sp)
    open(mark, 'w').close()


threading.Thread(target=board_cards, daemon=True).start()   # the board list is ready before the first visit


# ---------- board pictures: the board list shows one small picture of each board ----------
BOARDSHOTS = os.path.join(DATA, 'boardshots')
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
BOARDSHOT_FAILED = {}
BOARDSHOT_SEEN = [None, False]   # the board being photographed, and whether its page got the items


def boardshot(bid, mtime):
    """Photographs one board's preview (index.html?shot=<id>, a 320x160 card at 2x, about 15 KB). The picture's file
    time is the board file's time it showed, so a board saved while it was taken is taken again."""
    png, jpg = os.path.join(BOARDSHOTS, bid + '.png'), os.path.join(BOARDSHOTS, bid + '.jpg')
    tmp = jpg + '.' + secrets.token_hex(4) + '.jpg'
    BOARDSHOT_SEEN[:] = [bid, False]
    try:
        subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run', '--no-default-browser-check',
                        '--user-data-dir=' + os.path.expanduser('~/Library/Caches/clewbert-boardshots'),
                        '--window-size=320,160', '--force-device-scale-factor=2', '--virtual-time-budget=15000',
                        '--screenshot=' + png, 'http://127.0.0.1:%d/?shot=%s' % (PORT, bid)], capture_output=True, timeout=90)
        subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '75', png, '--out', tmp], capture_output=True, timeout=60)
        if not BOARDSHOT_SEEN[1]:
            raise OSError('the page did not get the board')   # a blank picture is not kept
        os.utime(tmp, (mtime, mtime))
        os.replace(tmp, jpg)
    except (OSError, subprocess.SubprocessError):
        BOARDSHOT_FAILED[bid] = mtime   # not tried again until the board changes
    finally:
        for f in (png, tmp):
            if os.path.exists(f):
                os.remove(f)


def boardshot_loop():
    """Keeps every board's picture current: a board that changed and then sat still for 15 s is photographed again,
    newest first (the first run makes all of them). The board list then downloads one small picture per card that the
    browser keeps, instead of up to 150 items and their pictures per card (the first 12 cards were 45 MB)."""
    os.makedirs(BOARDSHOTS, exist_ok=True)
    time.sleep(10)   # the server is listening
    while os.path.exists(CHROME):
        todo = []
        for fn in os.listdir(BOARDS):
            if not fn.endswith('.json'):
                continue
            bid = fn[:-5]
            try:
                mtime = os.stat(os.path.join(BOARDS, fn)).st_mtime
            except OSError:
                continue
            try:
                shot = os.stat(os.path.join(BOARDSHOTS, bid + '.jpg')).st_mtime
            except OSError:
                shot = 0
            if shot < mtime and time.time() - mtime > 15 and BOARDSHOT_FAILED.get(bid) != mtime:
                todo.append((mtime, bid))
        for mtime, bid in sorted(todo, reverse=True)[:20]:
            boardshot(bid, mtime)
        time.sleep(1 if todo else 20)


threading.Thread(target=boardshot_loop, daemon=True).start()
PUSH = push.Push(DATA, named_news) if push else None
NO_PUSH = {'message': 'Notifications need one Python package on the computer that runs Clewbert: pip3 install cryptography, then restart Clewbert.'}

if __name__ == '__main__':
    seed_examples()
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
