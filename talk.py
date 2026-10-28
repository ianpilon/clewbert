"""Talking to boards: the words on each board, a search over them, and the model that answers from them.

- Every image and PDF is read once, in the background, by bin/textof (Apple's on-device text recognition);
  the words are kept in DATA_DIR/text/<image name>.txt, so a question never waits on reading pictures.
- A question is answered only from board items: the closest matches go to the model as numbered items, the model
  answers in JSON and names the items it used, and code checks every number in the answer against those items.
- Every question also gets a map of the user's spaces and boards (layout_entry), so questions about the layout itself
  (how many spaces, what is in one, which board has something) can be answered from any level.
- Voice in and voice out: with an OpenAI or xAI key, that provider hears and speaks (models.py). Or your own services:
  speech-to-text at /ears (a WebSocket next to the app) and a text-to-speech server at CANVAS_TTS_URL.
"""
import base64, hashlib, http.client, json, math, os, queue, re, socket, subprocess, threading, time, urllib.parse, urllib.request
import models

MODEL_URL = os.environ.get('CANVAS_MODEL_URL', '')   # a chat/completions address on your own machine (Ollama, LM Studio, vLLM...), or pick a provider in Settings
MODEL = os.environ.get('CANVAS_MODEL', '')
TTS_URL = os.environ.get('CANVAS_TTS_URL', '')   # your own text-to-speech server; without one, speech uses the OpenAI or xAI key
# optional: Cloudflare Access in front of the app (your team name and the app's audience tag) tells the ears who is talking
ACCESS_CERTS = ('https://%s.cloudflareaccess.com/cdn-cgi/access/certs' % os.environ['CF_ACCESS_TEAM']) if os.environ.get('CF_ACCESS_TEAM') else ''
ACCESS_AUD = os.environ.get('CF_ACCESS_AUD', '')
BUDGET_WORDS = 3000          # board text sent with one question
FRAME_MAX, FRAME_FULL, FRAME_GIST_WORDS = 80, 2, 12   # frames of one board sent with a question: all as a gist, the 2 asked about whole
CHUNK_WORDS = 120
IMAGE_REF = re.compile(r'api/images/([0-9a-f]{64}\.(?:jpg|png|pdf))')
STOP = set('a an the and or but if of to in on at by for with from about into over is are was were be been being it its '
           'this that these those there here what which who whom whose when where why how do does did done have has had '
           'i me my we our you your he she they them his her their can could would should will shall may might must '
           'any some all no not on off up down out so than then too very just also tell say said know board boards '
           'please thing things anything something'.split())


class Reader:
    """Reads the words out of every image and PDF once and keeps them next to the data."""

    def __init__(self, images_dir, text_dir, tool):
        self.images, self.text, self.tool = images_dir, text_dir, tool
        os.makedirs(text_dir, exist_ok=True)
        self.queue = queue.Queue()
        if os.path.exists(tool):
            threading.Thread(target=self.run, daemon=True).start()
            for name in sorted(os.listdir(images_dir)):
                self.add(name)

    def path(self, name):
        return os.path.join(self.text, name + '.txt')

    def add(self, name):
        if IMAGE_REF.search('api/images/' + name) and not os.path.exists(self.path(name)):
            self.queue.put(name)

    def unread(self, names):
        return sum(1 for n in names if not os.path.exists(self.path(n)))

    def words(self, name):
        try:
            with open(self.path(name)) as f:
                return f.read()
        except OSError:
            return ''

    def run(self):
        while True:
            name = self.queue.get()
            if os.path.exists(self.path(name)):
                continue
            try:
                r = subprocess.run([self.tool, os.path.join(self.images, name)], capture_output=True, text=True, timeout=300)
                words = r.stdout.strip() if r.returncode == 0 else ''
            except (OSError, subprocess.TimeoutExpired):
                words = ''
            with open(self.path(name) + '.tmp', 'w') as f:
                f.write(words)
            os.replace(self.path(name) + '.tmp', self.path(name))


def image_name(ref):
    m = IMAGE_REF.search(ref or '')
    return m.group(1) if m else None


COUNT_NAMES = {'sticky note': 'sticky notes', 'text': 'text items', 'picture': 'pictures', 'PDF': 'PDFs', 'video': 'videos',
               'audio': 'sound clips', 'file': 'files', 'link': 'links', 'frame': 'frames', 'drawing': 'marker drawings',
               'sticker': 'stickers', 'arrow': 'arrows', 'web': 'web pages', 'shot': "agent browser screenshots"}


def board_contents(b):
    """Exact numbers of each kind of item: the items the voice sees are only the ones that match the question."""
    n, seen, copies = {}, set(), {}
    for it in b.get('items', []):
        t = it.get('type')
        k = {'area': 'frame', 'ink': 'drawing', 'emoji': 'sticker', 'link': 'link', 'arrow': 'arrow', 'file': 'file',
             'web': 'web', 'shot': 'shot'}.get(t)
        if t == 'note':
            k = 'text' if it.get('color') == 'none' else 'sticky note'
        elif t == 'img':
            k = 'PDF' if str(it.get('url') or '').endswith('.pdf') else 'picture'
        elif t == 'media':
            k = 'audio' if it.get('kind') == 'audio' else 'video'
        if k:
            n[k] = n.get(k, 0) + 1
            f = it.get('url') if k == 'PDF' else it.get('src') if k in ('picture', 'video', 'audio', 'file') else None
            if f and f in seen:   # the same file again: a copy (often hidden under the first)
                copies[k] = copies.get(k, 0) + 1
            elif f:
                seen.add(f)
    parts = []
    for k, v in sorted(n.items(), key=lambda kv: -kv[1]):
        c = copies.get(k, 0)
        parts.append('%d %s' % (v, k if v == 1 else COUNT_NAMES[k]) +
                     (' (%d different; %s)' % (v - c, '1 is a copy of one already on the board' if c == 1 else
                                              '%d are copies of ones already on the board' % c) if c else ''))
    return 'This board holds exactly: ' + (', '.join(parts) if parts else 'nothing yet') + '.'


def item_words(it, reader):
    """(kind, words) for one board item, as the model will see it."""
    t = it.get('type')
    if t == 'note':   # the text tool makes a note with no colour: words on the board, not a sticky note
        return 'text' if it.get('color') == 'none' else 'sticky note', it.get('text') or ''
    if t == 'area':
        return 'frame title', it.get('text') or ''
    if t == 'link':
        u = it.get('unfurl') or {}
        return 'link', ' — '.join(x for x in (u.get('title'), u.get('desc'), it.get('url')) if x)
    if t == 'img':
        pdf = image_name(it.get('url')) if str(it.get('url') or '').endswith('.pdf') else None
        if pdf:
            return 'PDF', ((it.get('label') or 'PDF') + '\n' + reader.words(pdf)).strip()
        words = reader.words(image_name(it.get('src')) or '')
        if it.get('url'):
            words += '\nLink on it: ' + it['url']
        return 'screenshot', words.strip() or (it.get('label') or 'A picture with no words in it.')   # a photo is still findable
    if t == 'emoji':
        return 'sticker', it.get('ch') or ''
    if t == 'web':
        return ('working demo' if it.get('demo') else 'web page on the board'), (it.get('label') or '') + ' ' + (it.get('url') or '')
    if t == 'team':   # the onboarding team card (guide.py)
        c = it.get('card') or {}
        return 'team card', ('Your first team, mission: a one-page plan for your first week. ' + '; '.join(
            '%s: %s' % (m['name'], m.get('job', '')) for m in ([c['captain']] if c.get('captain') else []) + (c.get('helpers') or []))
            ) if c else 'A button that starts Clewbert guiding you through building your first agent team, out loud.'
    if t == 'shot':
        return 'agent browser view', 'The agent\'s browser, last on ' + (it.get('label') or 'a page')
    if t == 'media':
        return it.get('kind') or 'video', it.get('label') or ''
    if t == 'file':
        name = str(it.get('src') or '').rsplit('/', 1)[-1]
        return 'file', ((it.get('label') or 'file') + '\n' + reader.words(name)).strip()
    return t or 'item', ''


