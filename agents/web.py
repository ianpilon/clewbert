"""The agent's view of the web: a search with no account or key, and a page read as plain text."""
import html, json, os, re, urllib.parse, urllib.request
from html.parser import HTMLParser

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36'
PAGE_WORDS = 2500
SKIP = {'script', 'style', 'noscript', 'svg', 'nav', 'footer', 'header', 'form', 'iframe', 'template'}
BLOCK = {'p', 'div', 'li', 'tr', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'section', 'article', 'td', 'th', 'pre', 'blockquote'}


def search(query, n=8):
    """[{title, url, snippet}]: your own SearXNG when SEARXNG_URL is set, else the first public engine that answers.
    Public engines turn away a home connection that searches a lot (a "prove you are human" page), so there are two."""
    engines = ([searxng] if SEARXNG_URL else []) + [brave, duckduckgo]
    for engine in engines:
        try:
            hits = engine(query, n)
        except OSError:
            continue
        if hits:
            return hits
    return []


SEARXNG_URL = os.environ.get('SEARXNG_URL', '')   # e.g. http://127.0.0.1:8888 (a SearXNG with the json format on)


def get(url, data=None):
    req = urllib.request.Request(url, data=data, headers={'User-Agent': UA, 'Accept-Language': 'en'})
    return urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'replace')


def searxng(query, n):
    j = json.loads(get(SEARXNG_URL.rstrip('/') + '/search?' + urllib.parse.urlencode({'q': query, 'format': 'json'})))
    return [{'title': r.get('title', ''), 'url': r['url'], 'snippet': r.get('content', '')} for r in j.get('results', [])[:n]]


def brave(query, n):
    page = get('https://search.brave.com/search?' + urllib.parse.urlencode({'q': query}))
    out = []
    for block in page.split('data-type="web"')[1:]:
        u = re.search(r'<a href="(https?://[^"]+)"', block)
        t = re.search(r'search-snippet-title[^"]*" title="([^"]*)"', block)
        s = re.search(r'class="generic-snippet[^"]*"><div class="content[^"]*">(.*?)</div>', block, re.S)
        if u and t:
            out.append({'title': html.unescape(t.group(1)), 'url': html.unescape(u.group(1)), 'snippet': text_of(s.group(1)) if s else ''})
        if len(out) >= n:
            break
    return out


