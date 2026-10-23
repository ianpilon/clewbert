#!/usr/bin/env python3
"""A real browser for Context Canvas agents: headless Chromium (Playwright), one session per agent task.

Usage: BROWSER_TOKEN=<secret> service.py PORT [BIND]      (BIND defaults to 0.0.0.0; every request needs the token)
Needs: pip install playwright && playwright install chromium

The agent never sees pixels or HTML. Each look gives it the page as a numbered list of the things it can use
("[3] textbox Where to? = ''", "[7] button Search") plus the page's readable text; it acts by number:
open, click n, type n text, select n option, scroll, back. A screenshot of the session is kept for the board.

  POST /s/<session>/<open|look|click|type|select|scroll|back|close>   JSON in, JSON out
  POST /s/<session>/render {html, steps}   show a page the agent wrote (a demo), play the steps a person would take
                                           ("press Space", "scroll down 600", "click Explode", "wait 1000"), and report
                                           its JavaScript errors; GET /s/<session>/shot?i=n is the picture after step n
  GET  /s/<session>/shot                                               the latest screenshot (JPEG)
  GET  /health
Playwright's sync API is not thread-safe, so one thread owns the browser and requests queue for it.
"""
import json, os, queue, re, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout

PORT = int(sys.argv[1])
BIND = sys.argv[2] if len(sys.argv) > 2 else '0.0.0.0'
TOKEN = os.environ.get('BROWSER_TOKEN', '')
IDLE = 20 * 60            # a session nobody used for this long is closed
MAX_SESSIONS = 6
TEXT_WORDS = 1200
VIEW = {'width': 1280, 'height': 800}
SESSION = re.compile(r'^[a-z0-9]{6,40}$')

# numbers every visible thing a person could click or type in, and describes it in a line
NUMBER = r"""() => {
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=tab],' +
    '[role=menuitem],[role=checkbox],[role=radio],[role=combobox],[role=switch],[role=option],[onclick],[contenteditable=true]';
  document.querySelectorAll('[data-cc-n]').forEach(e => e.removeAttribute('data-cc-n'));
  const out = []; let n = 0;
  for (const e of document.querySelectorAll(sel)) {
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < -innerHeight || r.top > innerHeight * 3) continue;
    const st = getComputedStyle(e);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0) continue;
    n++; e.setAttribute('data-cc-n', n);
    const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || '').toLowerCase();
    const role = e.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'select' ? 'select' : tag === 'textarea' ? 'textbox' :
      tag === 'input' ? (['checkbox', 'radio', 'submit', 'button', 'search', 'email', 'password'].includes(type) ? type : 'textbox') : tag);
    let name = (e.getAttribute('aria-label') || e.innerText || e.getAttribute('placeholder') || e.getAttribute('title') ||
      e.getAttribute('alt') || (type === 'submit' ? e.value : '') || e.getAttribute('name') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    if (!name && tag === 'a') name = (e.getAttribute('href') || '').slice(0, 80);
    const form = e.closest('form');
    // what kind of form: a search is harmless; one that takes a message or personal details sends something
    const formKind = !form ? null : (form.getAttribute('role') === 'search' || form.querySelector('input[type=search],input[name=q],input[name=query],input[name=search],input[name=s]')) ? 'search' :
      form.querySelector('textarea,input[type=email],input[type=tel],input[type=password],input[name*=mail],input[name*=phone]') ? 'personal' : 'other';
    out.push({n, role, name, formKind,
      value: (tag === 'input' || tag === 'textarea') && type !== 'password' && !['submit', 'button'].includes(type) ? String(e.value || '').slice(0, 60) : null,
      href: tag === 'a' ? e.href : null,
      submits: type === 'submit' || (tag === 'button' && (!type || type === 'submit') && !!form),
      inForm: !!form, password: type === 'password',
      options: tag === 'select' ? [...e.options].slice(0, 25).map(o => o.text.trim()) : null,
      inView: r.bottom > 0 && r.top < innerHeight});
    if (n >= 160) break;
  }
  return out;
}"""


# the words from the top of the screen down (about three screens), so scrolling shows new text
TEXT = r"""() => {
  const out = []; const seen = new Set();
  for (const e of document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,td,th,pre,blockquote,dt,dd,figcaption,label,span,a,div')) {
    if (e.closest('script,style,noscript,nav,footer')) continue;
    let own = '';
    for (const c of e.childNodes) if (c.nodeType === 3) own += c.textContent;
    own = own.replace(/\s+/g, ' ').trim();
    if (!own) continue;
    const r = e.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight * 3 || r.width < 2) continue;
    if (seen.has(e)) continue; seen.add(e);
    out.push(own);
  }
  return out.join(' ');
}"""


