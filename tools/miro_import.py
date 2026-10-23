#!/usr/bin/env python3
"""Copy one Miro board into Context Canvas.

Usage: /usr/bin/python3 tools/miro_import.py MIRO_BOARD_ID_OR_LINK OUT_DIR
  (system python: its SSL certificates work; the python.org build's do not)
The app's "New board > Import from Miro" runs import_board() inside server.py, straight into the data folder.

Reads the Miro REST API with the key in ~/.config/miro/key (the read-only app
"Context Canvas import" in the user's Miro dev team). Writes OUT_DIR/boards/<id>.json and
OUT_DIR/images/*, laid out like the server's data folder, so
  copy OUT_DIR into your data folder
publishes the board.

Brought over: sticky notes (with colour), text, cards, frames and shapes (as areas),
images, PDFs (first page shown, click opens the PDF), link embeds.
Not brought over: connectors, videos, pen drawings, mind maps, tables, rotation, text styling.
"""
import collections, concurrent.futures, hashlib, html, json, os, re, secrets, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request

SITE = os.environ.get('CANVAS_SITE', 'http://localhost:8135/')
KEY_FILE = os.path.expanduser('~/.config/miro/key')


class ImportError_(Exception):
    """A reason the user can read, shown on the import card."""


def key():
    try:
        with open(KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        raise ImportError_('The Miro key is missing on the server, so Miro boards cannot be read.')
MAX_PX = 1600
STICKY = {'gray': '#f5f6f8', 'light_yellow': '#fff9b1', 'yellow': '#f5d128', 'orange': '#ff9d48',
          'light_green': '#d5f692', 'green': '#c9df56', 'dark_green': '#93d275', 'cyan': '#67c6c0',
          'light_pink': '#ffcee0', 'pink': '#ea94bb', 'violet': '#c6a2d2', 'red': '#f0939d',
          'light_blue': '#a6ccf5', 'blue': '#6cd8fa', 'dark_blue': '#9ea9ff', 'black': '#000000'}


def get(url, auth=True, raw=False):
    req = urllib.request.Request(url, headers={'User-Agent': 'context-canvas-import',
                                               **({'Authorization': 'Bearer ' + key()} if auth else {})})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            return data if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429 or attempt == 2:   # a refusal will not change on retry
                raise
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def api(path):
    return get('https://api.miro.com/v2/' + path)


def plain(markup):
    t = re.sub(r'(?i)<br\s*/?>|</p>|</li>|</div>', '\n', markup or '')
    t = html.unescape(re.sub(r'<[^>]+>', '', t)).replace('\xa0', ' ')
    return re.sub(r'\n{3,}', '\n\n', t).strip()


def hex_color(c, default):
    m = re.fullmatch(r'#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?', c or '')
    return '#' + m.group(1).lower() if m else default


def sticky_font(text, w, h):
    """Miro grows sticky-note text to fill the note; aim for the text covering about half of it."""
    chars = max(1, len(text))
    longest = max((len(x) for x in text.split('\n')), default=1) or 1
    fs = min((.5 * w * h / (.8 * chars)) ** .5, h / 3, w / (longest * .6) if chars < 40 else h)
    return round(max(min(11, w / 10), fs), 1)


def sips_size(path):
    out = subprocess.run(['sips', '-g', 'pixelWidth', '-g', 'pixelHeight', path], capture_output=True, text=True).stdout
    w, h = (int(x) for x in re.findall(r'pixel(?:Width|Height): (\d+)', out))
    return w, h


class Store:
    def __init__(self, out):
        self.dir = os.path.join(out, 'images')
        os.makedirs(self.dir, exist_ok=True)

    def save(self, data, ext):
        name = hashlib.sha256(data).hexdigest() + '.' + ext
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            with open(path, 'wb') as f:
                f.write(data)
        return 'api/images/' + name

    def picture(self, data, ext, tmp):
        """Shrink to ≤1600 px like the app does on paste; big PNGs become JPEG."""
        src = os.path.join(tmp, secrets.token_hex(4) + '.' + ext)
        with open(src, 'wb') as f:
            f.write(data)
        if ext == 'pdf':
            png = src + '.png'
            subprocess.run(['sips', '-s', 'format', 'png', src, '--out', png], capture_output=True, check=True)
            src, ext = png, 'png'
        if ext not in ('jpg', 'png'):
            jpg = src + '.jpg'
            subprocess.run(['sips', '-s', 'format', 'jpeg', src, '--out', jpg], capture_output=True, check=True)
            src, ext = jpg, 'jpg'
        if max(sips_size(src)) > MAX_PX:
            subprocess.run(['sips', '-Z', str(MAX_PX), src], capture_output=True, check=True)
        if ext == 'png' and os.path.getsize(src) > 800_000:
            jpg = src + '.jpg'
            subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '82', src, '--out', jpg],
                           capture_output=True, check=True)
            src, ext = jpg, 'jpg'
        with open(src, 'rb') as f:
            return self.save(f.read(), ext)