def duckduckgo(query, n):
    page = get('https://html.duckduckgo.com/html/', urllib.parse.urlencode({'q': query}).encode())
    out = []
    for block in page.split('class="result__body"')[1:]:
        a = re.search(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        url = a.group(1)
        if 'uddg=' in url:   # DuckDuckGo wraps each link in a redirect
            url = urllib.parse.unquote(re.search(r'uddg=([^&]+)', url).group(1))
        if 'duckduckgo.com/y.js' in url:   # an ad
            continue
        s = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        out.append({'title': text_of(a.group(2)), 'url': url, 'snippet': text_of(s.group(1)) if s else ''})
        if len(out) >= n:
            break
    return out


def text_of(fragment):
    return html.unescape(re.sub(r'<[^>]+>', '', fragment)).strip()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self.in_title = [], 0, '', False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        elif tag == 'title':
            self.in_title = True
        elif tag in BLOCK:
            self.out.append('\n')

    def handle_endtag(self, tag):
        if tag in SKIP and self.skip:
            self.skip -= 1
        elif tag == 'title':
            self.in_title = False
        elif tag in BLOCK:
            self.out.append('\n')

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def read_page(url):
    """(title, words) of a web page, cut to PAGE_WORDS words."""
    if not re.match(r'^https?://', url):
        raise ValueError('only http and https pages can be read')
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'})
    with urllib.request.urlopen(req, timeout=25) as r:
        ctype = r.headers.get('Content-Type', '')
        raw = r.read(3_000_000)
    charset = (re.search(r'charset=([\w-]+)', ctype) or [None, 'utf-8'])[1]
    text = raw.decode(charset, 'replace')
    if 'html' not in ctype and not text.lstrip()[:200].lower().startswith(('<!doctype', '<html')):
        return '', ' '.join(text.split()[:PAGE_WORDS])
    p = _Text()
    p.feed(text)
    lines = [' '.join(l.split()) for l in ''.join(p.out).split('\n')]
    words = '\n'.join(l for l in lines if l).split(' ')
    return ' '.join(p.title.split()), ' '.join(words[:PAGE_WORDS])


# What a page is built with, from its own source: each clue names the text it was found by (no guessing)
FINGERPRINTS = [
    ('Next.js', r'__NEXT_DATA__|/_next/static'), ('Nuxt', r'__NUXT__|/_nuxt/'), ('React', r'data-reactroot|react-dom|react\.production'),
    ('Vue', r'data-v-[0-9a-f]{6,}|vue(\.runtime)?(\.global)?(\.prod)?\.js'), ('SvelteKit', r'/_app/immutable/|__sveltekit'),
    ('Svelte', r'class="[^"]*svelte-[a-z0-9]{5,}'), ('Angular', r'ng-version='), ('Gatsby', r'___gatsby'), ('Astro', r'astro-island|/_astro/'),
    ('Remix', r'__remixContext'), ('Vite', r'/@vite/|type="module" crossorigin src="/assets/index-'),
    ('WordPress', r'wp-content/|wp-includes/'), ('Webflow', r'data-wf-page|webflow\.js|assets\.website-files\.com'),
    ('Framer', r'framerusercontent\.com|data-framer-'), ('Squarespace', r'static1\.squarespace\.com'), ('Wix', r'static\.wixstatic\.com|wix-code'),
    ('Shopify', r'cdn\.shopify\.com'), ('Ghost', r'content="Ghost '), ('Hugo', r'content="Hugo '), ('Jekyll', r'content="Jekyll'),
    ('Three.js (3D)', r'three(\.module)?(\.min)?\.js|THREE\.WebGLRenderer'), ('GSAP (animation)', r'gsap(\.min)?\.js|ScrollTrigger'),
    ('Tailwind CSS', r'tailwind'), ('Bootstrap', r'bootstrap(\.min)?\.(css|js)'), ('jQuery', r'jquery(\.min)?\.js'),
    ('Google Analytics', r'googletagmanager\.com|google-analytics\.com'), ('Plausible', r'plausible\.io'), ('Fathom', r'usefathom\.com'),
]
HEADER_CLUES = [('server', None), ('x-powered-by', None), ('x-vercel-id', 'Vercel'), ('x-nf-request-id', 'Netlify'),
                ('cf-ray', 'Cloudflare'), ('x-github-request-id', 'GitHub Pages'), ('x-amz-cf-id', 'Amazon CloudFront'), ('fly-request-id', 'Fly.io')]
CONTACT = re.compile(r'(mailto:[\w.+-]+@[\w-]+\.[\w.-]+|tel:\+?[\d][\d\s().-]{5,}\d|https?://(?:www\.)?(?:github\.com|gitlab\.com|twitter\.com|x\.com|linkedin\.com|instagram\.com|'
                     r'youtube\.com|dribbble\.com|behance\.net|threads\.net|bsky\.app)/[^"\'\s<>)]+)', re.I)


def read_source(url):
    """The page's code as evidence: server headers, what it loads, framework clues and contact links."""
    if not re.match(r'^https?://', url):
        raise ValueError('only http and https pages can be read')
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=25) as r:
        headers = {k.lower(): v for k, v in r.headers.items()}
        raw = r.read(3_000_000).decode('utf-8', 'replace')
    out = ['Source of %s (%d characters)' % (url, len(raw))]
    hs = ['%s: %s' % (k, headers[k]) + (' (%s)' % name if name else '') for k, name in HEADER_CLUES if k in headers]
    out.append('Server headers: ' + ('; '.join(hs) if hs else 'none that name the host or software'))
    t = re.search(r'<title[^>]*>(.*?)</title>', raw, re.S | re.I)
    gen = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', raw, re.I)
    if t:
        out.append('Title: ' + text_of(t.group(1)))
    if gen:
        out.append('Generator tag: ' + gen.group(1))
    found = []
    for name, pat in FINGERPRINTS:
        m = re.search(pat, raw, re.I)
        if m:
            found.append('%s (found "%s")' % (name, m.group(0)[:60]))
    out.append('Built with, from clues in the source: ' + ('; '.join(found) if found else 'no known framework clues'))
    is_html = '<html' in raw[:3000].lower() or '<!doctype' in raw[:300].lower()
    scripts = [urllib.parse.urljoin(url, u) for u in re.findall(r'<script[^>]+src=["\']([^"\']+)', raw, re.I)]
    styles = re.findall(r'<link[^>]+rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)|<link[^>]+href=["\']([^"\']+\.css[^"\']*)', raw, re.I)
    if is_html:
        out.append('Scripts it loads (%d): %s' % (len(scripts), ', '.join(scripts[:25]) or 'none (all inline)'))
        out.append('Stylesheets: ' + (', '.join([a or b for a, b in styles][:10]) or 'none'))
        if len(text_of(re.sub(r'<(script|style)[^>]*>.*?</\1>', '', raw, flags=re.S | re.I))) < 400 and scripts:
            out.append('The page itself is almost empty: JavaScript builds it. Read its own script files (the ones on this '
                       'site) with read_page_source for what it is built with and who made it.')
    contacts = sorted({html.unescape(c).rstrip('/.,') for c in CONTACT.findall(raw)})
    out.append('Contact links in the source: ' + (', '.join(contacts[:20]) if contacts else 'none'))
    links = sorted({u for u in re.findall(r'href=["\'](/[^"\'#?]{1,60}|https?://[^"\'#?]{1,90})', raw)
                    if re.search(r'contact|about|team|imprint|legal|press|careers', u, re.I)})
    if links:
        out.append('Pages that may name the people behind it: ' + ', '.join(links[:10]))
    return '\n'.join(out)


