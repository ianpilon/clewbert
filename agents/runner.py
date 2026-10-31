"""Agents that work on a board: a task becomes a frame on the canvas, and an agent (or a team) fills it.

A run is a loop: the model sees the task, the board around it and its own steps so far, and answers with tool calls
(search the board, read the web, put things in the frame, ask the person, finish). Every step is saved to
DATA_DIR/runs/<id>.json, so a run picks up where it was after a restart. Results never go into a chat: they are
board items (marked with the run id) placed in the task frame; the page pulls them in while the board is open.

Teams: each board has a team (DATA_DIR/teams/<board>.json): a Lead and teammates, each with a role in plain words.
A team task goes to the Lead, who hands parts to teammates with assign. Each part is a run of its own with its own
frame, joined to the Lead's frame by an arrow. The Lead waits (status "team") until every part of that round has
ended, then reads their results and either hands out a next round or writes the final answer in its own frame.
"""
import base64, json, os, queue, re, secrets, threading, time, urllib.request
import models, talk
from . import web

MODEL_URL = os.environ.get('AGENT_MODEL_URL', '')   # a chat/completions address with tool calls on your own machine, or pick a provider in Settings
MODEL = os.environ.get('AGENT_MODEL', '')
# the strongest models for the Developer and the designers, tried in order (the usual model is the last resort):
# AGENT_CODE_MODELS="url|model;url|model", or one AGENT_CODE_MODEL_URL + AGENT_CODE_MODEL
CODE_MODELS = [tuple(x.split('|', 1)) for x in os.environ.get('AGENT_CODE_MODELS', '').split(';') if '|' in x] or (
    [(os.environ['AGENT_CODE_MODEL_URL'], os.environ.get('AGENT_CODE_MODEL', ''))] if os.environ.get('AGENT_CODE_MODEL_URL') else [])
CODE_MODEL_NAMES = {m for _, m in CODE_MODELS}
# a model that can see (screenshots of an agent's demo): what the page really looks like, not just whether it errored
VISION_MODEL_URL = os.environ.get('AGENT_VISION_MODEL_URL', '')
VISION_MODEL = os.environ.get('AGENT_VISION_MODEL', '')
WORKERS = int(os.environ.get('AGENT_WORKERS', '3'))
MAX_STEPS = 40
BROWSER_URL = os.environ.get('BROWSER_URL', '').rstrip('/')   # browser/service.py (a real browser), e.g. http://localhost:8790
BROWSER_TOKEN = os.environ.get('BROWSER_TOKEN') or (open(os.environ['BROWSER_TOKEN_FILE']).read().strip()
                                                    if os.path.exists(os.environ.get('BROWSER_TOKEN_FILE', '')) else '')
# a click or Enter that sends something out into the world waits for the person's yes
RISKY = re.compile(r"\b(send|submit|buy|purchase|pay|checkout|check out|place order|order now|sign ?up|register|subscribe|"
                   r"publish|post|book now|book|reserve|confirm|delete|donate|unsubscribe|apply now)\b", re.I)
YES = re.compile(r"^\W*(yes|yeah|yep|ok|okay|sure|go|go ahead|do it|approved?|confirm(ed)?)\b", re.I)
KEEP_RESULTS = 4          # the newest tool results stay whole; older ones are cut short so a long run fits the context
OLD_RESULT_CHARS = 400
CONTEXT_WORDS = 3000      # words of selected items given with the task
ACTIVE = ('queued', 'working', 'waiting', 'team')
WRITES = ('add_note', 'add_text', 'add_link', 'finish', 'ask_user', 'assign', 'make_demo', 'group_items')
# a task that asks to rearrange what is on the board (it once ended "Done, restructured" with nothing moved)
REARRANGE = re.compile(r"\b(re-?arrang\w*|restructur\w*|re-?organi[sz]\w*|regroup\w*|cluster\w*|"
                       r"(?:group|sort|move|organi[sz]e)\w* (?:the |these |those |all |every )?(?:items|notes|stickies|cards|links|content|frame|board|them|it)\b|"
                       r"into (?:themes|groups|sections|categories|clusters))", re.I)
BUILDING = re.compile(r"\b(build|make|create|code|implement|fix|rebuild)\w*\b.{0,80}\b(demo|page|prototype|app|site|tool|it)\b", re.I)   # a part that is to build
BUILD = re.compile(r"\b(demo|prototype|build|make (?:a|me a|an)|page|site|website|app|tool|interface|screen|ui|ux|dashboard|landing)\b", re.I)
GUESS = re.compile(r"\b(likely|probably|possibly|presumably|might (?:be|use|have)|may be|could be|appears to be|seems to be|"
                   r"it is (?:likely|possible) that|you (?:can|could|might) try)\b", re.I)
ENDED = ('done', 'stopped', 'failed')
MAX_ROUNDS = 5            # times a Lead can hand out work for one task (design, build, review, fix)
RESULT_WORDS = 2000       # words of one teammate's results the Lead (and the next round) reads back
STRONG_ROLES = ('developer', 'ux', 'ui')   # these run on the code model when there is one

# starter roles, in our own words; the person can rename them and rewrite every role
STARTERS = {
    'lead': {'name': 'Lead', 'color': '#1d4ed8', 'role': 'Plans the work: splits the task into parts, hands each part to the '
             'teammate who fits it, then puts their results together into the final answer.'},
    'researcher': {'name': 'Researcher', 'color': '#0f766e', 'role': 'Finds facts on the board and on the web. Reads each page '
                   'before using it. One finding per note, with where it came from.'},
    'writer': {'name': 'Writer', 'color': '#9333ea', 'role': 'Turns findings into clear writing: summaries, drafts, emails, '
               'posts. Plain words, short sentences, nothing that is not in the findings.'},
    'analyst': {'name': 'Analyst', 'color': '#c2410c', 'role': 'Compares and ranks: finds the patterns, the gaps and the best '
                'options in what the team found, and says why.'},
    'developer': {'name': 'Developer', 'color': '#0369a1', 'role':
        'Builds working demos and reads code.\n'
        'Reading a site: list its files with read_page_source, look inside them with search_code, read whole stretches with '
        'read_code, and quote the exact code that does the thing.\n'
        'Building: when the team has a UX spec and a UI spec (in the results of earlier rounds), build exactly to them: '
        'every value from the UI spec (colours, sizes, spacing, type, motion), every interaction and state from the UX spec. '
        'One complete HTML page with make_demo. Then check it like a person would with try_demo (open it, then each key, '
        'scroll and click in the spec), fix what is broken or differs from the specs, and make_demo again with the same '
        'title. Finish only when every state works and looks as specified; list in your frame anything you could not match.'},
    'ux': {'name': 'UX Designer', 'color': '#ca8a04', 'role':
        'Designs how it works for the person using it, before anything is built. Deliver a UX spec in your frame (add_text), '
        'in this order:\n'
        '1. Goal: what the person should understand or feel in the first 10 seconds.\n'
        '2. The main interaction: each control (key, scroll, click, touch) and exactly what happens on screen, starting with '
        'what they see before doing anything.\n'
        '3. States: start, in progress, end, empty, error. What changes between them, how fast (milliseconds) and how '
        '(easing).\n'
        '4. Guidance: the hints and labels on screen, where they sit, and when they fade.\n'
        '5. What to leave out.\n'
        'Base it on what the team found (how the original behaves) and keep what makes it work.\n'
        'Reviewing a demo: use try_demo with the steps a first-time visitor would take, compare every state with your spec, '
        'and list concrete fixes: what, where, expected and seen. "It runs" is never done.'},
    'ui': {'name': 'UI Designer', 'color': '#db2777', 'role':
        'Designs how it looks, as exact values a developer can copy. Deliver a UI spec in your frame (add_text): palette '
        '(hex for background, surfaces, text, muted text, one accent, other colours only with a reason), type (font stack; '
        'size in px, weight and letter-spacing for each level), spacing scale (4, 8, 12, 16, 24, 32, 48), corner radii, '
        'shadows and glow, layout (where each element sits, at desktop and at phone width), motion (what moves, duration, '
        'easing), and how each state looks.\n'
        'House style, unless the task says otherwise: dark first, deep navy surfaces (#0d1126 to #141936, cards one step '
        'lighter, about #1a2040) with depth from lighter surfaces, not borders. One cool accent: indigo (#6c7bff family) or '
        'cyan (#22d3ee family), never both. Near-white text, blue-grey labels (#8a93b8). Inter or a close geometric sans. '
        'Big light numerals with small uppercase letter-spaced labels. Soft, large, diffuse shadows and a soft glow in the '
        'accent colour. Rounded corners of 12 to 20 px. Never: purple-to-pink gradients, emoji as icons, blur on '
        'everything, the centred hero with three cards.\n'
        'Reviewing a demo: use try_demo for each state and check it against your spec value by value (colour, size, '
        'spacing, contrast, motion); list fixes with exact values.'},
    'checker': {'name': 'Fact-checker', 'color': '#be123c', 'role': 'Checks claims against their sources by reading the pages '
                'again. Says for each one whether it holds up, and why.'},
}
STARTERS['design-lead'] = {'name': 'Design Lead', 'color': '#7c3aed', 'role':
    'Leads design and manages the UX Designer and the UI Designer. Hands the UX part to the UX Designer first, then the UI '
    'part to the UI Designer from the UX spec. For a review, has both try the demo and list fixes. Then puts their work '
    'together into ONE design brief for whoever builds it: what to build, how it works (every control and state), how it '
    'looks (exact values), and the fixes, in order of importance. Never designs or builds it alone.'}
WHY = {'lead': 'Turns your goal into a plan and owns the final answer',
       'researcher': 'Finds the facts the work stands on', 'writer': 'Turns results into words people read',
       'analyst': 'Compares and ranks what the team found', 'checker': 'Makes sure what we say holds up',
       'developer': 'Builds what we show and reads code', 'design-lead': 'One owner for how it works and how it looks',
       'ux': 'Decides how it works for the person using it', 'ui': 'Decides how it looks, with exact values'}
for _k, _v in STARTERS.items():
    _v['why'] = WHY.get(_k, '')
STARTERS['ux']['under'] = STARTERS['ui']['under'] = 'design-lead'   # who they report to when that role is on the team
MAX_DEPTH = 3             # Lead, a manager, their reports
DEFAULT_TEAM = ('lead', 'researcher', 'writer')


def boss_of(m):
    """Who a teammate reports to (the Lead when not set); the Lead reports to the person."""
    return None if m['id'] == 'lead' else (m.get('reports_to') or 'lead')


def reports_of(members, mid):
    return [m for m in members if boss_of(m) == mid]