def frame_of(it, areas):
    """Title of the smallest frame the item sits in."""
    cx, cy = it['x'] + it['w'] / 2, it['y'] + it['h'] / 2
    inside = [a for a in areas if a is not it and a['x'] <= cx <= a['x'] + a['w'] and a['y'] <= cy <= a['y'] + a['h']]
    inside.sort(key=lambda a: a['w'] * a['h'])
    return next((a.get('text') for a in inside if a.get('text') and a.get('text') != 'Shape'), '')


def inside(f, i):
    """An item belongs to a frame when its middle is inside it (frames and arrows are not its items)."""
    return (i.get('type') not in ('area', 'arrow') and f['x'] <= i['x'] + i['w'] / 2 <= f['x'] + f['w']
            and f['y'] <= i['y'] + i['h'] / 2 <= f['y'] + f['h'])


def stacks(items):
    """Items stacked in one column, each right under the one before (a sticky note with its link card), as lists,
    top item first, in reading order."""
    below = {}
    for a in items:
        for b in items:
            if b is not a and abs(b['x'] - a['x']) < 16 and abs(b['w'] - a['w']) < 4 and 0 <= b['y'] - (a['y'] + a['h']) <= 16:
                below[a['id']] = b
                break
    under = {b['id'] for b in below.values()}
    out = []
    for a in sorted((i for i in items if i['id'] not in under), key=lambda i: (round(i['y']), i['x'])):
        s = [a]
        while s[-1]['id'] in below and len(s) < 20:
            s.append(below[s[-1]['id']])
        out.append(s)
    return out


def tokens(s):
    """Words for matching; a plural matches its singular ("PDFs" finds a PDF, "photos" a photo)."""
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9'&.-]*[a-z0-9]|[a-z0-9]", s.lower()):
        if w in STOP:
            continue
        if len(w) > 3 and w.endswith('s') and not w.endswith(('ss', 'us', 'is')):
            w = w[:-1]
        out.append(w)
    return out


# words in a question that name a kind of item: those items always go to the model (up to KIND_MAX), so "is there a
# PDF?" then "zoom into it" can show it even when no other word of the question matches it
KIND_WORDS = {'pdf': 'PDF', 'picture': 'screenshot', 'photo': 'screenshot', 'image': 'screenshot', 'screenshot': 'screenshot',
              'pic': 'screenshot', 'sticky': 'sticky note', 'note': 'sticky note', 'stickie': 'sticky note', 'text': 'text',
              'heading': 'text', 'file': 'file', 'markdown': 'file', 'md': 'file', 'document': 'file', 'doc': 'file',
              'video': 'video', 'audio': 'audio', 'sound': 'audio', 'link': 'link', 'frame': 'frame title',
              'sticker': 'sticker', 'emoji': 'sticker', 'demo': 'working demo'}
KIND_MAX = 8


def passages(boards, reader):
    """Every board item cut into short passages, each pointing back at its item."""
    out = []
    for b in boards:
        items = b.get('items') or []
        areas = [i for i in items if i.get('type') == 'area']
        for it in items:
            kind, words = item_words(it, reader)
            if not words.strip():
                continue
            frame = frame_of(it, areas) if kind != 'frame title' else ''
            w = words.split()
            for n, start in enumerate(range(0, len(w), CHUNK_WORDS)):
                out.append({'board': b['id'], 'boardName': b.get('name', ''), 'item': it['id'], 'kind': kind,
                            'frame': frame, 'part': n, 'text': ' '.join(w[start:start + CHUNK_WORDS])})
    return out


def rank(question, history, chunks):
    """BM25 over the passages; the last question in the conversation counts a little, for follow-ups."""
    q = tokens(question)
    q_prev = tokens(history[-1]['q']) if history else []
    docs = [tokens(c['text'] + ' ' + c['frame'] + ' ' + c['kind']) for c in chunks]
    n = len(docs) or 1
    avg = sum(len(d) for d in docs) / n or 1
    df = {}
    for d in docs:
        for w in set(d):
            df[w] = df.get(w, 0) + 1

    def score(d, terms, weight):
        s = 0.0
        for w in set(terms):
            f = d.count(w)
            if f:
                idf = math.log(1 + (n - df[w] + .5) / (df[w] + .5))
                s += weight * idf * f * 2.2 / (f + 1.2 * (.25 + .75 * len(d) / avg))
        return s

    scored = [(score(d, q, 1) + score(d, q_prev, .35), i) for i, d in enumerate(docs)]
    # ties (and broad questions with no matches) keep board order, so a summary sees the start of every item
    scored.sort(key=lambda x: (-x[0], chunks[x[1]]['part'], x[1]))
    return [chunks[i] for _, i in scored]


def context(question, history, boards, reader):
    """The numbered items sent with the question, plus a map back to (board, item)."""
    chunks = passages(boards, reader)
    picked, words = [], 0
    # short notes and frame titles always go first when they fit: they carry the board's own wording
    short = [c for c in chunks if c['kind'] in ('sticky note', 'text', 'frame title') and c['part'] == 0]
    if sum(len(c['text'].split()) for c in short) <= BUDGET_WORDS // 2:
        picked, words = short[:], sum(len(c['text'].split()) for c in short)
    named = {KIND_WORDS[w] for w in tokens(question) + (tokens(history[-1]['q']) if history else []) if w in KIND_WORDS}
    for c in [c for c in chunks if c['kind'] in named and c['part'] == 0][:KIND_MAX]:
        if c not in picked:
            picked.append(c)
            words += len(c['text'].split())
    for c in rank(question, history, chunks):
        if words >= BUDGET_WORDS:
            break
        if c in picked:
            continue
        picked.append(c)
        words += len(c['text'].split())
    # one numbered entry per item, its passages joined in order
    entries, index = [], {}
    for c in sorted(picked, key=lambda c: (c['board'], c['item'], c['part'])):
        key = (c['board'], c['item'])
        if key not in index:
            index[key] = len(entries)
            entries.append({'board': c['board'], 'boardName': c['boardName'], 'item': c['item'], 'kind': c['kind'],
                            'frame': c['frame'], 'text': c['text']})
        else:
            entries[index[key]]['text'] += ' … ' + c['text']
    return entries


