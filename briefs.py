"""Board briefs: the AI reads a whole board once, part by part, and writes one brief of it.

A question only sees ~3,000 words of a board, which is a few percent of a big one. So in the background each board is
cut into parts (one frame at a time, ~2,500 words each), the model writes short notes on every part, then combines the notes
into a brief: a summary plus main threads, people and companies, decisions, open questions. Each point keeps the
board items it came from. Kept in DATA_DIR/briefs/<board id>.json.

- The notes go up a tree: parts -> one summary per frame -> the brief. No call is sent more than CALL_WORDS of notes
  (batches are summarized first when there is more), so a board of any size fits the model (Nemotron: 65,536 tokens).
  The frame summaries are also what the voice uses to talk about, and zoom to, one frame of a board.
- Only changed parts are read again (each part has a hash of its text), so an edit costs one or two calls; a frame
  summary is redone only when one of its parts changed.
- A board is re-read a couple of minutes after its last edit, and waits until its pictures have been read.
- It pauses while questions are being asked (a home model server often answers one request at a time) and resumes after 20 s of quiet.
- Numbers in the notes and the brief must appear in what they came from; sentences or points that fail are dropped.
"""
import hashlib, http.client, json, math, os, re, threading, time, traceback
import talk

PART_WORDS = 2500
CALL_WORDS = 12000   # notes sent in one combining call (a board with 47,000 words of notes broke a 65,000-token model window)
PIECE_WORDS = 600
QUIET_SECS = 120
YIELD_SECS = 20   # a home model server often answers one thing at a time: no reading until questions have stopped this long

PART_SYSTEM = """You are reading one part of the user's canvas board (sticky notes, screenshots, PDFs) so that a brief of the
whole board can be written from everyone's notes later. Write factual notes, 60 to 160 words, on what this part holds:
its topics, the people and companies and what is said about them, decisions, commitments, dates and numbers, and open
questions. Use ONLY the numbered items. Screenshot and PDF items are words read out of pictures, so skip garbled bits.
Reply with JSON only: {"notes": "<plain sentences>", "sources": [<numbers of the items that matter most, at most 6>]}"""

FRAME_SYSTEM = """You combine notes on pieces of the user's canvas board (numbered) into one set of notes on all of them
together, 80 to 200 words: the topics, the people and companies and what is said about them, decisions, commitments,
dates and numbers, and open questions. Use ONLY the notes. Keep names and numbers exactly as written.
Reply with JSON only: {"notes": "<plain sentences>"}"""

BRIEF_SYSTEM = """You write the brief of the user's whole canvas board from notes on each of its parts (numbered).
Use ONLY the notes. Plain sentences, one per point, no markdown. Keep names and numbers exactly as in the notes.
Reply with JSON only:
{"summary": "<3 or 4 sentences: what this board is about and where it stands>",
 "sections": [
  {"title": "Main threads", "points": [{"text": "<one sentence>", "parts": [<part numbers>]}]},
  {"title": "People and companies", "points": [...]},
  {"title": "Decisions and commitments", "points": [...]},
  {"title": "Open questions", "points": [...]}]}
Give each section 3 to 8 points, or none when nothing fits. Every point names the parts it came from."""