SYSTEM = """You are an agent working on a canvas board (a Miro-style board of notes, text, screenshots, PDFs and links).
The person gave you a task. You work on it step by step with your tools and put what you find INTO YOUR TASK FRAME on
the board: that frame is where the person looks, there is no chat. Work like this:
- Start from the board: the selected items are given below; use search_board and read_item for more of the board.
- Use web_search and read_web_page when the task needs facts from outside the board. Search results are only leads:
  read the page before you put a finding on the board, and check it really is what the task asks for (a directory
  or a list of firms is not a firm). Never invent facts, names,
  numbers or links: put on the board only what you read on the board or on a web page, and say where it came from.
- Put results in the frame as you go, not all at the end: add_note for one finding, contact or idea per note (short,
  with its source); add_text with style "heading" to start a section and "body" for a paragraph; add_link for a page
  worth keeping.
- When the person wants to SEE something work (a demo, a prototype, "show me"), build it with make_demo: one complete
  HTML page. Fix any JavaScript errors it reports, then say on the board what the demo shows and how to use it.
- To find out HOW something on a site works, read its code: read_page_source lists the site's script and style
  files, search_code looks inside one of them for the words that matter. Quote the code you found.
- The browser (browser_open, browser_click, browser_type...) is a real browser: use it when a site needs clicking,
  typing, searching inside it, or JavaScript to show its content. read_web_page and read_page_source are quicker for
  plain reading. Act by the element numbers of the latest look. Anything that sends a message or personal details,
  buys, books, posts or signs up needs the person's yes first (ask_user), and you never type passwords. Type only what
  the task or the person gave you: never make up a name, email, phone number or address; ask for it instead.
- If a search finds nothing useful, try other words or another way before giving up. If you cannot go on without the
  person (a choice only they can make, information only they have), use ask_user with one short question. Never ask
  for permission to do the task you were given or whether to keep going.
- When the task is done, call finish with a short summary of what you found and what is still open.
You cannot send email or messages, call, buy or sign up for anything: never offer to.
On the board you can add to your frame, read every item of a frame (read_frame) and move existing items into new
titled groups in your frame (group_items). You cannot edit, delete or recolour items, or move frames: never say you did.
Never fill a gap with what is typical or likely ("it probably uses React", "you could try their contact form"): if you
did not find it, say plainly that you did not find it.
Write plainly, in short sentences. Do not repeat what is already in the frame."""

LEAD = """
You lead this board's team. Your teammates:
%s
How you work:
- First plan: split the task into parts and hand each part to ONE teammate with assign. Give each a complete task in
  plain words (they see the board items you were given and the results of earlier rounds, not this conversation).
  Parts that can run at the same time go in the same assign call. Finding things comes first: a part that works FROM
  findings (writing, ranking, checking) goes in a LATER round, after the findings are back.
- When their results come back you can hand out a next round (for example the writer working from the researcher's
  findings), or put the final answer together yourself: add_text and add_note in YOUR frame, then finish.
- For something people will use (a demo, a page, a prototype, a tool) and a team with designers, go design first:
  the design part goes to the Design Lead if you have one (they run the UX and UI Designers and hand back one design
  brief), otherwise UX Designer then UI Designer. Then the Developer builds from the design, then design reviews the
  demo and lists fixes, then the Developer fixes. Skip what does not apply. Tell each one what earlier rounds produced
  that they should use.
- To rearrange, regroup or sort what is on the board, hand that part to a teammate: they read every item of a frame
  (read_frame) and move the items into titled groups (group_items). Tell them which frame. Nothing on the board moves
  unless a teammate does that, so never report the board as rearranged otherwise.
- Do not do a teammate's part yourself. Hand out work at most %d times for one task."""


def tool(name, desc, props=None, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': desc, 'parameters': {
        'type': 'object', 'properties': props or {}, 'required': required or list((props or {}).keys())}}}


S = {'type': 'string'}
TOOLS = [
    tool('search_board', 'Search every item on this board (notes, text, words read out of screenshots and PDFs). Returns the best matches with their item ids.', {'query': S}),
    tool('read_item', 'Read the whole text of one board item by its id.', {'item_id': S}),
    tool('web_search', 'Search the web. Returns titles, links and short snippets.', {'query': S}),
    tool('read_web_page', 'Read the text of a web page.', {'url': S}),
    tool('read_code', 'Read a stretch of one code file whole, from statement number start (search_code shows the numbers). '
         'Use it to read the part of a site\'s own code that does something, after search_code found where it is.',
         {'url': S, 'start': {'type': 'integer'}, 'count': {'type': 'integer'}}, ['url', 'start']),
    tool('read_page_source', 'Read the CODE of a web page or one of its script files: server headers, the scripts and styles it '
         'loads, what it is built with (each clue with the text it was found by) and contact links (email, GitHub, X, LinkedIn). '
         'Use it for how a site is built or who made it.', {'url': S}),
    tool('search_code', 'Look INSIDE one code file of a site (a script or stylesheet listed by read_page_source, or the page): '
         'where the words appear, with the code around each place laid out readably. Without words it tells you which '
         'animation and 3D words the file uses. Use it to find out how something on a site works.', {'url': S, 'words': S}, ['url']),
    tool('add_note', 'Put a sticky note in your task frame: one finding, contact or idea. source = the web page you READ it on '
         '(with read_web_page), or "board" when it comes from the board.', {'text': S, 'source': S}),
    tool('add_text', 'Put text in your task frame. style "heading" starts a section, "body" is a paragraph.',
         {'text': S, 'style': {'type': 'string', 'enum': ['heading', 'body']}}),
    tool('add_link', 'Put a link card for a web page in your task frame.', {'url': S}),
    tool('make_demo', 'Build a working demo: ONE complete HTML page (its CSS in <style>, its JavaScript in <script>, nothing '
         'else needed) that shows what you found or built. It opens as a live card in your frame, and you get back any '
         'JavaScript errors and what the page shows, so you can fix it: call it again with the same title to replace it.',
         {'title': S, 'html': S}),
    tool('try_demo', 'Use a demo the way a person would and see it: give the demo\'s title and steps such as "press Space", '
         '"scroll down 600", "click Explode", "wait 1000". You get, for the page as opened and after each step, a '
         'description of what is on screen (from a model that looked at it) and any errors. Works on any demo on this '
         'board, a teammate\'s too.', {'title': S, 'steps': {'type': 'array', 'items': S}}, ['title']),
    tool('read_frame', 'Read EVERY item inside one frame of the board (by its title or id), one line each with its item id. '
         'A sticky note with a card stacked right under it is one entry. Use it to work through a whole frame; '
         'search_board only finds the best matches.', {'frame': S}),
    tool('group_items', 'Move existing board items into a new group: a frame with this title, put in your task frame. Items '
         'stacked right under an item (a link card under its sticky note) move with it. Call it once per group.',
         {'title': S, 'item_ids': {'type': 'array', 'items': S}}),
    tool('ask_user', 'Ask the person one short question and wait for the answer. It shows as a note in your frame.', {'question': S}),
    tool('finish', 'End the task with a short summary of what you found and what is still open.', {'summary': S}),
]
N = {'type': 'integer'}
BROWSER_TOOLS = [
    tool('browser_open', 'Open a web page in your browser. You get the page as numbered things you can use, plus its text.', {'url': S}),
    tool('browser_look', 'Look at the page again (after it changed, or to see the numbers again).'),
    tool('browser_click', 'Click thing number n on the page. confirm: true only after the person said yes to it.',
         {'n': N, 'confirm': {'type': 'boolean'}}, ['n']),
    tool('browser_type', 'Type text into thing number n (a box). enter: true presses Enter after, for a search.',
         {'n': N, 'text': S, 'enter': {'type': 'boolean'}, 'confirm': {'type': 'boolean'}}, ['n', 'text']),
    tool('browser_select', 'Choose an option in dropdown number n.', {'n': N, 'option': S}),
    tool('browser_scroll', 'Scroll the page down or up.', {'direction': {'type': 'string', 'enum': ['down', 'up']}}),
    tool('browser_back', 'Go back to the previous page.'),
]
ASSIGN = tool('assign', 'Hand parts of the task to teammates. Each part becomes their own task frame on the board, next to '
              'yours. You get their results back when every part has ended.',
              {'assignments': {'type': 'array', 'items': {'type': 'object', 'properties': {'teammate': S, 'task': S},
                                                          'required': ['teammate', 'task']}}})
# the Lead plans, hands out and puts the answer together; it has no web tools, so it cannot do the parts itself
LEAD_TOOLS = [t for t in TOOLS if t['function']['name'] in ('search_board', 'read_item', 'read_frame', 'add_text', 'ask_user', 'finish')] + [ASSIGN]


def now_ms():
    return int(time.time() * 1000)