SYSTEM = """You are Clewbert. You answer the user's questions about their canvas boards (Miro-style boards of sticky notes, screenshots and PDFs).
HOW THE APP IS ORGANISED: spaces are folders that hold boards (a board not in any space is "unsorted"); boards hold
items (notes, text, screenshots, PDFs, links, files, stickers, drawings); frames are titled areas that group items on
a board. the user can talk from the list of all boards, from one space, or from inside one board. The list of all boards
is the top level and the starting place: it shows every space. the user may call it the meta level, the top level, home,
the main page or all spaces. The "map of spaces and
boards" item lists every space and board with item counts, last edit dates and what each board is about, and says
where the user is now: use it for questions about the spaces and boards themselves (how many, which ones, what is in a
space, which board was edited last) and to point the user to the right space and board when they look for something
("That is on the Launch Plan board in the Marketing space, in the Ideas frame"). When the other items
do not hold what they look for, use the board descriptions in the map to say which board most likely has it, and say
that it is a likely place, not a sure one.
Use ONLY the numbered items below. A "board brief" item is a summary of the WHOLE board written after reading all of
it: use it for broad questions (what is this board about, review it, main themes, people, decisions, what is missing,
next steps) and the other items for details. A "sticky note" item is a sticky note; a "text" item is words typed
straight on the board (a heading or a paragraph), not a sticky note. For "how many" questions about one board, use the
numbers in the "board contents" item: the other items are only the ones that match the question. Between the two are the board's frames: a "frame gist" item says in a line
what one frame holds, a "frame summary" item sums up a whole frame. Use them to say what is where on the board and for
questions about one frame or part of the board; when the user asks to go into, dive into or show a frame, put that frame
item's number in "show" (the board zooms to the frame) and tell him what it holds. To go deeper ("more on that"), use
the other items in that frame and say something new, not what you already said. When he asks what frames or parts a
board has, never read them all out: say how many there are and name the three or four that matter most. Screenshot and PDF items are the words read out of the picture, so they can
have broken lines or small reading mistakes.
Your answer is spoken aloud: plain sentences, no lists, no markdown, at most four short sentences, and say numbers the
way they are written in the items. If the items do not answer the question, say plainly that you could not find it on
the board(s); never guess and never use outside knowledge.
Answer only about what they asked for. When they name a kind of thing ("YouTube links", "the PDF"), talk about and list
in "sources" only the items that match; never describe the other items to rule them out ("the other link is GitHub").

WHAT THIS APP CAN DO (you control it through "show" and "view"; never say you cannot do these):
- Show items: when the user asks to zoom in on, show, go to, find, point at or look at something, put its item number(s) in
  "show". The board zooms in close on them and outlines them, opening the right board first if needed. Then answer
  briefly, for example "Here is the Mainichi article screenshot." Use "show" only when the user asks to see something.
- "view": "fit" shows the whole board, "in" or "out" zooms one step, "brief" opens the board brief panel.
- "open": the name of a space or board, exactly as written in the map, to open it when the user asks to go to, open or be
  taken to a space or a whole board ("take me to the Research space"). "All boards" opens the list of all boards:
  use it when the user asks to go back out, go up, go home or go to the top or meta level. "Back to the space" means the
  space the current board is in.
- "task": inside a board, when the user asks for agent work ("have the team find...", "have an agent find...", "start a
  task to..."), put the task in their own words in "task" and say in one sentence that it started. The board's team (a
  Lead who hands parts to teammates) does it in the background and puts results in new frames on the board.
  The team can also sort what is on the board: read every item of a frame and move items into new titled groups
  (rearrange, regroup, sort into themes). Only an agent task's steps say whether items really moved: never say the
  board was rearranged unless its "agent task" item says items were grouped. Set
  "task_mode" to "one" only when the user asks for a single agent ("just one agent", "an agent on its own"), else "team".
- "agent task" items are agent tasks on the boards: what was asked, its state (queued, working, team = teammates
  working, waiting = a question for the user, done, stopped, failed), each teammate's part and, once it ended, what it put on
  the board. Use them for "how is it going", "is it done", "what did they find", "read me the results". To show a task,
  put its item number in "show" (that opens its frame).
- "follow_up": when the user asks for more on a task that ended ("also get their phone numbers", "yes, have them also..."),
  put {"task": <its item number>, "text": "<what they want, in their words>"} in "follow_up" and say it is on it
  (the team when the task has teammates, else the agent).
  The same team carries on in the same frame. If they do not say which task, use the one that ended most recently.
- "steer": when the user gives DIRECTION to a task that is still running ("tell the lead to use the developer", "have them
  read the code instead", "tell the researcher to talk to the developer", "try another approach"), put
  {"task": <its item number>, "text": "<their direction>"} in "steer" and say the Lead is re-planning. Teammates cannot
  talk to each other: only the Lead hands out work, so who-does-what goes to "steer", never to "agent_answer".
- "agent_answer": each "agent question" item is a question a teammate is waiting on (it says who asks). When the user
  answers one, put {"question": <its item number>, "text": "<their answer>"} in "agent_answer" and say you passed it on.
  When they ask what is waiting on them, read the questions out with who asks each.
- You do not browse yourself, but agent tasks DO: they search the web, read pages and read a page's source code. So
  any request that needs the web, a website, a browser or a site's code ("look it up", "check their site", "inspect
  the source", "use the browser") is agent work, never "I can't": if it is about a task that already ended, it is a
  "follow_up" of that task, else a new "task". Do not search the board for it first.
- A WORKING DEMO is only a "working demo" item: a live card that runs. Code pasted onto the board as notes or text is
  NOT a demo. When the user wants to see something running ("build a demo", "show me it working"), and there is no working
  demo item for it, that is agent work: a "follow_up" of the task it belongs to (or a new "task"), asking the team to
  build a working demo page. Never tell them to copy code into a file.
- "open_item": when they ask to OPEN a link, PDF, file or picture that is on the board ("open the YouTube link", "open
  the quick guide"), put that item's number in "open_item" and say you are opening it in a new tab. If more than one
  item could be meant ("open the link" with two links), leave it null and ask which one, naming them. Never use
  "web_card" for something already on the board: opening it is "open_item".
- "web_card": when the user wants to SEE a website himself ("open that site so we can look at it", "pull up their site"),
  put its address (from the items, or one they say) in "web_card": the live page opens as a card on the board that they
  can scroll and click. Say it is on the board.
- "add_teammate": inside a board, when the user asks to add someone to the team ("add a developer", "we need a designer"),
  put {"name": "<short role name>", "role": "<what they do, in plain words>"} in "add_teammate". Starter roles you can
  name: Researcher, Writer, Analyst, Developer (reads a site's code to find how something works), Fact-checker.
- Rearranging cards in a frame, YOURSELF, right away (no agent task): the "frame layout" items list a frame's cards
  numbered by place (place 1 is top left, then left to right, row by row; "rows of 5" means places 6 to 10 are row 2).
  "move_card": {"frame": <the frame layout's item number>, "from": <place>, "to": <place>} moves one card to another
  place; the cards between shift along. "The fourth one in the top row to be first" is from 4 to 1.
  "sort_frame": {"frame": <the frame layout's item number>} sorts the frame's cards into themes, each under a heading
  ("sort", "group", "organise", "restructure" or "rearrange the frame into themes or topics").
  "undo_edit": true puts back the last move or sort ("put it back", "undo that").
  "card": {"frame": <the frame layout's item number>, "place": <place>, "under": true or false, "open": true or false}
  ("under": true for the card stacked under the sticky note: "the card below that", "the link under it") when the user asks to show,
  go to, zoom to or open a card by its place ("open the second card", "show me the last one in the top row"). The app
  zooms to it, and with "open": true also opens its link in a new tab. Never use "view" or "show" for this.
  The app says what it moved, so keep "answer" short; if no frame layout item is the frame meant, ask which frame.
- It CANNOT edit the words of items, delete or create items (only agent tasks create them, and web_card), change
  colours, or open tabs other than "open_item". If asked, say so plainly and say what you can do instead. Never claim
  to have done anything you did not put in this JSON.
Reply with JSON only: {"answer": "<what to say>", "sources": [<item numbers that answer the question, outlined for the user; not ones you mention only to rule them out>], "show": [<item numbers to show, or empty>], "view": null, "open": null, "task": null, "task_mode": "team", "agent_answer": null, "follow_up": null, "web_card": null, "open_item": null, "add_teammate": null, "steer": null, "move_card": null, "sort_frame": null, "undo_edit": null, "card": null}"""


