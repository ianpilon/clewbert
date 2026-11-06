#!/usr/bin/env python3
"""Fill the cover-photo pool: public-domain (CC0) nature photos from Openverse, each checked by the vision model
before it is kept. A post with no picture of its own gets its cover made from one of these (a hook line from the
post over the photo), so the newsletter logo never stands in for it (2026-11-06).

Usage: tools/covers/fill_pool.py <data dir> [--want 100] [--model-url URL] [--model NAME]
Pool: <data>/covers/pool/<id>.jpg + <data>/covers/pool.json {accepted: {id: {...}}, rejected: [ids]}.
Openverse allows 20 requests a minute and 200 a day without a key; this script stays under both.
"""
import base64, http.client, json, os, subprocess, sys, tempfile, time, urllib.parse, urllib.request

QUERIES = ['honeycomb pattern', 'leaf veins macro', 'sand dunes aerial', 'ice crystals pattern', 'water ripples', 'nautilus shell',
           'forest aerial', 'river delta aerial', 'tree bark texture', 'rock strata', 'moss macro', 'snowflake macro',
           'wave pattern sand', 'mountain ridge aerial', 'canyon aerial', 'frost pattern', 'coral texture', 'dandelion macro',
           'fern fronds', 'cracked earth pattern', 'lichen macro', 'basalt columns', 'salt flats aerial', 'wheat field aerial',
           'ocean waves aerial', 'pine forest fog', 'desert ridges aerial', 'fractal romanesco', 'agate pattern', 'birch forest']
UA = 'context-canvas-cover-pool/1.0'
JUDGE = ('You judge background photos for a headline. Answer with one JSON object {"ok": true or false, "why": "a few words"}. '
         'ok is true only if this is a sharp photograph of a natural pattern, texture or landscape (plants, water, rock, ice, sand, '
         'forest, coral, shells, fields) with no people, no faces, no buildings, vehicles, text, logos, watermarks, borders or '
         'drawings, and with enough calm area for two lines of white text across the middle.')


def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=40) as f:
        return json.load(f)


def download(url, path):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=60) as f, open(path, 'wb') as out:
        n = 0
        while True:
            chunk = f.read(1 << 16)
            if not chunk:
                break
            n += len(chunk)
            if n > 14 << 20:
                raise OSError('too big')
            out.write(chunk)


def judge(path, model_url, model):
    small = os.path.join(tempfile.gettempdir(), 'cover-judge.jpg')
    subprocess.run(['sips', '-Z', '1024', '-s', 'format', 'jpeg', path, '--out', small], capture_output=True, timeout=60)
    with open(small if os.path.exists(small) else path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    content = [{'type': 'text', 'text': JUDGE}, {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}}]
    body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': content}], 'temperature': 0, 'max_tokens': 80,
                       'chat_template_kwargs': {'enable_thinking': False}}).encode()
    u = urllib.parse.urlsplit(model_url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=90)
    try:
        conn.request('POST', u.path, body, {'Content-Type': 'application/json'})
        resp = conn.getresponse()
        if resp.status != 200:
            raise OSError('model answered %d' % resp.status)
        text = json.load(resp)['choices'][0]['message']['content'] or ''
    finally:
        conn.close()
    start, end = text.find('{'), text.rfind('}')
    try:
        j = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    except ValueError:
        j = {}
    return bool(j.get('ok')), str(j.get('why') or text)[:120]


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__); sys.exit(1)
    data = os.path.abspath(os.path.expanduser(args[0]))
    want = int(args[args.index('--want') + 1]) if '--want' in args else 100
    model_url = args[args.index('--model-url') + 1] if '--model-url' in args else os.environ.get('CANVAS_MODEL_URL', 'http://100.121.91.119:8888/v1/chat/completions')
    model = args[args.index('--model') + 1] if '--model' in args else os.environ.get('CANVAS_MODEL', 'nemotron-3.5-super-vl')
    covers, pool_dir = os.path.join(data, 'covers'), os.path.join(data, 'covers', 'pool')
    os.makedirs(pool_dir, exist_ok=True)
    pj = os.path.join(covers, 'pool.json')
    try:
        with open(pj) as f:
            pool = json.load(f)
    except (OSError, ValueError):
        pool = {'accepted': {}, 'rejected': []}
    seen = set(pool['accepted']) | set(pool['rejected'])

    def save():
        with open(pj + '.tmp', 'w') as f:
            json.dump(pool, f, indent=1)
        os.replace(pj + '.tmp', pj)

    calls = 0
    for q in QUERIES:
        if len(pool['accepted']) >= want:
            break
        url = 'https://api.openverse.org/v1/images/?q=%s&license=cc0&page_size=20&mature=false' % urllib.parse.quote(q)
        try:
            j = get_json(url)
        except (OSError, ValueError) as e:
            print('%s: search failed (%s)' % (q, e), flush=True); time.sleep(4); continue
        calls += 1
        for r in j.get('results') or []:
            if len(pool['accepted']) >= want:
                break
            rid, w, h = str(r.get('id')), int(r.get('width') or 0), int(r.get('height') or 0)
            if rid in seen or w < 1000 or h < 560 or w < h * 1.15 or str(r.get('license')) != 'cc0':
                continue
            seen.add(rid)
            path = os.path.join(pool_dir, rid + '.jpg')
            try:
                download(r['url'], path)
                subprocess.run(['sips', '-Z', '2000', '-s', 'format', 'jpeg', '-s', 'formatOptions', '88', path, '--out', path], capture_output=True, timeout=60)
                ok, why = judge(path, model_url, model)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
                ok, why = False, 'failed: %s' % e
            if ok:
                pool['accepted'][rid] = {'file': rid + '.jpg', 'query': q, 'source': r.get('foreign_landing_url') or r.get('url'),
                                        'creator': r.get('creator') or '', 'license': 'cc0', 'width': w, 'height': h}
                print('KEEP %s (%s): %s' % (rid, q, why), flush=True)
            else:
                pool['rejected'].append(rid)
                if os.path.exists(path):
                    os.remove(path)
                print('drop %s (%s): %s' % (rid, q, why), flush=True)
            save()
        time.sleep(4)   # 20 searches a minute at most
    print('pool: %d accepted, %d rejected, %d searches' % (len(pool['accepted']), len(pool['rejected']), calls))


if __name__ == '__main__':
    main()