class Runner:
    """store: read_board(bid), edit_board(bid, fn) -> fn's result (fn changes the board under the server's lock),
    reader: talk.Reader, brief(bid) -> brief dict or None, changed(bid) called after the board is written."""

    def __init__(self, data_dir, store, reader, brief, changed):
        self.dir, self.teams_dir, self.shots, self.demos = (os.path.join(data_dir, d) for d in ('runs', 'teams', 'shots', 'demos'))
        os.makedirs(self.shots, exist_ok=True)
        os.makedirs(self.demos, exist_ok=True)
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(self.teams_dir, exist_ok=True)
        self.store, self.reader, self.brief, self.changed = store, reader, brief, changed
        self.lock = threading.Lock()
        self.runs = {}
        self.q = queue.Queue()
        for fn in sorted(os.listdir(self.dir)):
            if fn.endswith('.json'):
                try:
                    with open(os.path.join(self.dir, fn)) as f:
                        r = json.load(f)
                except (OSError, ValueError):
                    continue
                self.runs[r['id']] = r
                if r['status'] in ('queued', 'working'):   # cut off by a restart: carry on from the last saved step
                    r['status'] = 'queued'
                    self.q.put(r['id'])
        for r in list(self.runs.values()):   # a Lead whose teammates all ended while the server was down
            if r['status'] == 'team':
                self.team_check(r)
        for _ in range(WORKERS):
            threading.Thread(target=self.work, daemon=True).start()

    # ---------- the board's team ----------
    def team(self, bid):
        try:
            with open(os.path.join(self.teams_dir, bid + '.json')) as f:
                team = json.load(f)
        except (OSError, ValueError):
            team = {'members': [dict(STARTERS[k], id=k) for k in DEFAULT_TEAM]}
        for m in team['members']:   # teams saved before reporting lines: the starter's why, everyone under the Lead
            if not m.get('why'):
                m['why'] = WHY.get(m['id'], '')
        return team

    def team_text(self, bid):
        """The team as reporting lines, for the voice: who reports to whom, and why each one is there."""
        ms = self.team(bid)['members']
        names = {m['id']: m['name'] for m in ms}
        return 'This board\'s agent team (who reports to whom, and why):\n' + '\n'.join(
            '- %s: %s. Reports to %s.%s' % (m['name'], m.get('why') or 'no reason given', names.get(boss_of(m), 'you'),
                                             (' Manages: %s.' % ', '.join(x['name'] for x in reports_of(ms, m['id']))) if reports_of(ms, m['id']) else '')
            for m in ms)

    def set_team(self, bid, members):
        """Keep what the person set: names, roles, colours; the Lead is always there and always first."""
        out, seen = [], set()
        for m in members[:12]:
            mid = re.sub(r'[^a-z0-9-]', '', str(m.get('id') or '').lower())[:24] or 'm' + secrets.token_hex(3)
            name = ' '.join(str(m.get('name') or '').split())[:40]
            if not name or mid in seen or name.lower() in {x['name'].lower() for x in out}:
                continue
            seen.add(mid)
            color = m.get('color') if re.match(r'^#[0-9a-fA-F]{6}$', str(m.get('color') or '')) else '#475569'
            out.append({'id': mid, 'name': name, 'role': str(m.get('role') or '').strip()[:4000], 'color': color,
                        'why': ' '.join(str(m.get('why') or '').split())[:140], 'reports_to': str(m.get('reports_to') or '')})
        if not any(m['id'] == 'lead' for m in out):
            out.insert(0, dict(STARTERS['lead'], id='lead'))
        out.sort(key=lambda m: m['id'] != 'lead')
        ids = {m['id'] for m in out}
        for m in out:   # reporting lines: to someone on the team, never in a loop, at most MAX_DEPTH levels
            if m['id'] == 'lead':
                m.pop('reports_to', None)
                continue
            if m['reports_to'] not in ids or m['reports_to'] == m['id']:
                m['reports_to'] = 'lead'
        by = {m['id']: m for m in out}
        for m in out:
            seen, x, depth = set(), m, 1
            while boss_of(x) and depth <= MAX_DEPTH:
                if x['id'] in seen:
                    break
                seen.add(x['id'])
                x, depth = by[boss_of(x)], depth + 1
            if depth > MAX_DEPTH or (boss_of(x) and x['id'] in seen):
                m['reports_to'] = 'lead'
        team = {'members': out}
        path = os.path.join(self.teams_dir, bid + '.json')
        with open(path + '.tmp', 'w') as f:
            json.dump(team, f)
        os.replace(path + '.tmp', path)
        return team

    # ---------- what the page sees ----------
    def view(self, bid=None):
        """The tasks on one board, or on every board (bid None: the phone app's Agents screen)."""
        with self.lock:
            runs = [r for r in self.runs.values() if bid is None or r['board'] == bid]
        return [{'id': r['id'], 'board': r['board'], 'task': r['task'], 'status': r['status'], 'frame': r['frame'], 'focus': r.get('focus'),
                 'member': {k: (r.get('member') or {}).get(k) for k in ('id', 'name', 'color')} if r.get('member') else None,
                 'parent': r.get('parent'), 'created': r['created'], 'ended': r.get('ended'), 'summary': r.get('summary'),
                 'step': r['steps'][-1]['text'] if r['steps'] else '', 'steps': len(r['steps']),
                 'question': (r.get('question') or {}).get('text'), 'note': (r.get('question') or {}).get('note'),
                 'page': r.get('page_url'), 'demo_v': r.get('demo_v', 0), 'error': r.get('error'),
                 'demos': ['api/demos/%s.html' % d for d in r.get('demos', {})]} for r in sorted(runs, key=lambda r: r['created'])]

    def steps(self, rid):
        r = self.runs.get(rid)
        return r and {'id': rid, 'task': r['task'], 'status': r['status'], 'steps': r['steps']}

    # ---------- starting, answering, stopping ----------
    def start(self, bid, task, selected=(), anchor=None, mode='team', member=None, parent=None):
        """A new task. Its frame goes next to the selected items (anchor {x, y, unit} from the page), next to the Lead's
        frame for a teammate's part, or right of the board. mode 'team' gives it to the board's Lead when the team has
        teammates; 'one' is a single agent. member/parent: a teammate's part handed out by a Lead run."""
        rid = 'r' + secrets.token_hex(6)
        task = ' '.join(str(task).split())[:1500]
        team = self.team(bid)['members']
        if member is None and mode == 'team' and len(team) > 1:
            member = team[0]   # the Lead
        title = ('%s: %s' % (member['name'], task) if parent else task)[:90]

        def make_frame(board):
            items = board.get('items') or []
            unit = float((anchor or {}).get('unit') or 1 / max(((board.get('view') or {}).get('s') or 1), 1e-4))
            if anchor and 'x' in anchor:
                x, y = float(anchor['x']), float(anchor['y'])
            elif items:
                x, y = max(i['x'] + i['w'] for i in items) + 160 * unit, min(i['y'] for i in items)
            else:
                x, y = 100.0, 100.0
            pad, gap, card = 28 * unit, 18 * unit, 220 * unit
            w = 3 * card + 2 * gap + 2 * pad
            # keep clear of what is already there, including the room the frame will grow into below
            room = 900 * unit
            for _ in range(200):
                hit = [i for i in items if i.get('type') != 'arrow' and i['x'] < x + w and i['x'] + i['w'] > x
                       and i['y'] < y + room and i['y'] + i['h'] > y]
                if not hit:
                    break
                x = max(i['x'] + i['w'] for i in hit) + 60 * unit
            frame = {'id': 'f' + rid, 'type': 'area', 'text': title, 'color': '#f3f6fb', 'run': rid, 'agent': rid,
                     'x': x, 'y': y, 'w': w, 'h': 200 * unit, 'z': 1}
            board['items'] = [frame] + items
            if parent:   # the hand-off, drawn: an arrow from the Lead's frame to this one, named for the teammate
                lead_frame = next((i for i in items if i['id'] == self.runs[parent]['frame']), None)
                if lead_frame:
                    board['items'].append(arrow(lead_frame, frame, member['name'], member.get('color'), self.runs[parent]['id']))
            # where the next item goes, relative to the frame (so moving the frame moves where results land)
            return frame, {'u': unit, 'pad': pad, 'gap': gap, 'card': card, 'inner': w - 2 * pad, 'cx': 0, 'cy': 0, 'rowH': 0}
        frame, lay = self.store.edit_board(bid, make_frame)
        system = SYSTEM
        if member:
            system += '\n\nYOUR ROLE: you are the %s on this board\'s team. %s' % (member['name'], member.get('role', ''))
        mine = reports_of(team, member['id']) if member else []
        is_lead = bool(mine) and (member.get('id') == 'lead') == (not parent)   # a manager: the Lead on top, or one below it
        if is_lead:
            system += LEAD % ('\n'.join('- %s (%s): %s' % (m['name'], m.get('why', ''), m.get('role', '')) for m in mine),
                              MAX_ROUNDS if not parent else 3)
            if parent:
                system += ('\nYou report to the %s: they gave you this part. Your reports are only the people above; when '
                           'their work is done, put it together in your frame and finish, so it goes back up.'
                           % (self.runs[parent].get('member') or {}).get('name', 'Lead'))
        prompt = self.context(bid, selected)
        if parent:
            p = self.runs[parent]
            top = self.root(p)
            prompt += '\n\nTHE TEAM\'S TASK (from the person): %s' % top['task']
            earlier = '\n\n'.join(x for x in ((top.get('results') if top is not p else ''), p.get('results')) if x)
            if earlier:
                prompt += '\n\nRESULTS OF EARLIER ROUNDS:\n' + earlier[-16000:]
            prompt += '\n\nYOUR PART (from the %s): %s' % ((p.get('member') or {}).get('name', 'Lead'), task)
        else:
            prompt += '\n\nTASK: ' + task
        r = {'id': rid, 'board': bid, 'task': task, 'status': 'queued', 'created': now_ms(), 'frame': frame['id'],
             'focus': frame['id'], 'lay': lay, 'steps': [], 'selected': list(selected)[:200], 'lead': is_lead,
             'member': member, 'parent': parent, 'children': [],
             'model': 'code' if member and member.get('id') in STRONG_ROLES else None,
             'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}]}
        with self.lock:
            self.runs[rid] = r
        self.save(r)
        self.changed(bid)
        self.q.put(rid)
        return r

    def news(self):
        """What the person has not been told yet, on every board: whole tasks that ended, and questions waiting."""
        week = now_ms() - 7 * 86400 * 1000
        with self.lock:
            runs = list(self.runs.values())
        done = [r for r in runs if not r.get('parent') and r['status'] in ENDED and r.get('told') is False and (r.get('ended') or 0) > week]
        waiting = [r for r in runs if r['status'] == 'waiting' and r.get('question')]
        pick = lambda r: {'id': r['id'], 'board': r['board'], 'task': r['task'], 'status': r['status'], 'frame': r['frame'],
                          'summary': r.get('summary'), 'question': (r.get('question') or {}).get('text'), 'note': (r.get('question') or {}).get('note'),
                          'member': {k: (r.get('member') or {}).get(k) for k in ('name', 'color')} if r.get('member') else None,
                          'lead': bool(r.get('lead')), 'ended': r.get('ended'), 'error': r.get('error'),
                          'told': bool((r.get('question') or {}).get('told'))}
        return {'done': [pick(r) for r in sorted(done, key=lambda r: r['ended'])], 'waiting': [pick(r) for r in waiting]}

    def told(self, ids, questions=()):
        """Ended tasks, and waiting questions, the voice has read out: kept here, so a page reload does not read them again
        (the page's own list of read questions was lost on every refresh, and one was read out each time)."""
        for rid in ids:
            r = self.runs.get(rid)
            if r and r.get('told') is False:
                r['told'] = True
                self.save(r)
        for rid in questions:
            r = self.runs.get(rid)
            if r and r.get('question') and not r['question'].get('told'):
                r['question']['told'] = True
                self.save(r)

    def recent(self, bids, hours=6):
        """Tasks on these boards that are running, or ended in the last few hours: the voice can talk about them."""
        since = now_ms() - hours * 3600 * 1000
        with self.lock:
            runs = [r for r in self.runs.values() if r['board'] in bids and not r.get('parent')]
        return [r for r in runs if r['status'] in ACTIVE or (r.get('ended') or 0) > since]

    def progress(self, r):
        """A task in a few lines: its state, each teammate's part, and (when it ended) what it found."""
        lines = ['Task: %s' % r['task'], 'State: %s%s' % (r['status'], (' (last step: %s)' % r['steps'][-1]['text']) if r['steps'] and r['status'] in ACTIVE else '')]
        for c in [self.runs[c] for c in r.get('children', []) if c in self.runs]:
            lines.append('- %s: %s, part: %s%s' % ((c.get('member') or {}).get('name', 'Teammate'), c['status'], c['task'][:200],
                                                   (' (now: %s)' % c['steps'][-1]['text']) if c['steps'] and c['status'] in ACTIVE else ''))
        grouped = self.grouped(r)   # what really moved, so the voice never reports a rearrangement that did not happen
        if grouped:
            lines.append('Moved on the board: ' + '; '.join(grouped))
        elif REARRANGE.search(r['task']):
            lines.append('Moved on the board: nothing (no items were grouped or moved).')
        if r['status'] in ENDED:
            lines.append(self.results_of(r))
        return '\n'.join(lines)

    def follow_up(self, rid, text):
        """More work on a task that ended: the same agent (or Lead and team) carries on, in the same frame."""
        r = self.runs.get(rid)
        if not r or r.get('parent') or r['status'] not in ENDED or not str(text).strip():
            return False
        team = reports_of(self.team(r['board'])['members'], (r.get('member') or {}).get('id', 'lead'))
        r['messages'].append({'role': 'user', 'content': 'FOLLOW-UP from the person: %s\nWhat is already in your frame stays; add to '
                                                         'it, then finish again with a summary of what is new.' % str(text)[:2000] + (
            '\nHand the new work to the teammate whose role fits it (assign). Your teammates now: %s.'
            % '; '.join('%s: %s' % (m['name'], m.get('role', '')) for m in team) if r.get('lead') else '')})
        for k in ('finished', 'stop', 'frame_gone', 'ended', 'told', 'error'):
            r.pop(k, None)
        r['msg_base'] = sum(1 for m in r['messages'] if m['role'] == 'assistant')   # a fresh allowance of steps and rounds
        r['step_base'] = len(r['steps'])
        self.step(r, 'Follow-up: ' + str(text)[:200])
        r['status'] = 'queued'
        self.save(r)
        self.q.put(rid)
        return True

    def steer(self, rid, text):
        """The person redirects a running task. It goes to the top of the task (the Lead): parts of the current round
        still going are stopped (what they found comes back), and the Lead re-plans with the person's words and its team
        as it is now (teammates added since it started included). Teammates never redirect each other."""
        r = self.runs.get(rid)
        while r and r.get('parent'):   # direction given to a teammate's part goes to its Lead
            r = self.runs.get(r['parent'])
        text = str(text).strip()[:2000]
        if not r or r['status'] not in ACTIVE or not text:
            return None
        team = reports_of(self.team(r['board'])['members'], (r.get('member') or {}).get('id', 'lead'))
        r['steer_note'] = ('The person redirects this task: %s' % text + (
            '\nYour teammates now: %s.' % '; '.join('%s: %s' % (m['name'], m.get('role', '')) for m in team) if r.get('lead') else ''))
        self.step(r, 'You redirected it: ' + text[:200])
        self.save(r)
        if r['status'] == 'waiting':   # it was asking the person: the direction is the answer
            self.answer(r['id'], text)
        elif r['status'] == 'team':   # stop the parts it is waiting on; when they have all ended, it wakes with the note
            for c in r.get('round', []):
                if c in self.runs and self.runs[c]['status'] not in ENDED:
                    self.stop(c)
        return r['id']

    def root(self, r):
        while r.get('parent') in self.runs:
            r = self.runs[r['parent']]
        return r

    def answer(self, rid, text):
        r = self.runs.get(rid)
        if not r or r['status'] != 'waiting' or not r.get('question'):
            return False
        q = r.pop('question')
        r['approved'] = bool(YES.search(str(text)))   # a yes lets one waiting browser action through
        r['messages'].append({'role': 'tool', 'tool_call_id': q['call'], 'content': 'The person answered: ' + str(text)[:2000]})
        self.step(r, 'You answered: ' + str(text)[:200])
        r['status'] = 'queued'
        self.save(r)
        self.q.put(rid)
        return True

    def stop(self, rid):
        r = self.runs.get(rid)
        if not r or r['status'] not in ACTIVE:
            return False
        r['stop'] = True
        for c in r.get('children', []):   # stopping a Lead stops its team's parts too
            self.stop(c)
        if r['status'] in ('queued', 'waiting', 'team'):   # nothing in flight: stop now (a working run stops after its current step)
            r['status'] = 'stopped'
            r.pop('question', None)
            self.save(r)
            self.ended(r)
        return True

    def ended(self, r):
        """A run ended: a whole task is news for the person (until they are told); a teammate's part may let its Lead go on."""
        if r.get('browser'):   # its browser session is not needed any more
            self.browser(r, 'close', {}, shot=False)
            r.pop('browser', None)
        if not r.get('ended'):
            r['ended'] = now_ms()
            if not r.get('parent'):
                r['told'] = False
            self.save(r)
        p = self.runs.get(r.get('parent'))
        if p and p['status'] == 'team':
            self.team_check(p)

    def team_check(self, p):
        """When every part of the Lead's current round has ended, give the Lead their results and wake it."""
        parts = [self.runs[c] for c in p.get('round', []) if c in self.runs]
        if any(c['status'] not in ENDED for c in parts) or not p.get('team_call'):
            return
        text = '\n\n'.join(self.results_of(c) for c in parts)
        p['results'] = ((p.get('results') or '') + '\n\n' + text).strip()
        p['messages'].append({'role': 'tool', 'tool_call_id': p.pop('team_call'), 'content': 'Your teammates are done.\n\n' + text})
        self.step(p, 'The team finished this round')
        p['status'] = 'queued'
        self.save(p)
        self.q.put(p['id'])

    def results_of(self, c):
        """What one teammate's part produced: its summary and the words it put in its frame."""
        words = []
        for it in self.board_items(c['board']):
            if it.get('agent') == c['id'] and it.get('type') in ('note', 'link'):
                words.append(it.get('text') or it.get('url') or '')
            elif it.get('agent') == c['id'] and it.get('type') == 'area' and it['id'] != c['frame']:   # a group it made
                words.append('Group: ' + (it.get('text') or ''))
        body = ' '.join('\n'.join(words).split(' ')[:RESULT_WORDS])
        return '%s (%s), part: %s\nSummary: %s\nIn their frame:\n%s' % (
            (c.get('member') or {}).get('name', 'Teammate'), c['status'], c['task'], c.get('summary') or '(none)', body or '(nothing)')

    # ---------- the loop ----------
    def work(self):
        while True:
            rid = self.q.get()
            r = self.runs.get(rid)
            if not r or r['status'] != 'queued':
                continue
            try:
                self.run(r)
            except Exception as e:   # a run never takes the worker down with it
                r['status'], r['error'] = 'failed', str(e) if isinstance(e, models.NoModel) else '%s: %s' % (type(e).__name__, e)
                self.step(r, 'Stopped by an error: ' + r['error'][:200])
                self.save(r)
            if r['status'] in ENDED:
                self.ended(r)

    def run(self, r):
        r['status'] = 'working'
        self.save(r)
        while r['status'] == 'working':
            if r.get('steer_note'):
                r['messages'].append({'role': 'user', 'content': r.pop('steer_note')})
            if r.get('stop'):
                r['status'] = 'stopped'
                break
            if sum(1 for m in r['messages'] if m['role'] == 'assistant') - r.get('msg_base', 0) >= MAX_STEPS:
                self.wrap_up(r)   # out of steps: one last turn to write down what it learned, so nothing is lost
                r['status'] = 'done'
                break
            offered = LEAD_TOOLS if r.get('lead') else TOOLS + (BROWSER_TOOLS if BROWSER_URL else [])
            msg = self.call(r['messages'], offered, r.get('model'))
            names = {t['function']['name'] for t in offered}
            calls = msg.get('tool_calls') or []
            content = re.sub(r'<think>.*?</think>', '', msg.get('content') or '', flags=re.S).strip()
            if not calls and '<tool_call>' in content:   # tool calls written out as text: take them as the calls they are
                calls, content = text_calls(content)
            if not calls and (msg.get('_cut') or not content) and r.get('empty', 0) < 3:
                # cut off at the length limit (a whole page in one call), or nothing at all: say so and go on, never "done"
                r['empty'] = r.get('empty', 0) + 1
                r['messages'].append({'role': 'user', 'content': (
                    'Your reply was cut off at the length limit, so nothing was done. If it was a page for make_demo, make it '
                    'shorter: one compact page, under about 350 lines.' if msg.get('_cut') else
                    'Your reply was empty. Carry on with your next tool call.')})
                self.step(r, 'Its reply was cut off; trying again shorter' if msg.get('_cut') else 'Its reply was empty; carrying on')
                self.save(r)
                continue
            r['messages'].append({'role': 'assistant', 'content': content or None, **({'tool_calls': calls} if calls else {}),
                                  **({'_raw': msg['_raw']} if msg.get('_raw') else {})})   # Claude's own blocks go back as they were
            builder = r.get('parent') and BUILDING.search(r['task']) and not r.get('lead') and 'make_demo' in names
            if not calls and builder and not r.get('built') and r.get('nudged_build', 0) < 3:
                r['nudged_build'] = r.get('nudged_build', 0) + 1   # a build part does not end with words: it builds
                r['messages'].append({'role': 'user', 'content': 'Your part is to build a working demo. Build it now with make_demo.'})
                continue
            if not calls:   # plain words instead of a tool: they are the result
                if content:
                    self.place_text(r, content, 'body')
                r['status'] = 'done'
                break
            r['refused'] = False
            for c in calls:
                name = c['function']['name']
                try:
                    args = json.loads(c['function'].get('arguments') or '{}')
                except ValueError:
                    args = {}
                key = name + json.dumps(args, sort_keys=True)
                if name not in names:   # the model can call tools it was not given (a Lead searching the web): refuse
                    result = 'You do not have a tool called %s. Your tools: %s.' % (name, ', '.join(sorted(names)))
                elif key in r.setdefault('done_calls', []) and name in ('search_board', 'read_item', 'read_frame', 'web_search', 'read_web_page', 'read_page_source', 'search_code', 'browser_look'):
                    result = 'You already did exactly this and its result is above. Do something different.'
                else:
                    r['done_calls'].append(key)
                    try:
                        result = self.use(r, name, args if isinstance(args, dict) else {}, c['id'])
                    except Exception as e:   # a tool that breaks (a bad address, a page that will not load) is reported, not fatal
                        result = 'That did not work (%s: %s). Try another way.' % (type(e).__name__, str(e)[:200])
                if result is not None:
                    r['messages'].append({'role': 'tool', 'tool_call_id': c['id'], 'content': result})
                    if result.startswith(('Not added', 'Not done', 'Not typed')):
                        r['refused'] = True
            if any(c['function']['name'] == 'make_demo' for c in calls):
                r['built'] = True
            repeats = sum(1 for m in r['messages'][-2 * len(calls) - 1:] if m['role'] == 'tool'
                          and str(m.get('content', '')).startswith('You already did exactly this'))
            steps_in = sum(1 for m in r['messages'] if m['role'] == 'assistant') - r.get('msg_base', 0)
            if builder and not r.get('built') and (steps_in == 8 or repeats >= 1) and not r.get('nudged_build', 0) > 2:
                # a part that is to BUILD something keeps researching instead (a Developer once searched to the step limit)
                r['nudged_build'] = r.get('nudged_build', 0) + 1
                r['messages'].append({'role': 'user', 'content': 'Build it now: one complete HTML page with make_demo, from the specs '
                                      'and findings you already have (in the results of earlier rounds and on the board). '
                                      'Then check it with try_demo and improve it. Do not search further first.'})
            elif repeats >= 2:
                r['messages'].append({'role': 'user', 'content': 'You keep repeating the same action. Do something different, or '
                                      'write down what you have and finish.'})
            if any(c['function']['name'] in WRITES for c in calls):
                r['quiet'] = 0
            else:   # reading and searching without writing anything down: after a while, say so
                r['quiet'] = r.get('quiet', 0) + 1
                if r['quiet'] >= 12 and not r.get('lead'):
                    r['quiet'] = 0
                    r['messages'].append({'role': 'user', 'content': 'You have taken 12 steps without putting anything on the board. '
                                          'Write down what you understand so far now (add_text or add_note), then carry on.'})
            if r.get('frame_gone'):
                r['status'] = 'stopped'
                self.step(r, 'The task frame was deleted, so I stopped.')
            elif r.get('finished'):
                r['status'] = 'done'
            elif r.get('question'):
                r['status'] = 'waiting'
            elif r.get('team_call'):
                r['status'] = 'team'
                self.save(r)
                self.team_check(r)   # (every part could already have ended)
                break
            self.save(r)
        r.pop('stop', None)
        self.save(r)

    def wrap_up(self, r):
        r['messages'].append({'role': 'user', 'content': 'You are out of steps. Write down what you found now, plainly and '
                              'with the exact code or sources you saw: add_text, then finish.'})
        try:
            msg = self.call(r['messages'], [t for t in TOOLS if t['function']['name'] in ('add_text', 'finish')], r.get('model'))
        except (OSError, ValueError, KeyError):
            msg = {}
        calls = msg.get('tool_calls') or []
        content = re.sub(r'<think>.*?</think>', '', msg.get('content') or '', flags=re.S).strip()
        if not calls and '<tool_call>' in content:
            calls, content = text_calls(content)
        wrote = False
        for c in calls:
            try:
                a = json.loads(c['function'].get('arguments') or '{}')
            except ValueError:
                continue
            text = a.get('text') or a.get('summary')
            if text:
                if c['function']['name'] == 'finish':
                    r['summary'] = str(text)[:2000]
                if c['function']['name'] == 'add_text' or not wrote:
                    self.place_text(r, text, a.get('style') if a.get('style') in ('heading', 'body') else 'body')
                    wrote = True
        if content and not wrote:
            self.place_text(r, content, 'body')
            wrote = True
        self.step(r, 'Ran out of steps and wrote down what it found' if wrote else 'Ran out of steps')
        if not wrote:
            self.place_text(r, 'I stopped after %d steps. What is in this frame is what I found so far.' % MAX_STEPS, 'body')

    def call(self, messages, tools, model=None):
        job = 'builders' if model == 'code' else 'agents'
        if models.choice(job):   # an outside provider picked in Settings
            choice = models.complete(job, {'messages': trimmed(messages), 'tools': tools, 'max_tokens': 16000 if job == 'builders' else 3000}, 900)['choices'][0]
            return dict(choice['message'], _cut=choice.get('finish_reason') == 'length')
        if model == 'code':   # the strongest model that answers (the one picked in Settings first); spare boxes can be off
            picked = models.local('builders')
            for url, name in ([picked] if picked else []) + [m for m in CODE_MODELS if m != picked]:
                try:
                    return self.ask_model(messages, tools, url, name, big=True)
                except OSError:
                    continue
        url, name = models.local('agents') or (MODEL_URL, MODEL)
        if not url:
            raise models.NoModel(job)
        return self.ask_model(messages, tools, url, name)

    def ask_model(self, messages, tools, url, name, big=False):
        body = {'model': name, 'messages': trimmed(messages), 'tools': tools, 'max_tokens': 16000 if big or name in CODE_MODEL_NAMES else 3000, 'temperature': 0.3,
                'chat_template_kwargs': {'enable_thinking': False}}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=900) as f:
            choice = json.load(f)['choices'][0]
        return dict(choice['message'], _cut=choice.get('finish_reason') == 'length')

    def use(self, r, name, a, call_id):
        """Run one tool; returns what the model reads back (None = answered later, by the person)."""
        if name == 'search_board':
            hits = self.search(r['board'], a.get('query', ''))
            self.step(r, 'Searched the board for "%s"' % a.get('query', ''))
            if hits:
                r['focus'] = hits[0]['item']
            return '\n\n'.join('[%s] %s: %s' % (h['item'], h['kind'], h['text']) for h in hits) or 'Nothing on the board matches.'
        if name == 'read_item':
            it = next((i for i in self.board_items(r['board']) if i['id'] == a.get('item_id')), None)
            if not it:
                return 'There is no item with that id.'
            kind, words = talk.item_words(it, self.reader)
            r['focus'] = it['id']
            self.step(r, 'Read a %s on the board' % kind)
            return '%s: %s' % (kind, ' '.join(words.split()[:2500]) or '(no words)')
        if name == 'read_frame':
            return self.read_frame(r, str(a.get('frame') or ''))
        if name == 'group_items':
            return self.group_items(r, str(a.get('title') or '').strip()[:80], a.get('item_ids'))
        if name in ('web_search', 'search_board') and not str(a.get('query') or '').strip():
            return 'Give the search some words.'
        if name == 'web_search':
            self.step(r, 'Searched the web for "%s"' % a.get('query', ''))
            r['focus'] = r['frame']
            try:
                hits = web.search(a.get('query', ''))
            except OSError as e:
                return 'The search did not work (%s). Try other words or read a page you know.' % e
            self.saw(r, ' '.join(h['url'] for h in hits))
            return '\n\n'.join('%s\n%s\n%s' % (h['title'], h['url'], h['snippet']) for h in hits) or 'No results.'
        if name == 'read_web_page':
            url = a.get('url', '')
            self.step(r, 'Read %s' % url[:120])
            r['focus'] = r['frame']
            try:
                title, words = web.read_page(url)
            except (OSError, ValueError) as e:
                return 'Could not read that page (%s).' % e
            r.setdefault('read_urls', []).append(url)
            return '%s\n%s' % (title, words or '(no text on the page)')
        if name in ('read_page_source', 'read_web_page', 'add_link', 'search_code', 'read_code'):   # small models garble long file names: fix the case
            url = str(a.get('url') or '').strip()
            a = dict(a, url=next((u for u in r.get('seen_urls', []) if u.lower() == url.lower()), url))
        if name == 'read_code':
            url = a.get('url', '')
            self.step(r, 'Read the code of %s from statement %s' % (url[:100], a.get('start')))
            try:
                found = web.read_code(url, a.get('start'), a.get('count') or 120)
            except (OSError, ValueError, TypeError) as e:
                return 'Could not read that file (%s).' % e
            r.setdefault('read_urls', []).append(url)
            return found
        if name == 'search_code':
            url = a.get('url', '')
            self.step(r, 'Searched the code of %s%s' % (url[:100], (' for ' + str(a.get('words'))[:60]) if a.get('words') else ''))
            r['focus'] = r['frame']
            try:
                found = web.search_code(url, a.get('words', ''))
            except (OSError, ValueError) as e:
                return 'Could not read that file (%s).' % e
            r.setdefault('read_urls', []).append(url)
            return found
        if name == 'read_page_source':
            url = a.get('url', '')
            self.step(r, 'Read the source of %s' % url[:120])
            r['focus'] = r['frame']
            try:
                found = web.read_source(url)
            except (OSError, ValueError) as e:
                return 'Could not read that page (%s).' % e
            r.setdefault('read_urls', []).append(url)
            self.saw(r, found)
            return found
        if name.startswith('browser_'):
            return self.use_browser(r, name, a)
        if name in ('add_note', 'add_text'):   # a guess is sent back once; written again, it goes on the board marked as one
            text = str(a.get('text', ''))
            if GUESS.search(text) and text not in r.setdefault('guesses', []):
                r['guesses'].append(text)
                return ('Not added: it guesses ("%s"). Put on the board only what you found, with where you found it. If you '
                        'did not find something, say that plainly instead.' % GUESS.search(text).group(0))
            if GUESS.search(text):
                a = dict(a, text='Not confirmed: ' + text)
        if name == 'add_note':   # a finding needs a page the agent really read: search snippets alone are not enough
            src = str(a.get('source') or '').strip()
            if src.lower() in ('browser', 'the browser', 'page', 'this page', 'current page', 'web page') and r.get('page_url'):
                src = r['page_url']   # the page open in its browser
            if src.lower() != 'board' and src.rstrip('/') not in [u.rstrip('/') for u in r.get('read_urls', [])]:
                return ('Not added: read that page with read_web_page first, then add the note with the page as its source. '
                        'Pages you have read: %s' % (', '.join(r.get('read_urls', [])) or 'none yet'))
            text = str(a.get('text', '')).strip()
            key = ' '.join(re.findall(r'[a-z0-9]+', text.lower())[:8])
            if key and any(key == ' '.join(re.findall(r'[a-z0-9]+', (i.get('text') or '').lower())[:8])
                           for i in self.board_items(r['board']) if i.get('agent') == r['id']):
                return 'Not added: that is already on a note in your frame. Add something new, or finish.'
            self.place_note(r, text + ('' if src.lower() == 'board' or src in text else '\n\n' + src))
            self.step(r, 'Added a note')
            return 'Added.'
        if name == 'add_text':
            self.place_text(r, a.get('text', ''), a.get('style') if a.get('style') in ('heading', 'body') else 'body')
            self.step(r, 'Added a heading' if a.get('style') == 'heading' else 'Added text')
            return 'Added.'
        if name == 'add_link':
            url = a.get('url', '')
            if not re.match(r'^https?://\S+$', url):
                return 'That is not a web link.'
            self.place(r, {'type': 'link', 'url': url}, 280 * r['lay']['u'], 110 * r['lay']['u'])
            self.step(r, 'Added a link to %s' % url[:100])
            return 'Added.'
        if r.get('lead') and name in ('add_text', 'finish') and not any(
                x['text'].startswith('Handed out') for x in r['steps'][r.get('step_base', 0):]):
            # a Lead puts teammates' results together; it does not do the work itself (it once pasted code instead of a demo)
            return ('Not done: you are the Lead. Hand this to the teammate whose role fits it (assign) first; you write only '
                    'the final answer, from their results.')
        if r.get('lead') and name in ('search_board', 'read_item') and not r.get('children') and sum(
                1 for x in r['steps'][r.get('step_base', 0):] if x['text'].startswith(('Searched the board', 'Read a'))) >= 5:
            # a manager that keeps looking instead of delegating (a Lead once searched the board 14 times first)
            return 'Not done: you have looked enough. Hand the work out now with assign; your reports will dig deeper.'
        if name == 'make_demo':
            return self.make_demo(r, str(a.get('title') or 'Demo')[:80], str(a.get('html') or ''))
        if name == 'try_demo':
            return self.try_demo(r, str(a.get('title') or ''), a.get('steps') if isinstance(a.get('steps'), list) else [])
        if name == 'ask_user' and r.get('lead') and not r.get('children') and not r.get('asked_check'):
            r['asked_check'] = True   # a Lead asks before handing out anything: once, it is pointed at its team
            team = reports_of(self.team(r['board'])['members'], (r.get('member') or {}).get('id', 'lead'))
            return ('Not asked yet: can a teammate find this out? %s. If so, hand it to them with assign. Ask the person only '
                    'for a choice only they can make, or something only they know; then ask again.'
                    % '; '.join('%s: %s' % (m['name'], m.get('role', '')) for m in team))
        if name == 'ask_user':
            if r.get('question'):
                return 'You already asked a question; wait for that answer first.'
            u = r['lay']['u']
            note = self.place(r, {'type': 'note', 'text': str(a.get('question', ''))[:600], 'color': '#ffc9de', 'ask': r['id'],
                                  'fs': 14 * u}, 220 * u, 190 * u)
            r['question'] = {'call': call_id, 'text': str(a.get('question', ''))[:600], 'note': note and note['id']}
            self.step(r, 'Asked you: ' + r['question']['text'][:160])
            return None
        if name == 'assign' and r.get('lead'):
            return self.assign(r, a.get('assignments'), call_id)
        if name == 'finish' and r.get('parent') and BUILDING.search(r['task']) and not r.get('built') and not r.get('lead') \
                and r.get('nudged_build', 0) < 3 and BROWSER_URL is not None:
            r['nudged_build'] = r.get('nudged_build', 0) + 1
            return 'Not finished: your part is to build a working demo, and none is built yet. Build it with make_demo first.'
        if name == 'finish':
            asked = ' '.join([r['task']] + [m['content'] for m in r['messages'] if m['role'] == 'user' and 'FOLLOW-UP' in m['content']])
            if REARRANGE.search(asked) and not r.get('move_check') and not self.moved(r):
                r['move_check'] = True   # once: it may still end, but saying plainly that nothing moved
                return (('Not finished: the task asks to rearrange the board, but nothing on it was moved. Hand that to a '
                         'teammate (they have read_frame and group_items), or say plainly in your summary that nothing was moved.')
                        if r.get('lead') else
                        ('Not finished: your part asks to rearrange the board, but you have not moved anything. Use read_frame, '
                         'then group_items once per group; or say plainly in your summary that nothing was moved.'))
            left = self.left(r) if REARRANGE.search(asked) and self.moved(r) else []
            if left and not r.get('left_check'):
                r['left_check'] = True   # once: it groups the rest, or says what it left
                return ('Not finished: %d items in the frame you read are not in a group yet: %s. Group them too (a group for '
                        'odd ones out is fine), or say plainly in your summary which ones you left.'
                        % (len(left), ', '.join(x[0]['id'] for x in left[:80])))
            if r.get('refused'):   # it cannot report work that was just turned down
                return 'Not finished: something you just did was not done (see the replies above). Fix it first, then finish.'
            r['summary'] = str(a.get('summary') or '')[:2000]
            g = self.grouped(r)
            if g:   # the facts, in code, next to its own words
                n = sum(int(re.match(r'Grouped (\d+)', x).group(1)) for x in g)
                r['summary'] += ' [On the board: %d items moved into %d groups; %d items in the frames read are not in a group.]' % (
                    n, len(g), len(self.left(r)))
            if a.get('summary') and not any(s['text'] == 'Added text' for s in r['steps'][r.get('step_base', 0):]):   # it already wrote its answer out
                self.place_text(r, 'Summary', 'heading')
                self.place_text(r, a['summary'], 'body')
            r['finished'] = True
            self.step(r, 'Finished')
            return 'Done.'
        return 'There is no tool called %s.' % name

    def assign(self, r, parts, call_id):
        """The Lead hands out a round of work: one run per part, each in its own frame right of the Lead's."""
        if r.get('team_call'):
            return 'You already handed out work; wait for it.'
        limit = MAX_ROUNDS if not r.get('parent') else 3
        if sum(1 for s in r['steps'][r.get('step_base', 0):] if s['text'].startswith('Handed out')) >= limit:
            return 'You have handed out work %d times. Put the final answer together yourself and finish.' % limit
        team = {m['name'].lower(): m for m in reports_of(self.team(r['board'])['members'], (r.get('member') or {}).get('id', 'lead'))}
        if not isinstance(parts, list) or not parts:
            return 'Give at least one part: [{"teammate": name, "task": what to do}].'
        bad = [str(p.get('teammate')) for p in parts if not isinstance(p, dict) or str(p.get('teammate', '')).lower() not in team]
        if bad:
            return 'No teammate called %s. Your teammates: %s.' % (', '.join(bad), ', '.join(m['name'] for m in team.values()))
        # design first: for something people will use, the designers' specs come before the Developer builds
        ids = {n: m['id'] for n, m in team.items()}
        wanted = [ids.get(str(p.get('teammate', '')).lower()) for p in parts]
        designed = {(self.runs[c].get('member') or {}).get('id') for c in r.get('children', [])
                    if c in self.runs and self.runs[c]['status'] in ENDED}
        asked = ' '.join([r['task']] + [m['content'] for m in r['messages'] if m['role'] == 'user' and 'FOLLOW-UP' in m['content']])
        if 'ui' in wanted and 'ux' in ids.values() and 'ux' not in designed:   # the look follows the UX spec
            return ('Not handed out: the UI Designer works from the UX spec. Hand the UX Designer its part first, and the UI '
                    'Designer in the next round.')
        design = {'ux', 'ui', 'design-lead'}
        if 'developer' in wanted and BUILD.search(asked) and design & set(ids.values()) and not design & designed:
            have = [m['name'] for m in team.values() if m['id'] in design]
            return ('Not handed out: design comes first for something people will use. Hand the design part now (%s); the '
                    'Developer builds after that, from the design.' % ', '.join(have))
        if REARRANGE.search(asked) and not self.moved(self.root(r)) and not any(
                isinstance(p, dict) and REARRANGE.search(str(p.get('task', ''))) for p in parts):
            # a Lead turned "restructure the frame into themes" into "identify themes" + "write sections", and sent the same
            # parts again 37 times when told why: so the first part gets the moving added here, in code
            parts = [dict(parts[0], task=str(parts[0].get('task', '')).rstrip('. ') + '. Then move the items into those themes '
                          'on the board: read_frame that frame, then one group_items call per theme, every item in exactly one group.')] + parts[1:]
        frame = next((i for i in self.board_items(r['board']) if i['id'] == r['frame']), None)
        if not frame:
            r['frame_gone'] = True
            return 'Your frame was deleted.'
        u = r['lay']['u']
        r['round'] = []
        for p in parts[:6]:
            c = self.start(r['board'], p.get('task', ''), r.get('selected', []), {'x': frame['x'] + frame['w'] + 200 * u, 'y': frame['y'], 'unit': u},
                           member=team[str(p['teammate']).lower()], parent=r['id'])
            r['children'].append(c['id'])
            r['round'].append(c['id'])
        r['team_call'] = call_id
        self.step(r, 'Handed out %d part%s: %s' % (len(r['round']), '' if len(r['round']) == 1 else 's',
                                                    ', '.join(str(p['teammate']) for p in parts[:6])))
        return None

    def use_browser(self, r, name, a):
        cmd = name[len('browser_'):]
        el = (r.get('page_els') or {}).get(str(a.get('n')))
        if cmd in ('click', 'type', 'select') and not el:
            return 'There is no thing number %s on the page you last looked at. Use browser_look.' % a.get('n')
        if cmd == 'type' and el.get('password'):
            return 'Not done: signing in is not handled yet, and agents never type passwords. If the task needs it, ask the person.'
        if cmd == 'type' and el.get('formKind') == 'personal' and not given(r, a.get('text', '')):
            return ('Not typed: "%s" did not come from the task, the board or the person. Never make up names, emails, phone '
                    'numbers or addresses: ask the person (ask_user) for it.' % str(a.get('text', ''))[:80])
        risky = cmd in ('click', 'type') and el and (
            (cmd == 'click' and (RISKY.search(el.get('name') or '') or (el.get('submits') and el.get('formKind') == 'personal')))
            or (cmd == 'type' and a.get('enter') and el.get('formKind') == 'personal'))
        host = re.sub(r'^https?://(www\.)?([^/]+).*', r'\2', r.get('page_url', ''))
        if risky and not (a.get('confirm') and r.pop('approved', False)):
            r['approved'] = False
            return ('Not done: this would send or commit something on %s ("%s"). Ask the person first with ask_user, saying exactly '
                    'what will be sent or done. Only if they say yes, do it again with confirm true.' % (host, el.get('name') or cmd))
        args = {'open': {'url': a.get('url', '')}, 'click': {'n': a.get('n')}, 'select': {'n': a.get('n'), 'option': a.get('option', '')},
                'type': {'n': a.get('n'), 'text': a.get('text', ''), 'enter': bool(a.get('enter'))},
                'scroll': {'dir': a.get('direction', 'down')}}.get(cmd, {})
        if cmd == 'open':
            url = str(a.get('url') or '').strip()
            args['url'] = next((u for u in r.get('seen_urls', []) if u.lower() == url.lower()), url)
        if cmd == 'scroll':   # scrolling again and again usually means it already has what it needs
            r['scrolls'] = r.get('scrolls', 0) + 1
            if r['scrolls'] > 4:
                r['scrolls'] = 0
                return ('You have scrolled 5 times in a row. Use what you have read (it is above): put it on the board, or '
                        'open another page. Scroll again only if you still need something further down.')
        else:
            r['scrolls'] = 0
        what = {'open': 'Opened %s' % args.get('url', '')[:100], 'click': 'Clicked "%s"' % ((el or {}).get('name') or a.get('n')),
                'type': 'Typed into "%s"' % ((el or {}).get('name') or a.get('n')), 'select': 'Chose "%s"' % a.get('option', ''),
                'scroll': 'Scrolled %s' % a.get('direction', 'down'), 'back': 'Went back', 'look': 'Looked at the page'}[cmd]
        self.step(r, what + (' (you said yes)' if risky else ''))
        page = self.browser(r, cmd, args)
        if not page or 'elements' not in page:
            return 'The browser could not do that: %s' % ((page or {}).get('error') or 'no answer')
        r['page_url'] = page['url']
        r['page_els'] = {str(e['n']): {k: e.get(k) for k in ('name', 'role', 'submits', 'formKind', 'password')} for e in page['elements']}
        r.setdefault('read_urls', []).append(page['url'])   # what it reads in the browser can be a note's source
        self.saw(r, page['url'] + ' ' + ' '.join(e.get('href') or '' for e in page['elements']))
        return browser_text(page)

    def browser(self, r, cmd, args, shot=True):
        """One command to the browser service for this run's own session; keeps its latest screenshot for the board."""
        if not BROWSER_URL:
            return {'error': 'no browser is set up (BROWSER_URL)'}
        r['browser'] = True
        req = urllib.request.Request('%s/s/%s/%s' % (BROWSER_URL, r['id'], cmd), data=json.dumps(args).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Token': BROWSER_TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=100) as f:
                out = json.load(f)
        except (OSError, ValueError) as e:
            return {'error': 'the browser service did not answer (%s)' % e}
        if shot and 'elements' in out:
            try:
                req = urllib.request.Request('%s/s/%s/shot' % (BROWSER_URL, r['id']), headers={'X-Token': BROWSER_TOKEN})
                with urllib.request.urlopen(req, timeout=20) as f:
                    data = f.read()
                path = os.path.join(self.shots, r['id'] + '.jpg')
                with open(path + '.tmp', 'wb') as f:
                    f.write(data)
                os.replace(path + '.tmp', path)
                r['shots'] = r.get('shots', 0) + 1
                if not r.get('shot_item'):   # the browser view goes on the board, in the task frame
                    u = r['lay']['u']
                    it = self.place(r, {'type': 'shot', 'run': r['id'], 'label': out.get('url', '')}, 480 * u, 300 * u, wide=True)
                    r['shot_item'] = it and it['id']
            except OSError:
                pass
        return out

    def make_demo(self, r, title, page):
        """Save the agent's page as a demo, show it on the board as a live card, and tell the agent how it ran."""
        if '<' not in page or len(page) < 40:
            return 'Not built: give the whole HTML page in html.'
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:40] or 'demo'
        did = '%s-%s' % (r['id'], slug)
        with open(os.path.join(self.demos, did + '.html.tmp'), 'w') as f:
            f.write(page[:400000])
        os.replace(os.path.join(self.demos, did + '.html.tmp'), os.path.join(self.demos, did + '.html'))
        demos = r.setdefault('demos', {})
        first = did not in demos
        demos[did] = demos.get(did, 0) + 1
        r['demo_v'] = r.get('demo_v', 0) + 1   # the page reloads its demo cards when this changes
        if first:   # a big live card in the frame, sized for a page
            u = r['lay']['u']
            self.place(r, {'type': 'web', 'url': 'api/demos/%s.html' % did, 'demo': True, 'run': r['id'], 'label': title},
                       r['lay']['inner'], 520 * u, wide=True)
        self.step(r, ('Built the demo "%s"' if first else 'Updated the demo "%s"') % title)
        if not BROWSER_URL:
            return 'The demo is on the board. (No browser is set up to check it for errors.)'
        page_seen = self.browser(r, 'render', {'html': page})
        if 'elements' not in page_seen:
            return 'The demo is on the board, but the browser could not check it: %s' % page_seen.get('error')
        errs = page_seen.get('errors') or []
        looks = self.see(r, 'This is a screenshot of a demo web page an agent just built: "%s". Describe plainly what is '
                            'actually visible and where, and anything that looks broken, empty or missing. Under 90 words.' % title)
        return ('The demo is on the board. ' + ('It runs with no JavaScript errors.' if not errs else
                'JAVASCRIPT ERRORS (fix them and call make_demo again with the same title): ' + ' | '.join(errs)) +
                ('\nWHAT IT LOOKS LIKE (a model looked at a screenshot): ' + looks +
                 '\nIf that is not what the demo should show, fix it and call make_demo again with the same title.' if looks else '') +
                '\nText on the page:\n' + page_seen.get('text', '')[:1200])

    def try_demo(self, r, title, steps):
        """Play a demo on this board (by title, a teammate's too) step by step and describe each state from its picture."""
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:40]
        with self.lock:
            mine = [x for x in self.runs.values() if x['board'] == r['board'] and x.get('demos')]
        found = sorted(((x['created'], d) for x in mine for d in x['demos']), reverse=True)
        pick = next((d for _, d in found if slug and d.endswith('-' + slug)), None) or next(
            (d for _, d in found if slug and slug in d), None)
        if not pick:
            return 'No demo called "%s" on this board. Demos here: %s.' % (title, ', '.join(
                d.split('-', 1)[1] for _, d in found) or 'none yet')
        with open(os.path.join(self.demos, pick + '.html')) as f:
            page = f.read()
        steps = [str(x)[:80] for x in steps][:6]
        self.step(r, 'Tried the demo "%s"%s' % (title, (': ' + ', '.join(steps))[:150] if steps else ''))
        seen = self.browser(r, 'render', {'html': page, 'steps': steps}, shot=False)
        if 'states' not in seen:
            return 'The browser could not play it: %s' % seen.get('error')
        out = []
        for i, st in enumerate(seen['states']):
            img = self.browser_shot(r, i)
            looks = self.see(r, 'A screenshot of the demo "%s" %s. Describe plainly what is visible and where, and anything that '
                                'looks broken, empty, cramped or unreadable. Under 80 words.' % (
                                    title, 'as first opened' if i == 0 else 'after: ' + st['step']), img) if img else ''
            out.append('%s: %s%s' % (st['step'].upper() if i else 'OPENED', looks or '(no description: no vision model)',
                                     (' ERRORS: ' + ' | '.join(st['errors'])) if st['errors'] else ''))
        return 'Demo "%s":\n' % title + '\n'.join(out)

    def browser_shot(self, r, i):
        try:
            req = urllib.request.Request('%s/s/%s/shot?i=%d' % (BROWSER_URL, r['id'], i), headers={'X-Token': BROWSER_TOKEN})
            with urllib.request.urlopen(req, timeout=20) as f:
                return f.read()
        except OSError:
            return None

    def see(self, r, question, img=None):
        """Ask the vision model about a picture (default: the run's latest screenshot); '' when it cannot."""
        path = os.path.join(self.shots, r['id'] + '.jpg')
        vurl, vmodel = models.local('vision') or (VISION_MODEL_URL, VISION_MODEL)   # picked in Settings, else as at startup
        if not (vurl or models.choice('vision')) or (img is None and not os.path.exists(path)):
            return ''
        if img is None:
            with open(path, 'rb') as f:
                img = f.read()
        img = base64.b64encode(img).decode()
        body = {'model': vmodel, 'max_tokens': 300, 'temperature': 0.2, 'chat_template_kwargs': {'enable_thinking': False},
                'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': question},
                                                          {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + img}}]}]}
        try:
            if models.choice('vision'):
                out = models.complete('vision', body, 90)
            else:
                req = urllib.request.Request(vurl, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=90) as f:
                    out = json.load(f)
            return re.sub(r'<think>.*?</think>', '', out['choices'][0]['message']['content'] or '', flags=re.S).strip()[:900]
        except (OSError, ValueError, KeyError, IndexError):
            return ''

    def saw(self, r, text):
        """Remember the web addresses a tool showed the agent, exactly as written."""
        seen = r.setdefault('seen_urls', [])
        for u in re.findall(r'https?://[^\s,;"\'<>)]+', text):
            if u not in seen:
                seen.append(u)
        del seen[:-300]

    # ---------- the board ----------
    def board_items(self, bid):
        try:
            return self.store.read_board(bid).get('items') or []
        except (OSError, ValueError):
            return []

    def context(self, bid, selected):
        b = self.store.read_board(bid)
        lines = ['BOARD: "%s"' % b.get('name', '')]
        br = ((self.brief(bid) or {}).get('brief') or {}).get('summary')
        if br:
            lines.append('WHAT THE BOARD IS ABOUT: ' + br)
        items = b.get('items') or []
        chosen = [i for i in items if i['id'] in selected]
        for f in [i for i in chosen if i.get('type') == 'area']:   # a selected frame brings everything inside it
            chosen += [i for i in items if i not in chosen and f['x'] <= i['x'] + i['w'] / 2 <= f['x'] + f['w']
                       and f['y'] <= i['y'] + i['h'] / 2 <= f['y'] + f['h']]
        picked, words = [], 0
        for it in chosen:
            kind, text = talk.item_words(it, self.reader)
            w = text.split()
            if not w or words >= CONTEXT_WORDS:
                continue
            picked.append('[%s] %s: %s' % (it['id'], kind, ' '.join(w[:CONTEXT_WORDS - words])))
            words += len(w)
        lines.append('SELECTED ITEMS (the person pointed at these for this task):\n' + '\n\n'.join(picked) if picked
                     else 'Nothing was selected: use search_board to find what on the board relates to the task.')
        return '\n\n'.join(lines)

    def grouped(self, r):
        """The 'Grouped ...' steps of this run and every part under it."""
        return [x['text'] for x in r['steps'] if x['text'].startswith('Grouped')] + [
            g for c in r.get('children', []) if c in self.runs for g in self.grouped(self.runs[c])]

    def moved(self, r):
        return bool(self.grouped(r))

    def frame_called(self, items, ref):
        # the board's own frames only: "the 2026 frame" once matched an agent's task frame titled "...the 2026 frame..."
        frames = [i for i in items if i.get('type') == 'area' and not i.get('agent')]
        ref = ' '.join(ref.split()).lower()
        hit = next((f for f in frames if f['id'].lower() == ref or ' '.join((f.get('text') or '').split()).lower() == ref), None)
        words = set(re.findall(r'[a-z0-9]+', ref)) - {'the', 'frame', 'a', 'of', 'in', 'board'}
        score = lambda f: len(words & set(re.findall(r'[a-z0-9]+', (f.get('text') or '').lower())))
        return hit or max((f for f in frames if score(f)), key=score, default=None)

    def read_frame(self, r, ref):
        items = self.board_items(r['board'])
        f = self.frame_called(items, ref)
        if not f:
            return 'No frame called that. Frames on this board: %s.' % '; '.join(
                '"%s" (%s)' % ((i.get('text') or '')[:60], i['id']) for i in items if i.get('type') == 'area' and not i.get('agent'))[:3000]
        if f['id'] not in r.setdefault('read_frames', []):
            r['read_frames'].append(f['id'])
        st = talk.stacks([i for i in items if talk.inside(f, i)])
        lines, words = [], 0
        for k, stack in enumerate(st):
            line = '[%s] ' % stack[0]['id'] + ' | with '.join(
                '%s: %s' % (kind, ' '.join(w.split()[:40])) for kind, w in (talk.item_words(i, self.reader) for i in stack))
            words += len(line.split())
            if words > 9000:
                lines.append('(%d more not shown)' % (len(st) - k))
                break
            lines.append(line)
        r['focus'] = f['id']
        self.step(r, 'Read the frame "%s" (%d items)' % ((f.get('text') or '')[:60], len(st)))
        return 'Frame "%s" has %d items:\n%s' % (f.get('text') or '', len(st), '\n'.join(lines) or '(empty)')

    def group_items(self, r, title, ids):
        """Move board items (and what is stacked under them) into a new titled frame in the run's frame."""
        if not title or not isinstance(ids, list) or not ids:
            return 'Not done: give the group a title and the ids of the items to put in it.'
        if not r.get('read_frames'):   # grouping from a few search hits once left 50 of 60 items behind
            return 'Not done: first read the whole frame with read_frame, so every item in it gets a group.'
        items = self.board_items(r['board'])
        by_id = {i['id']: i for i in items}
        of = {}
        for stack in talk.stacks([i for i in items if i.get('type') not in ('area', 'arrow') and not i.get('agent')]):
            for i in stack:
                of[i['id']] = stack
        chosen, bad = [], []
        for x in ids:
            x = str(x).strip('[] ')
            if x not in of:
                bad.append(x)
            elif of[x] not in chosen:
                chosen.append(of[x])
        why = ('Not grouped: %s (no such item, or a frame, or an agent\'s own result).' % ', '.join(bad)) if bad else ''
        groups = [i for i in items if i.get('type') == 'area' and i.get('agent') and not i.get('run')]   # groups agents made
        taken = [s for s in chosen if any(talk.inside(g, s[0]) for g in groups)]
        if taken:   # one item, one group (a model once put all 59 items in every group in turn)
            why += ' Already in a group, so not moved again: %s.' % ', '.join(s[0]['id'] for s in taken)
            chosen = [s for s in chosen if s not in taken]
        if not chosen:
            return 'Not done. ' + why.strip()
        done = sum(int(re.match(r'Grouped (\d+)', x).group(1)) for x in self.grouped(self.root(r)))
        whole = len(self.left(r)) + done
        if whole >= 10 and len(chosen) > whole / 2:
            return ('Not done: one group got %d of the %d items. A theme holds part of the frame, not most of it: split these '
                    'into themes of a few items each, one group_items call per theme.' % (len(chosen), whole))
        pad, top, gap = 40, 70, 36   # board units, the way the board's own frames hold their cards
        cw = max(max(i['x'] + i['w'] for i in s) - min(i['x'] for i in s) for s in chosen)
        w = max(r['lay']['inner'], cw + 2 * pad)
        cols = max(1, int((w - 2 * pad + gap) // (cw + gap)))
        heights = [max(i['y'] + i['h'] for i in s) - min(i['y'] for i in s) for s in chosen]
        rows = [max(heights[k:k + cols]) for k in range(0, len(chosen), cols)]
        area = self.place(r, {'type': 'area', 'text': title, 'color': '#ffffff'}, w, top + sum(rows) + gap * (len(rows) - 1) + pad, wide=True)
        if not area:
            return 'Your frame was deleted.'
        spots = {}
        for k, s in enumerate(chosen):
            x0, y0 = min(i['x'] for i in s), min(i['y'] for i in s)
            dx = area['x'] + pad + (k % cols) * (cw + gap) - x0
            dy = area['y'] + top + sum(rows[:k // cols]) + gap * (k // cols) - y0
            for i in s:
                spots[i['id']] = (i['x'] + dx, i['y'] + dy)

        def move(board):
            z = max([i.get('z', 1) for i in board.get('items') or []] + [1])
            for i in board.get('items') or []:
                if i['id'] in spots:
                    z += 1
                    i['x'], i['y'], i['z'] = spots[i['id']][0], spots[i['id']][1], z
        self.store.edit_board(r['board'], move)
        self.changed(r['board'])
        r['focus'] = area['id']
        n = sum(len(s) for s in chosen)
        self.step(r, 'Grouped %d item%s under "%s"' % (len(chosen), '' if len(chosen) == 1 else 's', title))
        # what is left: the list read_frame gave is cut short after a few more steps, so say it again here
        left = self.left(r)
        gist = '\n'.join('[%s] %s' % (s[0]['id'], ' '.join(talk.item_words(s[0], self.reader)[1].split()[:8])) for s in left[:80])
        return ('Moved %d items (%d with what is stacked under them) into the group "%s". %s\n' % (len(chosen), n, title, why)
                + (('Still not in a group, in the frames you read (%d):\n%s' % (len(left), gist)) if left
                   else 'Every item of the frames you read is now in a group.' if r.get('read_frames') else ''))

    def read_by(self, r):
        """The frames this run and every part under it read."""
        return set(r.get('read_frames', [])).union(*[self.read_by(self.runs[c]) for c in r.get('children', []) if c in self.runs])

    def left(self, r):
        """Items (as stacks) still in the frames this run (or its team) read, not yet moved into a group."""
        items = self.board_items(r['board'])
        out = []
        for f in [i for i in items if i['id'] in self.read_by(r)]:
            out += talk.stacks([i for i in items if talk.inside(f, i) and not i.get('agent')])
        return out

    def search(self, bid, query):
        b = self.store.read_board(bid)
        b = dict(b, items=[i for i in b.get('items') or [] if not i.get('agent')])   # the board, not the agent's own work
        chunks = talk.passages([b], self.reader)
        out, seen = [], set()
        for c in talk.rank(query, [], chunks):
            if c['item'] in seen:
                continue
            seen.add(c['item'])
            out.append(c)
            if len(out) >= 8:
                break
        return out

    def place(self, r, item, w, h, wide=False):
        """Put a new item (size in board units) in the run's frame: cards fill rows, wide items take a row of their
        own, and the frame grows to hold them. Returns the item, or None when the frame was deleted."""
        L = r['lay']

        def put(board):
            items = board.get('items') or []
            frame = next((i for i in items if i['id'] == r['frame']), None)
            if not frame:
                r['frame_gone'] = True
                return None
            if wide or L['cx'] + w > L['inner'] + 1:   # a new row
                if L['cx'] > 0:
                    L['cy'] += L['rowH'] + L['gap']
                L['cx'], L['rowH'] = 0, 0
            new = dict(item, id='a' + secrets.token_hex(5), agent=r['id'], x=frame['x'] + L['pad'] + L['cx'],
                       y=frame['y'] + L['pad'] + L['cy'], w=w, h=h, z=max([i.get('z', 1) for i in items] + [1]) + 1)
            if wide:
                L['cy'] += h + L['gap']
            else:
                L['cx'] += w + L['gap']
                L['rowH'] = max(L['rowH'], h)
            frame['h'] = max(frame['h'], 2 * L['pad'] + L['cy'] + L['rowH'])
            items.append(new)
            return new
        new = self.store.edit_board(r['board'], put)
        if new:
            r['focus'] = new['id']
            self.changed(r['board'])
        return new

    def place_note(self, r, text):
        text = str(text).strip()[:1200]
        if text:
            u = r['lay']['u']
            self.place(r, {'type': 'note', 'text': text, 'fs': 13 * u}, 220 * u, max(130, min(320, 60 + len(text) * 0.9)) * u)

    def place_text(self, r, text, style):
        text = re.sub(r'^\s*#+\s*|\*\*', '', str(text), flags=re.M).strip()[:6000]   # the board shows plain text, not Markdown
        if not text:
            return
        u, width = r['lay']['u'], r['lay']['inner']
        fs = (22 if style == 'heading' else 15) * u
        per_line = max(10, width / (fs * 0.55))
        lines = sum(max(1, -(-len(p) // int(per_line))) for p in text.split('\n'))
        item = {'type': 'note', 'text': text, 'color': 'none', 'grow': 'h', 'fs': fs, **({'fw': 700} if style == 'heading' else {})}
        self.place(r, item, width, lines * fs * 1.35 + fs * 0.4, wide=True)

    # ---------- keeping runs ----------
    def step(self, r, text):
        r['steps'] = (r['steps'] + [{'t': now_ms(), 'text': text}])[-300:]

    def save(self, r):
        path = os.path.join(self.dir, r['id'] + '.json')
        with open(path + '.tmp', 'w') as f:
            json.dump(r, f)
        os.replace(path + '.tmp', path)


def text_calls(content):
    """<tool_call>{"name": ..., "arguments": {...}}</tool_call> written in the reply's text, as real tool calls."""
    calls = []
    for n, m in enumerate(re.finditer(r'<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|(?=<tool_call>)|$)', content, re.S)):
        try:
            c = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(c, dict) and c.get('name'):
            calls.append({'id': 'text%d%s' % (n, secrets.token_hex(3)), 'type': 'function',
                          'function': {'name': c['name'], 'arguments': json.dumps(c.get('arguments') or {})}})
    return calls, re.sub(r'<tool_call>.*?(</tool_call>|$)', '', content, flags=re.S).strip()


def given(r, text):
    """Did this text come from the person: the task, the board items it was given, or an answer?"""
    t = ' '.join(str(text).lower().split())
    if not t:
        return True
    said = ' '.join(m['content'].lower() for m in r['messages'] if m['role'] == 'user' or
                    (m['role'] == 'tool' and str(m.get('content', '')).startswith('The person answered')))
    return t in ' '.join(said.split())


def browser_text(page):
    """The page as the agent reads it: numbered things it can use, then the words on the page."""
    lines = ['Page: %s (%s)' % (page.get('title') or '', page.get('url')), 'Things you can use, by number:']
    for e in page['elements'][:90]:
        kind = e['role'] + (' (submits a search)' if e.get('submits') and e.get('formKind') == 'search' else
                            ' (submits a form)' if e.get('submits') else '')
        bits = ['[%d] %s' % (e['n'], kind), '"%s"' % e['name'] if e.get('name') else '']
        if e.get('value') is not None:
            bits.append("= '%s'" % e['value'])
        if e.get('options'):
            bits.append('options: ' + ', '.join(e['options'][:12]))
        if e.get('href') and e['role'] == 'link' and e.get('name') != e['href']:
            bits.append('-> ' + e['href'][:100])
        if not e.get('inView'):
            bits.append('(further down)')
        lines.append(' '.join(b for b in bits if b))
    if len(page['elements']) > 90:
        lines.append('... and %d more further down' % (len(page['elements']) - 90))
    lines.append('Text on the page:\n' + page.get('text', ''))
    if page.get('at_end'):
        lines.append('(this is the bottom of the page)')
    elif page.get('more_text'):
        lines.append('(more further down: browser_scroll)')
    if page.get('error'):
        lines.insert(0, 'Note: ' + page['error'])
    return '\n'.join(lines)


def arrow(a, b, label, color, agent):
    """An arrow item from frame a to frame b; the page redraws it from the two frames, x/y/w/h is only its box."""
    x0, y0 = min(a['x'], b['x']), min(a['y'], b['y'])
    return {'id': 'w' + secrets.token_hex(5), 'type': 'arrow', 'from': a['id'], 'to': b['id'], 'text': label,
            'color': color or '#475569', 'agent': agent, 'x': x0, 'y': y0, 'z': 2,
            'w': max(a['x'] + a['w'], b['x'] + b['w']) - x0, 'h': max(a['y'] + a['h'], b['y'] + b['h']) - y0}


def trimmed(messages):
    """The messages as sent: the newest tool results whole, older ones cut short."""
    tools = [i for i, m in enumerate(messages) if m['role'] == 'tool']
    old = set(tools[:-KEEP_RESULTS]) if len(tools) > KEEP_RESULTS else set()
    out = []
    for i, m in enumerate(messages):
        if i in old and len(m.get('content') or '') > OLD_RESULT_CHARS:
            m = dict(m, content=m['content'][:OLD_RESULT_CHARS] + ' … (older result, cut)')
        out.append(m)
    return out