def ask_model(question, history, entries, scope):
    lines = []
    for n, e in enumerate(entries, 1):
        many = scope == 'all' or scope.startswith('space:')
        where = ' | '.join(x for x in (('space: ' + e['spaceName']) if many and e.get('spaceName') else '', ('board: ' + e['boardName']) if many and e['boardName'] else '', ('in frame: ' + e['frame']) if e['frame'] else '') if x)
        lines.append('[%d] %s%s\n%s' % (n, e['kind'], (' (' + where + ')') if where else '', e['text']))
    msgs = [{'role': 'system', 'content': SYSTEM}]
    for h in history[-3:]:
        msgs += [{'role': 'user', 'content': h['q']}, {'role': 'assistant', 'content': json.dumps({'answer': h['a'], **(h.get('did') or {})})}]
    msgs.append({'role': 'user', 'content': '%s\n\nBOARD ITEMS:\n%s\n\nQUESTION: %s' % (scope_line(scope), '\n\n'.join(lines), question)})
    out = chat(msgs, 500)
    return (str(out.get('answer') or out.get('_text') or '').strip(), out.get('sources') or [], out.get('show') or [], out.get('view'),
            out.get('open'), out.get('task'), out.get('agent_answer'), out.get('task_mode'), out.get('follow_up'),
            out.get('web_card'), out.get('add_teammate'), out.get('steer'), out.get('open_item'),
            {k: out.get(k) for k in ('move_card', 'sort_frame', 'undo_edit', 'card')})


BACKGROUND = {'conn': None}   # the brief reader's call in flight, cut when a question arrives


def interrupt_background():
    conn = BACKGROUND['conn']
    if conn is not None and conn.sock is not None:
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)   # the model server drops the request when its caller hangs up
        except OSError:
            pass


def chat(msgs, max_tokens, timeout=90, background=False):
    """One model call that should come back as JSON; {'_text': ...} when it does not."""
    if not models.choice('voice') and not models.local('voice') and not MODEL_URL:
        raise models.NoModel('voice')
    if models.choice('voice'):   # an outside provider picked in Settings
        return parse_json(models.complete('voice', {'messages': msgs, 'max_tokens': max_tokens}, timeout)['choices'][0]['message']['content'] or '')
    url, model = models.local('voice') or (MODEL_URL, MODEL)   # picked in Settings, else as set at startup
    body = json.dumps({'model': model, 'messages': msgs, 'temperature': .2, 'max_tokens': max_tokens,
                       'chat_template_kwargs': {'enable_thinking': False}}).encode()
    u = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    if background:
        BACKGROUND['conn'] = conn
    try:
        conn.request('POST', u.path, body, {'Content-Type': 'application/json'})
        resp = conn.getresponse()
        if resp.status != 200:
            raise OSError('model answered %d' % resp.status)
        text = json.load(resp)['choices'][0]['message']['content']
    finally:
        if background:
            BACKGROUND['conn'] = None
        conn.close()
    return parse_json(text)


def parse_json(text):
    """The JSON object in a model reply, repaired when the model stopped before its closing brackets."""
    start = text.find('{')
    if start < 0:
        return {'_text': text.strip()}
    body = text[start:].strip()
    end = body.rfind('}')
    for candidate in (body[:end + 1] if end >= 0 else body, body):
        try:
            return json.loads(candidate)
        except ValueError:
            pass
    stack, in_str, esc = [], False, False   # close whatever is still open
    for ch in body:
        if in_str:
            esc = not esc and ch == '\\'
            if ch == '"' and not esc:
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in '{[':
            stack.append('}' if ch == '{' else ']')
        elif ch in '}]' and stack:
            stack.pop()
    try:
        return json.loads(body + ('"' if in_str else '') + ''.join(reversed(stack)))
    except ValueError:
        return {'_text': text.strip()}


def scope_line(scope):
    if scope == 'all':
        return 'The question is about ALL of the user\'s boards.'
    if scope.startswith('space:'):
        return 'The question is about the boards in the user\'s space "%s".' % scope[6:]
    return 'The question is about the board "%s".' % scope


SEE = re.compile(r"\b(zoom|show|go to|go back|back out|back up|head back|return to|take (me|us)|exit|leave|go home|bring up|pull up|find|where('?s| is| are)|point (at|to|out)|look at|let me see|display|open|focus|jump|highlight|fit|whole board|full board|go into|dive into|drill)\b", re.I)
DEMO = re.compile(r"\b(demo|prototype|working (?:page|version|example)|see it (?:running|working|in action)|show me it working|build (?:a|me a|it as a) (?:page|site|web ?page))\b", re.I)
YES = re.compile(r"^\W*(yes|yeah|yep|sure|ok(ay)?|please( do)?|do it|go ahead|go for it|open it|show me)\b", re.I)
OFFER = re.compile(r"\b(open|show|take you)\b[^?]*\?\s*$", re.I)   # an answer that ends by offering to open or show something
# the top level by the user's own names for it: opened even when the model only zoomed out instead
TOP = re.compile(r"\b(meta|(top|home|main|start) (level|page|screen|view)|go home|all (my |the )?(boards|spaces)|back out|(leave|exit|close) (this|the) board)\b", re.I)
NUM = re.compile(r'\d[\d,.:/]*\d|\d')


SHORT_NUM = re.compile(r'(\d+(?:\.\d+)?)\s?([kKmM])\b')   # "$135k", "1.2M": the voice says them as "135,000", "1,200,000"


def unsupported_numbers(answer, texts, question):
    text = ' '.join(texts) + ' ' + question
    long = ' '.join('%d' % round(float(a) * (1000 if u in 'kK' else 1000000)) for a, u in SHORT_NUM.findall(text))
    have = re.sub(r'[,\s]', '', text + ' ' + long)
    return [n for n in NUM.findall(answer) if re.sub(r'[,]', '', n).rstrip('.') not in have]


def brief_entries(boards, briefs, scope, question='', history=()):
    """The finished briefs in scope as items: the whole brief for one board, the summary paragraph for many. Inside one
    board, also one item per frame (from briefs.py's frame tree): the frames the question is about with their whole
    summary, the others with a short gist, so the voice can say what is where and zoom to a frame."""
    out = []
    for b in boards:
        br = (briefs.get(b['id']) or {}).get('brief')
        if not br:
            continue
        text = br.get('summary', '')
        if len(boards) == 1:
            text += '\n' + '\n'.join('%s: %s' % (sec['title'], ' '.join(p['text'] for p in sec['points']))
                                     for sec in br.get('sections', []) if sec.get('points'))
        out.append({'board': b['id'], 'boardName': b.get('name', ''), 'item': None, 'kind': 'board brief', 'frame': '', 'text': text})
    if len(boards) == 1:
        tree = (briefs.get(boards[0]['id']) or {}).get('tree') or {}
        frames = [tree['frames'][f] for f in tree.get('order') or [] if tree['frames'].get(f, {}).get('notes')]
        if frames and out:
            out[0]['text'] += '\nThis board has %d frames.' % sum(1 for f in frames if f['frame'])
        want = set(tokens(question))   # only frames this question names come whole: a "tell me more" gets the frame's items instead
        scored = sorted(((len(want & set(tokens(f['frame']))) * 3 + len(want & set(tokens(f['notes']))), n) for n, f in enumerate(frames)), reverse=True)
        whole = {n for score, n in scored[:FRAME_FULL] if score > 0}
        for n, f in enumerate(frames[:FRAME_MAX]):
            w = f['notes'].split()
            out.append({'board': boards[0]['id'], 'boardName': boards[0].get('name', ''), 'item': f.get('area'),
                        'kind': 'frame summary' if n in whole else 'frame gist', 'frame': f['frame'] or '(items outside any frame)',
                        'text': f['notes'] if n in whole else ' '.join(w[:FRAME_GIST_WORDS]) + (' …' if len(w) > FRAME_GIST_WORDS else ''),
                        'notes': f['notes']})
    return out[:40 + FRAME_MAX]