def board_parts(board, reader):
    """The board cut into parts: items grouped by frame, read top to bottom, long items split, ~2,500 words a part."""
    items = board.get('items') or []
    areas = [i for i in items if i.get('type') == 'area']
    groups, order = {}, []
    for it in sorted(items, key=lambda i: (i.get('y', 0), i.get('x', 0))):
        kind, words = talk.item_words(it, reader)
        if not words.strip() or (kind == 'frame title' and words == 'Shape'):
            continue
        frame = (it.get('text') or '') if kind == 'frame title' else talk.frame_of(it, areas)
        if frame not in groups:
            groups[frame] = []
            order.append(frame)
        w = words.split()
        for s in range(0, len(w), PIECE_WORDS):
            groups[frame].append({'item': it['id'], 'kind': kind, 'text': ' '.join(w[s:s + PIECE_WORDS])})
    # reading order of the frames: left to right on a wide board (a timeline), top to bottom on a tall one
    xs = [i['x'] for i in items] + [i['x'] + i['w'] for i in items]
    ys = [i['y'] for i in items] + [i['y'] + i['h'] for i in items]
    wide = bool(items) and (max(xs) - min(xs)) >= (max(ys) - min(ys))
    first = {}
    for a in areas:
        if a.get('text') in groups and a.get('text') not in first:
            first[a['text']] = (a['x'], a['y'])
    for frame in order:
        if frame not in first:   # items outside any frame: where the first of them sits
            p0 = next(i for i in items if i['id'] == groups[frame][0]['item'])
            first[frame] = (p0['x'], p0['y'])
    order.sort(key=lambda f: first[f] if wide else (first[f][1], first[f][0]))
    parts = []
    for frame in order:
        cur, n, k = [], 0, 0
        for p in groups[frame]:
            pw = len(p['text'].split())
            if cur and n + pw > PART_WORDS:
                parts.append(_part(frame, k, cur))
                cur, n, k = [], 0, k + 1
            cur.append(p)
            n += pw
        if cur:
            parts.append(_part(frame, k, cur))
    return parts


def _part(frame, k, pieces):
    text = '\n'.join(p['kind'] + ': ' + p['text'] for p in pieces)
    return {'key': '%s#%d' % (frame or '(not in a frame)', k), 'frame': frame, 'pieces': pieces,
            'hash': hashlib.sha1(text.encode()).hexdigest()}


def clean_notes(text):
    """Notes saved as the model's raw JSON (before replies were repaired) back to plain sentences."""
    text = (text or '').strip()
    if text.startswith('{'):
        j = talk.parse_json(text)
        if '_text' in j:
            m = re.search(r'"notes"\s*:\s*"(.*)', text, re.S)
            return re.split(r'"\s*,\s*"sources"', m.group(1))[0].replace('\\"', '"') if m else ''
        return str(j.get('notes') or '')
    return text


def _sentences(text):
    return [x.strip() for x in re.split(r'(?<=[.!?])\s+', text or '') if x.strip()]


def yield_to_questions():
    while time.time() - talk.LAST_ASK[0] < YIELD_SECS:
        time.sleep(2)


def read_part(board_name, part):
    yield_to_questions()
    ids, lines = [], []
    for p in part['pieces']:
        if p['item'] not in ids:
            ids.append(p['item'])
        lines.append('[%d] %s: %s' % (ids.index(p['item']) + 1, p['kind'], p['text']))
    where = ('the frame "%s"' % part['frame']) if part['frame'] else 'the items outside any frame'
    out = talk.chat([{'role': 'system', 'content': PART_SYSTEM},
                     {'role': 'user', 'content': 'Board: "%s". This part is %s.\n\nITEMS:\n%s' % (board_name, where, '\n\n'.join(lines))}],
                    350, timeout=180, background=True)
    source_text = ' '.join(p['text'] for p in part['pieces'])
    notes = ' '.join(x for x in _sentences(clean_notes(out.get('notes') or out.get('_text', '')))
                     if not talk.unsupported_numbers(x, [source_text], ''))   # a number not in the part = dropped
    sources = []
    for n in out.get('sources') or []:
        try:
            i = int(n) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(ids) and ids[i] not in sources:
            sources.append(ids[i])
    return {'hash': part['hash'], 'frame': part['frame'], 'notes': notes, 'sources': sources[:6] or ids[:3]}


def summarize(board_name, where, notes):
    """One call: several notes as one set of notes on all of them (numbers not in the notes are dropped)."""
    yield_to_questions()
    lines = ['[%d] %s' % (n, x) for n, x in enumerate(notes, 1)]
    out = talk.chat([{'role': 'system', 'content': FRAME_SYSTEM},
                     {'role': 'user', 'content': 'Board: "%s". These notes are on %s.\n\nNOTES:\n%s' % (board_name, where, '\n\n'.join(lines))}],
                    500, timeout=300, background=True)
    if '_text' in out:
        raise RuntimeError('the frame reply was not readable')
    source = ' '.join(notes)
    return ' '.join(x for x in _sentences(clean_notes(out.get('notes') or '')) if not talk.unsupported_numbers(x, [source], ''))