class Browser:
    """Owns Playwright on one thread; sessions are browser contexts (own cookies), one page each."""

    def __init__(self):
        self.q = queue.Queue()
        self.sessions = {}
        threading.Thread(target=self.loop, daemon=True).start()

    def do(self, sid, cmd, args):
        box = {'done': threading.Event()}
        self.q.put((sid, cmd, args, box))
        if not box['done'].wait(90):
            return {'error': 'the browser took too long'}
        return box['out']

    def loop(self):
        with sync_playwright() as p:
            self.chrome = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
            while True:
                try:
                    sid, cmd, args, box = self.q.get(timeout=30)
                except queue.Empty:
                    self.reap()
                    continue
                try:
                    box['out'] = self.run(sid, cmd, args)
                except PWTimeout:
                    box['out'] = {'error': 'the page did not answer in time', **self.safe_look(sid)}
                except PWError as e:
                    box['out'] = {'error': str(e).split('\n')[0][:300], **self.safe_look(sid)}
                except Exception as e:   # one bad request never stops the browser
                    box['out'] = {'error': '%s: %s' % (type(e).__name__, e)}
                box['done'].set()

    def reap(self):
        for sid, s in list(self.sessions.items()):
            if time.time() - s['used'] > IDLE:
                self.close(sid)

    def close(self, sid):
        s = self.sessions.pop(sid, None)
        if s:
            try:
                s['ctx'].close()
            except PWError:
                pass

    def session(self, sid):
        s = self.sessions.get(sid)
        if not s:
            while len(self.sessions) >= MAX_SESSIONS:
                self.close(min(self.sessions, key=lambda k: self.sessions[k]['used']))
            ctx = self.chrome.new_context(viewport=VIEW, locale='en-CA', user_agent=(
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'))
            s = {'ctx': ctx, 'page': ctx.new_page(), 'shot': None, 'errors': []}
            ctx.on('page', lambda pg: s.update(page=pg))   # a link that opens a new tab: follow it
            s['page'].on('pageerror', lambda e: s['errors'].append(str(e)[:300]))
            s['page'].on('console', lambda m: m.type == 'error' and s['errors'].append(m.text[:300]))
            self.sessions[sid] = s
        s['used'] = time.time()
        return s

    def run(self, sid, cmd, a):
        if cmd == 'close':
            self.close(sid)
            return {'ok': True}
        s = self.session(sid)
        pg = s['page']
        if cmd == 'open':
            url = str(a.get('url') or '')
            if not re.match(r'^https?://', url):
                return {'error': 'only http and https addresses can be opened'}
            pg.goto(url, wait_until='domcontentloaded', timeout=30000)
        elif cmd in ('click', 'type', 'select'):
            el = pg.locator('[data-cc-n="%d"]' % int(a.get('n') or 0))
            if not el.count():
                return {'error': 'there is no element %s on the page now; look again' % a.get('n'), **self.look(s)}
            el = el.first
            el.scroll_into_view_if_needed(timeout=5000)
            if cmd == 'click':
                el.click(timeout=8000)
            elif cmd == 'type':
                el.fill(str(a.get('text') or ''), timeout=8000)
                if a.get('enter'):
                    el.press('Enter')
            else:
                opt = str(a.get('option') or '')
                try:
                    el.select_option(label=opt, timeout=5000)
                except (PWError, PWTimeout):
                    el.select_option(value=opt, timeout=5000)
        elif cmd == 'render':   # a page the agent wrote: show it, let it run a moment, report what went wrong
            s['errors'], s['shots'] = [], []
            pg.set_content(str(a.get('html') or ''), wait_until='load', timeout=20000)
            pg.wait_for_timeout(1500)
            s['shots'].append(pg.screenshot(type='jpeg', quality=60))
            states = [{'step': 'opened the page', 'errors': list(s['errors'])}]
            for step in [str(x) for x in (a.get('steps') or [])][:8]:
                before = len(s['errors'])
                try:
                    play(pg, step)
                except (PWError, PWTimeout, ValueError) as e:
                    s['errors'].append('could not do "%s": %s' % (step, str(e).split(chr(10))[0][:150]))
                pg.wait_for_timeout(900)
                s['shots'].append(pg.screenshot(type='jpeg', quality=60))
                states.append({'step': step, 'errors': s['errors'][before:]})
            out = self.look(s)
            out['errors'], out['states'] = s['errors'][:10], states
            return out
        elif cmd == 'scroll':
            pg.mouse.wheel(0, VIEW['height'] * (0.8 if a.get('dir', 'down') == 'down' else -0.8))
        elif cmd == 'back':
            pg.go_back(wait_until='domcontentloaded', timeout=20000)
        elif cmd != 'look':
            return {'error': 'unknown command %s' % cmd}
        settle(s['page'])
        return self.look(s)

    def look(self, s):
        pg = s['page']
        elements = pg.evaluate(NUMBER)
        words = ' '.join(pg.evaluate(TEXT).split()).split(' ')
        y = pg.evaluate('() => [scrollY, document.documentElement.scrollHeight, innerHeight]')
        s['shot'] = pg.screenshot(type='jpeg', quality=55)
        return {'url': pg.url, 'title': pg.title(), 'elements': elements, 'text': ' '.join(words[:TEXT_WORDS]),
                'more_text': y[0] + y[2] * 3 < y[1] - 10, 'at_top': y[0] < 10, 'at_end': y[0] + y[2] >= y[1] - 10,
                'scroll': {'y': y[0], 'height': y[1], 'view': y[2]}}

    def safe_look(self, sid):
        try:
            return self.look(self.sessions[sid]) if sid in self.sessions else {}
        except Exception:
            return {}


def play(pg, step):
    """One step a person takes on a page: press <key>, scroll down/up <px>, click <text or #id>, type <text>, wait <ms>."""
    m = re.match(r'^\s*(press|scroll|click|type|wait)\s*(.*)$', step, re.I)
    if not m:
        raise ValueError('say press, scroll, click, type or wait')
    verb, rest = m.group(1).lower(), m.group(2).strip()
    if verb == 'press':
        key = {'space': ' ', 'spacebar': ' ', 'enter': 'Enter', 'escape': 'Escape', 'esc': 'Escape'}.get(rest.lower(), rest)
        pg.keyboard.press('Space' if key == ' ' else key)
    elif verb == 'scroll':
        n = re.search(r'\d+', rest)
        pg.mouse.move(VIEW['width'] // 2, VIEW['height'] // 2)
        pg.mouse.wheel(0, (int(n.group(0)) if n else 600) * (-1 if 'up' in rest.lower() else 1))
    elif verb == 'click':
        target = pg.locator(rest).first if rest.startswith(('#', '.')) else pg.get_by_text(rest, exact=False).first
        target.click(timeout=5000)
    elif verb == 'type':
        pg.keyboard.type(rest)
    else:
        pg.wait_for_timeout(min(int(re.search(r'\d+', rest).group(0)) if re.search(r'\d+', rest) else 1000, 5000))


def settle(pg):
    """Wait for the page to calm down after an action, but never long."""
    try:
        pg.wait_for_load_state('domcontentloaded', timeout=8000)
        pg.wait_for_load_state('networkidle', timeout=4000)
    except PWTimeout:
        pass


BROWSER = Browser()


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype='application/json'):
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        if TOKEN and self.headers.get('X-Token') != TOKEN:
            self.reply(401, {'error': 'wrong token'})
            return False
        return True

    def parts(self):
        return [p for p in self.path.split('?')[0].split('/') if p]

    def do_GET(self):
        p = self.parts()
        if p == ['health']:
            return self.reply(200, {'ok': True, 'sessions': len(BROWSER.sessions)})
        if not self.allowed():
            return
        if len(p) == 3 and p[0] == 's' and SESSION.match(p[1]) and p[2] == 'shot':
            s = BROWSER.sessions.get(p[1])
            i = re.search(r'[?&]i=(\d+)', self.path)
            if s and i and int(i.group(1)) < len(s.get('shots') or []):   # the picture after one step of a played demo
                return self.reply(200, s['shots'][int(i.group(1))], 'image/jpeg')
            return self.reply(200, s['shot'], 'image/jpeg') if s and s.get('shot') else self.reply(404, {'error': 'no screenshot'})
        self.reply(404, {'error': 'not found'})

    def do_POST(self):
        p = self.parts()
        if not self.allowed():
            return
        if len(p) != 3 or p[0] != 's' or not SESSION.match(p[1]):
            return self.reply(404, {'error': 'not found'})
        try:
            args = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        except ValueError:
            return self.reply(400, {'error': 'bad json'})
        self.reply(200, BROWSER.do(p[1], p[2], args if isinstance(args, dict) else {}))

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