def named_frame(question, entries):
    """The frame item whose name the question says (most of its words), as [entry]; [] when none is clearly named."""
    q = set(tokens(question))
    best, pick = 0, []
    for e in entries:
        if e['kind'] in ('frame summary', 'frame gist') and e['item']:
            name = set(tokens(e['frame']))
            hit = len(q & name) / len(name) if name else 0
            if hit >= .5 and len(q & name) > best:
                best, pick = len(q & name), [e]
    return pick


def named_target(question, said, targets):
    """The board or space the question names; for "open it", the one the answer says it opened. Boards before spaces."""
    # the answer counts only where it says it opened or went to a board or space (an answer quoting a card whose title
    # ended in a board's name opened that board): the words right after "opened", "taking you to"...
    went = ' '.join(m.group(1) for m in re.finditer(r"\b(?:opened|opening|open|taking you to|took you to|going to|here is|here's)\b"
                                                   r"((?:\s+the)?\s+[^.!?,\"]{1,40}?\s+(?:board|space))\b", said, re.I))
    for text in (question, went):
        low = text.lower()
        hits = [n for n in targets if n and n != 'all boards' and re.search(r'(?<!\w)' + re.escape(n) + r'(?!\w)', low)]
        if hits:
            return targets[max(hits, key=lambda n: ('board' in targets[n], len(n)))]
    return None


SOURCE_MATCH = .5   # share of the answer's words found in one item's text


def no_rule_out(said, used, entries):
    """"Any YouTube links?" gets the YouTube link, not "The other link is GitHub": a sentence about an item the answer
    did not come from, ruling it out, is dropped (the model adds one even when told not to)."""
    parts = re.split(r'(?<=\D[.!?])\s+|(?<=[.!?]["\'])\s+', said.strip())   # "the 5. Bring in anything frame" is one sentence
    others = [e for e in entries if e['item'] and e not in used and e['kind'] not in ('frame summary', 'frame gist', 'frame title')]
    if len(parts) < 2 or not used or not others:
        return said
    def about_other(s):
        if not re.match(r"(?:the |this |that )?(?:board's )?other\b|.*\b(?:is|are) not\b|.*\bdoes not count\b", s, re.I):
            return False
        ours = max(same_words(s, e.get('notes') or e['text']) for e in used)
        theirs = max(same_words(s, e.get('notes') or e['text']) for e in others)
        return theirs > ours
    kept = [s for s in parts if not about_other(s)]
    return ' '.join(kept) if kept else said


def same_words(a, b):
    """How much of answer a is already in answer b (share of a's words), for catching a repeated answer."""
    wa, wb = set(tokens(a)), set(tokens(b))
    return len(wa & wb) / len(wa) if wa else 0


def no_roll_call(said, entries):
    """A spoken answer never reads out a board's frames one by one: more than five named becomes the count and four."""
    names = [e['frame'] for e in entries if e['kind'] in ('frame summary', 'frame gist') and e['frame'] and not e['frame'].startswith('(')]
    named = sorted((said.lower().find(n.lower()), n) for n in names if len(n) > 3 and n.lower() in said.lower())
    if len(named) <= 5:
        return said
    return 'This board has %d frames. Among them are %s, %s, %s and %s.' % (
        len(names), *[n for _, n in named[:4]])


def board_gist(b, brief, words=35):
    """What a board is about, in a line: its brief's summary, else its frame titles, else its first notes."""
    text = ((brief or {}).get('brief') or {}).get('summary') or ''
    if not text:
        items = b.get('items') or []
        frames = [i.get('text') for i in items if i.get('type') == 'area' and i.get('text') and i.get('text') != 'Shape']
        text = ('Frames: ' + '; '.join(frames[:10])) if frames else ' / '.join(i.get('text') for i in items if i.get('type') == 'note' and i.get('text'))
    w = text.split()
    return ' '.join(w[:words]) + (' …' if len(w) > words else '')


def layout_entry(spaces, boards, briefs, here, detail=True):
    """The map of the user's spaces and boards as one item. spaces = [{id, name}] in nav order. detail=False (inside one
    board) leaves out what each board is about. 'targets' (names -> where to go) and 'spaceOf' stay on the server."""
    ids = {sp['id'] for sp in spaces}
    groups = [(sp['name'], sp['id'], [b for b in boards if b.get('space') == sp['id']]) for sp in spaces]
    groups.append(('Unsorted', 'unsorted', [b for b in boards if b.get('space') not in ids]))
    lines = ['the user has %d spaces and %d boards. the user is now %s.' % (len(spaces), len(boards), here)]
    targets, space_of = {'all boards': {'space': ''}}, {}
    for name, sid, bs in groups:
        if sid == 'unsorted' and not bs:
            continue
        targets[name.lower()] = {'space': sid}
        lines.append('%s "%s": %d board%s' % ('Boards not in any space,' if sid == 'unsorted' else 'Space', name, len(bs), '' if len(bs) == 1 else 's'))
        for b in sorted(bs, key=lambda b: -(b.get('updated') or 0)):
            targets.setdefault(b.get('name', '').lower(), {'board': b['id']})
            space_of[b['id']] = '' if sid == 'unsorted' else name
            line = '- board "%s": %d item%s, last edited %s' % (b.get('name', ''), len(b.get('items') or []), '' if len(b.get('items') or []) == 1 else 's', time.strftime('%Y-%m-%d', time.localtime((b.get('updated') or 0) / 1000)))
            gist = board_gist(b, briefs.get(b['id'])) if detail else ''
            lines.append(line + ('. About: ' + gist if gist else ''))
    return {'board': None, 'boardName': '', 'item': None, 'kind': 'map of spaces and boards', 'frame': '',
            'text': '\n'.join(lines), 'targets': targets, 'spaceOf': space_of}


def which_answer(given, entries, waiting):
    """The model's {question: n, text} as {run, text}; a bare text answers the only (or newest) waiting question."""
    if not given or not waiting:
        return None
    if isinstance(given, dict):
        try:
            e = entries[int(given.get('question')) - 1]
            if e.get('run'):
                return {'run': e['run'], 'text': str(given.get('text') or '').strip()[:2000]}
        except (TypeError, ValueError, IndexError):
            pass
        given = given.get('text')
    return {'run': waiting[-1]['id'], 'text': str(given or '').strip()[:2000]} if given else None