def _batches(nodes, size):
    out, cur, n = [], [], 0
    for x in nodes:
        k = len(x['notes'].split())
        if cur and n + k > size:
            out.append(cur)
            cur, n = [], 0
        cur.append(x)
        n += k
    return out + ([cur] if cur else [])


def fold(board_name, where, notes):
    """Notes on many pieces as notes on the whole, in calls that fit: batches are summarized first when there is too much."""
    nodes = [{'notes': x} for x in notes if x]
    while len(nodes) > 1 and sum(len(x['notes'].split()) for x in nodes) > CALL_WORDS:
        batches = _batches(nodes, CALL_WORDS)
        if len(batches) == len(nodes):
            break
        nodes = [{'notes': retry_if_cut(lambda b=b: summarize(board_name, where, [x['notes'] for x in b]))} for b in batches]
    if len(nodes) == 1:
        return nodes[0]['notes']
    return retry_if_cut(lambda: summarize(board_name, where, [x['notes'] for x in nodes])) if nodes else ''


def frame_tree(board, parts, old):
    """One summary per frame, in board order: {'order': [frame names], 'frames': {name: {hash, frame, area, notes, parts}}}.
    A frame of one part keeps that part's notes; a frame is summarized again only when one of its parts changed."""
    areas = {}
    for it in board.get('items') or []:
        if it.get('type') == 'area' and it.get('text') and it['text'] not in areas:
            areas[it['text']] = it['id']
    order, members = [], {}
    for n, p in enumerate(parts):
        if p['frame'] not in members:
            members[p['frame']] = []
            order.append(p['frame'])
        members[p['frame']].append(n)
    frames = {}
    for f in order:
        idx = members[f]
        h = hashlib.sha1(' '.join(parts[i]['hash'] for i in idx).encode()).hexdigest()
        was = (old or {}).get(f) or {}
        if was.get('hash') == h and was.get('notes'):
            notes = was['notes']
        elif len(idx) == 1:
            notes = clean_notes(parts[idx[0]]['notes'])
        else:
            notes = fold(board.get('name', ''), ('the frame "%s"' % f) if f else 'the items outside any frame',
                         [clean_notes(parts[i]['notes']) for i in idx])
        frames[f] = {'hash': h, 'frame': f, 'area': areas.get(f), 'notes': notes, 'parts': idx}
    return {'order': order, 'frames': frames}


def combine(board_name, parts, tree=None):
    """The brief, from the frame summaries (or, with no tree, from the part notes). Groups of frames are summarized
    first when all of them would not fit one call. Each point cites real parts, for its Show links."""
    for p in parts:
        p['notes'] = clean_notes(p['notes'])
    if tree:
        nodes = [{'notes': tree['frames'][f]['notes'], 'label': tree['frames'][f]['frame'], 'parts': tree['frames'][f]['parts']}
                 for f in tree['order'] if tree['frames'][f]['notes']]
    else:
        nodes = [{'notes': p['notes'], 'label': p['frame'], 'parts': [n]} for n, p in enumerate(parts) if p['notes']]
    while len(nodes) > 1 and sum(len(x['notes'].split()) for x in nodes) > CALL_WORDS:
        batches = _batches(nodes, CALL_WORDS)
        if len(batches) == len(nodes):
            break
        nodes = [{'notes': retry_if_cut(lambda b=b: summarize(board_name, 'several frames', [x['notes'] for x in b])),
                  'label': '', 'parts': [i for x in b for i in x['parts']]} for b in batches]
    lines = ['[%d]%s %s' % (n, (' (frame "%s")' % x['label']) if x['label'] else '', x['notes']) for n, x in enumerate(nodes, 1)]
    if not lines:
        return None
    yield_to_questions()
    out = talk.chat([{'role': 'system', 'content': BRIEF_SYSTEM},
                     {'role': 'user', 'content': 'Board: "%s"\n\nNOTES ON EACH PART:\n%s' % (board_name, '\n\n'.join(lines))}],
                    2500, timeout=300, background=True)
    if '_text' in out:   # still not JSON after repair: never show the raw reply, try again later
        raise RuntimeError('the brief reply was not readable')
    all_notes = ' '.join(x['notes'] for x in nodes)
    summary = ' '.join(x for x in _sentences(out.get('summary') or '')
                       if not talk.unsupported_numbers(x, [all_notes], ''))
    sections = []
    for sec in out.get('sections') or []:
        points = []
        for pt in sec.get('points') or []:
            cited = []
            for n in pt.get('parts') or []:
                try:
                    i = int(n) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= i < len(nodes) and i not in cited:
                    cited.append(i)
            nums = sorted({p for i in cited for p in nodes[i]['parts']})
            text = str(pt.get('text') or '').strip()
            cited_notes = ' '.join(nodes[i]['notes'] for i in cited) or all_notes
            if not text or talk.unsupported_numbers(text, [cited_notes], ''):
                continue
            items = []
            for i in nums:
                for s in parts[i]['sources']:
                    if s not in items:
                        items.append(s)
            points.append({'text': text, 'items': items[:6], 'parts': nums, 'frames': sorted({parts[i]['frame'] for i in nums if parts[i]['frame']})})
        sections.append({'title': str(sec.get('title') or ''), 'points': points})
    return {'summary': summary, 'sections': sections}