CODE_WORDS = ['transform', 'perspective', 'rotate', 'translateZ', 'translate3d', 'preserve-3d', 'animation', 'keyframes',
              'transition', 'requestAnimationFrame', 'addEventListener', 'keydown', 'scroll', 'canvas', 'webgl', 'three', 'gsap']


def search_code(url, words):
    """Look inside one code file (a script, a stylesheet or a page): each place the words appear, with the code around
    it laid out one statement per line (shipped code is squeezed onto a few huge lines). No words: which common
    animation and 3D words the file uses, and how often, to choose what to search for."""
    if not re.match(r'^https?://', url):
        raise ValueError('only http and https files can be read')
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=30) as r:
        code = r.read(6_000_000).decode('utf-8', 'replace')
    lines = []
    for line in code.split('\n'):   # one statement or rule per line
        lines += [l.strip() for l in re.split(r'(?<=[;{}])', line) if l.strip()]
    words = [w for w in re.split(r'[,\s]+', str(words or '')) if len(w) > 1][:8]
    head = 'Code file %s: %d characters, %d statements.' % (url, len(code), len(lines))
    if not words:
        counts = ['%s %d' % (w, len(re.findall(re.escape(w), code, re.I))) for w in CODE_WORDS]
        return head + '\nHow often common words appear: ' + ', '.join(c for c in counts if not c.endswith(' 0')) + \
            '\nSearch for the ones that matter with words.'
    out, used, shown, follow = [head], set(), 0, []
    for w in words:
        # the site's own code sits after the libraries in a bundle, so the last places come first
        if w.endswith('='):   # where a (short, minified) name is written: exact name, exact case, not ==
            pat = re.compile(r'(?<![\w$.])' + re.escape(w[:-1]) + r'\s*=(?![=>])')
            hits = [i for i, l in enumerate(lines) if pat.search(l)][::-1]
        else:
            hits = [i for i, l in enumerate(lines) if w.lower() in l.lower()][::-1]
        out.append('\n"%s": %d place%s%s' % (w, len(hits), '' if len(hits) == 1 else 's', ' (6 shown, the site\'s own code first)' if len(hits) > 6 else ''))
        for i in hits[:6]:
            if i in used or shown > 24:
                continue
            block = range(max(0, i - 3), min(len(lines), i + 4))
            used.update(block)
            shown += 1
            snippet = '\n'.join(lines[j][:300] for j in block)
            out.append('--- statement %d\n%s' % (i + 1, snippet))
            # a button or key wired to a function: say how to see what that function does
            for fn in re.findall(r'\bon[A-Z]\w*:\s*([A-Za-z_$][\w$]{0,3})\b|addEventListener\("\w+",\s*([A-Za-z_$][\w$]{0,3})\b', snippet):
                name = fn[0] or fn[1]
                if name and name + '=' not in follow:
                    follow.append(name + '=')
    if follow:
        out.append('\nTo see what the functions wired to those buttons or keys do, search for: ' + ', '.join(follow[:6]) +
                   ' (a short name like "T=" is where that function is written)')
    if not any(re.search(re.escape(w.rstrip('=')), code, re.I) for w in words):
        out.append('\nNothing found. Words that usually matter for movement and 3D: ' + ', '.join(CODE_WORDS[:10]) + ', style.transform, className')
    text = '\n'.join(out)
    return text[:16000] + ('\n(cut: search for fewer or narrower words)' if len(text) > 16000 else '')


def read_code(url, start, count=120):
    """A stretch of one code file, statement by statement (as numbered by search_code), to read a part of a site's own code whole."""
    if not re.match(r'^https?://', url):
        raise ValueError('only http and https files can be read')
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=30) as r:
        code = r.read(6_000_000).decode('utf-8', 'replace')
    lines = []
    for line in code.split('\n'):
        lines += [l.strip() for l in re.split(r'(?<=[;{}])', line) if l.strip()]
    start = max(1, int(start or 1))
    part = lines[start - 1:start - 1 + max(10, min(int(count or 120), 250))]
    out, size = [], 0
    for n, l in enumerate(part, start):
        if size > 14000:
            out.append('(cut here: read from statement %d next)' % n)
            break
        out.append('%d  %s' % (n, l[:400]))
        size += len(l)
    return 'Code file %s, statements %d to %d of %d:\n%s' % (url, start, start + len(part) - 1, len(lines), '\n'.join(out))