def which_task(given, entries, running=False):
    """The model's {task: n, text} as {run, text}; without a usable number, the task that ended last (or, for
    running=True, the task started last)."""
    if not isinstance(given, dict) or not str(given.get('text') or '').strip():
        return None
    tasks = [e for e in entries if e['kind'] == 'agent task']
    try:
        e = entries[int(given.get('task')) - 1]
    except (TypeError, ValueError, IndexError):
        e = None
    if not e or e['kind'] != 'agent task':
        e = (tasks[-1] if tasks else None) if running else max(tasks, key=lambda t: t.get('ended') or 0, default=None)
    return e and {'run': e['run'], 'text': str(given['text']).strip()[:2000]}


LAST_ASK = [0.0]   # when a question last ran: the brief reader (briefs.py) waits while the user is asking


def frame_layouts(board, question, seen, reader):
    """The frames the question is about: named in it, else the ones mostly on screen; each with its cards by place."""
    import arrange
    frames = [i for i in board.get('items') or [] if i.get('type') == 'area' and not i.get('agent')]
    asked = set(tokens(question)) | set(re.findall(r'\d{2,}', question))
    named = [f for f in frames if asked & (set(tokens(f.get('text') or '')) | set(re.findall(r'\d{2,}', f.get('text') or '')))]
    if not named and isinstance(seen, list) and len(seen) == 4:
        def shown(f):
            w = min(f['x'] + f['w'], seen[2]) - max(f['x'], seen[0])
            h = min(f['y'] + f['h'], seen[3]) - max(f['y'], seen[1])
            return max(w, 0) * max(h, 0)
        named = sorted((f for f in frames if shown(f) > 0), key=shown, reverse=True)[:2]
    return [{'board': board['id'], 'boardName': board.get('name', ''), 'item': f['id'], 'kind': 'frame layout',
             'frame': f.get('text') or '', 'text': arrange.frame_text(board, f, reader)} for f in named[:2]]


PLACE = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|front|start|end|place \d+|position \d+|\d+(?:st|nd|rd|th))"
MOVE_Q = re.compile(r"\b(?:(?:move|put|make|swap|switch|place|shift|bring)\b.{0,80}\b(?:%s|before|after|place|position|spot)|"
                    r"(?:should|to|will|must) (?:be|go|come) (?:the |in |at |to )?(?:the )?%s)\b" % (PLACE, PLACE), re.I)
ORDINALS = {w: k for k, w in enumerate('first second third fourth fifth sixth seventh eighth ninth tenth'.split(), 1)}
POINT = re.compile(r"\b(open|show|highlight|point|go to|zoom|take me|pull up|bring up|look at|find|which one|where)\b", re.I)
ORD_Q = re.compile(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|\d+(?:st|nd|rd|th))\b", re.I)
UNDER = re.compile(r"\b(below|under|underneath|beneath)\b", re.I)
CARD_Q = re.compile(r"\b(?:open|show|highlight|point (?:at|to)|go to|zoom (?:in )?(?:to|on|into)|take me to|pull up|bring up|look at|find)\b.{0,30}?\b(?:the )?"
                    r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|\d+(?:st|nd|rd|th))\b.{0,25}?\b(?:card|one|note|item|link|post)", re.I)


def card_open(want, question):   # the user's own word (the model set "open" for "show me the last one"), as a command: not "open source"
    return bool(re.search(r"^\W*(?:(?:can|could|would) you |please |now |and )*open\b|\bopen (?:it|that|this|them|the|its|up)\b", question, re.I))


def card_at(want, question, entries, board, reader, last=None, asked=False):
    """The card the question points at by its place, its words, or (for "the one below that") the card shown last:
    (the entry to show, its link card's entry or None, place, frame title, under, frame id, its sticky note's entry)."""
    import arrange
    layouts = [e for e in entries if e['kind'] == 'frame layout']
    if not layouts:
        return None
    place, lay = None, layouts[0]
    if isinstance(want, dict):
        try:
            n = int(want.get('frame'))
            if 1 <= n <= len(entries) and entries[n - 1]['kind'] == 'frame layout':
                lay = entries[n - 1]
            place = int(want.get('place'))
        except (TypeError, ValueError):
            place = None
    m = (ORD_Q if asked else CARD_Q).search(question)   # asked: a question about a card ("what card is fourth"), not "show"
    if place is None and m:   # it said "Here is the second card" and asked for no card: the words say which
        w = m.group(1).lower()
        place = ORDINALS.get(w) or (-1 if w == 'last' else int(re.match(r'\d+', w).group()))
    frame = next((i for i in board.get('items') or [] if i['id'] == lay['item']), None)
    cs = arrange.cards(board, frame) if frame else []
    under = bool(UNDER.search(question)) or (isinstance(want, dict) and want.get('under') is True)
    if cs and (under or POINT.search(question) or (asked and place is None)):   # named by its words ("the one below pricing ideas"): the words
        # win over the model's guess of a place; only when pointing at it, so "what is the pricing ideas card about" is answered
        asked = set(tokens(question))
        def name(s):   # the note's words before its date ("Pricing ideas — Oct 18, 2026")
            return set(tokens(' '.join(re.split(r'\s+—\s+', item_words(s[0], reader)[1])[0].split()[:8])))
        best = max(((len(asked & name(s)) / max(1, len(name(s))), k) for k, s in enumerate(cs, 1)), default=(0, 0))
        if best[0] >= .6:
            place = best[1]
    if place is None and last and last.get('frame') == lay['item'] and (
            under or (card_open(want, question) and re.search(r'\b(it|that|this|that one|this one)\b', question, re.I))):
        place = last.get('place')   # "the card below that", "open it": the card shown last
        under = under or bool(last.get('under'))
    if place is None:
        return None
    if not cs or not (place == -1 or 1 <= place <= len(cs)):
        return None
    if place == -1:   # "the last one": of the top row when the question says so, else of the frame
        place = len(arrange.grid(frame, cs)[0]) if re.search(r'\b(top|first) row\b', question, re.I) else len(cs)
    s = cs[place - 1]
    entry = lambda i: {'board': board['id'], 'boardName': board.get('name', ''), 'item': i['id'], 'kind': item_words(i, reader)[0],
                       'frame': lay['frame'], 'text': item_words(i, reader)[1]}
    link = next((i for i in s if i.get('url') or i.get('type') in ('file', 'img')), None)
    shown = s[1] if under and len(s) > 1 else s[0]
    return entry(shown), (entry(link) if link else None), place, lay['frame'], under and len(s) > 1, frame['id'], entry(s[0])


def link_under(board, item_id, reader):
    """The openable card in the same stack as this item (a sticky note's link card), as an entry."""
    for s in stacks([i for i in board.get('items') or [] if i.get('type') not in ('area', 'arrow')]):
        if any(i['id'] == item_id for i in s):
            link = next((i for i in s if i.get('url') or i.get('type') in ('file', 'img')), None)
            if link and link['id'] != item_id:
                return {'board': board['id'], 'boardName': board.get('name', ''), 'item': link['id'],
                        'kind': item_words(link, reader)[0], 'frame': '', 'text': item_words(link, reader)[1]}
    return None


UNDO_Q = re.compile(r"\b(put (it|them|that|everything|the cards?) back|undo|revert|back (to )?(how|the way) it was)\b", re.I)
SORT_Q = re.compile(r"\b(sort|group|organi[sz]e|restructure|rearrange|regroup|categori[sz]e|cluster)\w*\b.{0,60}\b(themes?|topics?|categor\w*|sections?|groups?)\b", re.I)
STARTED = re.compile(r"\bI(?:'ve| have)? (?:just )?(?:started|kicked off|set up|launched|begun) (?:an?|the) (?:agent )?(?:task|team)", re.I)