def _words(text):
    """Search words, with dotted and dashed ones also split (so "Mainichi" meets "mainichi.jp")."""
    out = set()
    for t in talk.tokens(text):
        out.add(t)
        out.update(x for x in re.split(r'[.\-/]', t) if len(x) > 2 and x not in talk.STOP)
    return out


def attribute(brief, parts):
    """Point each brief point at its own items: the items (in the parts it cites) whose words best match the point.
    The model only cites parts, and a part can hold dozens of items, so this is done here, by word overlap."""
    item_words, item_part = {}, {}
    for n, part in enumerate(parts):
        for p in part['pieces']:
            item_words.setdefault(p['item'], set()).update(_words(p['text']))
            item_part.setdefault(p['item'], set()).add(n)
    df = {}
    for ws in item_words.values():
        for w in ws:
            df[w] = df.get(w, 0) + 1
    total = len(item_words) or 1
    for sec in brief.get('sections') or []:
        for pt in sec.get('points') or []:
            want = _words(pt['text'])
            cited = set(pt.get('parts') or [])
            scored = []
            for item, ws in item_words.items():
                if cited and not (item_part[item] & cited):
                    continue
                score = sum(math.log(1 + total / df[w]) for w in want & ws)
                if score > 0:
                    scored.append((score, item))
            scored.sort(reverse=True)
            best = scored[0][0] if scored else 0
            pt['items'] = [item for score, item in scored[:3] if score >= best * .6] or pt.get('items', [])[:3]
    return brief


def retry_if_cut(fn):
    """Run a reading call again when a question cut it off; any other failure is a real error."""
    for _ in range(50):
        try:
            return fn()
        except (OSError, ValueError, http.client.HTTPException):
            if time.time() - talk.LAST_ASK[0] > YIELD_SECS + 10:
                raise
    raise OSError('kept being cut off')


