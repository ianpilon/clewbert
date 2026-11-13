"""OpenLoom videos on the board, with no copy: the card plays from OpenLoom and the voice and teams read OpenLoom's transcript.

Pasting an OpenLoom link asks OpenLoom (on this machine) for the canvas's own personal link to that video, so private videos
work and stay private. Asking needs the canvas key: a file OpenLoom makes on this machine, readable only by this user.
The personal link shows in the video's Share box as "Context Canvas (your boards)" (the name OpenLoom gives it); turning it off there stops the card.
"""
import json, os, re, threading, time, urllib.error, urllib.parse, urllib.request

LOCAL = os.environ.get('OPENLOOM_LOCAL_URL', '').rstrip('/')   # OpenLoom itself, same machine (e.g. http://127.0.0.1:8160); empty = off
PUBLIC = os.environ.get('OPENLOOM_URL', '').rstrip('/')   # where the browser fetches the video
KEY_FILE = os.environ.get('OPENLOOM_CANVAS_KEY_FILE', os.path.expanduser('~/.config/openloom/canvas.key'))
LINK = re.compile(r'^https?://([^/]+)/(?:share|r|embed)/([A-Za-z0-9_-]{4,64})(?:[/?#]|$)', re.I)

FILE = None   # set by server.py: <data>/openloom.json, video id -> the canvas's personal link token
_lock = threading.Lock()
_cache = {}   # video id -> (time, OpenLoom's answer); its media token lasts hours, re-asked after 30 min
FRESH = 1800


def video_id(url):
    """The OpenLoom video id in a link, or None when it is not an OpenLoom video link."""
    m = LINK.match(str(url or '').strip())
    if not m or not LOCAL:   # no OpenLoom set up: an ordinary link
        return None
    host = m.group(1).lower()
    return m.group(2) if 'openloom' in host or host == urllib.parse.urlsplit(PUBLIC).netloc.lower() else None


def _tokens():
    try:
        with open(FILE) as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def _save_tokens(d):
    tmp = FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, FILE)


def _key():
    try:
        with open(KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return ''


def _ask_link(vid):
    """The canvas's personal link token for this video, from OpenLoom."""
    def call(key):
        req = urllib.request.Request(LOCAL + '/api/canvas-link', data=json.dumps({'id': vid}).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', 'X-Canvas-Key': key})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())['invite']
    try:
        try:
            return call(_key())
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            return call(_key())   # OpenLoom makes the key file on the first ask; read it again
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LookupError('OpenLoom has no such video (or it is in the trash)')
        raise


def _token(vid, renew=False):
    with _lock:
        d = _tokens()
        if renew or not d.get(vid):
            d[vid] = _ask_link(vid)
            _save_tokens(d)
        return d[vid]


def info(vid, fresh=False, renew=False):
    """OpenLoom's answer for the video (title, size, media addresses, transcript), seen through the canvas's personal link."""
    hit = _cache.get(vid)
    if hit and not fresh and time.time() - hit[0] < FRESH:
        return hit[1]
    tok = _token(vid)
    try:   # a link turned off in OpenLoom stays off for cards already on boards; only a new paste (renew) asks for a new one
        with urllib.request.urlopen('%s/api/public-recording?id=%s&invite=%s' % (LOCAL, vid, tok), timeout=20) as resp:
            d = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404 and renew:
            _token(vid, renew=True)
            return info(vid, fresh=True)
        if e.code == 404:
            raise LookupError('OpenLoom will not show this video (it is gone, or its Context Canvas link was turned off)')
        raise
    _cache[vid] = (time.time(), d)
    return d


def card(url):
    """The fields of a video card for an OpenLoom link."""
    vid = video_id(url)
    if not vid:
        raise ValueError('not an OpenLoom video link')
    r = info(vid, fresh=True, renew=True)['recording']   # pasting it again after its link was turned off = a new link
    out = {'type': 'media', 'kind': 'video', 'loom': vid, 'label': r.get('title') or 'OpenLoom video',
           'src': 'api/openloom/%s/video' % vid, 'open': '%s/share/%s' % (PUBLIC, vid)}
    if r.get('thumbnailUrl'):
        out['poster'] = 'api/openloom/%s/poster' % vid
    if r.get('width') and r.get('height'):
        out['ar'] = r['height'] / r['width']
    return out


def media_url(vid, what):
    """Where the browser gets the video ('video') or its still ('poster'): OpenLoom's own address with a fresh token."""
    r = info(vid)['recording']
    path = (r.get('playbackUrl') or r.get('videoUrl')) if what == 'video' else r.get('thumbnailUrl')
    if not path:
        return None
    return path if path.startswith('http') else PUBLIC + path


def segments(vid):
    """[(seconds from, seconds to, words)] of the video's transcript, or [] while OpenLoom has none."""
    t = info(vid).get('transcript') or {}
    out = [(s['startMs'] / 1000, s['endMs'] / 1000, ' '.join(str(s.get('text') or '').split())) for s in t.get('segments') or []]
    if not out and t.get('fullText'):
        out = [(0, (info(vid)['recording'].get('durationMs') or 0) / 1000, t['fullText'])]
    return out


def words(vid, limit=12000):
    """The transcript as the voice and teams read it: a line per minute or so, each with where it starts."""
    try:
        segs = segments(vid)
        r = info(vid)['recording']
    except (OSError, ValueError, LookupError) as e:
        return '(OpenLoom transcript not available: %s)' % str(e)[:160]
    if not segs:
        return '(OpenLoom has no transcript for this video yet.)'
    lines, cur, start = [], [], None
    for t0, t1, text in segs:
        if start is None:
            start = t0
        cur.append(text)
        if t1 - start >= 60 or text.endswith(('.', '?', '!')) and t1 - start >= 40:
            lines.append('[%02d:%02d] %s' % (int(start) // 60, int(start) % 60, ' '.join(cur)))
            cur, start = [], None
    if cur:
        lines.append('[%02d:%02d] %s' % (int(start or 0) // 60, int(start or 0) % 60, ' '.join(cur)))
    mins = int((r.get('durationMs') or 0) / 60000)
    head = 'OpenLoom video, %d min. Transcript from OpenLoom:\n' % mins
    body = '\n'.join(lines)
    return head + (body if len(body) <= limit else body[:limit] + '\n(transcript continues; ask for the rest)')