def answer(question, history, boards, scope, reader, briefs=None, layout=None, waiting=(), tasks=(), team_text='', seen=None):
    t0 = LAST_ASK[0] = time.time()
    interrupt_background()
    briefs = briefs or {}
    entries = brief_entries(boards, briefs, scope, question, history) + context(question, history, boards, reader)
    entries += [dict(t, kind='agent task') for t in tasks]   # {board, boardName, item (its frame), run, text}
    if len(boards) == 1:
        entries.append({'board': boards[0]['id'], 'boardName': boards[0].get('name', ''), 'item': None, 'kind': 'board contents',
                        'frame': '', 'text': board_contents(boards[0])})
    if len(boards) == 1 and scope != 'all' and not scope.startswith('space:'):
        entries += frame_layouts(boards[0], question, seen, reader)
    if team_text and len(boards) == 1:
        entries.append({'board': boards[0]['id'], 'boardName': boards[0].get('name', ''), 'item': None, 'kind': 'board team',
                        'frame': '', 'text': team_text})
    entries += [{'board': w['board'], 'boardName': '', 'item': None, 'kind': 'agent question', 'frame': '', 'run': w['id'],
                 'text': '%s asks: %s' % ((w.get('member') or {}).get('name') or 'An agent', w['question'])} for w in waiting]
    if layout:
        for e in entries:
            e['spaceName'] = layout['spaceOf'].get(e['board'], '')
        entries = [layout] + entries
    if not entries:
        return {'answer': 'There is nothing on %s to answer from yet.' % ('this board' if scope != 'all' and not scope.startswith('space:') else 'these boards'), 'sources': [], 'ms': 0}
    reply = ask_model(question, history, entries, scope)
    if history and same_words(reply[0], history[-1]['a']) >= .7:   # Nemotron can read back its last answer: once more, asking for news
        again = ask_model(question + ' (Say something new: do not repeat your last answer.)', history, entries, scope)
        if same_words(again[0], history[-1]['a']) < .7:
            reply = again
    if not any(reply[-1].values()) and MOVE_Q.search(question) and any(e['kind'] == 'frame layout' for e in entries):
        # asked to move a card and the reply has no move: once more, without the conversation, which it copies (an earlier
        # "Done" turn with no move in it was read back word for word, retry and all)
        again = ask_model(question + ' (This asks you to move a card: put it in "move_card" with the frame layout\'s item '
                          'number and the from and to places.)', [], entries, scope)
        if any(again[-1].values()):
            reply = again
    said, nums, show_nums, view, go, task, agent_answer, task_mode, follow_up, web_card, add_teammate, steer, open_num, edits = reply

    def pick(numbers):
        out = []
        for n in numbers if isinstance(numbers, list) else []:
            try:
                i = int(n) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(entries) and entries[i] not in out:
                out.append(entries[i])
        return out
    used = pick(nums)
    opening = [e for e in pick([open_num]) if e['item']]   # "open the YouTube link": the page opens it in a new tab
    if opening:
        web_card = None   # the model also asked for a web card: that made a second copy of the link
        used = opening + [e for e in used if e not in opening]
    if not used and said:   # the model read an item but listed no sources: outline the item the question names and the answer is taken from
        asked = set(tokens(question))   # a long PDF matches most answers, so the question has to name it ("the quick guide PDF")
        best = max(((same_words(said, e.get('notes') or e['text']), n) for n, e in enumerate(entries)
                    if e['item'] and e['kind'] not in ('frame summary', 'frame gist', 'frame title', 'board brief', 'agent task')
                    and asked & set(tokens(e['kind'] + ' ' + ' '.join(e['text'].split()[:6])))), default=(0, 0))
        if best[0] >= SOURCE_MATCH:
            used = [entries[best[1]]]
    # move the board only when the user asked to see something; otherwise the page just outlines the sources as usual
    single = len(boards) == 1 and scope != 'all' and not scope.startswith('space:')
    last = next((h['did']['card'] for h in reversed(history) if (h.get('did') or {}).get('card')), None)
    card = card_at(edits.get('card'), question, entries, boards[0], reader, last) if single else None
    asked_to_see = bool(SEE.search(question)) or bool(YES.search(question) and history and OFFER.search(history[-1]['a']))   # "yes" to "Want me to open it?"
    shown = [e for e in pick(show_nums) if e['item']] if asked_to_see else []   # board items only (the brief is not on the canvas)
    if not asked_to_see:
        view = None
    if card:   # a card by its place: found in code (it showed the board brief for "the second card", a card it had no number for)
        head, link, place, title, under, frame_id, note = card
        shown, view = [head], None
        used = [head] if under or not link else [note, link]   # just the card: other sources lit up frames too
        opening = [link] if link and (card_open(edits.get('card'), question)) else []
        said = ('Here is the card under the sticky note at place %d of the %s frame: "%s".' if under else
                'Here is card %d of the %s frame: "%s".') % (place, title, ' '.join(head['text'].split()[:10]))
        said += ' I opened its link in a new tab.' if opening else ''
        checked_card = {'frame': frame_id, 'place': place, 'under': under}
    elif single and (any(e['kind'] == 'frame layout' for e in used) or (
            not any(e['item'] for e in used) and ORD_Q.search(question) and re.search(r'\b(card|note|one|link)\b', question, re.I))):
        # an answer about a card by its place listed the frame's card list as its source, so the whole frame lit up
        # ("what card is fourth in the 2026 frame"): outline the card itself, note and the card under it
        found = card_at(None, question, entries, boards[0], reader, last, asked=True)
        used = [e for e in used if e['kind'] != 'frame layout']
        if found:
            head, link, place, title, under, frame_id, note = found
            used = [note] + ([link] if link and link['item'] != note['item'] else []) + used
            name = ' '.join(re.split(r'\s+—\s+', note['text'])[0].split()[:4]).lower()
            if name not in said.lower():   # the model named another card than the one at that place: say the one there
                said = 'Card %d of the %s frame is "%s"%s.' % (place, title, ' '.join(note['text'].split()[:10]),
                                                              (', with "%s" under it' % ' '.join(link['text'].split()[:6])) if link and link['item'] != note['item'] else '')
            card = found   # the next "open it" or "the one below that" means this card
            checked_card = {'frame': frame_id, 'place': place, 'under': under}
    elif opening:   # "open it" on a sticky note: what opens is the link card stacked under it
        link = link_under(boards[0], opening[0]['item'], reader) if single else None
        if link:
            opening = [link]
    if any(e['kind'] == 'board brief' for e in pick(show_nums)) and view is None and not card:
        view = 'brief'
    for e in shown:
        if e not in used:
            used.append(e)
    # asking to SEE something working, with no live demo card on the board, is work for the team (pasted code is not a
    # demo): the model kept pointing at code the Lead had pasted as text
    if DEMO.search(question) and len(boards) == 1 and not task and not follow_up and not steer and not any(
            i.get('type') == 'web' and i.get('demo') for i in boards[0].get('items') or []):
        ended = [e for e in entries if e['kind'] == 'agent task' and e.get('ended')]
        if ended:
            follow_up = {'task': entries.index(max(ended, key=lambda e: e['ended'])) + 1, 'text': question}
        else:
            task = question
        said = ("There's no working demo on the board yet, only code pasted as text. I've asked the team to build one: "
                "it will show up as a live demo card in their frame, and I'll tell you when it's ready.")
    if asked_to_see and not shown and not task and not follow_up and not web_card and not opening:   # "put their site on the board" is not the "Put things on the board" frame   # "dive into <frame>": zoom there even when the model left "show" empty
        shown = named_frame(question, entries)
    opened = (layout['targets'].get(str(go or '').strip().strip('"').lower()) if layout and asked_to_see and not shown else None)
    if layout and asked_to_see and not shown and not opened and TOP.search(question):
        opened, view = {'space': ''}, None
    if layout and asked_to_see and not shown and not opened:   # the model can say "I have opened the Sales board" and leaves "open" empty
        opened = named_target(question, said, layout['targets'])
    edit = None   # a move or sort the voice does itself; server.py does it and says what really happened
    one = len(boards) == 1 and scope != 'all' and not scope.startswith('space:')
    for kind in ('move_card', 'sort_frame'):
        e = edits.get(kind)
        if one and isinstance(e, dict):
            f = pick([e.get('frame')])
            if not (f and f[0]['kind'] == 'frame layout') and len([x for x in entries if x['kind'] == 'frame layout']) == 1:
                f = [x for x in entries if x['kind'] == 'frame layout']   # one frame it can mean: that one
            if f and f[0]['kind'] == 'frame layout':
                edit = {'kind': kind, 'frame': f[0]['item'], 'from': e.get('from'), 'to': e.get('to')}
                break
            said = 'Which frame do you mean? Name it, or zoom to it on screen, and ask again.'
    if one and not edit and (edits.get('undo_edit') is True or UNDO_Q.search(question)):
        edit = {'kind': 'undo'}
    layouts = [e for e in entries if e['kind'] == 'frame layout']
    if one and not edit and SORT_Q.search(question) and layouts:   # it said "Done, grouped into themes" and asked for no sort
        edit = {'kind': 'sort_frame', 'frame': layouts[0]['item']}
    if not task and STARTED.search(said) and one:   # it said it started a task but gave none: start the one it was asked for
        task = history[-1]['q'] if YES.search(question) and history else question
    said = no_roll_call(said, entries)
    said = no_rule_out(said, used, entries)
    for e in shown:   # went into a frame but the answer is about something else: say what the frame holds
        if e['kind'] in ('frame summary', 'frame gist') and e['frame'].lower() not in said.lower():
            said = 'Here is the %s frame. %s' % (e['frame'], ' '.join(re.split(r'(?<=[.!?])\s+', e['notes'])[:2]))
            if e not in used:
                used.append(e)
    said = re.sub(r'\s*(?:Sources?:\s*)?(?:\[\d+\][,;\s]*(?:and\s*)?)+(?=[.!?]|$)', '', said).strip()   # item numbers are not spoken
    said = re.sub(r'(?<!\.)\.\.(?!\.)', '.', said)
    bad = unsupported_numbers(said, [e.get('notes') or e['text'] for e in (used or entries)] + ([layout['text']] if layout else [])
                              + [e.get('frame') or '' for e in entries], question)   # "the 5. Bring in anything frame": a frame's name is on the board too
    checked = not bad
    if bad:
        said = "I found related items, but I couldn't confirm the details in them, so I won't guess. They're highlighted for you."
    names = set()
    for b in boards:
        names |= {image_name(i.get('src')) for i in b.get('items') or []} | {image_name(i.get('url')) for i in b.get('items') or []}
    names.discard(None)
    LAST_ASK[0] = time.time()
    reading = [briefs[b['id']] for b in boards if (briefs.get(b['id']) or {}).get('state') == 'reading']
    return {'answer': said, 'checked': checked, 'ms': int((time.time() - t0) * 1000), 'model': (models.choice('voice') or (0, (models.local('voice') or (0, MODEL))[1]))[1],
            'unread': reader.unread(names),
            'show': [{'board': e['board'], 'item': e['item']} for e in shown],
            'open': opened,
            'task': str(task).strip()[:600] if task and len(boards) == 1 and scope != 'all' and not scope.startswith('space:') else None,
            'task_mode': 'one' if task_mode == 'one' else 'team',
            'edit': edit,
            'shown_card': checked_card if card else None,
            'agent_answer': which_answer(agent_answer, entries, waiting),
            'follow_up': which_task(follow_up, entries),
            'steer': which_task(steer, entries, running=True),
            'open_item': {'board': opening[0]['board'], 'item': opening[0]['item']} if opening else None,
            'web_card': str(web_card).strip() if isinstance(web_card, str) and re.match(r'^(https?://)?[\w.-]+\.[a-z]{2,}', str(web_card).strip(), re.I) else None,
            'add_teammate': add_teammate if isinstance(add_teammate, dict) and str(add_teammate.get('name') or '').strip() and len(boards) == 1 else None,
            'view': view if view in ('fit', 'in', 'out', 'brief') else None,
            'reading': {'done': sum(r.get('done', 0) for r in reading), 'total': sum(r.get('total', 0) for r in reading)} if reading else None,
            'sources': [{'board': e['board'], 'boardName': e['boardName'], 'item': e['item'], 'kind': e['kind'],
                         'snippet': e['text'][:140]} for e in used if e['board'] and e['kind'] not in ('agent question', 'board team')]}


