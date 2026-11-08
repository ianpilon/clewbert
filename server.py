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
import array, base64, concurrent.futures, gzip, hashlib, html, json, math, mimetypes, os, queue, re, secrets, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import ssl
if not ssl.get_default_verify_paths().cafile and os.path.exists('/etc/ssl/cert.pem'):
    # the python.org Python ships without certificates until "Install Certificates" is run: use the Mac's own list
    ssl._create_default_https_context = lambda: ssl.create_default_context(cafile='/etc/ssl/cert.pem')
import guide, integrations, models, talk
import briefs
try:
    import push   # phone notifications; needs the cryptography package, everything else runs without it
except ImportError:
    push = None
import router
import skills as skillbook   # the Upskill store (a profile's skill counts are called skills)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools'))
import miro_import
import arrange
from agents import runner as agents

PORT = int(sys.argv[1])
DATA = os.path.abspath(os.path.expanduser(sys.argv[2]))
models.SETTINGS = os.path.join(DATA, 'settings.json')   # which model does which job, and outside keys (owner-only file)
integrations.FILE = os.path.join(DATA, 'integrations.json')   # connected apps: Google sign-in and your own MCP servers (owner-only)
HERE = os.path.dirname(os.path.abspath(__file__))
BOARDS, IMAGES, TRASH, TALKS, FILES, UPLOADS, THUMBS, CLIPS = (os.path.join(DATA, d) for d in ('boards', 'images', 'trash', 'talks', 'files', 'uploads', 'thumbs', 'clips'))
for d in (BOARDS, IMAGES, TRASH, TALKS, FILES, UPLOADS, THUMBS, CLIPS):
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


def describe_old_pictures():
    """Pictures on the boards that were read before the vision model was asked (no words found): described in the
    background, newest first, a few seconds apart (2026-11-06)."""
    time.sleep(30)
    names = set()
    for fn in os.listdir(BOARDS):
        if fn.endswith('.json'):
            try:
                for it in read_board(fn[:-5]).get('items') or []:
                    if it.get('type') == 'img' and talk.image_name(it.get('src')):
                        names.add(talk.image_name(it['src']))
            except (OSError, ValueError, KeyError):
                pass
    def when(n):
        try:
            return os.path.getmtime(os.path.join(IMAGES, n))
        except OSError:
            return 0
    READER.again(sorted(names, key=when, reverse=True))


threading.Thread(target=describe_old_pictures, daemon=True).start()
FINDER = talk.Finder(BOARDS, READER)
threading.Thread(target=FINDER.fresh, daemon=True).start()   # the first search would wait ~8 s to read every board


def board_path(bid):
    return os.path.join(BOARDS, bid + '.json')


# ---------- link previews: read from the page's own tags, here (no outside service, no daily limit) ----------
PRIVATE_NET = re.compile(r'^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.|::1$|fc|fd|fe80)')


def fetch_image(url):
    """The bytes of a picture at a web address, as JPEG or PNG (anything else is converted with sips); refuses your own
    network, pages that are not pictures, and anything over 25 MB. Chrome's "Copy image" and a picture dragged from
    another tab give the board only the address (2026-11-04)."""
    host = urllib.parse.urlsplit(url).hostname or ''
    if not re.match(r'^https?://', url) or not host or any(PRIVATE_NET.match(a[4][0]) for a in socket.getaddrinfo(host, None)):
        raise ValueError
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                                               '(KHTML, like Gecko) Chrome/130.0 Safari/537.36', 'Accept': 'image/*,*/*;q=0.5',
                                               'Referer': 'https://%s/' % host})
    with urllib.request.urlopen(req, timeout=15) as f:
        data = f.read(25 * 1024 ** 2 + 1)
    if len(data) > 25 * 1024 ** 2:
        raise ValueError
    if data[:3] == b'\xff\xd8\xff' or data[:8] == b'\x89PNG\r\n\x1a\n':
        return data
    if not (data[:4] in (b'RIFF', b'GIF8') or data[4:12] in (b'ftypavif', b'ftypheic') or data[:2] == b'BM' or b'<svg' in data[:600].lower()):
        raise ValueError   # not a picture
    with tempfile.TemporaryDirectory() as tmp:
        src, out = os.path.join(tmp, 'in'), os.path.join(tmp, 'out.jpg')
        with open(src, 'wb') as f:
            f.write(data)
        subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '85', src, '--out', out], check=True, capture_output=True, timeout=60)
        with open(out, 'rb') as f:
            return f.read()


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


RULES_FILE = os.path.join(DATA, 'rules_board')
skillbook.DATA = DATA   # every agent's added skills (Upskill), DATA/skills.json
FIRST_RULES = [   # what the user has already told us, in their words where we have them; the user edits the board like any other
    'No middlemen: no Composio, no Pipedream, no hosted services in between, unless the user says so for this job.',
    'Ask the user before sending, posting, buying or anything that leaves the board.',
    'Every board is treated the same; no board is special.',
    'Never say done without proof on the board: a source for every fact, a commit for every code change.',
    'Never invent facts, names, numbers or links. If you did not find it, say so.',
    'Plain words and short sentences. Spell out acronyms.',
]
RULE_Q = re.compile(r"^\s*(?:please\s+)?(?:add\s+(?:this\s+)?(?:to\s+)?(?:my|the)\s+(?:standing\s+)?rules|add\s+a\s+(?:standing\s+)?rule(?:\s+for\s+(?:every|all)\s+teams?)?|"
                    r"remember\s+for\s+(?:every|all)\s+teams?)\s*[:,-]?\s*(.{6,})$|^\s*from\s+now\s+on,?\s+((?:every\s+team|all\s+teams|teams|agents)\s+.{6,})$", re.I)


AGENT_SPACE_FILE = os.path.join(DATA, 'agent_space')


def agent_space():
    """The one space every board a team or the assistant makes for a job lands in (2026-11-05): "Agent boards", top level,
    made on first use. A board dragged out of it is kept; a board left in it untouched for 30 days is put away."""
    with LOCK:
        sp = read_spaces()
        try:
            with open(AGENT_SPACE_FILE) as f:
                sid = f.read().strip()
        except OSError:
            sid = ''
        if not any(x['id'] == sid for x in sp['spaces']):
            hit = next((x for x in sp['spaces'] if x['name'].lower() == 'agent boards' and not x.get('parent')), None)
            if not hit:
                hit = {'id': secrets.token_hex(6), 'name': 'Agent boards', 'created': now_ms()}
                sp['spaces'].append(hit)
                write_json(SPACES, sp)
            sid = hit['id']
            with open(AGENT_SPACE_FILE, 'w') as f:
                f.write(sid)
        return sid