def resource(url):
    """Miro resource links answer with a short-lived download URL when redirect=false."""
    url = url.replace('format=preview', 'format=original')
    return get(get(url)['url'], auth=False, raw=True)


def ext_of(data):
    if data[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:5] == b'%PDF-':
        return 'pdf'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    return 'heic'


def board_id_from(link):
    """The board id from a Miro link (miro.com/app/board/<id>/...) or a bare id."""
    link = urllib.parse.unquote(str(link or '').strip())
    m = re.search(r'miro\.com/app/board/([A-Za-z0-9_=-]+)', link)
    if m:
        return m.group(1)
    if re.fullmatch(r'[A-Za-z0-9_-]{8,}=?', link):
        return link
    raise ImportError_('That is not a Miro board link. Open the board in Miro and copy the address from the browser.')


def import_board(link, out, space=None, progress=None):
    """Copy the Miro board into OUT (a data folder). Returns the new board's id, name and counts."""
    board_id = board_id_from(link)
    try:
        board = api('boards/' + urllib.parse.quote(board_id))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise ImportError_('Miro would not open that board for the import key. It can read only the boards your Miro key has access to.')
        raise
    items, cursor = [], None
    while True:
        page = api('boards/%s/items?limit=50%s' % (urllib.parse.quote(board_id), '&cursor=' + cursor if cursor else ''))
        items += page['data']
        cursor = page.get('cursor')
        if not cursor:
            break
    by_id = {i['id']: i for i in items}

    def centre(i):
        p = i['position']
        x, y = p['x'], p['y']
        if i.get('parent') and p.get('relativeTo') == 'parent_top_left' and i['parent']['id'] in by_id:
            f = by_id[i['parent']['id']]
            fx, fy = centre(f)
            x += fx - f['geometry']['width'] / 2
            y += fy - f['geometry']['height'] / 2
        return x, y

    store = Store(out)
    done = [0]
    if progress:
        progress(0, len(items))
    skipped = collections.Counter()
    tmp = tempfile.mkdtemp(prefix='miro-import-')

    def convert(i):
        t, d, st, g = i['type'], i.get('data') or {}, i.get('style') or {}, i.get('geometry') or {}
        w, h = g.get('width'), g.get('height')
        if t == 'text' and w and not h:   # Miro sizes text boxes to their content and gives no height
            fs = float(st.get('fontSize') or 14)
            per_line = max(1, int(w / (fs * .55)))
            lines = sum(max(1, -(-len(line) // per_line)) for line in plain(d.get('content')).split('\n'))
            h = lines * fs * 1.45 + fs
        if not w or not h:
            skipped[t + ' (no size)'] += 1
            return None
        cx, cy = centre(i)
        base = {'x': cx - w / 2, 'y': cy - h / 2, 'w': w, 'h': h}
        if t == 'frame':
            return {'type': 'area', 'text': plain(d.get('title')), 'color': hex_color(st.get('fillColor'), '#f4f5f7'), **base}
        if t == 'shape' and not plain(d.get('content')):
            return {'type': 'area', 'text': 'Shape', 'color': hex_color(st.get('fillColor'), '#f4f5f7'), **base}
        if t == 'shape':
            return {'type': 'note', 'text': plain(d.get('content')), 'color': hex_color(st.get('fillColor'), '#ffffff'), **base}
        if t == 'sticky_note':
            text = plain(d.get('content'))
            return {'type': 'note', 'text': text, 'color': STICKY.get(st.get('fillColor'), '#fff9b1'),
                    'fs': sticky_font(text, w, h), 'align': st.get('textAlign') or 'left', **base}
        if t == 'text':
            return {'type': 'note', 'text': plain(d.get('content')), 'color': 'none', 'fs': float(st.get('fontSize') or 14), **base}
        if t in ('card', 'app_card'):
            text = '\n\n'.join(x for x in (plain(d.get('title')), plain(d.get('description'))) if x)
            return {'type': 'note', 'text': text, 'color': '#ffffff', **base}
        if t == 'image' and d.get('imageUrl'):
            data = resource(d['imageUrl'])
            return {'type': 'img', 'src': store.picture(data, ext_of(data), tmp), **base}
        if t == 'document' and d.get('documentUrl'):
            data = resource(d['documentUrl'])
            ext = ext_of(data)
            if ext != 'pdf':
                skipped['document (not a PDF)'] += 1
                return None
            return {'type': 'img', 'src': store.picture(data, 'pdf', tmp), 'url': SITE + store.save(data, 'pdf'),
                    'label': d.get('title') or 'PDF', **base}
        if t == 'embed' and d.get('url'):
            u = {'title': d.get('title') or '', 'desc': d.get('description') or '', 'img': d.get('previewUrl') or '',
                 'pub': d.get('providerName') or ''}
            return {'type': 'link', 'url': d['url'], **({'unfurl': u} if u['title'] or u['img'] else {}), **base}
        skipped[t] += 1
        return None

    def step(i):
        r = safe(convert, i, skipped)
        done[0] += 1
        if progress:
            progress(done[0], len(items))
        return i, r

    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        results = list(pool.map(step, items))
    converted = [c for _, c in results if c]

    # stacking: big areas at the back, then pictures, then notes and links on top
    rank = {'area': 0, 'img': 1, 'link': 2, 'note': 2}
    converted.sort(key=lambda c: (rank[c['type']], -(c['w'] * c['h']) if c['type'] == 'area' else 0))
    for n, c in enumerate(converted, 1):
        c['id'] = 'm%d' % n
        c['z'] = n

    view = None
    if converted:
        x0 = min(c['x'] for c in converted); y0 = min(c['y'] for c in converted)
        x1 = max(c['x'] + c['w'] for c in converted); y1 = max(c['y'] + c['h'] for c in converted)
        s = max(.002, min(2, (1400 - 80) / max(1, x1 - x0), (800 - 80) / max(1, y1 - y0)))
        view = {'x': (1400 - (x1 - x0) * s) / 2 - x0 * s, 'y': (800 - (y1 - y0) * s) / 2 - y0 * s, 's': s}

    now = int(time.time() * 1000)
    new_id = secrets.token_hex(6)
    os.makedirs(os.path.join(out, 'boards'), exist_ok=True)
    path = os.path.join(out, 'boards', new_id + '.json')
    with open(path + '.tmp', 'w') as f:
        json.dump({'id': new_id, 'name': board.get('name') or 'Imported board', 'created': now, 'updated': now,
                   'items': converted, 'view': view, 'space': space}, f)
    os.replace(path + '.tmp', path)
    return {'id': new_id, 'name': board.get('name') or 'Imported board',
            'brought': dict(collections.Counter(c['type'] for c in converted)), 'skipped': dict(skipped),
            'images': sorted({n for c in converted for n in (image_ref(c.get('src')), image_ref(c.get('url'))) if n})}


def image_ref(ref):
    m = re.search(r'api/images/([0-9a-f]{64}\.(?:jpg|png|pdf))', ref or '')
    return m.group(1) if m else None


def main(link, out):
    r = import_board(link, out)
    print('board', r['id'], '|', r['name'])
    print('brought over:', r['brought'])
    print('not brought over:', r['skipped'])


def safe(fn, item, skipped):
    try:
        return fn(item)
    except Exception as e:
        skipped['%s (failed: %s)' % (item['type'], type(e).__name__)] += 1
        return None


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