class Briefer:
    """One background reader for all boards. touch() after an edit, now() after an import."""

    def __init__(self, data_dir, reader, read_board, board_ids):
        self.dir = os.path.join(data_dir, 'briefs')
        os.makedirs(self.dir, exist_ok=True)
        self.reader, self.read_board, self.board_ids = reader, read_board, board_ids
        self.touched, self.queue, self.lock, self.wake = {}, [], threading.Lock(), threading.Event()
        threading.Thread(target=self.run, daemon=True).start()

    def path(self, bid):
        return os.path.join(self.dir, bid + '.json')

    def load(self, bid):
        try:
            with open(self.path(bid)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save(self, bid, state):
        with open(self.path(bid) + '.tmp', 'w') as f:
            json.dump(state, f)
        os.replace(self.path(bid) + '.tmp', self.path(bid))

    def view(self, bid):
        """What the page shows: the brief, progress, and the notes on each part read so far, in board order
        (consecutive parts of one frame joined), so the reading can be followed as it happens."""
        st = self.load(bid)
        if not st:
            return {'state': 'waiting', 'notes': []}
        parts = st.pop('parts', {}) or {}
        st.pop('tree', None)
        notes = []
        for key in st.pop('order', None) or list(parts):
            p = parts.get(key)
            text = clean_notes((p or {}).get('notes'))
            if not text:
                continue
            if notes and notes[-1]['frame'] == p['frame']:
                notes[-1]['text'] += ' ' + text
                notes[-1]['items'] += [i for i in p['sources'] if i not in notes[-1]['items']]
            else:
                notes.append({'frame': p['frame'], 'text': text, 'items': list(p['sources'])})
        st['notes'] = notes
        # name every item a Show link points at, so the panel can say what each point came from
        wanted = {i for n in notes for i in n['items']}
        for sec in (st.get('brief') or {}).get('sections') or []:
            for pt in sec.get('points') or []:
                wanted.update(pt.get('items') or [])
        st['items'] = {}
        try:
            board = self.read_board(bid)
        except FileNotFoundError:
            return st
        for it in board.get('items') or []:
            if it['id'] in wanted:
                kind, words = talk.item_words(it, self.reader)
                label = (it.get('label') or ' '.join(words.split()[:14])).strip()
                st['items'][it['id']] = {'kind': kind, 'label': label[:110] or kind}
        return st

    def forget(self, bid):
        self.touched.pop(bid, None)
        try:
            os.remove(self.path(bid))
        except OSError:
            pass

    def touch(self, bid):
        self.touched[bid] = time.time()

    def now(self, bid):
        with self.lock:
            if bid not in self.queue:
                self.queue.append(bid)
        self.wake.set()

    def next_board(self):
        with self.lock:
            if self.queue:
                return self.queue.pop(0)
        due = [b for b, t in list(self.touched.items()) if time.time() - t > QUIET_SECS]
        if due:
            self.touched.pop(due[0], None)
            return due[0]
        return None

    def run(self):
        for bid in self.board_ids():   # at start: catch up on every board (unchanged ones cost nothing)
            self.now(bid)
        while True:
            bid = self.next_board()
            if not bid:
                self.wake.wait(15)
                self.wake.clear()
                continue
            try:
                self.brief(bid)
            except Exception:
                traceback.print_exc()
                st = self.load(bid) or {}
                st.update(state='error', error_at=int(time.time() * 1000))
                self.save(bid, st)
                self.touched[bid] = time.time()   # try again after the quiet period

    def brief(self, bid):
        try:
            board = self.read_board(bid)
        except FileNotFoundError:
            return
        names = {talk.image_name(i.get('src')) for i in board.get('items') or []} | {talk.image_name(i.get('url')) for i in board.get('items') or []}
        names.discard(None)
        if self.reader.unread(names):   # its pictures are still being read: come back later
            self.touched[bid] = time.time() - QUIET_SECS + 20
            return
        parts = board_parts(board, self.reader)
        old = self.load(bid) or {}
        old_parts = old.get('parts') or {}
        keep = {p['key']: old_parts[p['key']] for p in parts if old_parts.get(p['key'], {}).get('hash') == p['hash']}
        todo = [p for p in parts if p['key'] not in keep]
        if not todo and old.get('brief') is not None and set(old_parts) == set(keep) and old.get('state') == 'ready' and old.get('v') == 3:
            order = [p['key'] for p in parts]
            if old.get('order') != order:   # same parts in a new order: fix the Show links and note order, no reading needed
                attribute(old['brief'], parts)
                old.update(order=order)
                self.save(bid, old)
            return
        state = {'parts': keep, 'brief': old.get('brief'), 'state': 'reading', 'done': len(keep), 'total': len(parts),
                 'read_at': old.get('read_at'), 'order': [p['key'] for p in parts], 'v': old.get('v'), 'tree': old.get('tree')}
        self.save(bid, state)
        for p in todo:
            state['parts'][p['key']] = retry_if_cut(lambda: read_part(board.get('name', ''), p))
            state['done'] += 1
            self.save(bid, state)
        ordered = [state['parts'][p['key']] for p in parts]
        state['tree'] = frame_tree(board, ordered, (old.get('tree') or {}).get('frames'))
        self.save(bid, state)
        state['brief'] = retry_if_cut(lambda: combine(board.get('name', ''), ordered, state['tree'])) if ordered else {'summary': '', 'sections': []}
        attribute(state['brief'], parts)
        state.update(state='ready', read_at=int(time.time() * 1000), v=3)
        self.save(bid, state)