def rules_board():
    """The one board of standing rules every agent team reads (made on first use, one sticky note per rule)."""
    try:
        with open(RULES_FILE) as f:
            bid = f.read().strip()
        if os.path.exists(board_path(bid)):
            return bid
    except OSError:
        pass
    t = now_ms()
    bid = secrets.token_hex(6)
    items = [{'id': 'rule' + secrets.token_hex(4), 'type': 'note', 'text': text, 'x': 40 + (k % 3) * 320, 'y': 140 + (k // 3) * 200,
              'w': 290, 'h': 170, 'z': k + 1} for k, text in enumerate(FIRST_RULES)]
    items.insert(0, {'id': 'ruletitle', 'type': 'text', 'text': 'Standing rules for every agent team. One rule per note: add, change or delete '
                     'notes here, and every team reads them before it starts.', 'x': 40, 'y': 40, 'w': 940, 'h': 60, 'z': 0})
    with LOCK:
        write_json(board_path(bid), {'id': bid, 'name': 'Standing rules for agent teams', 'created': t, 'updated': t,
                                     'items': items, 'view': None, 'space': None, 'by': 'agent'})
        with open(RULES_FILE, 'w') as f:
            f.write(bid)
    return bid


def record_ask(talk_id, question, history, here, spaces_text, apps, waiting, pending, picked, bid, result):
    """Every question, with the very input the routing and the team planner saw, and what they made of it: DATA/asks.jsonl,
    append-only. tools/replay.py replays marked ones against the live model before a prompt change goes out (2026-11-07,
    after the planner copied a finished job's text as a new task and the fix could only be tried on one question)."""
    try:
        plan = result.get('team') or {}
        rec = {'id': secrets.token_hex(4), 't': now_ms(), 'talk': talk_id, 'board': bid, 'q': question,
               'history': [{'q': h['q'], 'a': h['a'], 'route': h.get('route')} for h in history][-3:], 'here': here, 'spaces_text': spaces_text,
               'apps': list(apps), 'waiting': waiting, 'pending': pending, 'selected': len(picked), 'route': result.get('route'),
               'task': plan.get('task'), 'mode': plan.get('mode'), 'why': plan.get('why'), 'ms': result.get('ms'),
               'apps_line': talk.apps_line(bid)}   # the planner's connected-apps line, so a replay elsewhere sees the same prompt
        with open(os.path.join(DATA, 'asks.jsonl'), 'a') as f:
            f.write(json.dumps(rec) + '\n')
    except (OSError, TypeError, ValueError, KeyError) as e:
        print('ask not recorded:', e)


def add_rule(text):
    bid = rules_board()

    def put(board):
        notes = [i for i in board.get('items') or [] if i.get('type') == 'note']
        k = len(notes)
        board['items'] = (board.get('items') or []) + [{'id': 'rule' + secrets.token_hex(4), 'type': 'note', 'text': text,
                                                        'x': 40 + (k % 3) * 320, 'y': 140 + (k // 3) * 200, 'w': 290, 'h': 170, 'z': k + 1}]
        board['updated'] = now_ms()
    edit_board(bid, put)
    return bid


def all_boards():
    out = []
    for fn in os.listdir(BOARDS):
        if fn.endswith('.json'):
            try:
                out.append(read_board(fn[:-5]))
            except (OSError, ValueError):
                pass
    return out


# Archived: a space or a board the user put away (2026-11-04). It stays on disk, opens from its space and the typed search, and
# is backed up like any other; it leaves the voice's search and board map, the brief writer and the board list, so the
# voice reads only what he still uses (IOG & Cardano alone was half of all items).
def archived_spaces(spaces=None):
    sp = spaces if spaces is not None else read_spaces()['spaces']
    arch = {x['id'] for x in sp if x.get('archived')}
    return arch | {x['id'] for x in sp if x.get('parent') in arch}


def is_archived(b, arch):
    return bool(b.get('archived')) or (b.get('space') in arch)


def live_boards(spaces=None):
    arch = archived_spaces(spaces)
    return [b for b in all_boards() if not is_archived(b, arch)]


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
        if ext in ('xlsx', 'xlsm'):
            return sheet_words(path)
    except (OSError, subprocess.TimeoutExpired, zipfile.BadZipFile, ET.ParseError, KeyError):
        pass
    return ''


def sheet_words(path, cap=2_000_000):
    """A spreadsheet as text: each sheet by name, one row per line, cells split by tabs (a team once compared the user's
    research with a spreadsheet it could not read, and worked from a screenshot instead)."""
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    rel = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
    col = lambda ref: sum((ord(ch) - 64) * 26 ** k for k, ch in enumerate(reversed(re.sub(r'\d', '', ref)))) - 1
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = [''.join(t.text or '' for t in si.iter('{%s}t' % ns['m']))
                  for si in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si', ns)] if 'xl/sharedStrings.xml' in names else []
        rels = {r.get('Id'): r.get('Target') for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        out, size = [], 0
        for sh in ET.fromstring(z.read('xl/workbook.xml')).iter('{%s}sheet' % ns['m']):
            target = rels.get(sh.get(rel), '').lstrip('/')
            target = target if target.startswith('xl/') else 'xl/' + target
            if target not in names:
                continue
            out.append('Sheet: %s' % sh.get('name'))
            for row in ET.fromstring(z.read(target)).iter('{%s}row' % ns['m']):
                cells = {}
                for c in row.findall('m:c', ns):
                    v, kind = c.find('m:v', ns), c.get('t')
                    if kind == 'inlineStr':
                        text = ''.join(t.text or '' for t in c.iter('{%s}t' % ns['m']))
                    elif v is None or v.text is None:
                        continue
                    elif kind == 's':
                        text = shared[int(v.text)] if int(v.text) < len(shared) else ''
                    elif kind == 'b':
                        text = 'TRUE' if v.text == '1' else 'FALSE'
                    else:
                        text = v.text
                    cells[col(c.get('r') or 'A')] = ' '.join(text.split())
                if cells:
                    line = '\t'.join(cells.get(k, '') for k in range(max(cells) + 1))
                    out.append(line)
                    size += len(line)
                    if size > cap:
                        return '\n'.join(out)
            out.append('')
    return '\n'.join(out).strip()


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

    def callback_url(self):
        """Where Google sends you back: this app's own address as you reached it (register it in Google Cloud)."""
        host = self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or 'localhost:%d' % PORT
        host = re.sub(r'^127\.0\.0\.1(?=:|$)', 'localhost', host)   # Google takes http only for localhost
        local = re.match(r'^(localhost|127\.0\.0\.1)(:\d+)?$', host)
        scheme = self.headers.get('X-Forwarded-Proto') or ('http' if local else 'https')
        return '%s://%s/api/integrations/google/callback' % (scheme, host)

    def app_page(self, title, text):
        """A small page in the browser tab after a sign-in, with the way back."""
        body = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>%s</title><body style="font:16px system-ui;max-width:520px;margin:15vh auto;padding:0 16px;'
                'background:#0b1220;color:#e5e9f0"><h2>%s</h2><p>%s</p><p><a style="color:#7cc4ff" href="/">Back to the canvas</a></p>'
                % (html.escape(title), html.escape(title), html.escape(text)))
        return self.reply(200, body.encode(), 'text/html; charset=utf-8')

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
        if len(parts) == 2 and parts[0] == 'textures' and re.match(r'^[a-z0-9-]{1,40}\.jpg$', parts[1]):   # frame fills that are pictures (chalkboards)
            try:
                with open(os.path.join(HERE, 'textures', parts[1]), 'rb') as f:
                    return self.reply(200, f.read(), 'image/jpeg', {'Cache-Control': 'max-age=604800'})
            except FileNotFoundError:
                return self.reply(404, {'error': 'no such texture'})
        if len(parts) == 2 and parts[0] == 'fonts' and re.match(r'^[a-z0-9-]{1,40}\.woff2$', parts[1]):   # the sticky notes' hand font
            try:
                with open(os.path.join(HERE, 'fonts', parts[1]), 'rb') as f:
                    return self.reply(200, f.read(), 'font/woff2', {'Cache-Control': 'max-age=604800'})
            except FileNotFoundError:
                return self.reply(404, {'error': 'no such font'})
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
            return self.reply(200, dict(models.public(), local=local_models(), startup=local_models(False), widget_token=widget_token()))
        if parts == ['api', 'integrations']:
            bid = (urllib.parse.parse_qs(self.path.partition('?')[2]).get('board') or [''])[0]
            return self.reply(200, integrations.public(bid if BOARD_ID.match(bid) else None))
        if parts == ['api', 'integrations', 'google', 'start']:   # Connect Google: off to Google's own sign-in page
            try:
                return self.reply(302, b'', 'text/plain', {'Location': integrations.google_start(self.callback_url())})
            except ValueError as e:
                return self.app_page('Google is not set up yet', str(e))
        if parts == ['api', 'integrations', 'google', 'callback']:   # ...and back here once you allowed it
            qs = urllib.parse.parse_qs(self.path.partition('?')[2])
            if qs.get('error'):
                return self.app_page('Google was not connected', 'Google said: %s.' % qs['error'][0][:100])
            try:
                email = integrations.google_finish((qs.get('code') or [''])[0], (qs.get('state') or [''])[0])
            except (ValueError, OSError) as e:
                return self.app_page('Google was not connected', str(e))
            return self.app_page('Google is connected', 'Agents can now use Gmail, Drive and Calendar for %s.' % (email or 'your account'))
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
        if parts == ['api', 'library']:   # the Agents screen's library: every agent and what it can do
            return self.reply(200, agent_library())
        if len(parts) == 3 and parts[:2] == ['api', 'library'] and re.match(r'^[a-z][a-z0-9-]{0,40}$', parts[2]):   # one agent's profile page
            p = agent_profile(parts[2])
            return self.reply(200, p) if p else self.reply(404, {'error': 'no such agent'})
        if parts == ['api', 'boards', 'status']:   # the board list's pills: which boards have agents at work, a question, or work not looked at
            return self.reply(200, {'boards': RUNNER.board_status()})
        if parts == ['api', 'tasks']:   # the phone app's Agents screen: every task on every board
            names = board_names()
            return self.reply(200, {'runs': [dict(r, boardName=names[r['board']]) for r in RUNNER.view() if r['board'] in names]})
        if parts == ['api', 'push', 'key']:
            return self.reply(200, {'key': PUSH.public}) if PUSH else self.reply(503, NO_PUSH)
        if len(parts) == 3 and parts[:2] == ['api', 'teams'] and BOARD_ID.match(parts[2]):
            return self.reply(200, dict(RUNNER.team(parts[2]), starters=agents.STARTERS))
        if parts == ['api', 'widget', 'hint']:   # the page, coming to the front: did the widget just ask for the search box or the voice?
            hint = WIDGET_HINT[1] if time.time() - WIDGET_HINT[0] < 30 else ''
            WIDGET_HINT[:] = [0, '']
            return self.reply(200, {'hint': hint})
        if parts == ['api', 'widget']:   # the phone's home-screen widget: counts and one line, only with the widget token
            if not secrets.compare_digest(self.headers.get('X-Widget-Token') or '', widget_token()):
                return self.reply(401, {'error': 'The widget token is missing or wrong. Settings shows it.'})
            return self.reply(200, widget_status())
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
            seen = (urllib.parse.parse_qs(self.path.partition('?')[2]).get('seen') or [''])[0]
            if seen in COVER_SEEN and ids[0] in out:
                COVER_SEEN[seen] = True   # a cover shot got its board (its own token: the board-picture loop may be shooting another board)
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
        if len(parts) == 4 and parts[:3] == ['api', 'substack', 'preview'] and re.match(r'^[0-9]{1,20}$', parts[3]):
            # the draft as Substack holds it now, drawn fresh on every load: what the Publish card beside it sends (2026-11-06)
            return self.reply(200, integrations.substack_preview_page(parts[3]).encode(), 'text/html; charset=utf-8', {
                'Cache-Control': 'no-store', 'Content-Security-Policy': "sandbox; connect-src 'none'"})
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
        if len(parts) == 3 and parts[:2] == ['api', 'boardshots'] and parts[2].endswith('.jpg') and BOARD_ID.match(re.sub(r'-dark$', '', parts[2][:-4])):
            # a board's picture for its card (<id>-dark.jpg: the dark one, for dark mode); the page asks with ?u=<updated>, so a
            # current one is kept by the browser. A dark one not taken yet is answered with the light one, never kept.
            bid = re.sub(r'-dark$', '', parts[2][:-4])
            path = os.path.join(BOARDSHOTS, parts[2])
            stand_in = parts[2].endswith('-dark.jpg') and not os.path.exists(path)
            if stand_in:
                path = os.path.join(BOARDSHOTS, bid + '.jpg')
            try:
                with open(path, 'rb') as f:
                    data = f.read()
                fresh = not stand_in and os.stat(path).st_mtime >= os.stat(board_path(bid)).st_mtime
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
        if len(parts) == 3 and parts[:2] == ['api', 'clips'] and FILE_NAME.match(parts[2]):   # a video's board-sized copy or its still
            return self.send_file(os.path.join(CLIPS, parts[2]))
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
            sealed = ext in ('html', 'htm')   # a dropped web page opens as a page, sealed off like a demo: its own blank origin, no cookies, no reach into our data (2026-11-05)
            if ext in PLAIN_TEXT or ext in ('svg', 'xhtml'):
                ctype = 'text/plain; charset=utf-8'
            elif sealed:
                ctype = 'text/html; charset=utf-8'
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
            if sealed:
                self.send_header('Content-Security-Policy', "sandbox allow-scripts allow-forms allow-popups allow-pointer-lock allow-modals; connect-src 'none'")
            self.send_header('Cache-Control', 'no-cache' if sealed else 'private, max-age=31536000, immutable')   # a page is small and its headers may change; the browser had kept the plain-text version for a year
            if not ctype.startswith(INLINE) and not ctype.startswith('text/plain'):
                if not sealed:
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
        if len(parts) == 4 and parts[:2] == ['api', 'library'] and parts[3] == 'skills' and parts[2] in upskill():   # Upskill
            text = ' '.join(str(json.loads(self.read_body()).get('text') or '').split())[:600]
            if not text:
                return self.reply(400, {'error': 'say the skill in a few words'})
            with LOCK:
                sk = skillbook.load()
                sk.setdefault(parts[2], []).append({'id': secrets.token_hex(4), 'text': text, 'added': now_ms()})
                write_json(os.path.join(DATA, 'skills.json'), sk)
            return self.reply(201, {'skills': sk[parts[2]]})
        if parts == ['api', 'push', 'subscribe']:   # this phone wants notifications
            PUSH.subscribe(json.loads(self.read_body()))
            return self.reply(201, {'ok': True})
        if parts == ['api', 'push', 'unsubscribe']:
            PUSH.unsubscribe(str(json.loads(self.read_body()).get('endpoint') or ''))
            return self.reply(200, {'ok': True})
        if parts == ['api', 'push', 'test']:   # the Agents screen's "Send a test": proves the whole path works
            return self.reply(200, {'sent': PUSH.send({'title': 'Notifications are on', 'body': 'Agents will tell you here when they finish or need you.',
                                                         'url': './#agents', 'tag': 'test'})})
        if parts == ['api', 'integrations']:   # a change from the Settings card's Connected apps
            try:
                return self.reply(200, integrations.change(json.loads(self.read_body())))
            except ValueError as e:
                return self.reply(400, {'error': str(e) if str(e).startswith('Substack') else 'That address does not look right. It starts with https:// or http://.'})
            except OSError as e:   # Substack would not take the cookie (424, not 502: Cloudflare would swap a 502 for its own page)
                print('%s substack: %s' % (time.strftime('%H:%M:%S'), e))
                return self.reply(424, {'error': str(e)})
        if parts == ['api', 'substack', 'draft']:   # a note's "Draft this on Substack": saved as a draft on the newsletter, never published
            if not (integrations.load().get('substack') or {}).get('cookie'):
                return self.reply(409, {'error': 'Substack is not connected. Open Settings, Connected apps, and connect it first.'})
            try:
                req = json.loads(self.read_body())
                if BOARD_ID.match(str(req.get('board') or '')):   # the cover picture (and any pictures inside) come from the note's board
                    req.update(_board=req['board'], _selected=[str(x) for x in (req.get('items') or [])][:200])
                return self.reply(200, integrations.substack_draft(req))
            except OSError as e:   # 424, not 502: Cloudflare swaps an origin 502 for its own page and the words would never reach the canvas
                print('%s substack: %s' % (time.strftime('%H:%M:%S'), e))
                return self.reply(424, {'error': str(e)})
        if parts == ['api', 'covers', 'make']:   # a made cover for a title and body, as a picture: to look at one before it is used (2026-11-06)
            req = json.loads(self.read_body())
            data, note = made_cover(str(req.get('title') or '')[:200], str(req.get('body') or '')[:20000])
            if not data:
                return self.reply(503, {'error': 'no cover could be made: the pool is empty or Chrome did not answer'})
            return self.reply(200, data, 'image/jpeg', {'Cache-Control': 'no-store', 'X-Cover-Note': note.encode('ascii', 'ignore').decode()})
        if parts == ['api', 'substack', 'publish']:   # the draft card's Publish, after the person picked email or site-only on the canvas
            if not (integrations.load().get('substack') or {}).get('cookie'):
                return self.reply(409, {'error': 'Substack is not connected. Open Settings, Connected apps, and connect it first.'})
            try:
                return self.reply(200, integrations.substack_publish(json.loads(self.read_body())))
            except OSError as e:   # 424, not 502: Cloudflare swaps an origin 502 for its own page and the words would never reach the canvas
                print('%s substack: %s' % (time.strftime('%H:%M:%S'), e))
                return self.reply(424, {'error': str(e)})
        if len(parts) == 4 and parts[:3] == ['api', 'integrations', 'test'] and re.match(r'^[0-9a-f]{8}$', parts[3]):
            return self.reply(200, integrations.test_server(parts[3]))
        if parts == ['api', 'settings', 'servers']:   # add or remove one of your own model servers, from the Settings card
            return self.reply(200, own_server(json.loads(self.read_body())))
        if len(parts) == 4 and parts[:3] == ['api', 'settings', 'test'] and parts[3] in models.JOBS:
            return self.reply(200, models.test(parts[3]) if models.choice(parts[3]) else local_test(parts[3]))
        if parts == ['api', 'widget', 'hint']:   # the widget was tapped: remember what it wants for the page about to come to the front
            if not secrets.compare_digest(self.headers.get('X-Widget-Token') or '', widget_token()):
                return self.reply(401, {'error': 'The widget token is missing or wrong. Settings shows it.'})
            hint = str(json.loads(self.read_body() or b'{}').get('hint') or '')
            if hint not in ('search', 'voice', 'agents'):
                raise ValueError
            WIDGET_HINT[:] = [time.time(), hint]
            return self.reply(200, {'ok': True})
        if parts == ['api', 'uploads']:   # a dropped file arrives in pieces: start, PUT each piece in order, then done
            up = secrets.token_hex(6)
            open(os.path.join(UPLOADS, up), 'wb').close()
            return self.reply(201, {'up': up})
        if len(parts) == 4 and parts[:2] == ['api', 'uploads'] and UPLOAD_ID.match(parts[2]) and parts[3] == 'done':
            part = os.path.join(UPLOADS, parts[2])
            if not os.path.exists(part):
                return self.reply(404, {'error': 'no such upload'})
            req = json.loads(self.read_body() or b'{}')
            name = str(req.get('name') or '')
            if req.get('size') is not None and int(req['size']) != os.path.getsize(part):   # the page says how big the file is
                os.remove(part)
                return self.reply(409, {'error': 'the file did not arrive whole; drop it again'})
            out = finish_upload(part, name)
            if out.get('kind') == 'video':
                CLIPS_Q.put(out['src'].rsplit('/', 1)[1])
            return self.reply(201, out)
        if parts == ['api', 'boards']:
            req = json.loads(self.read_body() or b'{}')
            t = now_ms()
            board = {'id': secrets.token_hex(6), 'name': str(req.get('name') or 'Untitled board')[:200],
                     'created': t, 'updated': t, 'items': [], 'view': None, 'space': None,
                     'by': 'user'}   # the person's own board, however much a team fills it (a blank demo board left Recent, 2026-11-07)
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
        if parts == ['api', 'images', 'fetch']:   # a picture copied or dragged out of another web page arrives as its address: get it here
            url = str(json.loads(self.read_body() or b'{}').get('url') or '')[:2000]
            try:
                data = fetch_image(url)
            except (OSError, ValueError, subprocess.CalledProcessError):
                return self.reply(422, {'error': 'That picture could not be fetched.'})
            name = hashlib.sha256(data).hexdigest() + ('.png' if data[:8] == b'\x89PNG\r\n\x1a\n' else '.jpg')
            path = os.path.join(IMAGES, name)
            with LOCK:
                if not os.path.exists(path):
                    with open(path + '.tmp', 'wb') as f:
                        f.write(data)
                    os.replace(path + '.tmp', path)
            READER.add(name)
            return self.reply(201, {'src': 'api/images/' + name})
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
        if len(parts) == 4 and parts[:2] == ['api', 'boards'] and parts[3] == 'seen' and BOARD_ID.match(parts[2]):   # the board was opened: its ended tasks are looked at
            return self.reply(200, {'seen': RUNNER.seen(parts[2])})
        if parts == ['api', 'name-frame']:   # a frame drawn around items asks for its name
            texts = [str(t) for t in (json.loads(self.read_body()).get('texts') or []) if str(t).strip()][:40]
            if not texts:
                raise ValueError
            try:
                return self.reply(200, {'title': talk.name_frame(texts)})
            except Exception as e:
                return self.reply(424, {'error': 'The model did not answer: %s' % e})
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
            history = [{'q': t['q'], 'a': t['a'], 'did': t.get('did'), 'route': t.get('route'), 't': t.get('t', 0)} for t in read_talk(talk_id)['turns'] if not t['a'].startswith(NOT_MOVED) and t.get('t', 0) >= since] if BOARD_ID.match(talk_id) or talk_id in ('all', 'unsorted') else []
            # carry: the voice brought the user here mid-conversation ("open the Research board"); what was said just before counts
            carry = [{'q': str(c['q'])[:500], 'a': str(c['a'])[:2000], 'did': None, 't': c['t']} for c in (req.get('carry') or [])[:6]
                     if isinstance(c, dict) and c.get('q') and c.get('a') and isinstance(c.get('t'), (int, float))]
            have = {(h['t'], h['q']) for h in history}
            history = sorted(history + [c for c in carry if (c['t'], c['q']) not in have], key=lambda h: h['t'])[-3:]
            waiting_on = PENDING.get(talk_id)
            if waiting_on and time.time() - waiting_on['t'] > 1800:   # old: forgotten
                PENDING.pop(talk_id, None)
                waiting_on = None
            said_back = pending_reply(question) if waiting_on else None
            m = RULE_Q.match(question) if not waiting_on else None
            if m:   # "add to my rules: ..." / "from now on every team ...": a standing rule, said once
                rule = (m.group(1) or m.group(2)).strip().rstrip('.') + '.'
                rule = rule[0].upper() + rule[1:]
                add_rule(rule)
                text = 'Added to your standing rules for every team: "%s"' % rule
                turn = {'q': question, 'a': text, 'sources': [], 'checked': None, 't': now_ms(), 'route': 'rules'}
                with LOCK:
                    conv = read_talk(talk_id)
                    conv['turns'] = (conv['turns'] + [turn])[-500:]
                    write_json(talk_path(talk_id), conv)
                return self.reply(200, {'answer': text, 'sources': [], 'turn': turn})

            def settle(said_back):   # a yes does it, a no drops it
                PENDING.pop(talk_id, None)
                if said_back == 'yes':
                    why = {'asked': waiting_on.get('asked', ''), 'said': waiting_on['say'], 'answer': question, 'when': now_ms(),
                           'by': 'Assistant', 'where': req.get('board') and 'on this board' or 'from %s' % talk_id}
                    try:
                        text, extra = run_pending(waiting_on['action'], why)
                    except (OSError, ValueError, KeyError) as e:
                        text, extra = 'That did not work (%s), so nothing was added.' % str(e)[:120], {}
                else:
                    text, extra = 'Okay, I won\'t.', {}
                turn = {'q': question, 'a': text, 'sources': [], 'checked': None, 't': now_ms(), 'route': 'confirm'}
                with LOCK:
                    conv = read_talk(talk_id)
                    conv['turns'] = (conv['turns'] + [turn])[-500:]
                    write_json(talk_path(talk_id), conv)
                return self.reply(200, {'answer': text, 'sources': [], 'turn': turn, **extra})
            if said_back:   # plain yes or no: no model needed
                return settle(said_back)
            if req.get('board'):
                try:
                    boards = [read_board(str(req['board']))] if BOARD_ID.match(str(req['board'])) else []
                except FileNotFoundError:
                    boards = []
                if not boards:
                    return self.reply(404, {'error': 'no such board'})
                scope = boards[0].get('name') or 'this board'
            elif talk_id == 'all':
                boards, scope = live_boards(list(spaces.values())), 'all'
            else:
                sid = None if space == 'unsorted' else space
                within = {sid} | {x['id'] for x in spaces.values() if sid and x.get('parent') == sid}   # a parent space includes its sub-spaces' boards
                boards = [b for b in all_boards() if (b.get('space') if b.get('space') in spaces else None) in within]
                scope = 'space:' + ('Unsorted' if sid is None else spaces[sid]['name'])
            try:
                picked = []
                if req.get('board') and isinstance(req.get('selected'), list):
                    on = {i['id'] for i in boards[0].get('items') or []}
                    picked = [str(x) for x in req['selected'][:300] if str(x) in on]
                here = ('inside the board "%s"' % scope if req.get('board') else 'on the list of all boards' if talk_id == 'all'
                        else 'in the %s space' % scope[6:] if talk_id != 'unsorted' else 'on the list of boards not in any space')
                if req.get('board'):
                    sp = spaces.get(boards[0].get('space'))
                    here += ' (in the %s space)' % sp['name'] if sp else ' (not in any space)'
                    if picked:
                        here += ', with %d item%s selected' % (len(picked), '' if len(picked) == 1 else 's')
                    frames = {i['id'] for i in boards[0].get('items') or []}   # a team whose frame was cleared from the board left nothing to answer from (2026-11-05)
                    done = [(r['task'].split(' (This is about')[0][:200], ' '.join(str(r.get('summary') or '').split())[:300])
                            for r in RUNNER.view(boards[0]['id']) if r.get('status') in ('done', 'partial') and not r.get('parent') and r.get('frame') in frames][-3:]
                    if done:   # "what would it take to build this" right after a team answered it: the answer is on the board;
                        # with what it found, so "now execute what they worked out" carries it (a team once wrote an essay instead, 2026-11-07)
                        here += '. Teams that already finished their work on this board: %s' % '; '.join(
                            '"%s"%s' % (d, ' (what it found: %s)' % s if s else '') for d, s in done)
                # what kind of question it is, decided while the map below is built (small talk and app questions skip it)
                apps = integrations.names() if integrations.board_on(boards[0]['id'] if req.get('board') else None) else []
                early = {}
                agent_waits = bool([w for w in RUNNER.news()['waiting'] if w['board'] in {b['id'] for b in boards}])
                if req.get('board') and not waiting_on:
                    live = [r for r in RUNNER.view(boards[0]['id']) if not r.get('parent') and r['status'] in agents.ACTIVE]
                    quick = status_answer(question, live) if live else frame_answer(question, boards[0], frames)
                    if quick:   # a status question or "show me the frame": answered from the runs themselves, no model (2026-11-07)
                        record_ask(talk_id, question, history, here, '', apps, bool(agent_waits), (waiting_on or {}).get('say', ''), picked,
                                   boards[0]['id'], dict(quick, route='status'))
                        turn = {'q': question, 'a': quick['answer'], 'sources': [], 'checked': None, 't': now_ms(), 'route': 'status'}
                        with LOCK:
                            conv = read_talk(talk_id)
                            conv['turns'] = (conv['turns'] + [turn])[-500:]
                            write_json(talk_path(talk_id), conv)
                        return self.reply(200, dict(quick, sources=[], turn=turn))

                def route_now():
                    try:
                        early['route'], early['reply'] = talk.small_talk(question, history, [1] if agent_waits else [], here, apps,
                                                                         (waiting_on or {}).get('say', ''))
                    except Exception:
                        early['route'], early['reply'] = 'boards', None
                routing = threading.Thread(target=route_now, daemon=True)
                routing.start()
                t_ask = time.time()
                # the map of all spaces and boards goes with every question, so the layout itself can be talked about
                everything = live_boards(list(spaces.values()))
                layout = talk.layout_entry([x for x in spaces.values() if x['id'] not in archived_spaces(list(spaces.values()))], everything, {b['id']: BRIEFER.load(b['id']) for b in everything}, here, detail=not req.get('board'),
                                           question=question, history=history)
                names = {b['id']: b.get('name', '') for b in boards}
                waiting = [w for w in RUNNER.news()['waiting'] if w['board'] in names]   # questions on the boards in scope
                tasks = [{'board': t['board'], 'boardName': names.get(t['board'], ''), 'item': t['frame'], 'run': t['id'], 'frame': '',
                          'ended': t.get('ended'), 'text': RUNNER.progress(t)} for t in sorted(RUNNER.recent(set(names)), key=lambda t: t['created'])][-6:]
                t_call = time.time()
                spaces_text = '\n'.join('- %s%s: %s' % (x['name'], ' (inside %s)' % spaces[x['parent']]['name'] if x.get('parent') in spaces else '',
                                                        ', '.join(b.get('name', '') for b in everything if b.get('space') == x['id'])[:300])
                                       for x in spaces.values())
                result = talk.answer(question, history, boards, scope, READER, {b['id']: BRIEFER.load(b['id']) for b in boards}, layout,
                                     waiting, tasks, RUNNER.team_text(boards[0]['id']) if req.get('board') else '', req.get('seen'), FINDER,
                                     here=here, apps=apps, routed=(routing, early),
                                     selected=picked, bid=boards[0]['id'] if req.get('board') else None, pending=(waiting_on or {}).get('say', ''),
                                     spaces=[x['name'] for x in spaces.values()], pending_raw=(waiting_on or {}).get('raw'),
                                     spaces_text=spaces_text)
                print('%s ask %.2fs route=%s prep=%dms %s %r%s' % (time.strftime('%Y-%m-%d %H:%M:%S'), time.time() - t_ask, result.get('route'), (t_call - t_ask) * 1000, result.get('timing') or '', question[:60],
                      ' task=%r' % (result.get('team') or {}).get('task', '')[:160] if result.get('team') else ''), flush=True)
                record_ask(talk_id, question, history, here, spaces_text, apps, bool(waiting), (waiting_on or {}).get('say', ''), picked,
                           boards[0]['id'] if req.get('board') else None, result)
                if result.get('route') in ('yes', 'no') and waiting_on:   # the check heard agreement or a refusal in other words
                    return settle(result['route'])
                proposal = result.pop('proposal', None)
                build = result.pop('agent_build', None)
                if build:   # a new kind of teammate, in their words: write its charter, keep it, and it joins the designer's kinds (2026-11-05)
                    try:
                        a = talk.agent_charter(build, [v['name'] for v in agents.STARTERS.values()])
                    except (OSError, ValueError, KeyError):
                        a = None
                    if a:
                        agents.add_custom(DATA, a)
                        result['answer'] = ('Built the %s. %s It is in the Agent library with its full charter, where you can read it, '
                                            'teach it skills, or ask me to change it; a job that fits it will use it.' % (a['name'], a['why'].rstrip('.') + '.'))
                        result['agent'] = a['key']
                    else:
                        result['answer'] = "I couldn't write that agent's charter from that. Say again what it should do, in a sentence or two."
                plan = result.pop('team', None)
                if plan:   # a team job: ask first, saying where its work goes (on the selected items, when there are some)
                    sel = []
                    if not picked and req.get('board') and REFERS_BACK.search(question):   # "now execute what they worked out": the same items
                        on = {i['id'] for i in boards[0].get('items') or []}
                        for r in reversed(RUNNER.view(boards[0]['id'])):
                            if not r.get('parent') and r.get('status') in ('done', 'partial') and r.get('selected'):
                                picked = [x for x in r['selected'] if x in on]
                                break
                    if picked:
                        sel, about = task_items(boards[0], [{'board': boards[0]['id'], 'item': x} for x in picked], [])
                        plan['task'] = plan.get('task', question) + about
                        plan['n_selected'] = len(picked)
                    pend = propose_team(plan, boards[0] if req.get('board') else None, space if space in spaces else None, list(spaces.values()), sel)
                    if pend:
                        pend['asked'] = question
                        PENDING[talk_id] = pend
                        result['answer'] = pend['say']
                if proposal:   # the Assistant wants to put things on a board: say it in plain words and wait for their word
                    pend = propose(proposal, boards[0] if req.get('board') else None, space if space in spaces else None, list(spaces.values()))
                    if pend:
                        pend['asked'] = question   # kept with what it adds: why it is on the board
                        PENDING[talk_id] = pend
                        result['answer'] = pend['say']
                        for k in ('task', 'show', 'open', 'view', 'web_card', 'add_teammate', 'follow_up', 'steer', 'agent_answer', 'edit'):
                            result.pop(k, None)   # one thing at a time: what it said it is about to do, nothing else
                        result['sources'] = result.get('sources') or []
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
                if task and req.get('board'):   # "have the team / an agent ...": asked first, like any team job
                    sel, about = task_items(boards[0], [{'board': boards[0]['id'], 'item': x} for x in picked] or result.get('sources') or [],
                                            read_talk(talk_id)['turns'])
                    pend = propose_team({'task': task + about, 'mode': mode, 'explicit': True, 'n_selected': len(picked)}, boards[0], None,
                                        list(spaces.values()), sel)
                    if pend:
                        pend['asked'] = question
                        PENDING[talk_id] = pend
                        result['answer'] = pend['say']
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
            if PENDING.get(talk_id):   # still waiting for their word (a question in between leaves it waiting)
                result['pending_action'] = {'id': PENDING[talk_id]['id'], 'say': PENDING[talk_id]['say']}
            turn = {'q': question, 'a': result['answer'], 'sources': result['sources'], 'checked': result.get('checked'), 't': now_ms(),
                    'route': result.get('route') or 'boards',
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
            gain = VOICE_GAIN.get(voice, 1.0)
            pcm = 'octet-stream' in upstream.headers.get('Content-Type', 'application/octet-stream')
            odd = b''
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
                        if pcm and gain != 1.0:
                            chunk, odd = louder(odd + chunk, gain)
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
            have = os.path.getsize(part)
            at = (urllib.parse.parse_qs(self.path.partition('?')[2]).get('at') or [None])[0]   # where this piece belongs: a piece
            if at is not None:   # sent twice (the browser retries a PUT whose reply was lost) was appended twice and shifted the rest of a video, 2026-11-07
                at = int(at)
                if at < have:   # already here
                    return self.reply(200, {'size': have})
                if at > have:   # a piece is missing before this one: the upload cannot be right
                    os.remove(part)
                    return self.reply(409, {'error': 'a piece of the file went missing; drop it again'})
            if have + len(data) > MAX_FILE:
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
            req = json.loads(self.read_body())
            name = str(req.get('name') or '').strip()[:80]
            if not name and 'archived' not in req:
                raise ValueError
            with LOCK:
                sp = read_spaces()
                hit = [x for x in sp['spaces'] if x['id'] == sid]
                if not hit:
                    return self.reply(404, {'error': 'no such space'})
                if name:
                    hit[0]['name'] = name
                if 'archived' in req:   # a top-level space only: its sub-spaces go with it
                    if hit[0].get('parent'):
                        return self.reply(400, {'error': 'archive the whole space, not a sub-space'})
                    if req['archived']:
                        hit[0]['archived'] = True
                    else:
                        hit[0].pop('archived', None)
                write_json(SPACES, sp)
            return self.reply(200, hit[0])
        if len(parts) == 4 and parts[:2] == ['api', 'boards'] and parts[3] == 'archive' and BOARD_ID.match(parts[2]):
            archived = bool(json.loads(self.read_body()).get('archived'))
            with LOCK:
                try:
                    board = read_board(parts[2])
                except FileNotFoundError:
                    return self.reply(404, {'error': 'no such board'})
                if archived:
                    board['archived'] = True
                else:
                    board.pop('archived', None)
                write_json(board_path(parts[2]), board)
            return self.reply(200, {'ok': True})
        if len(parts) == 4 and parts[:2] == ['api', 'boards'] and parts[3] == 'made-by' and BOARD_ID.match(parts[2]):   # the card's menu: who made this board
            by = json.loads(self.read_body()).get('by')
            if by not in ('agent', 'assistant', 'user'):
                raise ValueError
            with LOCK:
                try:
                    board = read_board(parts[2])
                except FileNotFoundError:
                    return self.reply(404, {'error': 'no such board'})
                board['by'] = by
                write_json(board_path(parts[2]), board)
            return self.reply(200, {'ok': True})
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
            # and a task frame keeps the size the agent grew it to while its team is still at work (once it is done, the
            # page's size wins: otherwise scaling a finished frame down snapped its height back, 2026-11-03)
            sent = {i.get('id'): i for i in req['items'] if isinstance(i, dict)}
            gone = set(req.get('deleted') or [])
            live = {r['id'] for r in RUNNER.view(bid) if r['status'] in agents.ACTIVE}
            # base: the board's 'updated' this page's copy was built on. An item another copy added after that (its 'added'
            # stamp is newer) is one this page never saw, so it stays: a phone page left in the background saved an older
            # copy and wiped a morning's cards (2026-11-05). Pages that send no base keep the old behaviour.
            base = int(req.get('base') or 0)
            for i in board.get('items') or []:
                if i.get('agent') and i['id'] not in sent and i['id'] not in gone:
                    req['items'].append(i)
                elif base and i['id'] not in sent and i['id'] not in gone and (i.get('added') or 0) > base:
                    req['items'].append(i)
                elif i.get('run') in live and i.get('type') == 'area' and i['id'] in sent:
                    sent[i['id']]['h'] = max(sent[i['id']].get('h', 0), i.get('h', 0))
                elif i.get('chart') and i['id'] in sent:   # a team's org chart box: its status is the team's to change
                    sent[i['id']].update(text=i.get('text'), color=i.get('color'), org=i.get('org'))
            # a page that has not seen a move made here yet sends the item at its old place: keep the move
            moves = {k: v for k, v in (MOVED.get(bid) or {}).items() if time.time() - v[2] < 900}
            MOVED[bid] = moves
            kept = False
            for k, (olds, new, _) in moves.items():
                it = sent.get(k)
                if it and any(abs(it.get('x', 0) - ox) < .5 and abs(it.get('y', 0) - oy) < .5 for ox, oy in olds):
                    it.update(x=new['x'], y=new['y'], z=new.get('z', it.get('z')), w=new.get('w', it.get('w')), h=new.get('h', it.get('h')))
                    kept = True
            # when each item first arrived: an item this board has not had is stamped now (added by hand), and a page that
            # never saw a stamp or a record does not wipe it (records of why began on October 31, 2026)
            stored = {i['id']: i for i in board.get('items') or [] if isinstance(i, dict) and i.get('id')}
            for it in req['items']:
                if not isinstance(it, dict) or not it.get('id'):
                    continue
                old = stored.get(it['id'])
                if old is None:
                    if not it.get('added') and not it.get('agent'):
                        it['added'] = now_ms()
                else:
                    for k in ('added', 'why', 'voice', 'clip', 'poster'):   # voice: the user's recorded reason for adding it; clip, poster: made here
                        if k in old and k not in it:
                            it[k] = old[k]
                clip_fill(it)
            board['items'] = req['items']
            board['view'] = req.get('view')
            board['updated'] = now_ms()
            write_json(board_path(bid), board)
        BRIEFER.touch(bid)   # read the changed parts again once the edits settle
        live_send(bid, {'kind': 'saved', 'updated': board['updated']}, self.headers.get('X-Client') or '')
        self.reply(200, {'updated': board['updated'], **({'refresh': True} if kept else {})})   # kept: this page should fetch the board

    def delete(self, parts):
        if len(parts) == 5 and parts[:2] == ['api', 'library'] and parts[3] == 'skills' and parts[2] in upskill():   # take a skill back
            with LOCK:
                sk = skillbook.load()
                sk[parts[2]] = [x for x in sk.get(parts[2]) or [] if x['id'] != parts[4]]
                write_json(os.path.join(DATA, 'skills.json'), sk)
            return self.reply(200, {'skills': sk[parts[2]]})
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
                except (OSError, ValueError, AttributeError, KeyError, TypeError):   # one board that will not read must not hide the rest
                    print('board %s left out of the list: it did not read' % bid, flush=True)
                    continue
                del card['items']
                CARDS[bid] = (mtime, card)
        return sorted((c for _, c in CARDS.values()), key=lambda b: b.get('updated') or 0, reverse=True)


def board_names():
    return {c['id']: c.get('name') or '' for c in board_cards()}


def widget_token():
    """The phone widget's key: a random word kept in the data folder, made the first time it is asked for.
    Settings shows it; the widget sends it as X-Widget-Token. Delete the file to make a new one."""
    path = os.path.join(DATA, 'widget-token')
    try:
        with open(path) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_hex(16)
    with open(path, 'w') as f:
        f.write(tok + '\n')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return tok


WIDGET_HINT = [0.0, '']   # what the widget's tap wants of the page (search, voice, agents) and when: the bar resumes the app instead of
                          # opening an address, which Android covers with a splash screen; the hint rides here (2026-11-04)


def widget_status():
    """What the phone widget shows under its title: how many questions wait, tasks run and finished tasks are untold,
    plus one line about the most pressing of them. Read-only; nothing from the boards themselves."""
    news = named_news()
    names = board_names()
    running = [r for r in RUNNER.view() if r['board'] in names and r['status'] in ('queued', 'working', 'team')]
    waiting, done = news['waiting'], news['done']
    if waiting:
        w = waiting[0]
        line = '%s asks: %s' % ((w.get('member') or {}).get('name') or 'The team', w.get('question') or '')
    elif done:
        line = 'Finished: %s' % done[-1]['task']
    elif running:
        line = 'Working: %s' % running[0]['task']
    else:
        line = 'All quiet. %d boards.' % len(names)
    return {'waiting': len(waiting), 'running': len(running), 'done': len(done), 'line': line[:140]}


def named_news():
    """RUNNER.news() with each item's board name; items on deleted boards are left out."""
    names = board_names()
    news = RUNNER.news()
    for k in ('done', 'waiting'):
        news[k] = [dict(n, boardName=names[n['board']]) for n in news[k] if n['board'] in names]
    return news


def made_by(b):
    """Who made a board: 'agent' (a team's work, the rules board, the router's Incoming), 'assistant' (made from a voice
    ask), or None for a board the user made. Boards made by code say so ('by'); older ones are judged by what is on
    them: half or more agent items = an agent's, half or more items added by the assistant or agents = the assistant's.
    The board list shows a badge for these so they are told apart from the user's own boards (2026-11-04)."""
    if b.get('by'):
        return None if b['by'] == 'user' else b['by']   # 'user': the person set it from the card's menu, whatever the items say
    if b.get('id') in code_boards():
        return 'agent'
    items = [i for i in (b.get('items') or []) if i.get('type') != 'arrow']
    if len(items) < 3:
        return None
    agent = sum(1 for i in items if i.get('agent') or (isinstance(i.get('why'), dict) and i['why'].get('run')))   # why is a record, but one card with it as text blanked the whole home page (2026-11-07)
    added = sum(1 for i in items if i.get('why') and not i.get('agent'))
    # one item of the person's own and the board is theirs, however much the teams added to it since: the person made
    # the board themselves and the teams filled it, and the badge called it an agent's (2026-11-05)
    if agent == len(items):
        return 'agent'
    if agent + added == len(items):
        return 'assistant'
    return None


CODE_BOARDS = [0.0, set()]
def code_boards():
    """The boards the app itself keeps: the rules board and the router's Incoming (their ids live in small files)."""
    if time.time() - CODE_BOARDS[0] > 30:
        ids = set()
        for path in (RULES_FILE, os.path.join(DATA, 'router', 'board')):
            try:
                with open(path) as f:
                    ids.add(f.read().strip())
            except OSError:
                pass
        CODE_BOARDS[:] = [time.time(), ids]
    return CODE_BOARDS[1]


def light(b, keep=150):
    """A board as its card needs it: name, space, dates, item count, the outline of all items, the biggest items."""
    items = b.get('items') or []
    out = {k: b.get(k) for k in ('id', 'name', 'space', 'created', 'updated')}
    if b.get('archived'):
        out['archived'] = True
    out['count'] = len(items)
    out['by'] = made_by(b)
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
             'agents': [(agents.MODEL, agents.MODEL_URL)] if agents.MODEL_URL else [], 'advisor': [],   # advisor: none until picked
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
    try:
        return edit_board(bid, change)
    except RulesLocked as e:
        return str(e), False


def regroup(ids, groups):
    """The flat list of card ids cut back into the groups' sizes."""
    out, k = [], 0
    for _, g in groups:
        out.append(ids[k:k + len(g)])
        k += len(g)
    return out


# ---------- the Assistant's confirmation line: say what is about to happen, wait for a yes ----------
PENDING = {}   # conversation id -> {'id', 'say', 'action', 't'}: proposed, not done; kept 30 minutes, in memory only
SAY_YES = re.compile(r"^\W*(yes|yeah|yep|yup|ya|yah|ok|okay|k|sure|go|go ahead|do it|please|please do|sounds good|perfect|great|"
                     r"that's right|correct|absolutely|definitely|of course|for sure|mm+[- ]?hm+|uh[- ]?huh|mhm+|alright|all right)\b[\w\s,.!']*$", re.I)
SAY_NO = re.compile(r"^\W*(no|nope|nah|never ?mind|cancel|stop|don't|do not|forget it|leave it|not now)\b", re.I)
CHANGE_WORDS = re.compile(r"\b(but|actually|instead|rather|only|except|change|different|another|other|new board|in the|into)\b", re.I)


JOB_WORDS = re.compile(r"\b(have (a|the|an) (team|agent|teammate)|have them|get (a|the) (team|agents)|spin up|start (a|the|another)|build|make|write|find|search|"
                       r"create|convert|put together|compare|research|look for|look up|redo|again|update (the|that|this|my|our|its|every|all)|"
                       r"(the|that|this|my) (card|note|table|file|list|post|sheet)s?)\b", re.I)
STATUS_Q = re.compile(r"\b(status|(an|quick|status|progress) update|update (me|us)|progress|how far along|where things stand|still (working|going|running)|"
                      r"where (are we|we're|we are|is it|it's|are they|they're|is the team) at|"
                      r"how('s| is| are) (it|they|the team|things) (going|doing|coming)|what('s| is| are) (it|they|the team) doing|"
                      r"what (it|they|the team)('s|'re| is| are) doing|how (is|are) the (team|agents) doing)\b", re.I)
FRAME_Q = re.compile(r"^\W*(ok(ay)?,?\s+)?(show|open|go to|zoom (in )?to|pull up)\s+(me\s+)?(the|that|their|its|the team's)\s+(frame|results?)\W*$", re.I)
REFERS_BACK = re.compile(r"\b(the team|they|them|their|that job|the intention|what (they|it) (found|worked out))\b", re.I)


def status_answer(question, live):
    """A status question while a team works: the answer from the runs themselves, in a few spoken sentences. The board
    answer model once turned "tell me where we're at" into a team offer and took 53 s (2026-11-07)."""
    if not STATUS_Q.search(question) or JOB_WORDS.search(question) or len(question.split()) > 30:
        return None
    r = live[-1]
    short = lambda t, k=12: ' '.join(str(t or '').split()[:k]) + ('…' if len(str(t or '').split()) > k else '')
    kids = [RUNNER.runs[c] for c in r.get('children', []) if c in RUNNER.runs]
    names = lambda xs: ', '.join((k.get('member') or {}).get('name', 'a teammate') for k in xs)
    done = [k for k in kids if k['status'] in agents.ENDED]
    going = [k for k in kids if k['status'] in agents.ACTIVE]
    who = 'The team' if r.get('lead') else (r.get('member') or {}).get('name') or 'The agent'
    say = '%s is still working on "%s".' % (who, short(r['task']))
    if kids:
        say += ' %d of %d parts are back%s.' % (len(done), len(kids), ' (%s)' % names(done) if done else '')
        if going:
            now = going[0]['steps'][-1]['text'] if going[0].get('steps') else ''
            say += ' %s %s working now%s.' % (names(going), 'is' if len(going) == 1 else 'are', ' (%s)' % short(now, 10) if now else '')
        elif r.get('steps'):
            say += ' The Lead\'s last step: %s.' % short(r['steps'][-1]['text'], 10)
    elif r.get('steps'):
        say += ' Last step: %s.' % short(r['steps'][-1]['text'], 12)
    if len(live) > 1:
        say += ' %d other team%s on this board %s working too.' % (len(live) - 1, '' if len(live) == 2 else 's', 'is' if len(live) == 2 else 'are')
    return {'answer': say, 'show': [{'board': r['board'], 'item': r['frame']}] if r.get('frame') else []}


def frame_answer(question, board, frames):
    """"Show me the frame" right after a team finished: that team's frame, not a frame the model picks (it zoomed to an
    unrelated frame and read its gist, 2026-11-07)."""
    if not FRAME_Q.match(question):
        return None
    runs = [r for r in RUNNER.view(board['id']) if not r.get('parent') and r.get('frame') in frames and r.get('status') in agents.ENDED]
    if not runs:
        return None
    r = max(runs, key=lambda x: x.get('ended') or x['created'])
    short = ' '.join(str(r['task']).split()[:12])
    return {'answer': 'Here is the frame of "%s".' % short, 'show': [{'board': board['id'], 'item': r['frame']}]}


def pending_reply(text):
    """'yes', 'no' or None (a change, or something else) for a reply while an action waits."""
    t = ' '.join(str(text).split())
    if len(t.split()) <= 6 and SAY_YES.match(t) and not CHANGE_WORDS.search(t):
        return 'yes'
    if SAY_NO.match(t) and len(t.split()) <= 4:
        return 'no'
    return None


def space_named(name, spaces):
    """The space a model named ("Side projects" for "Side Projects"), or None."""
    import difflib
    n = str(name or '').strip().lower()
    if not n:
        return None
    by = {x['name'].lower(): x for x in spaces}
    if n in by:
        return by[n]
    near = difflib.get_close_matches(n, list(by), 1, .75)
    return by[near[0]] if near else None


def propose(p, here_board, here_space, spaces):
    """A proposal from the Assistant -> the action and the sentence that says it (the app's words, never the model's)."""
    emails = [str(x) for x in (p.get('email_ids') or []) if str(x).strip()][:30]
    files = [str(x) for x in (p.get('file_ids') or []) if str(x).strip()][:30]
    events = [str(x) for x in (p.get('event_ids') or []) if str(x).strip()][:40]
    cards = [{'text': str(c.get('text') or '').strip()[:900], 'url': str(c.get('url') or '').strip()[:500]}
             for c in (p.get('cards') or []) if isinstance(c, dict) and str(c.get('text') or '').strip()][:30]
    n = len(emails) + len(files) + len(events) + len(cards)
    if not n:
        return None
    only = [k for k, v in (('email', emails), ('Drive file', files), ('calendar event', events), ('card', cards)) if v]
    what = '%d %s%s' % (n, only[0] if len(only) == 1 else 'card', '' if n == 1 else 's')
    title = ' '.join(str(p.get('frame_title') or '').split())[:80] or 'From your apps'
    a = {'emails': emails, 'files': files, 'events': events, 'cards': cards, 'title': title, 'what': what}
    if p.get('where') == 'this board' and here_board:
        a.update(board=here_board['id'])
        say = 'Want me to add %s to this board, in a frame called "%s"?' % (what, title)
    else:
        sp = space_named(p.get('space'), spaces) or (next((x for x in spaces if x['id'] == here_space), None) if here_space else None)
        name = ' '.join(str(p.get('board_name') or title).split())[:80]
        a.update(new_board=name, space=sp['id'] if sp else None)
        say = 'Want me to make a new board called "%s"%s with %s?' % (name, ' in %s' % sp['name'] if sp else ' with your unsorted boards', what)
    return {'id': secrets.token_hex(4), 'say': say, 'action': a, 't': time.time(), 'raw': p}


def attachment_item(filename, data):
    """An email attachment kept like a dropped file: a PDF becomes its first-page picture (a click opens the PDF), a
    picture is itself. -> board item fields (no place yet), or None."""
    if data[:5] == b'%PDF-':
        part = os.path.join(UPLOADS, secrets.token_hex(6))
        with open(part, 'wb') as f:
            f.write(data)
        r = finish_upload(part, filename)
        if not r.get('src'):
            return None
        item = {'type': 'img', 'src': r['src'], 'url': r['url'], 'label': filename}
        pic = os.path.join(IMAGES, r['src'].rsplit('/', 1)[1])
    elif data[:3] == b'\xff\xd8\xff' or data[:8] == b'\x89PNG\r\n\x1a\n':
        name = hashlib.sha256(data).hexdigest() + ('.png' if data[:4] == b'\x89PNG' else '.jpg')
        pic = os.path.join(IMAGES, name)
        if not os.path.exists(pic):
            with open(pic + '.tmp', 'wb') as f:
                f.write(data)
            os.replace(pic + '.tmp', pic)
        READER.add(name)
        item = {'type': 'img', 'src': 'api/images/' + name, 'label': filename}
    else:
        return None
    try:   # its shape, so the card on the board is not stretched
        out = subprocess.run(['sips', '-g', 'pixelWidth', '-g', 'pixelHeight', pic], capture_output=True, text=True, timeout=20).stdout
        w, h = (int(re.search(r'%s: (\d+)' % k, out).group(1)) for k in ('pixelWidth', 'pixelHeight'))
        item['ratio'] = h / w
    except (OSError, AttributeError, ValueError, subprocess.TimeoutExpired):
        item['ratio'] = 1.29
    return item


def card_source(c):
    """Where one card came from, in words, for its "why" record."""
    first = c.get('text', '').split('\n')
    if c.get('kind') == 'email':
        head, subj = first[0], (first[1] if len(first) > 1 else '')
        return {'app': 'Gmail', 'title': 'Email from %s: %s' % (head, subj), 'url': c.get('url', '')}
    if c.get('kind') == 'event':
        return {'app': 'Google Calendar', 'title': 'Event: %s, %s' % (first[1] if len(first) > 1 else '', first[0]), 'url': c.get('url', '')}
    if c.get('kind') == 'file':
        return {'app': 'Google Drive', 'title': 'Drive file: %s' % first[0], 'url': c.get('url', '')}
    return {'app': 'Assistant', 'title': 'Written by the Assistant from what it read', 'url': c.get('url', '')}


def propose_team(plan, here_board, here_space, spaces, sel=None):
    """A team job -> the action and the question that asks for it (where its work goes, said out loud)."""
    task = ' '.join(str(plan.get('task') or '').split())[:1200]
    if not task:
        return None
    mode = 'one' if plan.get('mode') == 'one' else 'team'
    who = 'an agent' if mode == 'one' else 'a team'
    lead = ('' if plan.get('explicit') else "That's a bigger job: %s. " % plan['why'].rstrip('.') if plan.get('why')
            else "That's a bigger job. ")   # the user did not ask for a team: say why one is offered
    a = {'kind': 'team', 'task': task, 'mode': mode, 'sel': list(sel or []), 'intent': plan.get('intent')}
    it = plan.get('intent') or {}
    if plan.get('changed'):   # a change to the set-up the user is about to say yes to: no "bigger job" or "can't" said twice
        lead = ''
    elif it.get('cannot') and it.get('deliver'):   # say up front what no team can do (a team once "engineered" an integration it could not build)
        lead += "A team can hand back %s, but it can't %s. " % (
            it['deliver'].rstrip('.'), it['cannot'][0].lower() + it['cannot'][1:].rstrip('.'))
    elif it.get('deliver') and (it.get('avoid') or it.get('acts')):   # the plan before the yes, when there is something to know (idea-to-spec's playback, 2026-11-07)
        lead += 'It hands back %s%s.%s ' % (it['deliver'].rstrip('.'), (', and does not %s' % (it['avoid'][0].lower() + it['avoid'][1:])) if it.get('avoid') else '',
                                           ' Each send or post waits for your yes.' if it.get('acts') else '')
    if here_board:
        a['board'] = here_board['id']
        n = plan.get('n_selected') or 0   # what the user selected (the team also gets the cards stacked with them)
        on = 'the %d selected item%s' % (n, '' if n == 1 else 's') if n else 'it'
        say = '%sWant me to start %s on %s and put its work on this board?' % (lead, who, on)
    else:
        name = ' '.join(str(plan.get('board_name') or 'Team work').split())[:80]
        a.update(new_board=name)   # a new board for a job always lands in Agent boards (2026-11-05): drag it out to keep it
        say = '%sWant me to start %s on it, on a new board called "%s" in Agent boards?' % (lead, who, name)
    return {'id': secrets.token_hex(4), 'say': say, 'action': a, 't': time.time(), 'raw': dict(plan, kind='team')}


def run_team(a):
    """Start the team the user said yes to, on this board or on a new board (in a new space when that was the plan)."""
    if a.get('new_board'):
        space = a.get('space')
        if a.get('new_space'):
            with LOCK:
                sp = read_spaces()
                hit = next((x for x in sp['spaces'] if x['name'].lower() == a['new_space'].lower()), None)
                if not hit:
                    hit = {'id': secrets.token_hex(6), 'name': a['new_space'], 'created': now_ms()}
                    sp['spaces'].append(hit)
                    write_json(SPACES, sp)
                space = hit['id']
        t = now_ms()
        space = agent_space()   # every board a team makes for a job lives in Agent boards (2026-11-05)
        board = {'id': secrets.token_hex(6), 'name': a['new_board'], 'created': t, 'updated': t, 'items': [], 'view': None, 'space': space, 'by': 'agent'}
        with LOCK:
            write_json(board_path(board['id']), board)
        r = RUNNER.start(board['id'], a['task'], [], None, a['mode'], intent=a.get('intent'))
        where = next((x['name'] for x in read_spaces()['spaces'] if x['id'] == space), None)
        return ('Started. %s working on the new board "%s"%s. I\'ll tell you when it\'s done or needs you.'
                % ('The team is' if a['mode'] == 'team' else 'An agent is', a['new_board'], ' in %s' % where if where else ''),
                {'open': {'board': board['id']}})
    r = RUNNER.start(a['board'], a['task'], a.get('sel') or [], None, a['mode'], intent=a.get('intent'))
    return ('Started. %s working on it here. I\'ll tell you when it\'s done or needs you.' % ('The team is' if a['mode'] == 'team' else 'An agent is'),
            {'started': {'run': r['id'], 'frame': r['frame']}})


def run_pending(a, why=None):
    if a.get('kind') == 'team':
        return run_team(a)
    """Do what was confirmed: build the cards from the apps themselves, put them in a frame. -> (what to say, extra reply fields)."""
    made, missed = [], 0
    for e in a['emails']:
        try:
            card = integrations.email_card(e)
        except (OSError, ValueError):
            missed += 1
            continue
        try:   # its PDFs and pictures go on the board themselves, in place of the link back to Gmail
            card['files'] = [f for f in (attachment_item(n, data) for n, data in integrations.email_attachments(e)) if f]
        except (OSError, ValueError):
            card['files'] = []
        made.append(card)
    for f in a['files']:
        try:
            made.append(integrations.file_card(f))
        except (OSError, ValueError):
            missed += 1
    for ev in a.get('events') or []:
        try:
            made.append(integrations.event_card(ev))
        except (OSError, ValueError):
            missed += 1
    made += [dict(c, kind='card') for c in a['cards']]
    if not made:
        return 'I couldn\'t get those from your apps just now, so nothing was added.', {}
    if a.get('new_board'):
        t = now_ms()
        board = {'id': secrets.token_hex(6), 'name': a['new_board'], 'created': t, 'updated': t, 'items': [], 'view': None,
                 'space': agent_space(), 'by': 'assistant'}
        with LOCK:
            write_json(board_path(board['id']), board)
        bid = board['id']
    else:
        bid = a['board']
    tag = 'voice-' + secrets.token_hex(4)

    def put(board):
        items = board.get('items') or []
        unit = agents.board_unit(board) if items else 1.0   # the size of what is already there, not the zoom (see board_unit)
        pad, gap, w, link_h, cols = 28 * unit, 18 * unit, 240 * unit, 70 * unit, min(3, len(made))
        x = (max(i['x'] + i['w'] for i in items) + 160 * unit) if items else 100.0
        y = min(i['y'] for i in items) if items else 100.0
        head = 0
        cells, col_y = [], [y + pad + head] * cols
        for k, c in enumerate(made):
            col = k % cols
            rows = sum(max(1, -(-len(l) // 30)) for l in c['text'].split('\n'))   # about 30 letters a line at this size
            nh = max(80, min(340, 34 + rows * 19)) * unit
            cx = x + pad + col * (w + gap)
            src = card_source(c)
            stamp = {'why': dict(why, source=src)} if why else {}
            if c.get('files'):   # an email with a PDF or picture: the attachment is what matters, no note with it
                nh = -6 * unit
            else:
                cells.append({'type': 'note', 'text': c['text'], 'lines': True, 'fs': 13 * unit, 'x': cx, 'y': col_y[col], 'w': w, 'h': nh, **stamp})
            for f in c.get('files') or []:   # an email's PDF or picture, under its note
                fh = w * min(max(f.pop('ratio', 1.29), .3), 2.5)
                cells.append(dict(f, x=cx, y=col_y[col] + nh + 6 * unit, w=w, h=fh,
                                  **({'why': dict(why, source=dict(src, title='%s, attached to the %s' % (f.get('label', 'A file'), src['title'][0].lower() + src['title'][1:])))} if why else {})))
                nh += 6 * unit + fh
            if c.get('url') and not c.get('files'):
                app = 'Gmail' if 'mail.google' in c['url'] else 'Calendar' if 'calendar' in c['url'] else 'Drive' if 'drive.google' in c['url'] or 'docs.google' in c['url'] else ''
                cells.append({**stamp, 'type': 'link', 'url': c['url'], 'unfurl': {'title': 'Open in ' + app if app else 'Open the link', 'pub': app or None},
                              'x': cx, 'y': col_y[col] + nh + 6 * unit, 'w': w, 'h': link_h})
                col_y[col] += nh + 6 * unit + link_h + gap
            else:
                col_y[col] += nh + gap
        fw, fh = 2 * pad + cols * w + (cols - 1) * gap, max(col_y) - y + pad - gap
        z = max([i.get('z', 1) for i in items] + [1])
        frame = {'id': 'f' + tag[6:], 'type': 'area', 'text': a['title'], 'color': '#f3f6fb', 'agent': tag, 'x': x, 'y': y, 'w': fw, 'h': fh, 'z': 1}
        if why:
            frame['why'] = dict(why, source={'app': 'Assistant', 'title': 'A frame for %s' % a['what']})
        board['items'] = [frame] + items + [dict(c, id='v' + secrets.token_hex(5), agent=tag, z=z + 1 + k) for k, c in enumerate(cells)]
        return frame['id']
    frame = edit_board(bid, put)
    live_send(bid, {'kind': 'saved', 'updated': now_ms()}, '')
    BRIEFER.touch(bid)
    n = len(made)
    pdfs = sum(len(c.get('files') or []) for c in made)
    if pdfs:
        n_word = '%d card%s with %d attachment%s' % (n, '' if n == 1 else 's', pdfs, '' if pdfs == 1 else 's')
    miss = ' %d couldn\'t be found in your apps.' % missed if missed else ''
    what = n_word if pdfs else '%d card%s' % (n, '' if n == 1 else 's')
    if a.get('new_board'):
        return 'Done: I made the board "%s" with %s.%s Opening it now.' % (a['new_board'], what, miss), {'open': {'board': bid}}
    return 'Done: I added %s to this board.%s' % (what, miss), {'show': [{'board': bid, 'item': frame}]}


MOVED = {}   # board -> {item: ([old places], new item, when)}: moves made here, kept when a page still showing an old place saves


class RulesLocked(ValueError):
    """A standing rule may be added, never rewritten or removed by the app (only the user edits one, on the page)."""


def rule_notes(items):
    """The standing rules on the rules board: every plain note that is not a built-in card, a mined report or an agent's."""
    return {i['id']: ' '.join(str(i.get('text') or '').split()) for i in items or []
            if isinstance(i, dict) and i.get('type') == 'note' and not i.get('builtin') and not i.get('mined') and not i.get('agent') and not i.get('chart')}


def rules_kept(before, after):
    """'' when every rule that was there is still there, word for word; else what would have been lost. Lessons the
    app learned are append-only: a rewrite drops what was learned without anyone seeing it go (the warning that came
    with Agentic Context Engineering, 2026-11-07)."""
    b, a = rule_notes(before), rule_notes(after)
    gone = [t for k, t in b.items() if k not in a]
    changed = [t for k, t in b.items() if k in a and a[k] != t]
    if not gone and not changed:
        return ''
    return 'it would %s' % ' and '.join(x for x in [('remove %d rule%s' % (len(gone), '' if len(gone) == 1 else 's')) if gone else '',
                                                    ('rewrite %d rule%s' % (len(changed), '' if len(changed) == 1 else 's')) if changed else ''] if x)


def edit_board(bid, fn):
    """Change a stored board under the lock (agents and the voice write this way); returns what fn returns. Items it
    moves are remembered for a while, and every open copy of the board is told to fetch it. On the rules board a
    change may add rules and move cards, never rewrite or remove a rule (RulesLocked; nothing is written)."""
    with LOCK:
        board = read_board(bid)
        before = {i['id']: (i['x'], i['y']) for i in board.get('items') or [] if 'x' in i}
        rules = json.loads(json.dumps(board.get('items') or [])) if bid == rules_board() else None
        out = fn(board)
        if rules is not None:
            lost = rules_kept(rules, board.get('items') or [])
            if lost:
                print('rules board: a change was refused, %s' % lost, flush=True)
                raise RulesLocked('Not done: the standing rules are append-only (%s). A rule can be added, never rewritten or removed by the app; only you change one, on the board.' % lost)
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


CLIP_URL = os.environ.get('CLIP_URL', 'http://100.121.91.119:8097').rstrip('/')   # the media service on the GX10 head (transcribe/serve.py there)
CLIPS_Q = queue.Queue()


def clip_fill(it):
    """A video card plays its board-sized copy when one has been made: 480p at about 0.7 Mbps with a still for the card, so
    the board streams a quarter of the original and draws the card without touching the video (the mini's line out is
    about 3 Mbps, 2026-11-07). The original stays behind Open and is what teams read."""
    if not isinstance(it, dict) or it.get('type') != 'media' or it.get('kind') != 'video' or it.get('clip') or not str(it.get('src') or '').startswith('api/files/'):
        return False
    stem = it['src'].rsplit('/', 1)[1].rsplit('.', 1)[0]
    meta = os.path.join(CLIPS, stem + '.json')
    if not (os.path.exists(os.path.join(CLIPS, stem + '.mp4')) and os.path.exists(os.path.join(CLIPS, stem + '.jpg')) and os.path.exists(meta)):
        return False
    try:
        with open(meta) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return False
    it['clip'], it['poster'] = 'api/clips/%s.mp4' % stem, 'api/clips/%s.jpg' % stem
    if m.get('w') and m.get('h') and not it.get('ar'):
        it['ar'] = m['h'] / m['w']
    return True


def clips_worker():
    """Makes the board-sized copy of each dropped video on the GX10, then writes it into every card that shows the video.
    At start, every video card without one is queued (videos dropped before this existed, or while the service was down)."""
    try:
        for b in live_boards():
            for it in read_board(b['id']).get('items') or []:
                if it.get('type') == 'media' and it.get('kind') == 'video' and not it.get('clip') and str(it.get('src') or '').startswith('api/files/'):
                    CLIPS_Q.put(it['src'].rsplit('/', 1)[1])
    except (OSError, ValueError, KeyError):
        pass
    done = set()
    while True:
        name = CLIPS_Q.get()
        stem = name.rsplit('.', 1)[0]
        if name in done or not os.path.exists(os.path.join(FILES, name)):
            continue
        if not os.path.exists(os.path.join(CLIPS, stem + '.json')):
            try:
                with open(os.path.join(FILES, name), 'rb') as f:
                    body = f.read()
                req = urllib.request.Request(CLIP_URL + '/clip', data=body, headers={'Content-Type': 'video/mp4', 'X-Name': name}, method='POST')
                with urllib.request.urlopen(req, timeout=7200) as resp:
                    out = json.loads(resp.read().decode())
                for part, ext in (('clip.mp4', 'mp4'), ('poster.jpg', 'jpg')):
                    with urllib.request.urlopen('%s/clips/%s/%s' % (CLIP_URL, out['token'], part), timeout=600) as resp:
                        data = resp.read()
                    with open(os.path.join(CLIPS, '%s.%s.tmp' % (stem, ext)), 'wb') as f:
                        f.write(data)
                    os.replace(os.path.join(CLIPS, '%s.%s.tmp' % (stem, ext)), os.path.join(CLIPS, '%s.%s' % (stem, ext)))
                write_json(os.path.join(CLIPS, stem + '.json'), {'w': out.get('w'), 'h': out.get('h'), 'duration_s': out.get('duration_s'), 'bytes': out.get('mp4_bytes'), 'made': now_ms()})
                print('clip made for %s: %d KB, %ss' % (name[:16], int(out.get('mp4_bytes') or 0) // 1024, out.get('seconds')), flush=True)
            except (OSError, ValueError, KeyError) as e:
                print('clip not made for %s: %s' % (name[:16], str(e)[:200]), flush=True)   # tried again at the next start
                continue
        done.add(name)
        try:   # every card that shows this video, on every board
            for b in live_boards():
                with LOCK:
                    board = read_board(b['id'])
                    if any(clip_fill(it) for it in board.get('items') or []):
                        board['updated'] = now_ms()
                        write_json(board_path(b['id']), board)
                    else:
                        continue
                live_send(b['id'], {'kind': 'saved', 'updated': board['updated']}, '')
        except (OSError, ValueError, KeyError):
            pass


threading.Thread(target=clips_worker, daemon=True).start()
BRIEFER = briefs.Briefer(DATA, READER, read_board, lambda: [b['id'] for b in sorted(live_boards(), key=lambda b: len(b.get('items') or []))])   # small boards first: they are ready in minutes

rules_board()   # made once, with the rules the user has already given
RUNNER = agents.Runner(DATA, type('Store', (), {'read_board': staticmethod(read_board), 'edit_board': staticmethod(edit_board)}),
                       READER, BRIEFER.load, BRIEFER.touch)
def builtins_frame():
    """A frame on the rules board that says what is built into the app for agent teams, written from the code's own
    settings each time the server starts, so it cannot drift from what the app really does. The user's rules stay theirs; these
    cards are marked builtin (agents do not read them as rules) and are written again on every start."""
    S = agents
    cards = [
        ('Reading the web', 'Nov 2', ['First: a plain read, under a second (the page as text)',
            'Then: Lightpanda, a light browser that runs the page\'s JavaScript (up to 20 seconds), when the plain read '
            'is turned away or comes back nearly empty',
            'Last: Chrome on the browser service, only to click, type, fill in a form, or show you the page (its screenshot goes on the board)',
            'Lightpanda: ' + ('on' if S.BROWSER_URL else 'off (no browser machine set up)')]),
        ('Blocked sites', 'Nov 2', ['Skipped after: 2 pages of one site turn both readers away',
            'For: the whole job; every teammate is told at once, and the Lead each round',
            'Never: getting around a block (search engine caches, web archives, reader services)',
            'Second tries: a part about a blocked site is not sent back']),
        ('Steps', 'Nov 2', ['Each teammate: %d steps' % S.PART_STEPS, 'The Lead: %d steps' % S.MAX_STEPS,
            'Free: reading the board, and a page a teammate already read in this job (up to %d per teammate)' % S.FREE_TURNS]),
        ('Sticky notes', 'Nov 2', ['Hold: one short thought, up to %d characters (links not counted)' % S.STICKY_MAX,
            'Shape: a fixed square or rectangle; it never grows or scrolls, words that do not fit fade out',
            'Longer from an agent: goes on a card instead, whatever the agent asked for',
            'Font: handwritten']),
        ('Cards', 'Nov 2', ['For: a finding with details (title, "Label: value" rows, its source link)',
            'Also: the intent, done-when, check results and disagreements a team puts on the board']),
        ('Org chart', 'Nov 1', ['Where: above the team\'s work, when a team is set up',
            'Shows: each teammate, why they are on the team, when their part is done, and how it is going']),
        ('After each job', 'Nov 2', ['A card: What got in the way (blocked sites, pages read with Lightpanda, free steps)']),
        ('Quotes', 'Nov 4', ['Quote marks mean word for word: a note, card or text whose quote is on no page or board item that agent read itself is refused, and the quote is named',
            'Counts as read: pages, search results and board items the agent opened with its own tools',
            'Never counts: the brief it was given, or what a teammate handed back (an invented quote once travelled that way)',
            'Shortened quotes: a quote with ... in it is checked piece by piece',
            'Not checked: anything without quote marks; the agent can still describe a page in its own words']),
        ('Watchdog', 'Nov 3', ['Looks: every minute, at every team',
            'Stuck: no progress for %d minutes gets one push to you and a line in the team\'s frame' % router.STALL_MIN,
            'Stopped early: a team that ends unfinished or with an error is started again once, from its frame',
            'Never: a team you stopped, a teammate\'s part (the Lead retries those), or work that ended before a restart']),
        ('Cut results', 'Nov 7', ['Older tool results are cut to %d characters so a long run fits; the cut ends with a [CUT: ...] marker' % S.OLD_RESULT_CHARS,
            'Never cut: a read of one of the selected items (the source of the job)',
            'Refused: a note, card, text, file or summary that pastes from a cut result; the agent is told to read it again first']),
        ('Looking before acting', 'Nov 7', ['A Lead with a team: after 5 board looks it is told to hand the work out',
            'A Lead alone: after 8 looks it is told to write the result now; after 12 it counts as stuck',
            'Every part names the selected items it is about, so a look-alike card is not used instead']),
        ('Notes from earlier teams', 'Nov 7', ['Each note says when it applies; a new job gets the last 3 on its board and the 2 that fit best',
            'A note is counted for when the job it went to ended done, against when it fell short',
            'Retired (never given out again): 3 misses and more misses than hits, or 60 days without a hit']),
        ('Failure patterns', 'Nov 7', ['Nightly at 3:45: reads the last 14 days of jobs and their checks, notes and questions',
            'Looks for: jobs that stopped, failed or fell short, parts that did not pass their check, a job asked again within the hour, '
            'questions marked bad, and one-agent jobs over 30 model calls',
            'Writes: one Failure patterns card next to this frame, with each recurring problem, how often, which jobs, and what a fix would touch',
            'Changes nothing by itself: the fix is still a change to a prompt, a rule or a tool, tried on the recorded questions first']),
        ('Putting results together', 'Nov 7', ['A Lead with its parts back: after 8 board looks it is told to write the final answer from them; after 12 it counts as stuck',
            'A Lead the watchdog starts again writes from the rounds it already ran; it does not hand the finished parts out again',
            'A part that reads a teammate\'s frame is handed out in the round after that teammate, never the same one',
            'A part that ran out of steps but passed its done-when check counts as done, not "didn\'t finish"']),
        ('Building on a finished team', 'Nov 7', ['The planner sees what each finished team on the board found, so "now execute what they worked out" carries it',
            'A job that refers to the team ("they", "the intention") with nothing selected works on the items that team was given']),
        ('Status by voice', 'Nov 7', ['"Where are we at", "status update", "how is it going" while a team works: answered from the runs themselves, no model, never a team offer',
            '"Show me the frame" after a team finished: that team\'s frame',
            'A finished team\'s news never reads a web address aloud: it says "a link"',
            'Words that make no sense (speech heard wrong): "I didn\'t catch that", not a board search']),
        ('Demos on a phone', 'Nov 7', ['Every hover or mouse interaction in a demo must also work with touch',
            'A replaced demo keeps every control the earlier one had',
            'A finished team\'s notification opens the board at its frame']),
        ('Proposed rules', 'Nov 7', ['After each night\'s mining: a pattern in 2 or more jobs gets ONE proposed rule, in plain words, tried on every team at once',
            'A pattern on the planner\'s side gets a line for the Assistant instead, kept only if the recorded questions still replay right',
            'Judged after 6 more jobs: recurring at half its old rate or less = kept as a rule note; otherwise dropped. At most 2 new a night',
            'Listed in the Proposed rules card next to this frame; delete a line in proposals.json or the note to stop one']),
        ('Cost in view', 'Nov 7', ['Every read an agent makes comes back with "Step N of M", so it can wrap up before the cap does']),
        ('Rules are append-only', 'Nov 7', ['The app can add a standing rule to this board and move its cards; it can never rewrite or remove one: such a change is refused and nothing is written',
            'Only you change a rule, on the board. A rewrite would drop a lesson without anyone seeing it go (the warning from Agentic Context Engineering)']),
        ('Not doing', 'Nov 7', ['The intent card names the one or two things an eager team would do that you did not ask for; the Lead and every part carry the same line',
            'Said before the yes when there is one: "It hands back X, and does not Y"']),
        ('One question first', 'Nov 7', ['When your words could mean two different jobs, the Assistant asks one question with the choices instead of guessing; it never asks twice for one job']),
        ('Checked at the source', 'Nov 7', ['A part that cites a web page is checked against that page, read again: a figure, name or quote the page does not hold fails the check and goes back',
            'A part that built a demo has it opened once in the browser; a demo that does not open cleanly fails the check']),
        ('Advice when stuck', 'Nov 7', ['When a part fails its check and goes back for the missing piece, or the Lead fails the final check, a stronger model (the Advisor in Settings) is asked once what to do differently; its advice rides along with the retry',
            'Off until an Advisor is picked in Settings; the step "Asked the advisor" shows where it came in']),
        ('Notes that proved themselves', 'Nov 7', ['A team note counted for 3 times with at most a third as many misses becomes a Lead skill on every board',
            'The note stays; the skill no longer needs to fit the task by words']),
        ('Router', 'Nov 3', ['Reads: new inbox email and calendar changes in the next 3 days, every 3 minutes',
            'Drops first: no-reply senders, promotions and social mail, anything already seen',
            'Then: your own model sorts the rest by the rules note on the Incoming board: ignore, note, or suggest a task',
            'Record-only: it never starts a task, replies or sends; a suggested task has Start this task in its card\'s menu',
            'Email goes to: the models on your own machines only, never an outside provider']),
    ]
    try:
        bid = rules_board()

        def put(board):
            items = [i for i in board.get('items') or [] if not i.get('builtin')]
            x0 = max([i['x'] + i.get('w', 0) for i in items if 'x' in i and not i.get('mined')] + [0]) + 120   # the miner's card sits right of the frame
            y0 = min([i['y'] for i in items if 'y' in i] + [40])
            w, gap, pad, top = 320, 24, 28, 70
            heights = [44 + 19 * sum(1 + len(l) // 30 for l in lines) for _, _, lines in cards]   # rows wrap in their value column
            cols = [[], []]
            for k in range(len(cards)):
                cols[0 if sum(heights[j] for j in cols[0]) <= sum(heights[j] for j in cols[1]) else 1].append(k)
            out = [{'id': 'builtin-frame', 'type': 'area', 'builtin': 1, 'color': '#f3f6fb', 'x': x0, 'y': y0, 'z': 0,
                    'w': 2 * w + gap + 2 * pad, 'h': top + pad + max(sum(heights[j] + gap for j in c) for c in cols),
                    'text': 'Built into the app (ask Claude to change these)'}]
            for c, col in enumerate(cols):
                y = y0 + top
                for k in col:
                    title, since, lines = cards[k]
                    out.append({'id': 'builtin-%d' % k, 'type': 'note', 'builtin': 1, 'card': 1, 'grow': 'h', 'color': '#eef4ff', 'fs': 13,
                                'x': x0 + pad + c * (w + gap), 'y': y, 'w': w, 'h': heights[k], 'z': 2,
                                'text': '%s\n%s\nSince: %s' % (title, '\n'.join(lines), since)})
                    y += heights[k] + gap
            board['items'] = items + out
        edit_board(bid, put)
    except (OSError, ValueError, KeyError) as e:
        print('built-in rules frame not written:', e)


threading.Thread(target=builtins_frame, daemon=True).start()   # what the app does for agent teams, on the rules board
threading.Thread(target=RUNNER.backfill_charts, daemon=True).start()   # older teams get their org chart, once
threading.Thread(target=RUNNER.backfill_cards, daemon=True).start()   # older teams' long notes become cards, once

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


def boardshot(bid, mtime, dark=False):
    """Photographs one board's preview (index.html?shot=<id>, a 320x160 card at 2x, about 15 KB), light, or dark for dark
    mode (<id>-dark.jpg). The picture's file time is the board file's time it showed, so a board saved while it was taken
    is taken again."""
    name = bid + ('-dark' if dark else '')
    png, jpg = os.path.join(BOARDSHOTS, name + '.png'), os.path.join(BOARDSHOTS, name + '.jpg')
    tmp = jpg + '.' + secrets.token_hex(4) + '.jpg'
    BOARDSHOT_SEEN[:] = [bid, False]
    try:
        subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run', '--no-default-browser-check',
                        '--user-data-dir=' + os.path.expanduser('~/Library/Caches/clewbert-boardshots'),
                        '--window-size=320,160', '--force-device-scale-factor=2', '--virtual-time-budget=15000',
                        '--screenshot=' + png, 'http://127.0.0.1:%d/?shot=%s%s' % (PORT, bid, '&dark=1' if dark else '')], capture_output=True, timeout=90)
        subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '75', png, '--out', tmp], capture_output=True, timeout=60)
        if not BOARDSHOT_SEEN[1]:
            raise OSError('the page did not get the board')   # a blank picture is not kept
        os.utime(tmp, (mtime, mtime))
        os.replace(tmp, jpg)
    except (OSError, subprocess.SubprocessError):
        BOARDSHOT_FAILED[name] = mtime   # not tried again until the board changes
    finally:
        for f in (png, tmp):
            if os.path.exists(f):
                os.remove(f)


COVER_W, COVER_H = 1456, 816   # Substack's post cover size


def covershot(bid, ids):
    """A post's cover: the chosen items on the board (the whole board when none), photographed off the page at Substack's
    cover size, dark like the app. JPEG bytes, or None when the page did not get the board."""
    token = secrets.token_hex(6)
    png = os.path.join(BOARDSHOTS, 'cover-%s.png' % token)
    jpg = png[:-4] + '.jpg'
    COVER_SEEN[token] = False
    try:
        subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run', '--no-default-browser-check',
                        '--user-data-dir=' + os.path.expanduser('~/Library/Caches/context-canvas-boardshots'),
                        '--window-size=%d,%d' % (COVER_W, COVER_H), '--virtual-time-budget=20000', '--screenshot=' + png,
                        'http://127.0.0.1:%d/?shot=%s&dark=1&cover=1&seen=%s&items=%s' % (PORT, bid, token, ','.join(i for i in ids if ITEM_ID_OK.match(i)))],
                       capture_output=True, timeout=120)
        subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '85', png, '--out', jpg], capture_output=True, timeout=60)
        if not COVER_SEEN.get(token):
            raise OSError('the page did not get the board')
        with open(jpg, 'rb') as f:
            return f.read()
    except (OSError, subprocess.SubprocessError) as e:
        print('%s cover: no screenshot of %s (%s)' % (time.strftime('%H:%M:%S'), bid, e), flush=True)
        return None
    finally:
        COVER_SEEN.pop(token, None)
        for f in (png, jpg):
            if os.path.exists(f):
                os.remove(f)


ITEM_ID_OK = re.compile(r'^[0-9a-zA-Z_-]{1,24}$')
COVER_SEEN = {}   # cover shot token -> whether its page got the board


def cover_for(bid, ids, title, body):
    """The cover picture of a post made from this board (integrations.COVER): a picture among the chosen items; else the
    model's pick among the board's pictures when one is about the post; else a cover made from a line of the post over a
    nature photo; else a screenshot of the chosen items. Returns (picture bytes or None, what it is in words)."""
    try:
        items = {i['id']: i for i in read_board(bid).get('items') or [] if i.get('id')}
    except (OSError, ValueError, KeyError) as e:
        print('%s cover: no board %s (%s)' % (time.strftime('%H:%M:%S'), bid, e), flush=True)
        return None, ''

    def picture(it):   # a picture item's bytes, or None (PDFs and missing files are not covers)
        name = talk.image_name(it.get('src')) if it.get('type') == 'img' else None
        if not name or name.endswith('.pdf'):
            return None
        try:
            with open(os.path.join(IMAGES, name), 'rb') as f:
                return f.read()
        except OSError:
            return None

    def said(it):
        return ' '.join((READER.words(talk.image_name(it.get('src')) or '') or it.get('label') or 'a picture with no words in it').split())

    chosen = [items[i] for i in ids if i in items]
    pics = [it for it in chosen if picture(it)]
    if len(pics) == 1:
        return picture(pics[0]), 'the picture on the board (%s)' % said(pics[0])[:60]
    cands = pics or [it for it in items.values() if picture(it)]
    if cands:
        try:
            pick = talk.pick_cover(title, body, [{'id': it['id'], 'words': said(it)[:300]} for it in cands[:20]], len(chosen))
        except (OSError, ValueError):
            pick = ''
        if pick in items and picture(items[pick]):
            return picture(items[pick]), 'the picture on the board (%s)' % said(items[pick])[:60]
    made, note = made_cover(title, body)   # no picture of its own: a hook line over a nature photo, never the newsletter logo (2026-11-06)
    if made:
        return made, note
    shot = covershot(bid, [it['id'] for it in chosen])
    if shot:
        return shot, ('a screenshot of the %d chosen item%s' % (len(chosen), '' if len(chosen) == 1 else 's')) if chosen else 'a screenshot of the whole board'
    return None, ''


COVERS = os.path.join(DATA, 'covers')   # the cover-photo pool: tools/covers/fill_pool.py fills it (public-domain nature photos, vetted)


def made_cover(title, body):
    """A cover made for a post that has no picture of its own: a hook line from the post, set large in a white serif over
    a nature photo from the pool, darkened a little, the newsletter's name small at the top left. Photographed with
    headless Chrome at Substack's cover size. (2026-11-06: the user's face must never stand in for a post.)"""
    try:
        with open(os.path.join(COVERS, 'pool.json')) as f:
            pool = json.load(f)
    except (OSError, ValueError):
        return None, ''
    photos = [v for v in (pool.get('accepted') or {}).values() if os.path.exists(os.path.join(COVERS, 'pool', v.get('file', '')))]
    if not photos:
        return None, ''
    last_path = os.path.join(COVERS, 'last.txt')
    try:
        with open(last_path) as f:
            last = f.read().strip()
    except OSError:
        last = ''
    photo = secrets.choice([v for v in photos if v['file'] != last] or photos)
    hook = talk.hook_line(title, body) or ' '.join(str(title).split()[:12])
    label = (integrations.load().get('substack') or {}).get('publication') or ''
    size = 96 if len(hook) <= 40 else 82 if len(hook) <= 70 else 66
    page = ('<!doctype html><meta charset="utf-8"><style>html,body{margin:0;width:%dpx;height:%dpx;overflow:hidden}'
            'body{background:#111 url("file://%s") center/cover no-repeat;position:relative;font-family:Baskerville,"Hoefler Text",Georgia,serif;color:#fff}'
            'body:after{content:"";position:absolute;inset:0;background:rgba(0,0,0,.42)}'
            '.l{position:absolute;left:44px;top:36px;z-index:2;font:500 20px/1 -apple-system,"Helvetica Neue",Arial,sans-serif;letter-spacing:.02em;opacity:.85}'
            'h1{position:absolute;inset:0;z-index:2;margin:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:0 150px;'
            'font-weight:400;font-size:%dpx;line-height:1.08;text-wrap:balance;text-shadow:0 2px 24px rgba(0,0,0,.35)}</style>'
            '<span class="l">%s</span><h1>%s</h1>') % (COVER_W, COVER_H, html.escape(os.path.join(COVERS, 'pool', photo['file'])), size,
                                                        html.escape(label), html.escape(hook))
    token = secrets.token_hex(6)
    src = os.path.join(BOARDSHOTS, 'made-%s.html' % token)
    png, jpg = src[:-5] + '.png', src[:-5] + '.jpg'
    try:
        with open(src, 'w') as f:
            f.write(page)
        subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run', '--no-default-browser-check',
                        '--user-data-dir=' + os.path.expanduser('~/Library/Caches/context-canvas-boardshots'),
                        '--window-size=%d,%d' % (COVER_W, COVER_H), '--virtual-time-budget=5000', '--screenshot=' + png, 'file://' + src],
                       capture_output=True, timeout=120)
        subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '88', png, '--out', jpg], capture_output=True, timeout=60)
        with open(jpg, 'rb') as f:
            data = f.read()
        with open(last_path, 'w') as f:
            f.write(photo['file'])
        return data, 'made from your line \u201c%s\u201d over a nature photo' % hook
    except (OSError, subprocess.SubprocessError) as e:
        print('%s cover: could not make one (%s)' % (time.strftime('%H:%M:%S'), e), flush=True)
        return None, ''
    finally:
        for f in (src, png, jpg):
            if os.path.exists(f):
                os.remove(f)


def picture_of(bid, item_id):
    """A picture card's bytes and caption, for inside a post (integrations.PICTURE); None when it is not a picture here."""
    try:
        it = next((i for i in read_board(bid).get('items') or [] if i.get('id') == item_id), None)
    except (OSError, ValueError, KeyError):
        return None
    name = talk.image_name(it.get('src')) if it and it.get('type') == 'img' else None
    if not name or name.endswith('.pdf'):
        return None
    try:
        with open(os.path.join(IMAGES, name), 'rb') as f:
            return f.read(), ' '.join(str(it.get('label') or '').split())[:200]
    except OSError:
        return None


integrations.COVER = cover_for
integrations.PICTURE = picture_of

# The trained voices come out of the engine at different loudness: measured 2026-11-03 over 4 lines, Gen Jones -28.4 dBFS,
# Kathryn Louise -27.9, Barry Eads -26.7, William Manire -25.8, Pete Williams -24.5, Steve Andersen -18.5. Gen Jones was
# barely heard on the user's phone. Each is raised to about -20 dBFS here, as the speech streams through.
VOICE_GAIN = {'gen-jones': 2.6, 'kathryn-louise': 2.5, 'barry-eads': 2.2, 'william-manire': 1.9, 'pete-williams': 1.7}


def louder(data, gain):
    """Raises 16-bit speech by gain; the loudest peaks are rounded off softly instead of clipping. -> (samples, a split byte to carry on)."""
    n = len(data) // 2 * 2
    a = array.array('h', data[:n])
    knee, room = 24000, 32767 - 24000
    for i, x in enumerate(a):
        y = x * gain
        if y > knee:
            y = knee + room * math.tanh((y - knee) / room)
        elif y < -knee:
            y = -knee - room * math.tanh((-y - knee) / room)
        a[i] = int(y)
    return a.tobytes(), data[n:]


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
            for dark in (False, True):   # the light picture first, then the dark one for dark mode
                name = bid + ('-dark' if dark else '')
                try:
                    shot = os.stat(os.path.join(BOARDSHOTS, name + '.jpg')).st_mtime
                except OSError:
                    shot = 0
                if shot < mtime and time.time() - mtime > 15 and BOARDSHOT_FAILED.get(name) != mtime:
                    todo.append((mtime, not dark, bid, dark))
        for mtime, _, bid, dark in sorted(todo, reverse=True)[:20]:   # newest boards first, light before dark
            boardshot(bid, mtime, dark)
        time.sleep(1 if todo else 20)


threading.Thread(target=boardshot_loop, daemon=True).start()
PUSH = push.Push(DATA, named_news) if push else None
NO_PUSH = {'message': 'Notifications need one Python package on the computer that runs Clewbert: pip3 install cryptography, then restart Clewbert.'}


def incoming_board():
    """The router's board: what came into email and the calendar, sorted by the rules note the user edits (made on first use)."""
    path = os.path.join(DATA, 'router', 'board')
    try:
        with open(path) as f:
            bid = f.read().strip()
        if os.path.exists(board_path(bid)):
            return bid
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    t = now_ms()
    bid = secrets.token_hex(6)
    items = [{'id': 'routertitle', 'type': 'note', 'color': 'none', 'grow': 'h', 'fs': 15, 'x': 40, 'y': 40, 'w': 1700, 'h': 60, 'z': 1,
              'text': 'Incoming: the router reads new email and calendar events every few minutes and sorts them with the rules on '
                      'the left. It only records; it never starts, replies or sends.'},
             {'id': 'routerrules', 'type': 'note', 'text': router.FIRST_RULES, 'x': 40, 'y': 140, 'w': 300, 'h': 300, 'z': 2, 'fs': 12}]
    with LOCK:
        write_json(board_path(bid), {'id': bid, 'name': 'Incoming', 'created': t, 'updated': t, 'items': items, 'view': None, 'space': None, 'by': 'agent'})
        with open(path, 'w') as f:
            f.write(bid)
    return bid


class _NoPush:   # notifications are optional here: the watchdog and router still work, they only skip the phone
    def send(self, *a, **k):
        return 0


WATCHDOG = router.Watchdog(RUNNER, PUSH or _NoPush())


def put_away_agent_boards():
    """Once an hour: a board in the Agent boards space, untouched for 30 days, is archived (never deleted; it opens from
    Archived and stays in the backup). A board dragged out of that space is kept. Incoming and the rules board are never put away (2026-11-05)."""
    while True:
        try:
            keep = {ROUTER.board, rules_board()}
            cut = time.time() * 1000 - 30 * 86400 * 1000
            home = agent_space()
            for b in all_boards():
                if b.get('space') == home and not b.get('archived') and b['id'] not in keep and (b.get('updated') or b.get('created') or 0) < cut:
                    with LOCK:
                        board = read_board(b['id'])
                        board['archived'] = True
                        write_json(board_path(b['id']), board)
                    print('%s put away the agent board "%s" (%s): untouched for 30 days' % (time.strftime('%Y-%m-%d %H:%M:%S'), b.get('name', ''), b['id']), flush=True)
        except Exception as e:   # never takes the server down
            print('%s put-away pass failed: %s: %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), type(e).__name__, e), flush=True)
        time.sleep(3600)

ROUTER = router.Router(DATA, incoming_board(), read_board, edit_board, RUNNER, PUSH or _NoPush())
threading.Thread(target=put_away_agent_boards, daemon=True).start()


def agent_library():
    """Every agent in the app and what it can do, for the top of the Agents screen. Written from the code's own settings
    and tool lists on each call, so it cannot drift from what the agents really do."""
    def model(job):
        c = models.choice(job)
        return ('%s on %s (an outside provider)' % (c[1], c[0])) if c else 'your own machines'
    names = lambda tools: {t['function']['name'] for t in tools}
    uses = {'search_board': 'reads the board', 'add_text': 'writes in its frame', 'ask_user': 'asks you', 'assign': 'hands parts to teammates',
            'web_search': 'searches and reads the web', 'make_demo': 'builds a demo page and tries it like a person',
            'add_file': 'makes a Markdown file on the board'}
    google = bool((integrations.load().get('google') or {}).get('refresh_token'))
    rs = ROUTER.state
    c = rs.get('counts') or {}
    today = c.get('day') == time.strftime('%Y-%m-%d')
    briefs = len([f for f in os.listdir(os.path.join(DATA, 'briefs')) if f.endswith('.json')]) if os.path.isdir(os.path.join(DATA, 'briefs')) else 0
    always = [
        {'name': 'Assistant', 'color': '#2563eb', 'what': 'The voice you talk to. Answers about your boards, email, calendar and Drive, '
         'moves and adds things on a board, and starts agent teams when you ask.', 'never': 'Sends, books or deletes anything without your yes.',
         'status': 'Runs when you talk to it, on %s.' % model('voice')},
        {'name': 'Router', 'color': '#f59e0b', 'what': 'Every 3 minutes reads new inbox email and calendar changes in the next 3 days, and '
         'sorts them by your rules note: ignore, note it, or suggest a task.', 'never': 'Starts a task, replies or sends. Email never goes '
         'to an outside provider.', 'status': ('' if rs.get('last') else 'Has not looked yet.') if google
         else 'Waiting: Google is not connected (Settings).', 'last': rs.get('last'),
         'today': ('Today: %d noted, %d suggested tasks, %d ignored.' % (c.get('note', 0), c.get('task', 0), c.get('ignore', 0) + c.get('skipped', 0))) if today else '',
         'link': {'board': ROUTER.board, 'label': 'Open the Incoming board'}},
        {'name': 'Watchdog', 'color': '#dc2626', 'what': 'Every minute looks at every team. A team with no progress for %d minutes gets one '
         'push to you; a team that stops unfinished or with an error is started again once, from its frame.' % router.STALL_MIN,
         'never': 'Touches a team you stopped, or work that ended before a restart.', 'last': WATCHDOG.last,
         'status': '' if WATCHDOG.last else 'Starting.',
         'today': 'Since the server started: %d stuck teams flagged, %d restarted.' % (WATCHDOG.flagged, WATCHDOG.retried)},
        {'name': 'Board brief writer', 'color': '#0f766e', 'what': 'In the background reads each whole board, part by part, and writes '
         'one brief of it, so the Assistant can answer about a board of any size.', 'never': 'Changes a board. It pauses while you are asking.',
         'status': '%d boards have a brief, on %s.' % (briefs, model('voice'))},
        {'name': 'Plan critic', 'color': '#b45309', 'what': 'Reads the team a Lead designed for a job before anyone starts, and hands back the '
         'smallest plan that still delivers the whole intent: members cut or merged, each with a reason, or the Lead alone.',
         'never': 'Changes the task, drops anything the team must hand back, weakens a done-when line, or adds members.',
         'status': ('On: every new team passes through it, on %s.' % model('agents')) if models.flag('critic') else 'Off (Settings): teams run as designed.'},
    ]
    lead = names(agents.LEAD_TOOLS)
    worker = names(agents.TOOLS)
    team = []
    for key, m in agents.STARTERS.items():
        if key == 'lead':
            u = [uses[n] for n in ('search_board', 'add_text', 'add_file', 'ask_user', 'assign') if n in lead | {'assign'}]
        else:
            u = [uses[n] for n in ('search_board', 'add_text', 'add_file', 'web_search', 'ask_user') if n in worker]
            if agents.BROWSER_URL:
                u.append('clicks, types and fills in forms in the agents\' browser')
            if key.startswith('engineer'):
                u.append('reads and edits code on the team\'s own branch, then checks and commits it')
            if key == 'developer' and 'make_demo' in worker:
                u.append(uses['make_demo'])
            if google:
                u.append('your connected Google apps (sending needs your yes)')
        role = re.split(r'(?<=\.)\s', m.get('role', ''), maxsplit=1)[0]
        team.append({'name': m['name'], 'color': m.get('color', '#475569'), 'what': role, 'uses': u,
                     'model': 'the code model' if key in agents.STRONG_ROLES else model('agents')})
    stats = agent_work()
    for a in always:
        a['key'] = re.sub(r'[^a-z]+', '-', a['name'].lower()).strip('-')
    for a, key in zip(team, agents.STARTERS):
        a['key'] = key
        w = stats.get(key) or []
        checked = [x for x in w if x['check']]
        a['jobs'] = len(w)
        saved = sum(max(0, x['human'] - x['ran']) for x in w) / 60
        a['stat'] = ('%d jobs' % len(w) + (' · %d%% passed' % round(100 * sum(x['check']['pass'] for x in checked) / len(checked)) if checked else '')
                     + (' · ~%s h saved' % (round(saved) if saved >= 10 else round(saved, 1)) if saved >= .1 else '')) if w else 'No jobs yet'
    team.sort(key=lambda a: -a['jobs'])   # the ones called on most first
    for k, a in enumerate(team[:3]):
        if a['jobs']:
            a['badge'] = 'Most called on' if k == 0 else 'Top 3'
    return {'always': always, 'team': team}


# What a person would take for each thing an agent did, in minutes: a rough, stated estimate, shown on the profile page.
# (step-text pattern, skill, what one is called, minutes for a person)
WORK_RATES = [
    (r'^(Read|Opened) https?:', 'Web research', 'pages read', 4),
    (r'^Read the (source|code) of|^Searched the code of', 'Web research', 'page sources read', 6),
    (r'^Searched the web for', 'Web research', 'web searches', 2),
    (r'^(Searched the board|Read (a|the) )', 'Reading the board', 'board items read', 0.5),
    (r'^Added (a note|text|a heading|a card)', 'Writing on the board', 'notes and cards written', 3),
    (r'^Added a link', 'Writing on the board', 'links added', 1),
    (r'^Grouped (\d+)', 'Organizing', 'items grouped', 0.25),
    (r'^(Made|Updated) the demo', 'Building demos', 'demo versions built', 20),
    (r'^Tried the demo', 'Building demos', 'demo checks', 1),
    (r'^Used (gmail|calendar|drive)', 'Email and calendar', 'lookups', 2),
    (r'^(Clicked|Typed|Scrolled|Filled)', 'Using a browser', 'clicks and keystrokes', 0.5),
    (r'^Handed out (\d+)', 'Leading', 'parts handed out', 5),
    (r'^Checked against its done', 'Checking', 'checks', 2)]


def work_of(steps):
    """What one run did, counted from its steps: {(skill, name): count}, and the minutes a person would have taken."""
    got, mins = {}, 0
    for st in steps:
        t = st.get('text') or ''
        for pat, skill, name, rate in WORK_RATES:
            m = re.match(pat, t)
            if m:
                n = int(m.group(1)) if m.groups() and str(m.group(1)).isdigit() else 1
                got[(skill, name)] = got.get((skill, name), 0) + n
                mins += n * rate
                break
    return got, mins


def agent_work():
    """Every part each standard role did, from the saved tasks: role key -> [work], newest first. A teammate a Lead made for
    one job (its own name, an id like researcher-2) counts toward its role, with a "made for" line."""
    runs = RUNNER.view()
    by = {r['id']: r for r in runs}
    names = board_names()
    out = {}
    for r in runs:
        if r.get('member'):
            key = re.sub(r'-\d+$', '', str(r['member'].get('id') or ''))
        elif not r.get('parent') and r.get('crew'):
            key = 'lead'   # a team's top task is its Lead's work
        else:
            continue
        key = key if key in agents.STARTERS else agents.KIND_BASE.get(key, key)
        if key not in agents.STARTERS:
            continue
        root = r
        while root.get('parent') and root['parent'] in by:
            root = by[root['parent']]
        m = next((c for c in root.get('crew') or [] if c.get('id') == (r.get('member') or {}).get('id')), None) if r.get('member') else None
        name = (r.get('member') or {}).get('name') or agents.STARTERS[key]['name']
        with RUNNER.lock:
            full = RUNNER.runs.get(r['id']) or {}
            steps = list(full.get('steps') or [])
        did, mins = work_of(steps)
        ran = max(0, ((r.get('ended') or (steps[-1]['t'] if steps else r['created'])) - r['created']) / 60000)
        out.setdefault(key, []).append({'id': root['id'], 'task': r['task'], 'board': r['board'], 'boardName': names.get(r['board'], ''),
            'start': r['created'], 'ran': round(ran, 1), 'human': round(mins), 'did': [[k[0], k[1], v] for k, v in did.items()],
            'summary': ' '.join(str(full.get('summary') or '').split())[:400], 'team': len(root.get('crew') or []),
            'status': r['status'], 'when': r.get('ended') or r['created'], 'check': r.get('check'),
            'madeFor': name if name != agents.STARTERS[key]['name'] else None, 'why': (m or {}).get('why'), 'job': root['task']})
    for w in out.values():
        w.sort(key=lambda x: -x['when'])
    return out


AG_WORDS = {'done': 'done', 'partial': 'done, with gaps', 'unfinished': 'stopped before finishing', 'failed': 'stopped by an error',
            'stopped': 'stopped by the user', 'working': 'still working', 'queued': 'waiting to start', 'waiting': 'waiting on the user', 'team': 'leading a team'}

BUILTIN_WHY = {
    'assistant': 'Built first, with the app: so you can ask about your boards, email and calendar by voice and get things done hands-free.',
    'router': 'Added on Nov 3, 2026, after DeepSeek\'s Super-agent article: so new email and calendar changes are sorted for you, '
              'with code rules first and the model second, and nothing is done without you while it is new.',
    'watchdog': 'Added on Nov 3, 2026, after DeepSeek\'s Super-agent article: teams were left showing "Stopped: didn\'t finish" or '
                'went quiet with nobody noticing.',
    'board-brief-writer': 'Added so a question can be answered about a board of any size: a question only sees about 3,000 words of a board at once.',
    'plan-critic': 'Added on Nov 5, 2026, after a five-member team spent 68 model calls and 121 tool calls on a job that was one read and one '
                   'write (copy a card to a file): the team designer matched the job to its role templates, not to the size of the intent. '
                   'The critic is one agent with a switch, so it can be tested on the same job with it on and off, and removed without touching the rest.'}


# what each kind of question to the Assistant would take a person, in minutes: (label, skill, what one is called, minutes)
ASK_RATES = {None: ('Answered about this board', 'Answering about a board', 'questions about a board', 3),
             'boards': ('Searched across your boards', 'Searching your boards', 'searches across boards', 5),
             'outside': ('Looked in your email, calendar or Drive', 'Email, calendar and Drive', 'lookups', 5),
             'team': ('Started an agent team', 'Starting teams', 'teams started', 2),
             'confirm': ('Did what you confirmed', 'Doing what you asked', 'confirmed actions', 1),
             'chat': ('Talked with you', 'Conversation', 'replies', 0)}


class Resume:
    """The Assistant's Experience entries, written by the local model like an executive assistant's LinkedIn: per board a
    short role title and 2-4 accomplishment bullets, from the real questions and answers only. Kept in
    DATA/library/assistant.json and written again only when that board's work changes; plain counts until then."""
    def __init__(self):
        self.path = os.path.join(DATA, 'library', 'assistant.json')
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            with open(self.path) as f:
                self.done = json.load(f)
        except (OSError, ValueError):
            self.done = {}
        self.want, self.lock, self.wake = {}, threading.Lock(), threading.Event()
        threading.Thread(target=self.loop, daemon=True).start()

    def get(self, who, name, lines, fallback):
        """The entry for one agent (who: its key and how to describe it) on one project; counts (fallback) until written."""
        k = hashlib.sha1(json.dumps(['v3', who] + lines).encode()).hexdigest()[:16]   # v3: one writer for every agent
        slot = who[0] + '|' + name
        got = self.done.get(slot)
        if got and got.get('key') == k:
            return {'title': got['title'], 'bullets': got['bullets']}
        with self.lock:
            self.want[slot] = (k, who, name, lines)
        self.wake.set()
        return {'title': (got or {}).get('title') or '', 'bullets': (got or {}).get('bullets') or fallback}

    def loop(self):
        while True:
            self.wake.wait(60)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.want:
                        break
                    slot, (k, who, name, lines) = self.want.popitem()
                try:
                    self.done[slot] = dict(self.write(who, name, lines), key=k)
                    write_json(self.path, self.done)
                except Exception as e:   # the model was away: the counts stay, it tries again when the page asks next
                    print('resume:', e, flush=True)
                    time.sleep(30)

    def write(self, who, name, lines):
        work = '\n'.join(lines[:40])
        prompt = ('Write the Experience entry for "%s" on the LinkedIn profile of %s. Below is the real work done there. Write it '
                  'the way a strong professional in that role describes their work: what they took care of and what it achieved '
                  'for the user, not a list of tasks. Only claim results that passed their check or that the work says were done; leave '
                  'out what failed. Group similar work and lead with the most useful result. Past tense, plain words, no "I", "me" or "my", no greetings, no quotes of questions. '
                  'Leave out mistakes, corrections, explanations of how the app works, and searches that found nothing. No fact, '
                  'name or number that is not in the work below. Answer with JSON only: {"title": the kind of work done there in at '
                  'most 5 words, such as "Calendar and inbox management" or "Market research briefings", never "Board support" or '
                  '"Executive assistant", "bullets": 1 to 4 bullets, each one sentence of at most 25 words}. Fewer, stronger '
                  'bullets are better.\n\nWork:\n%s') % (name, who[1], work)
        url, model_name = models.local('agents') or (agents.MODEL_URL, agents.MODEL)   # your own machines only
        body = {'model': model_name, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 800, 'temperature': 0.3,
                'chat_template_kwargs': {'enable_thinking': False}}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=300) as f:
            text = json.load(f)['choices'][0]['message'].get('content') or ''
        d = json.loads(re.search(r'\{.*\}', text, re.S).group(0))
        bullets = [' '.join(str(b).split()) for b in d.get('bullets') or [] if str(b).strip()][:4]
        if not bullets:
            raise ValueError('no bullets')
        return {'title': ' '.join(str(d.get('title') or '').split())[:60], 'bullets': bullets}


RESUME = Resume()


def assistant_experience(a):
    """The Assistant's work, from the conversations kept per board (DATA/talks/<board or space>.json): each board is a
    project, each question a job. It keeps no timing, so the page shows a person's time only, not time saved."""
    names = board_names()
    spaces = {s['id']: s.get('name') or 'A space' for s in read_spaces()['spaces']}
    cards = [c for c in board_cards() if c.get('count')]   # newest first; only boards with something on them have a picture
    projects, skills, human, n = [], {}, 0, 0
    for f in os.listdir(TALKS):
        if not f.endswith('.json'):
            continue
        scope = f[:-5]
        try:
            with open(os.path.join(TALKS, f)) as fh:
                turns = json.load(fh).get('turns') or []
        except (OSError, ValueError):
            continue
        if not turns:
            continue
        name = 'All boards' if scope == 'all' else names.get(scope) or ('Space: ' + spaces[scope[1:]] if scope.startswith('s') and scope[1:] in spaces else None)
        if not name:
            continue
        jobs, seen = [], set()
        for x in turns:
            q = ' '.join(str(x.get('q') or '').split())
            if x.get('route') == 'chat' or len(q) < 4 or q.lower().strip('?.! ') in seen:   # greetings and repeats are not work
                continue
            seen.add(q.lower().strip('?.! '))
            label, skill, what, rate = ASK_RATES.get(x.get('route'), ASK_RATES[None])
            skills.setdefault(skill, {}).setdefault(what, 0)
            skills[skill][what] += 1
            human += rate
            n += 1
            jobs.append({'task': x.get('q') or '', 'madeFor': label, 'when': x.get('t') or 0, 'start': x.get('t') or 0, 'human': rate,
                         'summary': ' '.join(str(x.get('a') or '').split())[:300], 'status': 'checked' if x.get('checked') else 'answered',
                         'did': [], 'sources': len(x.get('sources') or [])})
        jobs.sort(key=lambda j: -j['when'])
        if not jobs:
            continue
        thumbs = [] if scope in names else [c['id'] for c in cards if scope == 'all' or c.get('space') == scope[1:]][:4]   # All boards or a space: its newest boards
        projects.append({'board': scope if scope in names else '', 'thumbs': thumbs, 'boardName': name, 'jobs': jobs, 'from': min(j['when'] for j in jobs),
                         'to': max(j['when'] for j in jobs), 'human': sum(j['human'] for j in jobs), 'ran': None})
    checked = sum(1 for e in projects for j in e['jobs'] if j['status'] == 'checked')
    who = ('assistant', 'an executive assistant (an AI assistant) who supports the user')
    for e in projects:   # written like an executive assistant's LinkedIn entry, not a log of every question
        jobs = e.pop('jobs')
        kinds = {}
        for j in jobs:
            kinds[j['madeFor']] = kinds.get(j['madeFor'], 0) + 1
        e.update(RESUME.get(who, e['boardName'], ['- %s | asked: %s | answered: %s' % (j['madeFor'], j['task'][:200], j['summary'][:300]) for j in jobs],
                            ['%s: %d' % (k, v) for k, v in sorted(kinds.items(), key=lambda p: -p[1])]))
        e['count'] = len(jobs)
    a['experience'] = sorted([e for e in projects if e['count'] > 1], key=lambda e: -e['to'])   # a one-off job is not its own entry
    a['also'] = [{'board': e['board'], 'boardName': e['boardName']} for e in sorted(projects, key=lambda e: -e['to']) if e['count'] <= 1]
    a['skills'] = sorted([[k, sorted(v.items(), key=lambda p: -p[1])] for k, v in skills.items()], key=lambda p: -sum(c for _, c in p[1]))
    a['value'] = {'human': human, 'ran': None, 'rates': [[r[2], r[3]] for r in ASK_RATES.values() if r[3]]} if n else None
    a['facts'] = [['Questions answered', str(n)], ['Boards and spaces', str(len(projects))],
                  ['Checked against the board', str(checked)]]
    a['history'] = []


def agent_profile(key):
    """One agent's page: its library entry, why it exists, and its work history."""
    lib = agent_library()
    a = next((x for x in lib['always'] + lib['team'] if x['key'] == key), None)
    if not a:
        return None
    a = dict(a)
    if key in agents.STARTERS:
        a['why'] = ('One of the standard roles a Lead can put on a team. A Lead can also make a teammate of this kind for one job, '
                    'with its own name and reason; that work is counted here, with a "made for" line.')
        w = agent_work().get(key) or []
        checked = [x for x in w if x['check']]
        a['facts'] = [['Jobs', str(len(w))], ['Passed their check', '%d of %d' % (sum(x['check']['pass'] for x in checked), len(checked)) if checked else '-'],
                      ['Boards', str(len({x['board'] for x in w}))], ['Made for a job', str(len({x['madeFor'] for x in w if x['madeFor']}))]]
        a['last'] = w[0]['when'] if w else None
        a['history'] = w[:200]
        human, ran = sum(x['human'] for x in w), sum(x['ran'] for x in w)
        a['value'] = {'human': human, 'ran': round(ran), 'rates': [[name, rate] for _, _, name, rate in WORK_RATES]} if w else None
        skills = {}
        for x in w:
            for skill, name, n in x['did']:
                skills.setdefault(skill, {}).setdefault(name, 0)
                skills[skill][name] += n
        a['skills'] = sorted([[k, sorted(v.items(), key=lambda p: -p[1])] for k, v in skills.items()], key=lambda p: -sum(n for _, n in p[1]))
        boards = {}
        for x in w:
            boards.setdefault(x['board'], []).append(x)
        a['experience'] = sorted([{'board': b, 'boardName': xs[0]['boardName'] or 'A board', 'jobs': xs, 'from': min(x['start'] for x in xs),
                                   'to': max(x['when'] for x in xs), 'human': sum(x['human'] for x in xs), 'ran': round(sum(x['ran'] for x in xs))}
                                  for b, xs in boards.items()], key=lambda e: -e['to'])
        who = (key, 'the %s, an AI agent on the user\'s agent teams (its role: %s)' % (a['name'], agents.STARTERS[key].get('role', '')[:300]))
        for e in a['experience']:   # a LinkedIn entry per board: role title and what it delivered, the jobs as links under it
            lines = []
            for x in e['jobs']:
                did = {}
                for _, n, c in x['did']:
                    did[n] = did.get(n, 0) + c
                lines.append('- task: %s%s | did: %s | result: %s%s' % (x['task'][:250], ' | made for the job as %s: %s' % (x['madeFor'], (x['why'] or '')[:200]) if x['madeFor'] else '',
                    ', '.join('%d %s' % (c, n) for n, c in did.items()) or 'nothing recorded',
                    ('passed its check' if x['check']['pass'] else 'did not pass its check') if x['check'] else AG_WORDS.get(x['status'], x['status']),
                    ' | its own summary: ' + x['summary'][:300] if x['summary'] else ''))
            passed = sum(1 for x in e['jobs'] if x['check'] and x['check']['pass'])
            e.update(RESUME.get(who, e['boardName'], lines, ['%d %s here' % (len(e['jobs']), 'job' if len(e['jobs']) == 1 else 'jobs')]
                                + (['%d passed their check' % passed] if passed else [])))
    else:
        a['why'] = BUILTIN_WHY.get(key, '')
        a['facts'] = []
        if key == 'assistant':
            assistant_experience(a)
        elif key == 'router':
            b = read_board(ROUTER.board)
            cards = sorted([i for i in b.get('items') or [] if i.get('router')], key=lambda i: -(i.get('why') or {}).get('when', 0))
            a['history'] = [{'task': i['text'].split('\n')[0], 'board': ROUTER.board, 'boardName': 'Incoming', 'when': (i.get('why') or {}).get('when'),
                             'status': 'task' if i.get('router') and i.get('color') == router.COLORS['task'] else 'note'} for i in cards[:100]]
        elif key == 'watchdog':
            a['history'] = list(reversed(WATCHDOG.events[-100:]))
        elif key == 'plan-critic':
            names = board_names()
            jobs = sorted([x for x in RUNNER.view() if not x.get('parent') and x.get('crew')], key=lambda x: -x['created'])
            seen = [x for x in jobs if x.get('critic')]
            with RUNNER.lock:
                full = {x['id']: RUNNER.runs.get(x['id']) or {} for x in jobs}
            cuts = sum(f.get('critic', {}).get('before', 0) - f.get('critic', {}).get('after', 0) for f in full.values() if f.get('critic') and not f['critic'].get('skipped'))
            a['facts'] = [['Teams reviewed', str(len(seen))], ['Members cut or merged away', str(cuts)],
                          ['Jobs scored', str(sum(1 for x in jobs if x.get('score')))]]
            a['history'] = [{'task': x['task'], 'board': x['board'], 'boardName': names.get(x['board'], ''), 'id': x['id'], 'when': x.get('ended') or x['created'],
                             'status': x['status'], 'check': {'pass': not full[x['id']].get('gaps')} if full[x['id']].get('final_done') else None,
                             'line': ' | '.join(l for l in [('Critic: ' + x['critic']) if x.get('critic') else 'Critic off', ('Score: ' + x['score']) if x.get('score') else ''] if l)}
                            for x in jobs[:100]]
        elif key == 'board-brief-writer':
            d = os.path.join(DATA, 'briefs')
            files = sorted((f for f in os.listdir(d) if f.endswith('.json')), key=lambda f: -os.path.getmtime(os.path.join(d, f))) if os.path.isdir(d) else []
            names = board_names()
            a['history'] = [{'task': 'Brief of ' + (names.get(f[:-5]) or 'a board'), 'board': f[:-5], 'boardName': names.get(f[:-5], ''),
                             'when': int(os.path.getmtime(os.path.join(d, f)) * 1000), 'status': 'done'} for f in files[:60] if f[:-5] in names]
        else:
            a['history'] = []
        own_resume(key, a)
    a['how'] = agent_how(key, a) or {}
    a['how'].update(upskill=True, skills=skillbook.load().get(key) or [],   # every agent has the same Upskill box
                    nomodel=key == 'watchdog')   # plain code: words it is taught cannot change it
    return a


def upskill():
    return set(agents.STARTERS) | {'assistant', 'router', 'watchdog', 'board-brief-writer', 'plan-critic'}   # every agent in the library, voice-made ones included


def google_tools():   # the connected Google tools, from the app's own list: name, what it does, and whether it asks your yes
    return [[n.replace('_', ' '), v[0].split('. ')[0].rstrip('.') + '.' + (' Asks your yes first.' if v[3] else '')] for n, v in integrations.GOOGLE.items()]


def agent_how(key, a):
    """How an agent works, from the code itself: its prompt, the instructions every agent shares, its tools (with what each
    does), its model, and the skills the user added (teammates only)."""
    google = bool((integrations.load().get('google') or {}).get('refresh_token'))
    desc = lambda ts: [[t['function']['name'].replace('_', ' '), t['function']['description'].split('. ')[0].rstrip('.') + '.'] for t in ts]
    if key in agents.STARTERS:
        if key == 'lead':
            tools = agents.LEAD_TOOLS
        else:
            tools = agents.TOOLS + (agents.BROWSER_TOOLS if agents.BROWSER_URL else []) + (agents.CODE_TOOLS if key == 'engineer' else agents.CODE_READ)
        apps = google_tools() if google and key != 'lead' else []
        return {'prompt': agents.STARTERS[key].get('role', ''), 'shared': agents.SYSTEM, 'tools': desc(tools) + apps, 'model': a.get('model'),
                'skills': skillbook.load().get(key) or [], 'upskill': True,
                'note': 'A board can change this prompt for its own team (on the board: Edit team, then ... on this teammate). Skills added here go to this role on every board.'}
    if key == 'router':
        return {'prompt': router.JUDGE % ('(your rules note on the Incoming board)', '(the new email and calendar items)'),
                'note': 'Before the model sees anything, code drops no-reply senders, promotions and social mail, and anything already seen. '
                        'To change what it does, edit the rules note on the Incoming board.', 'link': a.get('link'), 'model': 'your own machines only'}
    if key == 'watchdog':
        return {'prompt': router.__doc__.split('\n\n')[1].replace('\n', ' '), 'note': 'Plain code, no model: it checks every minute.'}
    if key == 'plan-critic':
        return {'prompt': talk.TEAM_CRITIC, 'model': a.get('model') or (models.choice('agents') or (None, 'your own machines'))[1],
                'note': 'One model call per team, after the team designer and before any member starts. Code checks its answer: a member it '
                        'names that the designer never proposed, or an empty plan, means the designer\'s plan runs unchanged. Every job\'s score '
                        '(model calls, tool calls, web pages, time, members, items, questions, checks) is read from the saved runs after it ends, '
                        'critic or not, so on and off can be compared on the same job. The switch is in Settings.'}
    if key == 'board-brief-writer':
        return {'prompt': ' '.join(briefs.__doc__.split('\n\n')[1].split()), 'note': 'Runs on %s.' % ('an outside provider' if models.choice('voice') else 'your own machines')}
    if key == 'assistant':
        return {'tools': google_tools() if google else [],
                'note': 'Its instructions are in talk.py: it answers from your boards and connected apps, and asks your yes before anything that sends, books or deletes.'}
    return None


def own_resume(key, a):
    """The Router, Watchdog and Board brief writer: one Experience entry each, from their own work log."""
    h = [x for x in a.get('history') or [] if x.get('when')]
    if key not in ('router', 'watchdog', 'board-brief-writer'):
        return
    a['experience'] = []
    if not h:
        return
    if key == 'router':
        b = read_board(ROUTER.board)
        lines = ['- ' + ' | '.join(i['text'].split('\n')[:5]) for i in b.get('items') or [] if i.get('router')]
        place, name, thumbs = ROUTER.board, 'Incoming', []
        fallback = ['%d emails and calendar changes noted or turned into suggested tasks' % len(h)]
    elif key == 'watchdog':
        lines = ['- %s team: %s' % ('flagged a stuck' if x['status'] == 'stuck' else 'restarted a stopped', x['task'][:200]) for x in h]
        place, name, thumbs = '', 'Your agent teams', []
        fallback = ['%d teams flagged or restarted' % len(h)]
    else:
        d = os.path.join(DATA, 'briefs')
        lines = []
        for x in h[:30]:
            try:
                with open(os.path.join(d, x['board'] + '.json')) as f:
                    sm = ((json.load(f).get('brief') or {}).get('summary') or '')
            except (OSError, ValueError):
                sm = ''
            lines.append('- briefed %s: %s' % (x['boardName'], ' '.join(str(sm).split())[:250]))
        place, name, thumbs = '', 'Your boards', [x['board'] for x in h[:4]]
        fallback = ['Kept a brief of %s boards up to date' % len(h)]
    who = (key, 'the %s, an AI agent in the user\'s Clewbert app (%s)' % (a['name'], a['what']))
    e = {'board': place, 'boardName': name, 'thumbs': thumbs, 'from': min(x['when'] for x in h), 'to': max(x['when'] for x in h),
         'human': 0, 'ran': None, 'count': len(h)}
    e.update(RESUME.get(who, name, lines, fallback))
    a['experience'] = [e]

if __name__ == '__main__':
    seed_examples()
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