def voices():
    with urllib.request.urlopen(TTS_URL + '/v1/audio/voices', timeout=6) as r:
        return json.load(r)


def speak(text, voice):
    """The shim's streaming answer: raw 16-bit PCM; the caller copies it to the browser as it arrives."""
    body = json.dumps({'model': 'qwen3-tts', 'input': text, 'voice': voice, 'stream': True}).encode()
    req = urllib.request.Request(TTS_URL + '/v1/audio/speech', data=body, headers={'Content-Type': 'application/json'})
    return urllib.request.urlopen(req, timeout=120)


# ---------- who is calling (for the ears): the Cloudflare Access cookie, signature checked ----------
_certs = {'t': 0, 'keys': {}}


def _b64u(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def _keys():
    if time.time() - _certs['t'] > 3600:
        with urllib.request.urlopen(ACCESS_CERTS, timeout=10) as r:
            keys = json.load(r)['keys']
        _certs['keys'] = {k['kid']: (int.from_bytes(_b64u(k['n']), 'big'), int.from_bytes(_b64u(k['e']), 'big')) for k in keys}
        _certs['t'] = time.time()
    return _certs['keys']


def access_user(cookie_header):
    """The signed-in email if the CF_Authorization cookie is a valid Access token for this app, else None."""
    m = re.search(r'(?:^|;\s*)CF_Authorization=([^;]+)', cookie_header or '')
    if not m or not ACCESS_CERTS:
        return None
    try:
        head, payload, sig = m.group(1).split('.')
        hdr, claims = json.loads(_b64u(head)), json.loads(_b64u(payload))
        key = _keys().get(hdr.get('kid')) if hdr.get('alg') == 'RS256' else None
        if not key:
            return None
        n, e = key
        size = (n.bit_length() + 7) // 8
        got = pow(int.from_bytes(_b64u(sig), 'big'), e, n).to_bytes(size, 'big')
        digest = hashlib.sha256((head + '.' + payload).encode()).digest()
        want = b'\x00\x01' + b'\xff' * (size - 54) + b'\x00' + bytes.fromhex('3031300d060960864801650304020105000420') + digest
        aud = claims.get('aud')
        if got != want or ACCESS_AUD not in (aud if isinstance(aud, list) else [aud]) or claims.get('exp', 0) < time.time():
            return None
        return claims.get('email') or 'signed-in'
    except (ValueError, KeyError, OSError):
        return None
