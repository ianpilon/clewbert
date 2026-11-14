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
import base64, hashlib, http.client, ipaddress, json, os, queue, re, secrets, subprocess, threading, time, urllib.error, urllib.parse, urllib.request
import integrations, models, openloom, skills, talk
from . import code, shell, skillfiles, web

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
TRANSCRIBE_URL = os.environ.get('TRANSCRIBE_URL', '').rstrip('/')   # a whisper service (see transcribe/serve.py in the private app): empty = no transcriber
IMAGE_URL = os.environ.get('IMAGE_URL', '').rstrip('/')   # an OpenAI-style image generator (POST /v1/images/generations -> b64_json): empty = no pictures
MAX_STEPS = 40
RETRY_STEPS = 12         # a job the Watchdog starts again gets a short second go, from what it already found (a restarted Lead once ran 46 more steps of the same search, 2026-11-05)
 # a page that turns automated readers away: Cloudflare and the like ("Sorry, you have been blocked", Nov 2: 3 of 6 tries
# went back to the same blocked site)
BLOCKED_PAGE = re.compile(r'sorry, you have been blocked|attention required! \| cloudflare|^just a moment\.\.\.|access denied|'
                          r'verify you are (a )?human|enable javascript and cookies to continue|are you a robot|'
                          r'pardon our interruption|checking your browser|px-captcha', re.I | re.M)
BOARD_READS = ('search_board', 'read_item', 'read_frame')
FREE_TURNS = 10          # turns that only read the board or got an answer kept from earlier do not count as steps, up to this many
PART_STEPS = 16          # a teammate's part is small: done in about 12 steps, or cut too big (long runs drift)
BUILD_STEPS = 40         # a part that builds tunes by looking: 25 look-and-fix rounds got the AudioPen clone close in a terminal, 5 could not on the board (2026-11-09)
KEEP_IMAGES = 3          # the newest screenshots stay in a seeing model's context as pictures; older ones become a line of text
BROWSER_URL = os.environ.get('BROWSER_URL', '').rstrip('/')   # browser/service.py (a real browser), e.g. http://localhost:8790
BROWSER_TOKEN = os.environ.get('BROWSER_TOKEN') or (open(os.environ['BROWSER_TOKEN_FILE']).read().strip()
                                                    if os.path.exists(os.environ.get('BROWSER_TOKEN_FILE', '')) else '')
# a click or Enter that sends something out into the world waits for the person's yes
RISKY = re.compile(r"\b(send|submit|buy|purchase|pay|checkout|check out|place order|order now|sign ?up|register|subscribe|"
                   r"publish|post|book now|book|reserve|confirm|delete|donate|unsubscribe|apply now)\b", re.I)
YES = re.compile(r"^\W*(yes|yeah|yep|ok|okay|sure|go|go ahead|do it|approved?|confirm(ed)?)\b", re.I)
KEEP_RESULTS = 4          # the newest tool results stay whole; older ones are cut short so a long run fits the context
OLD_RESULT_CHARS = 400
CUT_MARK = ' … [CUT: the rest is no longer shown; make the same call again before quoting or copying from it]'
CUT_PASTED = re.compile(r'older result, cut|\[CUT:')   # a note once held "(older result, cut)" as if it were the text (2026-11-07 mining: 11 jobs)
CONTEXT_WORDS = 3000      # words of selected items given with the task
ACTIVE = ('queued', 'working', 'waiting', 'team')
STICKY_MAX = 120   # characters a sticky note holds; more goes on a card


def card_text(text):
    """Long note text as a card's text: its first sentence becomes the title line."""
    first = text.split('\n', 1)[0]
    m = re.match(r'(.{8,110}?[.!?])\s+(.+)', first, flags=re.S) if len(first) > 110 or '\n' not in text else None
    return m.group(1).rstrip('.') + '\n' + m.group(2) + text[len(first):] if m else text


ON_THE_BOARD = re.compile(r'\b(paste|the (full|whole|complete|entire|rest of the|end of the|final|last|middle)[^?]{0,40}'
                          r'(text|words|content|quote|sentence|reason|note|file|transcript)|unverified|cut off|truncat|preview|'
                          r'send me|share the|provide the|give me the|confirm (the|that|what)|the exact (text|wording))\b', re.I)
WRITES = ('add_note', 'add_card', 'add_text', 'add_link', 'add_file', 'transcribe', 'make_picture', 'finish', 'ask_user', 'assign', 'make_demo', 'group_items')
# a task that asks to rearrange what is on the board (it once ended "Done, restructured" with nothing moved)
REARRANGE = re.compile(r"\b(re-?arrang\w*|restructur\w*|re-?organi[sz]\w*|regroup\w*|cluster\w*|"
                       r"(?:group|sort|move|organi[sz]e)\w* (?:the |these |those |all |every )?(?:items|notes|stickies|cards|links|content|frame|board|them|it)\b|"
                       r"into (?:themes|groups|sections|categories|clusters))", re.I)
BUILDING = re.compile(r"\b(build|make|create|code|implement|fix|rebuild)\w*\b.{0,80}\b(demo|page|prototype|app|site|tool|it)\b", re.I)   # a part that is to build
BUILD = re.compile(r"\b(demo|prototype|build|make (?:a|me a|an)|page|site|website|app|tool|interface|screen|ui|ux|dashboard|landing)\b", re.I)
READ_TOOLS = ('read_web_page', 'read_item', 'search_board', 'read_frame', 'web_search', 'read_page_source', 'read_code', 'search_code',
              'browser_open', 'browser_look', 'browser_click', 'browser_type', 'browser_scroll', 'browser_select', 'browser_back')   # tools whose result is text the agent saw with its own eyes


def plain(text):
    """Text as the gate compares it: letters and digits only, lower case. Punctuation, spacing, curly quotes and dashes
    differ between a page and a note copied from it; the words do not."""
    return re.sub(r'[^a-z0-9]', '', str(text or '').lower())


def quotes_in(text):
    """The quoted spans of a note, pairing quote marks in order (a note with several short quotes in a row otherwise
    pairs a closing mark with the next opening one). Only spans long enough to be a real quote, not a label."""
    t = str(text or '').replace('\u201c', '"').replace('\u201d', '"')
    marks = [m.start() for m in re.finditer('"', t)]
    return [t[a + 1:b] for a, b in zip(marks[::2], marks[1::2]) if len(t[a + 1:b].strip()) >= 25]


def read_corpus(r):
    """Everything this run read for itself: the results of its read tools, as one text (a frame read with read_frame
    counts: an analyst was refused six quotes of a teammate's own notes it had just read, 2026-11-09). The task it was
    given and the checker's words are left out on purpose: that is how an invented quote travelled from a checker, through
    the Lead's brief, into a reader's 'exact' notes (2026-11-04). A Lead also gets its teammates' frames: corpus()."""
    names = {}
    parts = []
    for m in r.get('messages') or []:
        if m.get('role') == 'assistant':
            for tc in m.get('tool_calls') or []:
                names[tc.get('id')] = (tc.get('function') or {}).get('name')
        elif m.get('role') == 'tool' and names.get(m.get('tool_call_id')) in READ_TOOLS:
            c = m.get('content')
            parts.append(c if isinstance(c, str) else json.dumps(c))
    return plain('\n'.join(parts))


def missing_quote(text, corpus):
    """The first quoted span in text that is not on any page the run read, or None. A quote with '...' in it is checked
    piece by piece, so a shortened quote passes as long as every piece is real."""
    for q in quotes_in(text):
        for piece in re.split(r'\.\.\.|\u2026', q):
            key = plain(piece)
            if len(key) >= 10 and key not in corpus:
                return q
    return None


GUESS = re.compile(r"\b(likely|probably|possibly|presumably|might (?:be|use|have)|may be|could be|appears to be|seems to be|"
                   r"it is (?:likely|possible) that|you (?:can|could|might) try)\b", re.I)
ENDED = ('done', 'stopped', 'failed', 'unfinished', 'partial')   # partial: finished, but the final check found gaps   # unfinished: out of steps or stuck, never shown as Done
# a teammate saying it could not read or open something it was given (the final check must count it as a gap)
CANT_READ = re.compile(r"[^.\n]{0,120}\b(?:could ?n[o']t|can ?n[o']t|unable to|was not able to|wasn't able to|never)\s+(?:be\s+)?"
                       r"(?:read|open|access|load|extract|parse)(?:ed)?\b[^.\n]{0,120}", re.I)
MAX_ROUNDS = 5            # times a Lead can hand out work for one task (design, build, review, fix)
RESULT_WORDS = 2000       # words of one teammate's results the Lead (and the next round) reads back
STRONG_ROLES = ('developer', 'ux', 'ui', 'engineer')   # these run on the code model when there is one

def sees(job='agents'):
    """Can the model doing this job look at a picture itself? Yes on an outside provider (they all take images), when it is
    the very model picked to look at screenshots (the vision job), or when AGENT_MODEL_SEES is set. Then screenshots and
    the pictures the person selected go into its context as images, not as a 300-token caption from a second call: on
    2026-11-09 the same DeepSeek got 40 pictures in a terminal and 0 on the board, and the board's demo looked nothing
    like the reference."""
    if os.environ.get('AGENT_MODEL_SEES') or models.choice(job):
        return True
    mine = models.local(job) or models.local('agents') or (MODEL_URL, MODEL)
    eyes = models.local('vision') or (VISION_MODEL_URL, VISION_MODEL)
    return bool(mine[1] and eyes[1] and mine[1] == eyes[1])


def text_of(content):
    """The words of a message whose content may be a list of text and image parts."""
    if isinstance(content, str):
        return content
    return ' '.join(str(p.get('text') or '') for p in (content or []) if isinstance(p, dict) and p.get('type') == 'text')


def learned(data, member_id):
    """The skills added to this teammate's role, for its prompt on every board (a job-made teammate gets its kind's skills)."""
    base = re.sub(r'-\d+$', '', str(member_id or ''))
    return skills.text(KIND_BASE.get(base, base))


# starter roles, in our own words; the person can rename them and rewrite every role
STARTERS = {
    'lead': {'name': 'Lead', 'color': '#1d4ed8', 'role': 'Plans the work: splits the task into parts, hands each part to the '
             'teammate who fits it, then puts their results together into the final answer.'},
    'researcher': {'name': 'Researcher', 'color': '#0f766e', 'role': 'Finds facts on the board and on the web. Reads each page '
                   'before using it. One finding per note, with where it came from. When a search turns up fewer than asked, or only sold or old ones, try other sites before reporting: a plain web search, other brokers, the marketplaces.'},
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
    'distiller': {'name': 'Stream-of-consciousness analyst', 'color': '#b45309', 'role':
        'Turns a brain dump (a voice note, a transcript, a rambling text) into a structured piece in the speaker\'s own voice. '
        'Read the whole dump first (read_item; a long one in parts). Three kinds of words are in it: the words carrying the '
        'idea, the words carrying the speaker to the idea (um, like, so basically, false starts, repeats, "like I said"), and '
        'asides that wander off the point. Keep the first in the speaker\'s own phrasing, drop the second, set the third aside.\n'
        'Rules: never add a fact, a claim, a name or an opinion that is not in the dump; never paraphrase a point into someone '
        'else\'s voice, keep the speaker\'s sentence where it stands on its own and only trim around it; keep the speaker\'s '
        'tone, swearing included, unless it was filler; keep the order the speaker reached the points in unless two passages '
        'are clearly one point; an aside is listed, never deleted silently; nothing marked uncertain, no questions back. '
        'A word that is plainly a speech-to-text slip ("out of my house onto paper" for head) stays as said in the piece and '
        'goes on ONE add_note that starts "Heard as:", each slip with the word it probably was, so the speaker can fix it; '
        'never change it yourself.\n'
        'Deliver with add_file, as a Markdown file named after the dump: a first line "What you\'re getting at:" with the point '
        'in one sentence of the speaker\'s words; then the points as short paragraphs or a list, each in the speaker\'s words; '
        'then "Set aside:" with the asides, one line each. Then add_note with the "What you\'re getting at" sentence alone, '
        'so it is readable on the board at a glance. Finish with the file name in your summary.'},
}
STARTERS['design-lead'] = {'name': 'Design Lead', 'color': '#7c3aed', 'role':
    'Leads design and manages the UX Designer and the UI Designer. Hands the UX part to the UX Designer first, then the UI '
    'part to the UI Designer from the UX spec. For a review, has both try the demo and list fixes. Then puts their work '
    'together into ONE design brief for whoever builds it: what to build, how it works (every control and state), how it '
    'looks (exact values), and the fixes, in order of importance. Never designs or builds it alone.'}
STARTERS['engineer'] = {'name': 'Engineer', 'color': '#0e7490', 'role':
    'Writes real code in one of the user\'s code bases, on the team\'s branch. Nothing it does is live: the user reviews the branch first.\n'
    'Work in small steps: find the code that matters (code_files, code_search), read it whole (code_read), then change it '
    '(code_edit for a change inside a file, code_write for a new file). Match the code base\'s own style. After changes, '
    'code_check; fix every error it reports. Then code_commit with a message that says what changed and why. Finish only '
    'after a commit with a clean check, and say in your frame what you changed, what you did not get to, and what the user '
    'should check when the user reviews it. You have a terminal (code_run): run the checks and tests the change needs and say what '
    'they printed. It has no network, no node and cannot start the app: never say you ran something you did not.\n'
    'You start with no memory of earlier work on this branch. Your first code tool hands you the notes the engineer before '
    'you left (TEAM-PROGRESS.md), the last commits and a smoke test: read them before anything else, and if the smoke test is '
    'broken deal with that first. Do ONE piece of verified progress, not everything at once. Before you commit, rewrite '
    'TEAM-PROGRESS.md for the next engineer: what works and how you checked, what you tried that failed, what is left.'}
WHY = {'lead': 'Turns your goal into a plan and owns the final answer', 'engineer': 'Writes the code, on a branch for your review',
       'researcher': 'Finds the facts the work stands on', 'writer': 'Turns results into words people read',
       'analyst': 'Compares and ranks what the team found', 'checker': 'Makes sure what we say holds up',
       'developer': 'Builds what we show and reads code', 'design-lead': 'One owner for how it works and how it looks',
       'ux': 'Decides how it works for the person using it', 'ui': 'Decides how it looks, with exact values',
       'distiller': 'Turns your brain dump into a structured piece, in your own voice'}
for _k, _v in STARTERS.items():
    _v['why'] = WHY.get(_k, '')
STARTERS['ux']['under'] = STARTERS['ui']['under'] = 'design-lead'   # who they report to when that role is on the team
MAX_DEPTH = 3             # Lead, a manager, their reports
DEFAULT_TEAM = ('lead', 'researcher', 'writer')
KIND_BASE = {'research': 'researcher', 'analyze': 'analyst', 'check': 'checker', 'write': 'writer', 'ux': 'ux', 'ui': 'ui',
             'demo': 'developer', 'engineer': 'engineer', 'distill': 'distiller'}   # a member the team design asks for -> the starter whose know-how it gets


CUSTOM = {}   # agents the person made by voice (key -> charter), kept in <data>/library/custom_agents.json and registered like starters


def custom_path(data):
    return os.path.join(data, 'library', 'custom_agents.json')


def register_custom(a):
    if a['kind'] in KIND_BASE and KIND_BASE[a['kind']] != a['key']:   # a kind word a starter already uses: this agent gets its own
        a['kind'] = re.sub(r'[^a-z]', '', a['key'])[:20] or a['key']
    CUSTOM[a['key']] = a
    STARTERS[a['key']] = {'name': a['name'], 'color': a.get('color') or '#b45309', 'role': a['role'], 'why': a.get('why') or '', 'custom': True}
    WHY[a['key']] = a.get('why') or ''
    KIND_BASE[a['kind']] = a['key']


def load_custom(data):
    try:
        with open(custom_path(data)) as f:
            items = json.load(f)
    except (OSError, ValueError):
        items = []
    for a in items:
        if isinstance(a, dict) and a.get('key') and a.get('kind') and a.get('role'):
            register_custom(a)


def add_custom(data, a):
    """Save an agent the person asked for by voice, and make it a teammate kind the designer can pick from now on."""
    os.makedirs(os.path.dirname(custom_path(data)), exist_ok=True)
    try:
        with open(custom_path(data)) as f:
            items = [x for x in json.load(f) if x.get('key') != a['key']]
    except (OSError, ValueError):
        items = []
    with open(custom_path(data) + '.tmp', 'w') as f:
        json.dump(items + [a], f, indent=1)
    os.replace(custom_path(data) + '.tmp', custom_path(data))
    register_custom(a)


def custom_kinds_text():
    """The voice-made kinds, as lines for the team designer's list."""
    return ''.join('- %s: %s\n' % (a['kind'], a.get('kind_desc') or a.get('why') or '') for a in CUSTOM.values())


def boss_of(m):
    """Who a teammate reports to (the Lead when not set); the Lead reports to the person."""
    return None if m['id'] == 'lead' else (m.get('reports_to') or 'lead')


def reports_of(members, mid):
    return [m for m in members if boss_of(m) == mid]

SYSTEM = """You are an agent working on a canvas board (a Miro-style board of notes, text, screenshots, PDFs and links).
Write for the person in plain words: a term from a tool or a field ("scoring gate", "locked test") gets a few everyday
words saying what it means the first time, or is said in everyday words instead.
The person gave you a task. You work on it step by step with your tools and put what you find INTO YOUR TASK FRAME on
the board: that frame is where the person looks, there is no chat. Work like this:
- Start from the board: the selected items are given below; use search_board and read_item for more of the board.
- The selected items are the source: work from them, not from other cards that look alike, and say the id you used.
- Quote only from a read that is whole in front of you. A result marked cut, or one that ends mid-sentence, is read
  again (read_item, the part named) before one word of it is quoted; to copy a whole item into a file, add_file with from_item.
- Use web_search and read_web_page when the task needs facts from outside the board. Search results are only leads:
  read the page before you put a finding on the board, and check it really is what the task asks for (a directory
  or a list of firms is not a firm). Never invent facts, names,
  numbers or links: put on the board only what you read on the board or on a web page, and say where it came from.
- Put results in the frame as you go, not all at the end: add_note for ONE short thought, name or idea (at most 120
  characters, a sticky note: it is a fixed square, longer text is cut off); add_card for a finding with details (a title, then one fact per line as "Label: value", and
  its source), e.g. a listing with its price, revenue and location; add_text with style "heading" to start a section and "body" for a paragraph; add_link for a page
  worth keeping.
- When the person wants to SEE something work (a demo, a prototype, "show me"), build it with make_demo: one complete
  HTML page. Fix any JavaScript errors it reports, then say on the board what the demo shows and how to use it.
- To find out HOW something on a site works, read its code: read_page_source lists the site's script and style
  files, search_code looks inside one of them for the words that matter. Quote the code you found.
- To get facts from a page, use read_web_page: it is the fastest way, it runs the page's JavaScript when it has to, and
  a site that blocks it is blocked for every tool. The browser (browser_open, browser_click, browser_type...) is a real
  browser and slow: use it only when you must click, type, search inside a site or fill in a form, or when the person
  should SEE the page (its screenshot goes on the board). If a site turns you away, never try to get around it (search
  engine caches, web archives, reader services): say on the board that it is blocked and use another site. Act by the element numbers of the latest look. Anything that sends a message or personal details,
  buys, books, posts or signs up needs the person's yes first (ask_user), and you never type passwords. Type only what
  the task or the person gave you: never make up a name, email, phone number or address; ask for it instead.
- If a search finds nothing useful, try other words or another way before giving up. If you cannot go on without the
  person (a choice only they can make, information only they have), use ask_user with one short question. Never ask
  for permission to do the task you were given or whether to keep going. Never ask the person for anything that is on
  the board or on a page you can read: a card's words, a file's text, the end of a long quote. Read it again with
  read_item or read_frame instead; a repeat read brings the whole text back when the earlier one was cut.
- When the task is done, call finish with a short summary of what you found and what is still open.
  In the summary, and on every note and card, call a board item by its title or what it is (the voice-note file, the
  picture of the dogs), never by its id: ids are for tool calls only, and what you write is read aloud to the person.
You cannot send email or messages, call, buy or sign up for anything: never offer to.
On the board you can add to your frame, read every item of a frame (read_frame) and move existing items into new
titled groups in your frame (group_items). Never redraw, sketch or recreate a picture that is on the board: name it by
its id (pictures go into a post by id). Read the items your task names and your teammates' frames; the other frames
on the board are earlier work (old runs, tests), so read one only when your task names it or what you need is not in
the items you were given. You cannot edit, delete or recolour items, or move frames: never say you did.
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
- Read the selected items and your teammates' frames. The other frames on the board are earlier work (old runs, tests):
  read one only when the task names it, or a part came back short and the answer may be there.
- Keep each part SMALL: one thing one teammate can finish in about 12 steps ("the pricing page of ElevenLabs", not
  "pricing for five vendors"). Hand out many small parts rather than a few big ones, up to 8 at once. Give every part
  a done_when: what must be in that teammate's frame for the part to count as done, something a checker can see ("a
  note per plan with its monthly price and the pricing page it came from").
- Every part is checked against its done_when when it ends; one that falls short goes back to its teammate once,
  automatically. You get each part back with its check: build on parts that passed, and say plainly what is still
  missing from parts that did not. A part still NOT DONE after its second try is a dead end: hand it out again only
  with a genuinely different way in (another source, the browser), and at most once more; otherwise report it as
  missing. (A Lead once sent the same unreadable pricing page out five times.)
- A search for things (listings, vendors, people, examples) is never done from one site. Split it by source: one part
  per site or kind of site (brokers, marketplaces, associations, a plain web search), each told which to use, so the
  results come from several. (A team once took all five practices from one broker and missed two better ones elsewhere.)
- Do not do a teammate's part yourself. Hand out work at most %d times for one task."""


def tool(name, desc, props=None, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': desc, 'parameters': {
        'type': 'object', 'properties': props or {}, 'required': required or list((props or {}).keys())}}}


S = {'type': 'string'}
N = {'type': 'integer'}
TOOLS = [
    tool('search_board', 'Search every item on this board (notes, text, words read out of screenshots and PDFs). Returns the best matches with their item ids.', {'query': S}),
    tool('read_item', 'Read the text of one board item by its id. A long item comes in parts of 2,000 words: the reply says '
         '"part 1 of N"; ask for part 2, 3... to read on. To COPY an item into a file, do not read it: add_file with from_item does it whole.',
         {'item_id': S, 'part': N}),
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
    tool('add_note', 'Put a sticky note in your task frame: ONE short thought, name or idea, at most 120 characters (a sticky '
         'note is a fixed square). Anything longer goes on a card (add_card). source = the web page you READ it on (with read_web_page), or "board" when it comes '
         'from the board. Quote marks mean word for word from a page or item you read; a quote that is not there is refused.', {'text': S, 'source': S}),
    tool('add_card', 'Put a card in your task frame for a finding with details: a short title (what it is), then the facts, one '
         'per line as "Label: value" (for example "Asking price: $219,000"). source = the web page you READ it on (with '
         'read_web_page), or "board". Quote marks mean word for word from a page or item you read; a quote that is not there is refused.', {'title': S, 'facts': S, 'source': S}, ['title', 'facts', 'source']),
    tool('add_text', 'Put text in your task frame. style "heading" starts a section, "body" is a paragraph.',
         {'text': S, 'style': {'type': 'string', 'enum': ['heading', 'body']}}),
    tool('add_link', 'Put a link card for a web page in your task frame.', {'url': S}),
    tool('transcribe', 'Get the full transcript of a video or a recording, in ONE call, by a speech recognizer by a speech recognizer (not by '
         'reading pages or captions): url = a YouTube or other video link, or from_item = the id of a link, video or audio '
         'card on the board. The whole transcript lands in your frame as a Markdown file with timestamps. An hour of audio '
         'takes about a minute. Never try to transcribe or fetch captions any other way.', {'url': S, 'from_item': S, 'name': S}),
    tool('make_picture', 'Make a picture from words with the image generator set up for this install (about a minute a picture). '
         'It lands as a picture card in your task frame. prompt = what the picture shows, in plain detailed English: subject, '
         'setting, light, style (photo, sketch, poster, diagram). shape = square, wide or tall. name = a short title for the card.',
         {'prompt': S, 'shape': S, 'name': S}, ['prompt']),
    tool('add_file', 'Make a real Markdown (.md) file on the board: it sits in your task frame as a file card the person can open, '
         'download and share. To COPY a file or card that is on the board, give from_item = its id and leave text out: the '
         'server copies its whole content, word for word, any size, in this one call (never retype a file yourself). '
         'Otherwise name = a short file name ending in .md and text = the whole file, in Markdown, complete (nothing cut, no "...").',
         {'name': S, 'text': S, 'from_item': S}),
    tool('make_demo', 'Build a working demo: ONE complete HTML page (its CSS in <style>, its JavaScript in <script>, nothing '
         'else needed) that shows what you found or built. It opens as a live card in your frame, and you get back any '
         'JavaScript errors and what the page shows, so you can fix it: call it again with the same title to replace it. '
         'The person opens demos on a phone too: every hover or mouse interaction must also work with touch (pointer '
         'events or touchmove, never mousemove alone), and a replacement keeps every control the earlier version had.',
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
R = {'type': 'string', 'description': 'the code base, by name'}
CODE_TOOLS = [
    tool('code_files', 'List the files of a code base, or of one folder in it.', {'repo': R, 'folder': S}, ['repo']),
    tool('code_search', 'Find exact words in every file of a code base. Returns file:line: text.', {'repo': R, 'words': S}),
    tool('code_read', 'Read lines of one file (line numbers shown), from start, count lines (at most 400).',
         {'repo': R, 'path': S, 'start': {'type': 'integer'}, 'count': {'type': 'integer'}}, ['repo', 'path']),
    tool('code_edit', 'Change one exact stretch of a file: old must appear exactly once (copy it from code_read, without '
         'the line numbers); it becomes new.', {'repo': R, 'path': S, 'old': S, 'new': S}),
    tool('code_write', 'Write a whole file: a new file, or one replaced whole. Prefer code_edit for a change inside a file.',
         {'repo': R, 'path': S, 'content': S}),
    tool('code_check', 'Check the syntax of every file you changed (Python parsed, JSON loaded; nothing is run).', {'repo': R}),
    tool('code_run', 'Run one command line (bash) in your branch folder and get what it printed. The user watches it in the terminal card in '
         'your frame. For checks and tests: python3, git (read only), grep, sed, ls. It is shut in: no network (nothing can be installed or '
         'fetched), no node or npm, it cannot start the app or a server, it reads and writes only your branch folder and $TMPDIR, and a '
         'command is stopped after 60 seconds. JavaScript syntax: "$JSC" -e "new Function(read(\'file.js\'))" prints nothing when the file '
         'parses and the error when it does not. A longer script: code_write it, then run the file.', {'repo': R, 'command': S}),
    tool('code_commit', 'Commit your changes on your branch, team/<job id> (the only branch; it cannot be named otherwise), with a message that says what changed and why.', {'repo': R, 'message': S}),
]
CODE_NAMES = {t['function']['name'] for t in CODE_TOOLS}
PROGRESS = 'TEAM-PROGRESS.md'   # the notes one engineer leaves the next, on the team's branch
# words in a part that ask for what no teammate can do; the part gets CANNOT_NOTE so it says so and does the rest (2026-11-08)
IMPOSSIBLE = re.compile(r"\b(?:node -e|node --check|npm\b|pip install|run (?:the |its |your |a )?(?:app|server)s?\b|"
                        r"(?:create|make|new|own) (?:a |the )?branch|branch (?:named|called)|named branch|branch [\w-]+-[\w-]+|microphone|\bmic\b|"
                        r"hears? (?:it|the|an?) |speaks? (?:back|aloud)|audio comes back|deploy|push(?:es|ed)? to github)", re.I)
CANNOT_NOTE = ('\nWhat you cannot do here, so do not try it and do not count it against yourself: start the app or a server, use node '
               'or npm, install anything, hear or speak sound, use a microphone, push or deploy. (An engineer can run checks and tests '
               'with code_run; nobody else can run a command.) Commits land on branch team/<job id>, the only '
               'branch name there is. If this part needs one of those, write ONE note saying so plainly, do the rest, and finish.')
CODE_READ = [t for t in CODE_TOOLS if t['function']['name'] in ('code_files', 'code_search', 'code_read')]   # every teammate may read the team's branch
ASSIGN = tool('assign', 'Hand parts of the task to teammates. Each part becomes their own task frame on the board, next to '
              'yours. You get their results back when every part has ended.',
              {'assignments': {'type': 'array', 'items': {'type': 'object', 'properties': {'teammate': S, 'task': S, 'done_when': S},
                                                          'required': ['teammate', 'task', 'done_when']}}})
# the Lead plans, hands out and puts the answer together; it has no web tools, so it cannot do the parts itself
LEAD_TOOLS = [t for t in TOOLS if t['function']['name'] in ('search_board', 'read_item', 'read_frame', 'add_text', 'add_file', 'transcribe', 'make_picture', 'ask_user', 'finish')] + [ASSIGN]


def board_unit(board):
    """How big new work should be on this board: the size of what is already there, not the zoom. Sized for the zoom,
    a board viewed zoomed right out got frames many times the size of its own items, and each one pushed the fit
    further out, so the next team's work came out bigger again (2026-11-04). Each hand-made item's width over the
    width its kind is born with (a sticky 200, a card 300, a file chip 260, a link 300, a picture 440, a page 620):
    the middle of those ratios is the unit, so a page or a big picture does not drag the scale up (same as cvUnit)."""
    def nominal(i):
        if i.get('type') == 'note':
            if i.get('color') != 'none':
                return 200
            return 300 if i.get('card') else 260 if i.get('doc') else 620 if (i.get('grow') == 'h' or i.get('lines')) else 260
        return {'file': 260, 'link': 300, 'media': 440, 'img': 440, 'emoji': 80, 'shot': 440, 'term': 440}.get(i.get('type'), 300)
    its = [i for i in (board.get('items') or []) if i.get('type') not in ('area', 'arrow', 'ink') and float(i.get('w') or 0) > 0]
    hand = [i for i in its if not i.get('agent')]   # what the user put there by hand sets the scale; earlier oversized agent work does not
    rs = sorted(float(i['w']) / nominal(i) for i in (hand or its))
    if not rs:
        return 1 / max(((board.get('view') or {}).get('s') or 1), 1e-4)
    return max(.05, min(50., rs[len(rs) // 2]))


def now_ms():
    return int(time.time() * 1000)


def job_score(runs, root, items=()):
    """What a whole job cost, read from its saved runs and the board once it ends (the plan critic's measure, 2026-11-05):
    model calls (one per assistant message), tool calls, web pages read, working time without the waits on the person,
    members started, items put on the board, questions asked, and how the checks went. runs = id -> run, items = the board's."""
    team, todo = [root], list(root.get('children') or [])
    while todo:
        c = todo.pop()
        if c in runs and runs[c] not in team:
            team.append(runs[c])
            todo += runs[c].get('children') or []
    ids = {x['id'] for x in team}
    model = sum(1 for x in team for m in x.get('messages') or [] if m.get('role') == 'assistant')
    calls = [tc for x in team for m in x.get('messages') or [] for tc in (m.get('tool_calls') or [])]
    names = [(tc.get('function') or {}).get('name') for tc in calls]
    end = max([root.get('ended') or 0] + [x.get('ended') or 0 for x in team] + [x['steps'][-1]['t'] for x in team if x.get('steps')])
    waits = []
    for x in team:
        asked = None
        for s in x.get('steps') or []:
            if s['text'].startswith('Asked you:'):
                asked = s['t']
            elif s['text'].startswith('You answered:') and asked:
                waits.append((asked, s['t']))
                asked = None
        if asked and x['status'] == 'waiting':   # still unanswered: the wait runs to the job's last activity
            waits.append((asked, end))
    waits.sort()
    waited, upto = 0, 0   # the waits as one set of intervals: two members asking at once is one wait
    for a, b in waits:
        a = max(a, upto)
        if b > a:
            waited += b - a
            upto = b
    secs = max(0, (end - root['created'] - waited) // 1000)
    kinds = {}   # what the team put on the board, counted from its own calls (the items may be deleted later), refused ones left out
    for x in team:
        msgs = x.get('messages') or []
        results = {m.get('tool_call_id'): str(m.get('content') or '') for m in msgs if m.get('role') == 'tool'}
        for m in msgs:
            for tc in m.get('tool_calls') or []:
                name = (tc.get('function') or {}).get('name')
                if name in ('add_note', 'add_card', 'add_text', 'add_link', 'add_file', 'make_demo') and not results.get(tc.get('id'), '').startswith(('Not added', 'Not done', 'Made the demo again')):
                    kinds[name[4:] if name.startswith('add_') else 'demo'] = kinds.get(name[4:] if name.startswith('add_') else 'demo', 0) + 1
    parts = [x for x in team if x is not root and x.get('check')]
    final = (not root.get('gaps')) if root.get('final_done') else None
    s = {'model_calls': model, 'tool_calls': len(calls), 'web_pages': names.count('read_web_page'), 'seconds': secs,
         'members': sum(1 for x in team if x is not root and x.get('member')), 'items': sum(kinds.values()), 'kinds': kinds,
         'questions': names.count('ask_user'), 'parts_passed': sum(1 for x in parts if x['check'].get('pass')), 'parts_checked': len(parts),
         'final': final, 'ended': bool(root.get('ended')), 'when': now_ms()}
    mins = '%d min' % max(1, round(secs / 60)) if secs < 5400 else '%.1f h' % (secs / 3600)
    check = ('final check passed' if final else 'final check found gaps' if final is False else 'no final check') + (
        ', parts %d of %d passed' % (s['parts_passed'], s['parts_checked']) if parts else '')
    s['line'] = '%d model calls · %d tool calls · %d web pages · %s · %d members · %d items · %d question%s · %s' % (
        model, len(calls), s['web_pages'], mins, s['members'], s['items'], s['questions'], '' if s['questions'] == 1 else 's', check)
    return s



SAND = '#f3eee4'   # a job frame's fill: the page's "Sand" frame fill


def local_address(url):
    """True for localhost and private network addresses (192.168.x, 10.x, ...)."""
    host = (urllib.parse.urlsplit(url).hostname or '').lower()
    if host in ('localhost', '') or host.endswith('.local'):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local

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
        load_custom(data_dir)   # agents the person made by voice join the starters
        self.lock = threading.Lock()
        self.runs = {}
        self.apps = {}   # run id -> its connected-app tools this step (see integrations.py)
        self.pages = {}   # job (root run) id -> {url: page text}: a page one teammate read is not fetched again
        self.q = queue.Queue()
        for fn in sorted(os.listdir(self.dir)):
            if fn.endswith('.json'):
                try:
                    with open(os.path.join(self.dir, fn)) as f:
                        r = json.load(f)
                except (OSError, ValueError):
                    continue
                self.runs[r['id']] = r
                r.pop('checking', None)   # from a file saved before 2026-11-13: two Leads sat on "Agents at work" for two days
                if r['status'] in ('queued', 'working'):   # cut off by a restart: carry on from the last saved step
                    r['status'] = 'queued'
                    self.q.put(r['id'])
        for r in list(self.runs.values()):   # a Lead whose teammates all ended while the server was down
            if r['status'] == 'team':
                self.team_check(r)
        threading.Thread(target=self.tidy_all, daemon=True).start()   # every job clean on its board (2026-11-13)
        for r in list(self.runs.values()):   # jobs that ended before scores existed get theirs now (the baseline for the plan critic)
            if not r.get('parent') and r['status'] in ENDED and not r.get('score'):
                self.score(r, paint=False)
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
        return [{'id': r['id'], 'board': r['board'], 'task': r['task'], 'status': r['status'], 'frame': r['frame'], 'focus': r.get('focus'), 'selected': r.get('selected') or [],
                 'member': {k: (r.get('member') or {}).get(k) for k in ('id', 'name', 'color')} if r.get('member') else None,
                 'parent': r.get('parent'), 'created': r['created'], 'ended': r.get('ended'), 'summary': r.get('summary'),
                 'step': r['steps'][-1]['text'] if r['steps'] else '', 'steps': len(r['steps']),
                 'question': (r.get('question') or {}).get('text'), 'note': (r.get('question') or {}).get('note'),
                 'page': r.get('page_url'), 'demo_v': r.get('demo_v', 0), 'error': r.get('error'), 'gaps': r.get('gaps'),
                 'demos': ['api/demos/%s.html' % d for d in r.get('demos', {})],
                 'crew': [{k: m.get(k) for k in ('id', 'name', 'color', 'why', 'done_when', 'role')} for m in r['crew']] if r.get('crew') else None,
                 'check': {'pass': bool(r['check'].get('pass')), 'missing': r['check'].get('missing')} if r.get('check') else None,
                 'retry_of': r.get('retry_of'), 'score': (r.get('score') or {}).get('line') if r['status'] in ENDED else None, 'critic': (r.get('critic') or {}).get('line')}
                for r in sorted(runs, key=lambda r: r['created'])]

    def steps(self, rid):
        r = self.runs.get(rid)
        return r and {'id': rid, 'task': r['task'], 'status': r['status'], 'steps': r['steps']}

    # ---------- starting, answering, stopping ----------
    def start(self, bid, task, selected=(), anchor=None, mode='team', member=None, parent=None, intent=None, done_when=None, ask=None):
        """A new task. Its frame goes next to the selected items (anchor {x, y, unit} from the page), next to the Lead's
        frame for a teammate's part, or right of the board. mode 'team' gives it to the board's Lead when the team has
        teammates; 'one' is a single agent. member/parent: a teammate's part handed out by a Lead run. ask: the person's
        own words when task is a rewording of them (the voice's plan): they go to the Lead and every part, unchanged."""
        rid = 'r' + secrets.token_hex(6)
        task = ' '.join(str(task).split())[:1500]
        team = self.team(bid)['members']
        if member is None and mode == 'team' and len(team) > 1:
            member = team[0]   # the Lead
        title = ('%s: %s' % (member['name'], task) if parent else task)[:90]

        def make_frame(board):
            items = board.get('items') or []
            unit = float((anchor or {}).get('unit') or board_unit(board))
            picked = [i for i in items if i['id'] in set(selected or ()) and i.get('type') != 'arrow']
            mates = {x['frame'] for x in self.job_runs(self.root(self.runs[parent]))} if parent in self.runs else set()
            row = [i for i in items if i['id'] in mates]
            if row:   # a part: right of the job's last frame, at the Lead's height (frames grow down, so the row stays clear)
                lead_frame = next((i for i in row if i['id'] == self.root(self.runs[parent])['frame']), row[0])
                x, y = max(i['x'] + i['w'] for i in row) + 200 * unit, lead_frame['y']
            elif anchor and 'x' in anchor:
                x, y = float(anchor['x']), float(anchor['y'])
            elif picked:   # a team asked for by voice: beside what is selected (it went to the far top right of the board, 2026-11-13)
                x, y = max(i['x'] + i['w'] for i in picked) + 80 * unit, min(i['y'] for i in picked)
            elif items:
                x, y = max(i['x'] + i['w'] for i in items) + 160 * unit, min(i['y'] for i in items)
            else:
                x, y = 100.0, 100.0
            pad, gap, card = 28 * unit, 18 * unit, 220 * unit
            w = 3 * card + 2 * gap + 2 * pad
            # the nearest free spot: room below for the frame to grow into and above for the team's chart. Sliding right only,
            # past anything in a 900-tall band, put a team far from its card on a busy column, its chart up by an older team's
            room, above, step = 900 * unit, (450 * unit if member is not None and not parent else 0), 120 * unit
            near = [i for i in items if i.get('type') != 'arrow' and i['x'] < x + w + 40 * step and i['x'] + i['w'] > x
                    and i['y'] < y + room + 20 * step and i['y'] + i['h'] > y - above - 20 * step]
            free = lambda cx, cy: not any(i['x'] < cx + w and i['x'] + i['w'] > cx and i['y'] < cy + room and i['y'] + i['h'] > cy - above for i in near)
            spot = None if row else next((p for d in range(41) for p in sorted(((x + a * step, y + b * step) for a in range(d + 1) for b in {d - a, a - d}),
                                                              key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2) if free(*p)), None)
            if spot:
                x, y = spot
            else:   # nothing free near: the old way, further right
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
            system += '\n\nYOUR ROLE: you are the %s on this board\'s team. %s' % (member['name'], member.get('role', '')) + learned(
                os.path.dirname(self.dir), member.get('id'))
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
            if top.get('ask'):
                prompt += '\n\nTHE PERSON\'S OWN WORDS, exactly as said (the task above is a rewording; where they differ, these win): "%s"' % top['ask']
            earlier = '\n\n'.join(x for x in ((top.get('results') if top is not p else ''), p.get('results')) if x)
            if earlier:
                prompt += '\n\nRESULTS OF EARLIER ROUNDS:\n' + earlier[-16000:]
            prompt += '\n\nYOUR PART (from the %s): %s' % ((p.get('member') or {}).get('name', 'Lead'), task)
            if done_when:
                steps = BUILD_STEPS if (member and member.get('id') in STRONG_ROLES) or BUILDING.search(task) else PART_STEPS
                prompt += ('\n\nYOUR PART IS DONE WHEN: %s\nA checker reads your frame against this when you finish. You '
                           'have %d steps: put each result in your frame as soon as you have it.' % (done_when, steps))
        else:
            prompt += '\n\nTASK: ' + task
            if ask and plain(ask) != plain(task):
                prompt += '\n\nTHE PERSON\'S OWN WORDS, exactly as said (the task above is a rewording; where they differ, these win): "%s"' % ask
        rules = self.rules()
        if rules:   # the user's standing rules, for every team and every part
            prompt += '\n\nTHE USER\'S STANDING RULES FOR EVERY TEAM (follow them over anything else here):\n' + rules
        model = 'code' if member and member.get('id') in STRONG_ROLES else None
        first = {'role': 'user', 'content': prompt}
        pictures = self.pictures(bid, selected) if sees('builders' if model == 'code' else 'agents') else []
        if pictures:   # the pictures the person selected, for the model's own eyes (a reference to match, not a caption of one)
            first = {'role': 'user', 'keep': True, 'img': True, 'content': [{'type': 'text', 'text': prompt + '\n\nTHE PICTURES THE PERSON SELECTED are attached below (%d). Look at them: they are what the result is measured against.' % len(pictures)}]
                     + [{'type': 'image_url', 'image_url': {'url': u}} for u in pictures]}
        r = {'id': rid, 'board': bid, 'task': task, 'status': 'queued', 'created': now_ms(), 'frame': frame['id'],
             'focus': frame['id'], 'lay': lay, 'steps': [], 'selected': list(selected)[:200], 'lead': is_lead,
             'member': member, 'parent': parent, 'children': [], 'intent': intent if not parent else None,
             'done_when': str(done_when or '').strip()[:400] or None, 'ask': (' '.join(str(ask).split())[:1500] or None) if ask and not parent else None,
             'model': model,
             'messages': [{'role': 'system', 'content': system}, first]}
        if not parent:
            r['wrap'] = True   # the whole job in one frame of its own, moved by its title (2026-11-13)
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
                          'lead': bool(r.get('lead')), 'ended': r.get('ended'), 'error': r.get('error'), 'gaps': r.get('gaps'), 'draft': r.get('draft'),
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

    def board_status(self):
        """One word per board for the board list: a team at work, a question waiting on the person, or a task that ended
        and has not been looked at yet (the board not opened since it ended). Opening the board clears it (seen); a board
        with nothing going on is not in the answer (2026-11-05: the pill is the system status of each board)."""
        week = now_ms() - 7 * 86400 * 1000
        with self.lock:
            runs = list(self.runs.values())
        rank = {'waiting': 0, 'working': 1, 'done': 2, 'stopped': 3}
        out = {}
        for r in runs:
            if r['status'] == 'waiting' and r.get('question'):
                state = 'waiting'
            elif r.get('parent'):
                continue   # a teammate's part: its task speaks for it
            elif r['status'] in ACTIVE:
                state = 'working'
            elif r['status'] in ENDED and not r.get('seen') and (r.get('ended') or 0) > week:
                state = 'done' if r['status'] in ('done', 'partial') else 'stopped'
            else:
                continue
            cur = out.get(r['board'])
            if not cur or rank[state] < rank[cur['state']]:
                out[r['board']] = {'state': state, 'when': r.get('ended') or r['created'], 'task': r['task'][:120]}
        return out

    def seen(self, bid):
        """The person opened the board: its ended tasks have been looked at, and the board list stops flagging them."""
        with self.lock:
            runs = [r for r in self.runs.values() if r['board'] == bid]
        n = 0
        for r in runs:
            if r['status'] in ENDED and not r.get('seen'):
                r['seen'] = True
                self.save(r)
                n += 1
        return n

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
        team = reports_of(r.get('crew') or self.team(r['board'])['members'], (r.get('member') or {}).get('id', 'lead'))
        r['messages'].append({'role': 'user', 'content': 'FOLLOW-UP from the person: %s\nWhat is already in your frame stays; add to '
                                                         'it, then finish again with a summary of what is new.' % str(text)[:2000] + (
            '\nHand the new work to the teammate whose role fits it (assign). Your teammates now: %s.'
            % '; '.join('%s: %s' % (m['name'], m.get('role', '')) for m in team) if r.get('lead') else '')})
        for k in ('finished', 'stop', 'frame_gone', 'ended', 'told', 'seen', 'error', 'final_done', 'final_retry', 'gaps'):
            r.pop(k, None)
        r['msg_base'] = sum(1 for m in r['messages'] if m['role'] == 'assistant')   # a fresh allowance of steps and rounds
        r['step_base'] = len(r['steps'])
        if str(text).startswith('The watchdog started you again'):   # a restart, not new work: it may write from the rounds it ran
            r.setdefault('watch_retry', time.time())
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
        if r.get('lead') and not r.get('parent') and r.get('crew') and r['status'] in ('done', 'partial', 'unfinished') and not r.get('noted'):
            r['noted'] = True
            self.team_notes(r)
            self.save(r)
            self.hurdles(r)
        if not r.get('parent') and r['status'] in ENDED:
            self.score(r)
            try:
                self.spread(r)   # the last frame may have grown into another after the last card went in
            except Exception as e:
                print('spread %s: %s' % (r['id'], e), flush=True)
        if r.get('chart'):
            self.crew_paint(r)
        p = self.runs.get(r.get('parent'))
        if p and p.get('chart'):
            self.crew_paint(p)
        if p and p['status'] == 'team':
            self.team_check(p)

    def score(self, r, paint=True):
        """Write the job's score onto its root run and the Lead's card on the board."""
        try:
            items = self.store.read_board(r['board']).get('items') or []
        except (OSError, ValueError, KeyError):
            items = []
        with self.lock:
            r['score'] = job_score(self.runs, r, items)
        self.save(r)
        if paint and r.get('chart'):
            self.crew_paint(r)

    def team_check(self, p):
        """When every part of the Lead's current round has ended: check each against its done-when (a part that falls
        short goes back once, for what is missing), then give the Lead their results and wake it."""
        parts = [self.runs[c] for c in p.get('round', []) if c in self.runs]
        if any(c['status'] not in ENDED for c in parts) or not p.get('team_call'):
            return
        with self.lock:   # parts end on several workers at once: one of them checks
            if p.get('checking'):
                return
            p['checking'] = True
        try:
            again = []
            for c in parts:
                if c.get('done_when') and c.get('check') is None and c['status'] != 'stopped':
                    c['check'] = self.check_part(c)
                    if c['check']['pass'] and c['status'] == 'unfinished':   # out of steps, but the work asked for is in its frame:
                        c['status'] = 'done'   # that is done, not "didn't finish" (six parts wore that label in one day, 2026-11-07)
                        c['ended'] = c.get('ended') or int(time.time() * 1000)
                        self.step(c, 'Ran out of steps, but its check passed: counted as done')
                    self.save(c)
                    if not c['check']['pass'] and not c.get('retry_of') and not self.about_blocked(p, c['task']):   # a blocked site will not open on a second try either
                        again.append(self.retry_part(p, c))
            self.crew_paint(p)
            if again:
                p['round'] = p['round'] + [x['id'] for x in again]
                self.step(p, 'Sent %d part%s back once: %s' % (len(again), '' if len(again) == 1 else 's',
                                                             ', '.join((x.get('member') or {}).get('name', '') for x in again)))
                self.save(p)
                return
        finally:
            p['checking'] = False
        text = '\n\n'.join(self.results_of(c) for c in parts)   # a part sent back and its second try both speak: the second holds only the missing piece
        gone = self.root(p).get('blocked') or {}
        if gone:   # the Lead once sent a teammate back to a blocked site four rounds running
            text += ('\n\nSITES THAT BLOCK AUTOMATED READERS (for this whole job): %s. Do not send anyone to them again; say in '
                     'your answer that they could not be read.' % ', '.join(sorted(gone)))
        p['results'] = ((p.get('results') or '') + '\n\n' + text).strip()
        p['messages'].append({'role': 'tool', 'tool_call_id': p.pop('team_call'), 'content': 'Your teammates are done.\n\n' + text})
        self.step(p, 'The team finished this round')
        p['status'] = 'queued'
        self.save(p)
        self.q.put(p['id'])

    def final_check(self, r, summary):
        """The whole job against what the intent card promised: the Lead's frame first, then its parts' frames."""
        it = r.get('intent') or {}
        bar = 'The user gets: %s. What the user wants: %s' % (it.get('deliver'), it.get('want') or r['task'])
        words = self.frame_words(r) + '\n' + summary
        kids = [self.runs[c] for c in r.get('children', []) if c in self.runs]
        retried = {x.get('retry_of') for x in kids if (x.get('check') or {}).get('pass')}
        gaps = []   # a team once said Done while its own notes said the user's spreadsheet was never read
        for x in kids:
            fw = self.frame_words(x)
            if (x.get('check') or {}).get('pass') or x['id'] in retried:   # a first try whose missing piece came in on the second try counts whole
                words += '\n\n[%s]\n%s' % ((x.get('member') or {}).get('name', ''), fw)
            elif x['id'] not in retried and x.get('check'):
                gaps.append('A part never passed its check: %s (missing: %s)' % (x['task'][:200], x['check'].get('missing') or x['check'].get('why')))
            gaps += ['A teammate wrote: "%s"' % m.group(0).strip() for m in CANT_READ.finditer(fw)][:3]
        if gaps:
            bar += ('. KNOWN GAPS, from the team\'s own work (if one of them is something the job needed, such as a file or '
                    'source the user gave, the job is NOT passed; name it in "missing"):\n- ' + '\n- '.join(gaps[:10]))
        try:
            v = talk.check_part(r['task'], bar, words)
        except (OSError, ValueError, KeyError) as e:
            v = {'pass': False, 'missing': '', 'why': 'The final check could not run (%s).' % type(e).__name__}
        self.card(r, ('Final check: passed. ' + v['why']) if v['pass'] else
                  ('Final check: not everything. ' + ('Missing: %s' % v['missing'] if v['missing'] else v['why'])),
                  '#e7f6ec' if v['pass'] else '#fff4e0')
        self.step(r, 'Final check against the intent: %s' % ('passed' if v['pass'] else 'gaps found'))
        if v['pass'] or r.get('final_retry'):   # this check ends the job: settle teammates who contradict each other
            self.conflicts(r, kids)
        return v

    def conflicts(self, r, kids):
        """A card for each fact two teammates disagree on, saying which one is right (a verifier once wrote that the user's
        spreadsheet had no price column; the writer rightly overruled it, but the wrong card stood alone on the board)."""
        frames = '\n\n'.join('[%s]\n%s' % ((x.get('member') or {}).get('name', 'Teammate'), self.frame_words(x)[:4000]) for x in kids)
        try:
            found = talk.team_conflicts('[Lead]\n%s\n\n%s' % (self.frame_words(r)[:4000], frames)) if len(kids) > 1 else []
        except (OSError, ValueError, KeyError):
            return
        for c in found:
            settled = c['right'] and c['right'].lower() != 'cannot tell'
            self.card(r, 'Teammates disagree: %s. %s %s' % (c['about'].rstrip('.'), c['said'].rstrip('.') + '.',
                      ('%s is right: %s' % (c['right'], c['why'])) if settled else ('The board cannot settle it: %s Check this one yourself.' % c['why'])),
                      '#fff4e0')
        if found:
            self.step(r, 'Found %d place%s where teammates disagree' % (len(found), '' if len(found) == 1 else 's'))

    def check_part(self, c):
        """One part, read against its done-when by a checker call; the verdict goes on the board in the part's frame.
        A part that passed is then checked at its surface: the first page it cites is read again and held against the
        words about it, and a demo it built is opened once (a verifier that reads the board's say-so proves nothing, 2026-11-07)."""
        try:
            v = talk.check_part(c['task'], c['done_when'], self.frame_words(c))
        except (OSError, ValueError, KeyError) as e:
            v = {'pass': False, 'missing': '', 'why': 'The check could not run (%s).' % type(e).__name__, 'error': True}
        if v['pass']:
            try:
                v = self.surface_check(c) or v
            except Exception as e:   # any failure here once killed the thread that wakes the Lead, and the server start (2026-11-13)
                self.step(c, 'Could not check at the source (%s)' % type(e).__name__)
        self.card(c, ('Check: passed. ' + v['why']) if v['pass'] else
                  ('Check: not done. ' + ('Missing: %s' % v['missing'] if v['missing'] else v['why'])), '#e7f6ec' if v['pass'] else '#fff4e0')
        self.step(c, 'Checked against its done-when: %s' % ('passed' if v['pass'] else 'not done'))
        return v

    def surface_check(self, c):
        """The part's words against the thing itself: the first web page it cites, read again now; the newest demo it
        built, opened once. None when there is nothing to touch or it holds."""
        items = [i for i in self.board_items(c['board']) if i.get('agent') == c['id'] and i.get('type') != 'area' and not i.get('chart')]
        for it in items:
            text = str(it.get('text') or it.get('url') or '')
            for url in re.findall(r'https?://[^\s)\]>"\']+', text)[:3]:
                url = url.rstrip('.,;')
                if self.blocked(c, url) or self.site(url) in ('context-canvas.voiceclaw.ca',) or local_address(url):
                    continue   # a local address is an example or a machine here, not a source (a quoted example address hung a team, 2026-11-13)
                page = self.read_page(c, url)
                if page.startswith('Could not read') or 'blocked' in page[:200].lower() and len(page) < 400:
                    return None   # the page cannot be read now: nothing to hold the words against
                v = talk.surface_check(url, text, page)
                if v['holds']:
                    self.step(c, 'Checked at the source: %s holds up' % url[:90])
                    return None
                self.step(c, 'Checked at the source: %s does not say that' % url[:90])
                return {'pass': False, 'why': 'Checked against %s: %s' % (url[:120], v['why']),
                        'missing': 'The page at %s does not show what the note about it says (%s). Read that page again and correct the note to what it does say, or say plainly that the page does not hold it.' % (url, v['why'])}
        if c.get('demos'):
            did = sorted(c['demos'], key=lambda d: c['demos'][d])[-1]
            title = did.split('-', 1)[1].replace('-', ' ') if '-' in did else did
            out = self.try_demo(c, title, [])
            if out.startswith('The browser could not play it') or ' ERRORS: ' in out:
                self.step(c, 'Opened the demo "%s": it does not open cleanly' % title[:60])
                return {'pass': False, 'why': 'Opened the demo once: it does not open cleanly.',
                        'missing': 'The demo "%s" does not open cleanly in a browser: %s. Fix it so it opens without errors, then try_demo it yourself.' % (title, out[-400:])}
            self.step(c, 'Opened the demo "%s": it opens cleanly' % title[:60])
        return None

    def retry_part(self, p, c):
        """Send a part that fell short back once, to the same teammate, for only what is missing."""
        frame = next((i for i in self.board_items(p['board']) if i['id'] == p['frame']), None) or {'x': 0, 'y': 0, 'w': 0}
        u = p['lay']['u']
        piece = c['check']['missing'] or c['check']['why']
        task = ('SECOND TRY, ONE PIECE ONLY. Your first try fell short; everything it put in its frame still counts. Do ONLY this '
                'missing piece now, nothing else, in this frame: %s\n\n(The whole part, for context, was: %s)' % (piece, c['task']))
        advice = self.advice(c, piece)   # effort scaling: the same approach failed once, so a stronger model says what to change (2026-11-07)
        if advice:
            task += '\n\nADVICE FROM THE ADVISOR (a stronger model, asked once because the first try fell short; follow it): ' + advice
        x = self.start(p['board'], task, p.get('selected', []), {'x': frame['x'] + frame['w'] + 200 * u, 'y': frame['y'], 'unit': u},
                       member=c.get('member'), parent=p['id'], done_when='this piece is in the frame, complete: %s' % piece)   # a small piece with its own small check (2026-11-05)
        x['retry_of'] = c['id']
        p['children'].append(x['id'])
        self.save(x)
        return x

    def advice(self, c, missing):
        """Ask the advisor (a stronger model picked in Settings) once per run what to do differently, when a part or the
        Lead has fallen short of its check. '' when no advisor is picked. The step says it was asked, so the record shows
        where the stronger model came in (OpenAI's effort scaling rung, 2026-11-07)."""
        if c.get('advised') or not models.advisor():
            return ''
        c['advised'] = True
        text = talk.advise(c['task'], c.get('done_when') or (c.get('intent') or {}).get('deliver'), missing, self.frame_words(c),
                           [x['text'] for x in c['steps']])
        self.step(c, ('Asked the advisor what to change: ' + text[:160]) if text else 'Asked the advisor; it had nothing to add')
        return text

    def frame_words(self, c):
        """Everything a part put in its frame, as text (its notes, texts, links, code-change cards)."""
        out = []
        for it in self.board_items(c['board']):
            if it.get('agent') == c['id'] and it.get('type') != 'area' and not it.get('chart'):
                if it.get('type') == 'file':   # a file by its words, not its name; a long one by its size and its ends, so a checker never judges a cut view (2026-11-05)
                    words = talk.item_words(it, self.reader)[1]
                    if len(words) > 6000:
                        words = ('FILE %s: %d words, %d characters, complete on the board (the server holds all of it). BEGINS: %s\n[... %d characters not shown ...]\nENDS: %s'
                                 % (it.get('label') or 'file', len(words.split()), len(words), words[:1500], len(words) - 3000, words[-1500:]))
                    out.append(words)
                else:
                    out.append(it.get('text') or it.get('url') or it.get('label') or '')
        return '\n'.join(x for x in out if x and not x.startswith(('Done when: ', 'Check: ', 'Final check: ', 'Intent\n', 'Team for this job\n')))

    def card(self, r, text, color):
        """A wide card in a run's frame: the done-when bar, a check's verdict."""
        u, width = r['lay']['u'], r['lay']['inner']
        fs = 15 * u
        per_line = max(10, width / (fs * 0.55))
        h = sum(max(1, -(-len(x) // int(per_line))) for x in text.split('\n')) * fs * 1.35 + fs * 2
        self.place(r, {'type': 'note', 'text': text, 'color': color, 'card': 1, 'grow': 'h', 'fs': fs}, width, h, wide=True)

    def results_of(self, c):
        """What one teammate's part produced: its summary and the words it put in its frame."""
        words = []
        for it in self.board_items(c['board']):
            if it.get('agent') == c['id'] and it.get('type') in ('note', 'link'):
                words.append(it.get('text') or it.get('url') or '')
            elif it.get('agent') == c['id'] and it.get('type') == 'area' and it['id'] != c['frame']:   # a group it made
                words.append('Group: ' + (it.get('text') or ''))
        body = ' '.join('\n'.join(words).split(' ')[:RESULT_WORDS])
        v = c.get('check')
        verdict = ('\nCheck against its done-when (%s): %s' % (c.get('done_when'), 'PASSED. ' + v['why'] if v['pass'] else
                   'NOT DONE. Missing: ' + (v['missing'] or v['why']))) if v else ''
        return '%s (%s), part: %s%s\nSummary: %s\nIn their frame:\n%s' % (
            (c.get('member') or {}).get('name', 'Teammate') + (' - second try, the missing piece only' if c.get('retry_of') else ''),
            c['status'], c['task'], verdict, c.get('summary') or '(none)', body or '(nothing)')

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
        if not r.get('parent') and not r.get('intent_card'):
            self.intent_card(r)
        if r.get('lead') and not r.get('parent') and not r.get('crew_done'):
            self.design_crew(r)
        if r.get('parent') and r.get('done_when') and not r.get('dw_card'):   # the part's bar, first in its frame
            r['dw_card'] = True
            self.card(r, 'Done when: ' + r['done_when'], '#f8fafc')
        self.save(r)
        while r['status'] == 'working':
            if r.get('steer_note'):
                r['messages'].append({'role': 'user', 'content': r.pop('steer_note')})
            if r.get('stop'):
                r['status'] = 'stopped'
                break
            if sum(1 for m in r['messages'] if m['role'] == 'assistant') - r.get('msg_base', 0) >= self.cap(r):
                self.wrap_up(r)   # out of steps: one last turn to write down what it learned, so nothing is lost
                r['status'] = 'unfinished'   # a Lead that looped to the limit got a green Done with nothing done (Oct 31)
                break
            offered = LEAD_TOOLS if r.get('lead') else TOOLS + (BROWSER_TOOLS if BROWSER_URL else [])
            if str((r.get('member') or {}).get('id', '')).startswith('engineer'):
                offered = offered + CODE_TOOLS   # the team's branch of a code base: read, edit, check, commit, and a shut-in terminal
            elif not r.get('lead') and code.repos(os.path.dirname(self.dir)):
                offered = offered + CODE_READ   # a researcher or checker reads the code (and the engineer's changes) itself
            apps = [] if r.get('lead') else integrations.tools(r['board'])   # connected apps (Settings), unless off on this board
            self.apps[r['id']] = {x[1]: x for x in apps}   # (functions: kept beside the run, not in its saved file)
            if apps and r.get('apps_told') != sorted(self.apps[r['id']]):   # tell it once, and again when the apps change
                r['apps_told'] = sorted(self.apps[r['id']])
                r['messages'].append({'role': 'user', 'content': integrations.summary(apps)})
            offered = offered + [x[0] for x in apps]
            msg = self.call(r['messages'], offered, r.get('model'))
            names = {t['function']['name'] for t in offered}
            calls = msg.get('tool_calls') or []
            content = re.sub(r'<think>.*?</think>', '', msg.get('content') or '', flags=re.S).strip()
            if not calls and ('<tool_call>' in content or 'DSML' in content):   # tool calls written out as text: take them as the calls they are
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
            builder = r.get('parent') and BUILDING.search(r['task']) and not r.get('lead') and 'make_demo' in names and str(
                (r.get('member') or {}).get('id', '')).startswith('developer')   # "facts for building our own integration" is research, not a build
            if not calls and builder and not r.get('built') and r.get('nudged_build', 0) < 3:
                r['nudged_build'] = r.get('nudged_build', 0) + 1   # a build part does not end with words: it builds
                r['messages'].append({'role': 'user', 'content': 'Your part is to build a working demo. Build it now with make_demo.'})
                continue
            if not calls and r.get('team_call'):   # words while its parts are still out: wait for them (a Lead was marked done mid-job and the voice said so, 2026-11-05)
                r['status'] = 'team'
                break
            if not calls:   # plain words instead of a tool: they are the result
                if content:
                    self.place_text(r, content, 'body')
                r['status'] = 'done'
                break
            r['refused'] = False
            free = True   # did this turn only read the board, or get answers kept from earlier? then it is not a step
            for c in calls:
                name = c['function']['name']
                r.pop('_free', None)
                try:
                    args = json.loads(c['function'].get('arguments') or '{}')
                except ValueError:
                    args = {}
                key = name + json.dumps(args, sort_keys=True)
                if name in ('search_board', 'web_search') and isinstance(args, dict):   # a query that only grows by a few words is the same search (a Lead's query gained a sentence a turn)
                    key = name + ' '.join(str(args.get('query') or '').lower().split())[:60]
                tool_at = [i for i, m in enumerate(r['messages']) if m['role'] == 'tool']
                cut = set(tool_at[:-KEEP_RESULTS]) if len(tool_at) > KEEP_RESULTS else set()
                stale = name in ('read_item', 'read_frame', 'read_web_page') and r.get('call_at', {}).get(key) in cut   # its earlier result is cut short now: read it whole again
                if name not in names:   # the model can call tools it was not given (a Lead searching the web): refuse
                    result = 'You do not have a tool called %s. Your tools: %s.' % (name, ', '.join(sorted(names)))
                elif key in r.setdefault('done_calls', []) and not stale and name in ('search_board', 'read_item', 'read_frame', 'web_search', 'read_web_page', 'read_page_source', 'search_code', 'browser_look'):
                    result = 'You already did exactly this and its result is above. Do something different.'
                    r['loop_turn'] = True
                else:
                    if key in r['done_calls'] and not stale:   # the same call again (a make_demo with no page, refused and resent 30 times)
                        r['loop_turn'] = True
                    r['done_calls'].append(key)
                    r.setdefault('call_at', {})[key] = len(r['messages'])   # where its result will sit: a later repeat may find it cut
                    try:
                        result = self.use(r, name, args if isinstance(args, dict) else {}, c['id'])
                    except Exception as e:   # a tool that breaks (a bad address, a page that will not load) is reported, not fatal
                        result = 'That did not work (%s: %s). Try another way.' % (type(e).__name__, str(e)[:200])
                if result is not None and isinstance(result, str) and name in READ_TOOLS + BOARD_READS:
                    # what this is costing, in view (after Exo's cost legibility): the agent can wrap up before the cap does it
                    used = sum(1 for m in r['messages'] if m['role'] == 'assistant') - r.get('msg_base', 0)
                    result += '\n(Step %d of %d.)' % (used, self.cap(r))
                if result is not None:
                    r['messages'].append(dict({'role': 'tool', 'tool_call_id': c['id'], 'content': result},
                                              **({'keep': True} if name == 'read_item' and isinstance(args, dict) and args.get('item_id') in (r.get('selected') or []) else {})))
                    if result.startswith(('Not added', 'Not done', 'Not typed')):
                        r['refused'] = True
                free = free and (name in BOARD_READS or bool(r.pop('_free', False)))
            shown = r.pop('_imgs', None)
            if shown:   # the screenshots the tools just took, as pictures, for a model that can see them
                r['messages'].append({'role': 'user', 'img': True, 'content': [{'type': 'text', 'text': 'The screenshot%s just taken, for your own eyes (%s). Judge by what you see here, not by a description.' % (
                    's' if len(shown) > 1 else '', '; '.join(c for c, _ in shown))}] + [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b}} for _, b in shown]})
                imgs = [i for i, m in enumerate(r['messages']) if m.get('img') and not m.get('keep')]
                for i in imgs[:-KEEP_IMAGES]:   # older screenshots leave the run for good (the saved run would grow by a picture a step)
                    if not isinstance(r['messages'][i]['content'], str):
                        r['messages'][i] = dict(r['messages'][i], content=text_of(r['messages'][i]['content']) + ' (an earlier screenshot, no longer shown)')
            if calls and free and r.get('free_turns', 0) < FREE_TURNS:
                r['msg_base'] = r.get('msg_base', 0) + 1
                r['free_turns'] = r.get('free_turns', 0) + 1
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
            r['repeated'] = r.get('repeated', 0) + 1 if r.pop('loop_turn', False) else 0   # turns in a row spent repeating or ignoring a refusal
            if r['repeated'] >= 3:   # nudges did not stop a Lead running the same two searches 25 times: code stops it
                self.wrap_up(r)
                self.step(r, 'Stuck: kept repeating the same action, so it was stopped')
                r['status'] = 'unfinished'
                break
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
                r['status'] = 'partial' if r.get('gaps') else 'done'
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

    def cap(self, r):
        """Steps this run gets: a retry a few, a part that builds more than one that reads, a Lead the most."""
        if r.get('watch_retry'):
            return RETRY_STEPS
        if r.get('parent') and not r.get('lead'):
            return BUILD_STEPS if r.get('model') == 'code' or BUILDING.search(r['task']) else PART_STEPS
        return MAX_STEPS

    def sees(self, r):
        return sees('builders' if r.get('model') == 'code' else 'agents')

    def show(self, r, img, caption):
        """Queue a screenshot for the model's own eyes; it goes into the context after this turn's tool results."""
        if img and self.sees(r):
            r.setdefault('_imgs', []).append((caption[:120], base64.b64encode(img).decode()))
            return True
        return False

    def pictures(self, bid, selected):
        """The selected pictures (pasted screenshots, photos) as data URLs, up to 3, each under 3 MB."""
        out = []
        for it in self.board_items(bid):
            if it['id'] in selected and it.get('type') == 'img' and not str(it.get('url') or '').endswith('.pdf'):
                name = str(it.get('src') or '').rsplit('/', 1)[-1]
                path = os.path.join(os.path.dirname(self.dir), 'images', name)
                if re.fullmatch(r'[0-9a-f]{16,}\.(jpe?g|png|webp|gif)', name) and os.path.exists(path) and os.path.getsize(path) < 3_000_000:
                    with open(path, 'rb') as f:
                        mime = {'jpg': 'jpeg'}.get(name.rsplit('.', 1)[1], name.rsplit('.', 1)[1])
                        out.append('data:image/%s;base64,%s' % (mime, base64.b64encode(f.read()).decode()))
            if len(out) >= 3:
                break
        return out

    def corpus(self, r):
        """What this run may quote: what it read itself, and for a Lead, what its teammates put in their frames (their
        hand-back on the board, not the checker's words about it)."""
        text = read_corpus(r)
        if r.get('children'):
            text += '\n' + plain('\n'.join(self.frame_words(self.runs[c]) for c in r['children'] if c in self.runs and self.runs[c]['status'] in ENDED))
        return text

    def wrap_up(self, r):
        r['messages'].append({'role': 'user', 'content': 'You are out of steps. Write down what you found now, plainly and '
                              'with the exact code or sources you saw: add_text, then finish.'})
        try:
            msg = self.call(r['messages'], [t for t in TOOLS if t['function']['name'] in ('add_text', 'finish')], r.get('model'))
        except (OSError, ValueError, KeyError):
            msg = {}
        calls = msg.get('tool_calls') or []
        content = re.sub(r'<think>.*?</think>', '', msg.get('content') or '', flags=re.S).strip()
        if not calls and ('<tool_call>' in content or 'DSML' in content):
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
        if not models.choice('voice') and url == (models.local('voice') or (talk.MODEL_URL, talk.MODEL))[0]:
            choice = talk.yielding_post(url, json.dumps(body).encode(), 900)['choices'][0]   # the voice's own model: a question goes first
        else:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=900) as f:
                choice = json.load(f)['choices'][0]
        return dict(choice['message'], _cut=choice.get('finish_reason') == 'length')

    def use(self, r, name, a, call_id):
        """Run one tool; returns what the model reads back (None = answered later, by the person)."""
        if name in WRITES and CUT_PASTED.search(json.dumps(a)):   # the words came from a cut result, not from the item
            return ('Not added: that text comes from a result that was cut short. Read the item or page again (read_item, '
                    'the part named; read_web_page) and quote from the fresh text.')
        looks = sum(1 for x in r['steps'][r.get('step_base', 0):] if x['text'].startswith(('Searched the board', 'Read a', 'Read the frame'))) \
            if r.get('lead') and name in ('search_board', 'read_item', 'read_frame') else 0
        if looks and r.get('children'):   # a Lead with parts back: its looks since the last round came in
            since = max([i for i, x in enumerate(r['steps']) if x['text'].startswith(('Handed out', 'The team finished this round'))]
                        + [r.get('step_base', 0)])
            looks = sum(1 for x in r['steps'][since:] if x['text'].startswith(('Searched the board', 'Read a', 'Read the frame')))
            if looks >= 8:   # the design Lead read 110 items after its last round and ran out of steps (2026-11-07)
                if looks >= 12:
                    r['loop_turn'] = True
                return ('Not done: you have read enough (%d looks) and your teammates\' results are above. Write the final answer '
                        'now from them (add_text), then finish.' % looks)
        elif looks >= 5 and (r.get('crew') is None or len(r['crew']) > 1):
            # a manager that keeps looking instead of delegating (a Lead once searched the board 14 times first;
            # this sat below the search itself and never ran, so a Lead searched 25 times on Oct 31)
            r['loop_turn'] = True   # told and still searching: three of these and it is stopped as Stuck
            return 'Not done: you have looked enough. Hand the work out now with assign; your reports will dig deeper.'
        elif looks >= 8:   # a Lead alone: it once spent 44 model calls searching for a one-call copy job (2026-11-07 mining: 9 jobs)
            if looks >= 12:
                r['loop_turn'] = True
            return ('Not done: you have read enough (%d looks). Do the task now with what you read: write the result with '
                    'add_text or add_file (a copy = add_file with from_item), then finish.' % looks)
        alone = r.get('lead') and r.get('crew') is not None and len(r['crew']) <= 1   # a Lead with no teammates does the job itself (2026-11-05)
        if r.get('lead') and not alone and name in ('add_text', 'finish') and not any(
                x['text'].startswith('Handed out') for x in (r['steps'] if r.get('watch_retry') else r['steps'][r.get('step_base', 0):])):
            # a Lead the watchdog started again writes from the rounds it already ran: it once re-ran all four finished parts
            # because only hand-outs since the restart counted (17 minutes, 2026-11-07)
            # a Lead puts teammates' results together; it does not do the work itself (it once pasted code instead of a demo)
            return ('Not done: you are the Lead. Hand this to the teammate whose role fits it (assign) first; you write only '
                    'the final answer, from their results.')
        if name == 'search_board':
            hits = self.search(r['board'], a.get('query', ''), r['id'])
            self.step(r, 'Searched the board for "%s"' % a.get('query', ''))
            if hits:
                r['focus'] = hits[0]['item']
            return '\n\n'.join('[%s] %s: %s' % (h['item'], h['kind'], h['text']) for h in hits) or 'Nothing on the board matches.'
        if name == 'read_item':
            it = next((i for i in self.board_items(r['board']) if i['id'] == a.get('item_id')), None)
            if not it:
                return 'There is no item with that id.'
            kind, words = talk.item_words(it, self.reader)
            if it.get('type') == 'file':   # name and text ran together ("file: Test 2.md Test 2"): a two-word file read as a file with no text (2026-11-06)
                text, label = self.item_text(it)
                tail = words[len(talk._item_words(it, self.reader)[1]):]   # why it is on the board, when known
                words = 'the file "%s"; its whole text is %d words, between the marks « and »: «%s»%s' % (
                    label, len(text.split()), text or '(no readable words in it)', tail)
            r['focus'] = it['id']
            ws = words.split()
            per, total = 2000, max(1, -(-len(ws) // 2000))
            part = min(max(int(a.get('part') or 1), 1), total)
            self.step(r, 'Read a %s on the board%s' % (kind, ' (part %d of %d)' % (part, total) if total > 1 else ''))
            head = ('[part %d of %d, words %d-%d of %d%s] ' % (part, total, (part - 1) * per + 1, min(part * per, len(ws)), len(ws),
                    '; ask for part %d to read on' % (part + 1) if part < total else '; this is the end') if total > 1 else '')
            return '%s: %s%s' % (kind, head, ' '.join(ws[(part - 1) * per:part * per]) or '(no words)')
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
            return self.read_page(r, str(a.get('url') or '').strip())
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
        if name == 'read_page_source' and self.blocked(r, a.get('url', '')):
            return self.blocked(r, a.get('url', ''))
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
        if name == 'transcribe':   # a video or recording -> its words, by whisper on your own machine in one call (2026-11-05): no caption scraping
            url, it = str(a.get('url') or '').strip(), None
            if a.get('from_item'):
                it = next((i for i in self.board_items(r['board']) if i['id'] == a.get('from_item')), None)
                if not it:
                    return 'Not done: there is no item with that id.'
                url = str(it.get('url') or '').strip() if it.get('type') in ('link', 'web') else ''
            if it and it.get('loom'):   # an OpenLoom video: OpenLoom already has its transcript, so read that instead of transcribing again
                return talk.item_words(it, self.reader)[1]
            if url and openloom.video_id(url):
                return openloom.words(openloom.video_id(url))
            if not url and not (it and it.get('type') in ('file', 'media') and it.get('src')):
                return 'Not done: give url (a video link) or from_item (a link, video or audio card on the board).'
            if not TRANSCRIBE_URL:
                return 'Not done: no transcriber is set up on this install (TRANSCRIBE_URL). Say so in your frame and finish.'
            self.step(r, 'Transcribing %s' % (url[:80] or (it.get('label') or 'a recording')))
            try:
                if url:
                    body, ctype, headers = json.dumps({'url': url}).encode(), 'application/json', {}
                else:
                    src = str(it['src']).rsplit('/', 1)[-1]
                    with open(os.path.join(os.path.dirname(self.dir), 'files', src), 'rb') as f:
                        body = f.read()
                    ctype, headers = ('video/mp4' if src.endswith(('mp4', 'mov', 'webm', 'mkv')) else 'audio/x'), {'X-Name': (it.get('label') or src)[:120]}
                req = urllib.request.Request(TRANSCRIBE_URL + '/transcribe', data=body, headers=dict({'Content-Type': ctype}, **headers), method='POST')
                with urllib.request.urlopen(req, timeout=3600) as resp:
                    out = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                try:
                    msg = json.loads(e.read().decode()).get('error', '')
                except Exception:
                    msg = str(e)
                return 'Not done: the transcriber said: %s. Say so in your frame and finish; do not look for captions elsewhere.' % str(msg)[:300]
            except (OSError, ValueError) as e:
                return 'Not done: the transcriber could not be reached (%s). Say so in your frame and finish; do not look for captions elsewhere.' % str(e)[:200]
            title = ' '.join(str(out.get('title') or a.get('name') or 'Transcript').split())[:120]
            mins = int(out.get('duration_s') or 0) // 60
            paras, cur, start = [], [], None
            for s in out.get('segments') or []:   # paragraphs of about a minute, each with where it starts
                if start is None:
                    start = s['t0']
                cur.append(s['text'])
                if s['t1'] - start >= 60 or s['text'].rstrip().endswith(('.', '?', '!')) and s['t1'] - start >= 40:
                    paras.append('[%02d:%02d] %s' % (int(start) // 60, int(start) % 60, ' '.join(cur)))
                    cur, start = [], None
            if cur:
                paras.append('[%02d:%02d] %s' % (int(start or 0) // 60, int(start or 0) % 60, ' '.join(cur)))
            md = '# %s\n\nTranscript, %d min%s. Made by speech recognition (whisper large-v3-turbo), so a few words may be misheard.\n\n%s\n' % (
                title, mins, ', from %s' % url if url else '', '\n\n'.join(paras) or out.get('text', ''))
            made = self.add_file(r, str(a.get('name') or '') or title + ' - transcript.md', md)
            return ('Transcribed "%s" (%d min of audio, %d words) into the file %s in your frame; it is the whole thing. Say its name in your summary.'
                    % (title, mins, len(out.get('text', '').split()), made['label'])) if made else 'Not done: the frame is gone.'
        if name == 'make_picture':   # a picture from words, by the image generator set for this install (2026-11-09): a picture card, searchable like a dropped photo
            if not IMAGE_URL:
                return 'Not done: no image generator is set up on this install (IMAGE_URL). Say so in your frame and finish.'
            prompt = ' '.join(str(a.get('prompt') or '').split())
            if len(prompt) < 3:
                return 'Not done: say what the picture shows.'
            w, h = {'wide': (1216, 832), 'tall': (832, 1216)}.get(str(a.get('shape') or '').lower(), (1024, 1024))
            self.step(r, 'Making a picture: %s' % prompt[:80])
            try:
                req = urllib.request.Request(IMAGE_URL + '/v1/images/generations', data=json.dumps({'prompt': prompt, 'size': '%dx%d' % (w, h)}).encode(),
                                             headers={'Content-Type': 'application/json'}, method='POST')
                with urllib.request.urlopen(req, timeout=900) as resp:
                    png = base64.b64decode(json.loads(resp.read().decode())['data'][0]['b64_json'])
            except (OSError, ValueError, KeyError, IndexError) as e:
                return 'Not done: the image generator could not make it (%s). Say so in your frame; do not look for a picture elsewhere.' % str(e)[:200]
            fn = hashlib.sha256(png).hexdigest() + '.png'
            images = os.path.join(os.path.dirname(self.dir), 'images')
            os.makedirs(images, exist_ok=True)
            path = os.path.join(images, fn)
            if not os.path.exists(path):
                with open(path + '.tmp', 'wb') as f:
                    f.write(png)
                os.replace(path + '.tmp', path)
            self.reader.add(fn)
            label = ' '.join(str(a.get('name') or '').split())[:60] or prompt[:60]
            u = r['lay']['u']
            it = self.place(r, {'type': 'img', 'src': 'api/images/' + fn, 'label': label, 'ratio': h / w}, 480 * u, 480 * u * h / w, wide=True)
            return ('Made the picture "%s" (%dx%d) in your frame. Say in your summary what it shows.' % (label, w, h)) if it else 'Not done: the frame is gone.'
        if name == 'add_file' and a.get('from_item'):   # a copy of a board item, made by the server: no model retypes a file (2026-11-05)
            it = next((i for i in self.board_items(r['board']) if i['id'] == a.get('from_item')), None)
            if not it:
                return 'Not added: there is no item with that id.'
            text, label = self.item_text(it)
            if len(text) < 2:
                return 'Not added: that item has no words to copy.'
            out = self.add_file(r, str(a.get('name') or '') or re.sub(r'\.[a-z0-9]{1,5}$', '', label, flags=re.I) + '.md', text)
            return ('Copied %s whole into the file %s (%d words) in your frame; nothing is missing from it. Say its name in your '
                    'summary.' % (it['id'], out['label'], len(text.split()))) if out else 'Not added: the frame is gone.'
        if name == 'add_file':   # a real file on the board, made from the agent's own words (2026-11-05)
            text = str(a.get('text') or '').strip()
            if len(text) < 2:
                return 'Not added: the file needs text.'
            if '...' in text or '…' in text:
                return 'Not added: the file text has "..." in it. Write the whole thing, nothing cut.'
            it = self.add_file(r, str(a.get('name') or ''), text)
            return ('Made the file %s (%d characters) in your frame. Say its name in your summary.' % (it['label'], len(text))) if it else 'Not added: the frame is gone.'
        if name in ('add_card', 'add_note', 'add_text', 'add_file', 'add_link', 'make_demo') and r.get('drafted'):   # the preview + Publish card are the hand-back (a writer kept adding its own draft card, 2026-11-06)
            return 'Not added: the draft as Substack holds it and its Publish card are already in your frame, and nothing more goes on the board about it. Call finish.'
        if name == 'add_card':   # a card is a note with a title line: the same checks, then drawn as a card
            name, a = 'add_note', dict(a, text=(str(a.get('title') or '').strip() + '\n' + str(a.get('facts') or '').strip()).strip(), card=1)
        if name in ('add_note', 'add_text'):   # quote marks promise word for word: a quote that is on no page the agent read is turned away
            bad = missing_quote(a.get('text', ''), self.corpus(r))
            if bad:
                self.step(r, 'Turned away a quote that is not on any page it read')
                return ('Not added: the quote "%s" is not on any page you read, word for word. Quote marks mean the exact words '
                        'from a page you read with read_web_page or an item you read with read_item. Either quote the real words, '
                        'or describe it without quote marks and say where it came from.' % bad[:120])
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
            how = self.place_note(r, text + ('' if src.lower() == 'board' or src in text else '\n\n' + src), src if src.lower() != 'board' else 'board',
                                  card=bool(a.get('card')))
            self.step(r, 'Added a text block' if how == 'text' else 'Added a card' if a.get('card') else 'Added a note')
            return 'Added, as a text block: it is longer than a card holds, so every word is kept.' if how == 'text' else 'Added.'
        if name == 'add_text':
            self.place_text(r, a.get('text', ''), a.get('style') if a.get('style') in ('heading', 'body') else 'body')
            self.step(r, 'Added a heading' if a.get('style') == 'heading' else 'Added text')
            return 'Added.'
        if name == 'add_link':
            url = a.get('url', '')
            if not re.match(r'^https?://\S+$', url):
                return 'That is not a web link.'
            self.place(r, {'type': 'link', 'url': url, '_src': url}, 280 * r['lay']['u'], 110 * r['lay']['u'])
            self.step(r, 'Added a link to %s' % url[:100])
            return 'Added.'
        if name in self.apps.get(r['id'], {}):
            return self.use_app(r, self.apps[r['id']][name], a)
        if name == 'make_demo':
            return self.make_demo(r, str(a.get('title') or 'Demo')[:80], str(a.get('html') or ''))
        if name == 'try_demo':
            return self.try_demo(r, str(a.get('title') or ''), a.get('steps') if isinstance(a.get('steps'), list) else [])
        if name == 'ask_user' and r.get('lead') and not r.get('children') and not r.get('asked_check') and (r.get('crew') is None or len(r['crew']) > 1):
            r['asked_check'] = True   # a Lead asks before handing out anything: once, it is pointed at its team
            team = reports_of(self.team(r['board'])['members'], (r.get('member') or {}).get('id', 'lead'))
            return ('Not asked yet: can a teammate find this out? %s. If so, hand it to them with assign. Ask the person only '
                    'for a choice only they can make, or something only they know; then ask again.'
                    % '; '.join('%s: %s' % (m['name'], m.get('role', '')) for m in team))
        if name == 'ask_user' and r.get('ask_refused', 0) < 2 and ON_THE_BOARD.search(str(a.get('question') or '')):
            r['ask_refused'] = r.get('ask_refused', 0) + 1   # a reader and a checker each stalled a job for 40 min asking for a card's own text (2026-11-05)
            return ('Not asked: that is on the board, not something only the person knows. Read it with read_item (the whole '
                    'text comes back, also on a repeat read when the earlier one was cut) or read_frame, then go on. If a part '
                    'truly cannot be read, say so in your frame and finish; ask the person only for a choice only they can make.')
        if name == 'ask_user':
            if r.get('question'):
                return 'You already asked a question; wait for that answer first.'
            u = r['lay']['u']
            note = self.place(r, {'type': 'note', 'text': str(a.get('question', ''))[:600], 'color': '#ffc9de', 'ask': r['id'],
                                  'fs': 14 * u}, 220 * u, 190 * u)
            r['question'] = {'call': call_id, 'text': str(a.get('question', ''))[:600], 'note': note and note['id']}
            self.step(r, 'Asked you: ' + r['question']['text'][:160])
            return None
        if name in CODE_NAMES:
            if name not in {t['function']['name'] for t in CODE_READ} and not str((r.get('member') or {}).get('id', '')).startswith('engineer'):
                return 'Only the engineer changes code. Read it with code_files, code_search and code_read.'
            return self.use_code(r, name, a)
        if name == 'finish' and str((r.get('member') or {}).get('id', '')).startswith('engineer') and r.get('code_repos') \
                and not r.get('code_clean'):
            return ('Not finished: your changes are not committed with a clean check. Run code_check, fix what it reports, '
                    'then code_commit, then finish.')
        if name == 'assign' and r.get('lead'):
            return self.assign(r, a.get('assignments'), call_id)
        if name == 'finish' and r.get('parent') and BUILDING.search(r['task']) and not r.get('built') and not r.get('lead') \
                and r.get('nudged_build', 0) < 3 and BROWSER_URL is not None \
                and (r.get('member') or {}).get('kind') not in ('write', 'distill', 'research', 'check', 'analyze'):   # "build the post from..." is not a demo (2026-11-06: a writer drew the dogs)
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
            it = r.get('intent') or {}
            if not r.get('parent') and it.get('deliver') and not r.get('final_done'):   # Done only when the job checks out
                v = self.final_check(r, str(a.get('summary') or ''))
                if not v['pass'] and not r.get('final_retry'):
                    r['final_retry'] = True
                    advice = self.advice(r, v['missing'] or v['why'])
                    return ('Not finished: the final check read the work against what the user was promised (%s) and found this '
                            'missing: %s. Hand out one more round for it if it can be had, or say plainly in your frame that '
                            'it is missing and why; then finish again.' % (it['deliver'], v['missing'] or v['why'])
                            + ('\n\nADVICE FROM THE ADVISOR (a stronger model, asked once; follow it): ' + advice if advice else ''))
                r['final_done'] = True
                r['gaps'] = None if v['pass'] else (v['missing'] or v['why'])
            r['summary'] = str(a.get('summary') or '')[:2000]
            if not r.get('parent') and r.get('draft'):   # the draft, from the record: a Lead wrote "Substack chose the cover itself" when the board picture was the cover (2026-11-06)
                dr = r['draft']
                r['summary'] += ' [On Substack: "%s" is saved as a draft, not published. Cover picture: %s. Pictures inside the post: %d. Its preview and Publish button are on the board.]' % (
                    dr['title'], dr['cover'] or 'none', dr['pictures'])
            g = self.grouped(r)
            if g:   # the facts, in code, next to its own words
                n = sum(int(re.match(r'Grouped (\d+)', x).group(1)) for x in g)
                r['summary'] += ' [On the board: %d items moved into %d groups; %d items in the frames read are not in a group.]' % (
                    n, len(g), len(self.left(r)))
            if a.get('summary') and not any(s['text'] == 'Added text' for s in r['steps'][r.get('step_base', 0):]):   # it already wrote its answer out
                self.place_text(r, 'Summary', 'heading')
                self.place_text(r, a['summary'], 'body')
            r['finished'] = True
            self.step(r, 'Done, with gaps: missing %s' % r['gaps'] if r.get('gaps') else 'Finished')
            return 'Done.'
        return 'There is no tool called %s.' % name

    def assign(self, r, parts, call_id):
        """The Lead hands out a round of work: one run per part, each in its own frame right of the Lead's."""
        if r.get('team_call'):
            return 'You already handed out work; wait for it.'
        limit = MAX_ROUNDS if not r.get('parent') else 3
        if sum(1 for s in r['steps'][r.get('step_base', 0):] if s['text'].startswith('Handed out')) >= limit:
            return 'You have handed out work %d times. Put the final answer together yourself and finish.' % limit
        members = self.root(r).get('crew') or self.team(r['board'])['members']   # the team made for this job, else the board's
        team = {m['name'].lower(): m for m in reports_of(members, (r.get('member') or {}).get('id', 'lead'))}
        if not isinstance(parts, list) or not parts:
            return 'Give at least one part: [{"teammate": name, "task": what to do}].'
        bad = [str(p.get('teammate')) for p in parts if not isinstance(p, dict) or str(p.get('teammate', '')).lower() not in team]
        if bad:
            return 'No teammate called %s. Your teammates: %s.' % (', '.join(bad), ', '.join(m['name'] for m in team.values()))
        now = [str(p.get('teammate', '')).lower() for p in parts]
        for p in parts:   # a part that reads a teammate's frame cannot run in the same round as that teammate (2026-11-07: a
            me = str(p.get('teammate', '')).lower()   # shipping researcher ran out of steps waiting on URLs that did not exist yet)
            needs = [n for n in now if n != me and n in str(p.get('task', '')).lower()]
            if needs:
                return ('Not handed out: %s\'s part reads %s\'s work, which does not exist yet (they would run at the same time). '
                        'Hand out %s now, and %s in the next round, after that work is back.'
                        % (team[me]['name'], team[needs[0]]['name'], team[needs[0]]['name'], team[me]['name']))
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
        about = ('\nThe selected items this job is about: %s. Work from these.' % ', '.join(r['selected'][:20])) if r.get('selected') else ''
        avoid = (self.root(r).get('intent') or {}).get('avoid')
        if avoid:
            about += '\nNot doing (the person did not ask for it): %s.' % avoid
        for p in parts[:8]:   # a team once built from a look-alike card instead of the one the person pointed at (2026-11-07 mining: 4 jobs)
            limits = CANNOT_NOTE if IMPOSSIBLE.search('%s %s' % (p.get('task', ''), p.get('done_when', ''))) else ''   # six retries chased a shell nobody has (2026-11-08)
            c = self.start(r['board'], str(p.get('task', '')) + about + limits, r.get('selected', []), {'x': frame['x'] + frame['w'] + 200 * u, 'y': frame['y'], 'unit': u},
                           member=team[str(p['teammate']).lower()], parent=r['id'], done_when=p.get('done_when'))
            r['children'].append(c['id'])
            r['round'].append(c['id'])
        r['team_call'] = call_id
        self.crew_paint(r)
        self.step(r, 'Handed out %d part%s: %s' % (len(r['round']), '' if len(r['round']) == 1 else 's',
                                                    ', '.join(str(p['teammate']) for p in parts[:8])))
        return None

    def use_app(self, r, app, a):
        """A connected app's tool: reading runs; sending, creating, changing or deleting waits for the person's yes."""
        _, name, needs_yes, fn, label = app
        shown = ', '.join('%s=%s' % (k, str(v)[:60]) for k, v in a.items() if k != 'confirm')[:200]
        if needs_yes and not (a.get('confirm') and r.pop('approved', False)):
            r['approved'] = False
            return ('Not done: %s on %s changes something outside the board. Ask the person first with ask_user, saying exactly '
                    'what will be done (who it goes to, what it says). Only if they say yes, make the same call again with confirm '
                    'true.' % (name, label))
        self.step(r, 'Used %s: %s%s' % (name, shown, ' (you said yes)' if needs_yes else ''))
        if name == 'substack_draft':   # the post's cover picture comes from this board and the cards the job was started on
            a = dict(a, _board=r['board'], _selected=r.get('selected') or [])
            d = integrations.substack_draft(a)
            try:
                self.substack_cards(r, d)   # what the post will look like, and the Publish button, on the board (2026-11-06)
                shown = ' The board now shows the draft as Substack holds it and its Publish card in your frame: that is the whole hand-back, so nothing more goes on the board about it (a card, note or demo would be turned away), and never redraw a picture. Call finish now, naming the post by its title.'
            except (OSError, ValueError, KeyError, TypeError):
                shown = ''
            return (integrations.substack_draft_text(d) + shown)[:integrations.TOOL_CHARS]
        return str(fn(a))[:integrations.TOOL_CHARS]

    def substack_cards(self, r, d):
        """After a teammate saves a Substack draft: a preview card that shows the draft as Substack holds it (drawn from
        Substack's own copy on every load, the cover marked as the cover) and, beside it, the same draft card the note menu
        makes, with its Publish button, so the person's yes is one read and one click on the board. The draft is recorded on
        this run and on the whole job, so the voice's done news and the Lead's summary state it from the record, not from memory."""
        u = r['lay']['u']
        prev = self.place(r, {'type': 'web', 'url': 'api/substack/preview/%s' % d['id'], 'demo': True, 'run': r['id'],
                              'sub': {'id': str(d['id'])}, 'label': 'Substack draft: ' + d['title']}, r['lay']['inner'], 560 * u, wide=True)
        pub = d.get('publication') or 'your newsletter'
        card = self.place(r, {'type': 'link', 'url': d['url'], 'sub': {'id': str(d['id']), 'state': 'draft', 'title': d['title'], 'pub': pub},
                              'unfurl': {'title': 'Substack draft: ' + d['title'], 'img': d.get('cover_url') or '', 'pub': 'substack.com',
                                         'desc': 'Saved as a draft on %s by the team, not published. The preview beside this card is the draft as Substack holds it: Publish\u2026 sends exactly that.%s' % (
                                             pub, ' Cover picture (on the newsletter page and in shared links, not inside the post): %s.' % d['cover'] if d.get('cover') else ' No cover picture: the newsletter logo stands in for it.')}},
                          300 * u, (270 if d.get('cover_url') else 132) * u)
        rec = {'app': 'substack', 'id': str(d['id']), 'title': d['title'], 'url': d['url'], 'cover': d.get('cover') or '', 'pictures': int(d.get('pictures') or 0),
               'board': r['board'], 'frame': r['frame'], 'card': (card or {}).get('id'), 'preview': (prev or {}).get('id'), 'at': now_ms()}
        r['draft'] = rec
        r['drafted'] = True   # nothing more goes on the board about it: the cards above are the whole hand-back
        top = r
        while top.get('parent') and self.runs.get(top['parent']):
            top = self.runs[top['parent']]
        if top is not r:
            top['draft'] = rec
            self.save(top)
        self.step(r, 'Put the post preview and its Publish button on the board')

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
            if self.blocked(r, args['url']):
                return self.blocked(r, args['url'])
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
        if cmd == 'open' and BLOCKED_PAGE.search((page.get('title') or '') + '\n' + (page.get('text') or '')[:600]):
            light = self.light_read(page['url'])   # Chromium was turned away: Lightpanda sometimes is not
            if light:
                r.setdefault('read_urls', []).append(page['url'])
                self.pages.setdefault(self.root(r)['id'], {})[page['url']] = light
                return ('The browser was turned away, but the page was read another way (as text, no clicking). It is below; '
                        'use read_web_page for its other pages.\n\n' + light)
            return self.block(r, page['url'], 'it shows the browser a "you have been blocked" page')
        r['page_url'] = page['url']
        r['page_els'] = {str(e['n']): {k: e.get(k) for k in ('name', 'role', 'submits', 'formKind', 'password')} for e in page['elements']}
        r.setdefault('read_urls', []).append(page['url'])   # what it reads in the browser can be a note's source
        self.saw(r, page['url'] + ' ' + ' '.join(e.get('href') or '' for e in page['elements']))
        return browser_text(page)

    def read_page(self, r, url):
        """A web page as text, the fastest way that works: a plain read (under a second); Lightpanda when the plain read
        is turned away or comes back nearly empty (a page that needs its JavaScript); a site that turns both away is
        remembered as blocked for the whole job. A page any teammate already read comes from memory, as a free step."""
        if self.blocked(r, url):
            return self.blocked(r, url)
        r['focus'] = r['frame']
        kept = self.pages.setdefault(self.root(r)['id'], {})
        if url in kept:
            r['_free'] = True
            r.setdefault('read_urls', []).append(url)
            self.step(r, 'Read %s (read earlier in this job)' % url[:110])
            return kept[url]
        why, title, words = '', '', ''
        try:
            title, words = web.read_page(url)
            if BLOCKED_PAGE.search(title + '\n' + words[:600]):
                why, words = 'it answers with a "you have been blocked" page', ''
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403, 429, 503):
                return 'Could not read that page (HTTP %d).' % e.code
            why = 'it answers HTTP %d' % e.code
        except (OSError, ValueError, http.client.HTTPException) as e:   # HTTPException: a bad address like "http://host:3C"
            why = str(e)[:120]
        if why or len(words.split()) < 80:   # turned away, or a page that only fills in with JavaScript
            light = self.light_read(url)
            if light and len(light.split()) > len(words.split()):
                self.step(r, 'Read %s (with Lightpanda)' % url[:110])
                r.setdefault('read_urls', []).append(url)
                kept[url] = light
                return light
        if why and ('HTTP' in why or 'blocked' in why):
            return self.block(r, url, why)
        if why:
            self.step(r, 'Could not read %s' % url[:120])
            return 'Could not read that page (%s).' % why
        self.step(r, 'Read %s' % url[:120])
        r.setdefault('read_urls', []).append(url)
        kept[url] = '%s\n%s' % (title, words or '(no text on the page)')
        return kept[url]

    def light_read(self, url):
        """The page's text from Lightpanda on the browser machine (it runs the page's JavaScript), or '' if it fails."""
        if not BROWSER_URL:
            return ''
        req = urllib.request.Request(BROWSER_URL + '/read', data=json.dumps({'url': url}).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Token': BROWSER_TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=30) as f:
                out = json.load(f)
        except (OSError, ValueError):
            return ''
        text = out.get('text') or ''
        if out.get('error') or (out.get('status') or 200) >= 400 or BLOCKED_PAGE.search(text[:600]):
            return ''
        return ' '.join(text.split(' ')[:web.PAGE_WORDS])

    def site(self, url):
        host = re.sub(r'^https?://([^/:?#]+).*', r'\1', str(url or '')).lower()
        parts = host.split('.')
        return '.'.join(parts[-3:] if len(parts) > 2 and len(parts[-1]) == 2 and parts[-2] in ('co', 'com', 'org', 'net', 'gov', 'ac') else parts[-2:])

    def block(self, r, url, why):
        """A page both readers were turned away from. Two such pages and the site is blocked for the whole job, so no
        teammate spends steps on it again (one failed search page must not rule out listing pages that do read)."""
        root, site = self.root(r), self.site(url)
        failed = root.setdefault('turned_away', {}).setdefault(site, [])
        if url not in failed:
            failed.append(url)
        if len(failed) < 2:
            self.step(r, 'Turned away by %s' % url[:110])
            return ('Turned away: %s would not let us read this page (%s). Try another of its pages once; if that is turned '
                    'away too, the site is skipped. Do not try to get around it (caches, archives, other readers).' % (site, why))
        root.setdefault('blocked', {})[site] = why
        self.step(r, '%s blocks automated readers; skipping it' % site)
        return self.blocked(r, url)

    def hurdles(self, r):
        """A card at the end of a job: what got in the way (blocked sites, pages Lightpanda had to read, free steps), so a
        "Done, with gaps" says why without opening the steps. Nothing got in the way: no card."""
        kids = [self.runs[c] for c in r.get('children', []) if c in self.runs]
        light = sum(1 for x in [r] + kids for s in x.get('steps', []) if '(with Lightpanda)' in s['text'])
        free = sum(x.get('free_turns', 0) for x in [r] + kids)
        gone = r.get('blocked') or {}
        once = {k: v for k, v in (r.get('turned_away') or {}).items() if k not in gone}
        lines = (['Blocked sites: ' + '; '.join('%s (%s)' % kv for kv in sorted(gone.items()))] if gone else []) + \
                (['Turned away once: ' + ', '.join('%s (%d page)' % (k, len(v)) for k, v in sorted(once.items()))] if once else []) + \
                (['Read with Lightpanda: %d page%s that needed JavaScript or turned the plain reader away' % (light, '' if light == 1 else 's')] if light else []) + \
                (['Free steps: %d (reading the board, and pages a teammate had already read)' % free] if free else [])
        if gone or once or light:
            self.card(r, 'What got in the way\n' + '\n'.join(lines), '#fff4e0')

    def about_blocked(self, p, task):
        """Is this part about a site that is blocked for the job ("BizBuySell Ontario listings")?"""
        t = re.sub(r'[^a-z0-9.]', '', str(task).lower())
        return any(s.split('.')[0] in t for s in (self.root(p).get('blocked') or {}))

    def blocked(self, r, url):
        """The answer for a site already found blocked in this job (a free step), or ''."""
        gone = self.root(r).get('blocked') or {}
        site = self.site(url) if self.site(url) in gone else next((s for s in gone if s in str(url).lower()), '')   # its page through an archive, a cache or a reader
        why = gone.get(site)
        if not why:
            return ''
        r['_free'] = True
        return ('Blocked: %s turns automated readers away (%s). Do not try it again, any page on it, with any tool, and do not '
                'try to get around it (caches, archives, other readers). Say on the board that this site is blocked; if your part '
                'was only about this site, that note is your result: finish. Otherwise use another site.' % (site, why))

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
                self.show(r, data, 'your browser on ' + str(out.get('url', ''))[:80])
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
        shot = os.path.join(self.shots, r['id'] + '.jpg')
        if self.sees(r) and os.path.exists(shot):   # the model looks for itself; the screenshot follows this result
            looks = ('see its screenshot below. Compare it with the reference pictures and the page you are rebuilding, and '
                     'fix what does not match (call make_demo again with the same title); a demo is done when it looks right, not when it runs')
        else:
            looks = self.see(r, 'This is a screenshot of a demo web page an agent just built: "%s". Describe plainly what is '
                                'actually visible and where, and anything that looks broken, empty or missing. Under 90 words.' % title)
            looks = ('(a model looked at a screenshot): ' + looks + '\nIf that is not what the demo should show, fix it and call make_demo again with the same title.') if looks else ''
        return ('The demo is on the board. ' + ('It runs with no JavaScript errors.' if not errs else
                'JAVASCRIPT ERRORS (fix them and call make_demo again with the same title): ' + ' | '.join(errs)) +
                ('\nWHAT IT LOOKS LIKE ' + looks if looks else '') +
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
            when = 'as first opened' if i == 0 else 'after: ' + st['step']
            if self.show(r, img, 'the demo "%s" %s' % (title, when)):
                looks = 'see its screenshot below'
            else:
                looks = self.see(r, 'A screenshot of the demo "%s" %s. Describe plainly what is visible and where, and anything that '
                                    'looks broken, empty, cramped or unreadable. Under 80 words.' % (title, when), img) if img else ''
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

    def search(self, bid, query, own=None):
        b = self.store.read_board(bid)
        b = dict(b, items=[i for i in b.get('items') or [] if not own or i.get('agent') != own])   # not its own work; other teams' work counts (a team could not find the last team's findings)
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
            # what is really in the frame so far: the page measures text as it draws it and saves the real heights, which run
            # longer than the guesses here, so a new row starts under the real bottom, not the guessed one (2026-11-04)
            mine = [i for i in items if i.get('agent') == r['id'] and i.get('type') not in ('area', 'arrow')
                    and frame['x'] - 1 <= i['x'] < frame['x'] + frame['w']]
            real = max([i['y'] + i['h'] for i in mine] + [frame['y'] + L['pad']]) - frame['y'] - L['pad']
            if wide or L['cx'] + w > L['inner'] + 1:   # a new row
                if L['cx'] > 0:
                    L['cy'] += L['rowH'] + L['gap']
                if mine:
                    L['cy'] = max(L['cy'], real + L['gap'])
                L['cx'], L['rowH'] = 0, 0
            new = dict(item, id='a' + secrets.token_hex(5), agent=r['id'], x=frame['x'] + L['pad'] + L['cx'],
                       y=frame['y'] + L['pad'] + L['cy'], w=w, h=h, z=max([i.get('z', 1) for i in items] + [1]) + 1)
            src = new.pop('_src', None)   # why it is on the board: who put it there, for which task, from where
            new['why'] = {'by': (r.get('member') or {}).get('name') or 'An agent', 'task': r['task'][:400],
                          'asked': self.root(r)['task'][:400] if r.get('parent') else '', 'when': now_ms(), 'run': r['id'],
                          **({'source': {'app': 'Web' if src.startswith('http') else 'Board', 'title': src[:200],
                                         'url': src if src.startswith('http') else ''}} if src else {})}
            if wide:
                L['cy'] += h + L['gap']
            else:
                L['cx'] += w + L['gap']
                L['rowH'] = max(L['rowH'], h)
            frame['h'] = max(frame['h'], 2 * L['pad'] + L['cy'] + L['rowH'], real + 2 * L['pad'])
            items.append(new)
            return new
        new = self.store.edit_board(r['board'], put)
        if new:
            r['focus'] = new['id']
            self.spread(self.root(r))
            self.changed(r['board'])
        return new

    def job_runs(self, r):
        """This run and every part under it, oldest first."""
        out = [r]
        for c in r.get('children', []):
            if c in self.runs:
                out += self.job_runs(self.runs[c])
        return sorted(out, key=lambda x: x['created'])

    def spread(self, root, force=False):
        """Keep a job clean on its board (2026-11-13: "it should always be clean"):
        1. its frames in one row: the Lead's frame stays, each part's frame to the right of the one before, at the Lead's
           height, with its cards. Frames only grow down, so a row never runs into itself. Frames used to go in any free
           spot 900 tall and then grew 12,000+ down over each other, 2026-11-13). Done only when
           two of the job's frames overlap (or force), so a layout made by hand stays.
        2. clear of what was there before it: when its frames grew over older cards, or come within a gap of an older team's
           whole box, the whole job (row, chart, cards) moves the shorter way, right or down, until it is clear (jobs
           stacked one under another stay a stack). What was added after the job (the user's notes on its work) and their own
           frames (a team may sit inside one) never move it.
        The board is read first and written only when something moves. True when it moved."""
        jr = self.job_runs(root)
        if not any(x.get('frame') for x in jr):
            return False
        mine = {x['id'] for x in jr}
        ids = [x['frame'] for x in jr if x.get('frame')]
        gap = 200 * float((root.get('lay') or {}).get('u') or 1)
        hit = lambda a, b: a['x'] < b['x'] + b['w'] and b['x'] < a['x'] + a['w'] and a['y'] < b['y'] + b['h'] and b['y'] < a['y'] + a['h']

        def tidy(items):
            moved = False
            by = {i['id']: i for i in items}
            fr = [by[f] for f in ids if f in by]
            if len(fr) > 1 and (force or any(hit(a, b) for n, a in enumerate(fr) for b in fr[n + 1:])):
                lead, x = fr[0], fr[0]['x'] + fr[0]['w'] + gap
                for f in fr[1:]:
                    dx, dy = x - f['x'], lead['y'] - f['y']
                    if abs(dx) > .5 or abs(dy) > .5:
                        for i in items:
                            if i['id'] == f['id'] or (i.get('agent') == f.get('agent') and i.get('type') not in ('area', 'arrow')):
                                i['x'] += dx
                                i['y'] += dy
                        moved = True
                    x += f['w'] + gap
            moved = self.job_wrap(root, items, mine) or moved
            wrap = next((i for i in items if i['id'] == 'j' + root['id']), None)
            inside = lambda o, f: o['x'] >= f['x'] - 1 and o['y'] >= f['y'] - 1 and o['x'] + o['w'] <= f['x'] + f['w'] + 1 and o['y'] + o['h'] <= f['y'] + f['h'] + 1
            block = [i for i in items if 'x' in i and (i.get('agent') in mine or (wrap and not i.get('agent') and i is not wrap and inside(i, wrap)))]   # its frame carries what is in it, as a drag does
            frames = [i for i in block if i.get('type') == 'area' and i.get('agent') in mine]
            if not frames:
                return moved
            # what was there first: each older team as ONE box (its frames and chart) with a clear gap around it, so jobs stacked
            # one under another read as separate (12 units apart they read as one tangle, 2026-11-13); a card as itself, no gap
            # (a job sits 80 beside the card it is about). The user's own frames never count: a team may sit inside one.
            m = 2 * gap if root.get('wrap') else gap / 2   # wrapped jobs: 800 apart, room in the gap for the next job's title
            teams = {}
            for i in items:
                r = self.runs.get(i.get('agent'))
                if r and i.get('agent') not in mine and i.get('type') == 'area' and 'x' in i:
                    j = self.root(r)
                    if j['created'] < root['created'] and j['board'] == root['board']:
                        b = teams.setdefault(j['id'], [i['x'], i['y'], i['x'] + i['w'], i['y'] + i['h']])
                        b[:] = [min(b[0], i['x']), min(b[1], i['y']), max(b[2], i['x'] + i['w']), max(b[3], i['y'] + i['h'])]
            older = [{'x': b[0] - m, 'y': b[1] - m, 'w': b[2] - b[0] + 2 * m, 'h': b[3] - b[1] + 2 * m} for b in teams.values()]
            carried = {id(i) for i in block}
            older += [i for i in items if not i.get('agent') and i.get('type') not in ('arrow', 'area') and 'x' in i
                      and (i.get('added') or 0) < root['created'] and id(i) not in carried]   # not what its own frame holds
            for _ in range(30):
                x0, y0 = min(f['x'] for f in frames), min(f['y'] for f in frames)
                me = {'x': x0, 'y': y0, 'w': max(f['x'] + f['w'] for f in frames) - x0, 'h': max(f['y'] + f['h'] for f in frames) - y0}
                hits = [o for o in older if (any(hit(f, o) for f in frames) if 'id' in o else hit(me, o))]   # a card: under a frame; a team: near the job
                if not hits:
                    break
                # the shorter way out: right of what it hit, or below it (jobs stacked one under another stay a stack)
                dx = max(o['x'] + o['w'] for o in hits) + m - x0
                dy = max(o['y'] + o['h'] for o in hits) + m - y0
                for i in block:
                    if dy < dx:
                        i['y'] += dy
                    else:
                        i['x'] += dx
                moved = True
            return moved
        if not tidy(json.loads(json.dumps(self.board_items(root['board'])))):   # a dry run on a copy: nothing to move, nothing written
            return False
        return self.store.edit_board(root['board'], lambda board: tidy(board.get('items') or []))

    def job_wrap(self, root, items, mine):
        """The job's own frame ('j' + root id): around its chart, every part's frame, their cards and lines, and the cards
        it is about when they sit beside it, so its title moves the whole job (2026-11-13: many teams on one board).
        Made for jobs with 'wrap' (new jobs, or a board's jobs wrapped on request); fitted to what it holds after each
        change. Marked chart 'job': the page never lays it out as a part's frame. True when it changed."""
        wid = 'j' + root['id']
        wrap = next((i for i in items if i['id'] == wid), None)
        if not wrap and not root.get('wrap'):
            return False
        parts = [i for i in items if i.get('agent') in mine and i['id'] != wid and 'x' in i and i.get('w') is not None]
        if not parts:
            return False
        u = float((root.get('lay') or {}).get('u') or 1)
        box = [min(i['x'] for i in parts), min(i['y'] for i in parts), max(i['x'] + i['w'] for i in parts), max(i['y'] + i['h'] for i in parts)]
        near, sel = 600 * u, set(root.get('selected') or [])
        held = [i for i in items if not i.get('agent') and 'x' in i and i.get('type') != 'arrow' and (
                (i['id'] in sel and i['x'] < box[2] + near and i['x'] + i['w'] > box[0] - near and i['y'] < box[3] + near and i['y'] + i['h'] > box[1] - near)
                or (wrap and i.get('type') != 'area' and i['x'] >= wrap['x'] - 1 and i['y'] >= wrap['y'] - 1
                    and i['x'] + i['w'] <= wrap['x'] + wrap['w'] + 1 and i['y'] + i['h'] <= wrap['y'] + wrap['h'] + 1))]
        for i in held:
            box = [min(box[0], i['x']), min(box[1], i['y']), max(box[2], i['x'] + i['w']), max(box[3], i['y'] + i['h'])]
        # titles are 12 px on screen at any zoom: 80 units was 3 px at an overview zoom (3%) and the chart's title sat on the
        # job's. 500 above holds a title from about 3% up; 150 around holds the lines that loop under the frames
        lead = next((i for i in items if i['id'] == root.get('frame')), None)
        L = root.get('lay') or {}
        built = (L.get('inner') or 0) + 2 * (L.get('pad') or 0)   # the Lead's frame width as the team drew it
        k = min(4.0, max(0.05, lead['w'] / built)) if lead and built else 1.0   # a job scaled down by hand: margins scale with it
        pad, top = 150 * u * k, 500 * u * k
        g = {'x': box[0] - pad, 'y': box[1] - top, 'w': box[2] - box[0] + 2 * pad, 'h': box[3] - box[1] + top + pad}
        recolour = bool(wrap and wrap.get('color') == '#f8fafc')   # the first job frames' grey: sand is the look (2026-11-13); a picked colour stays
        if recolour:
            wrap['color'] = SAND
        if wrap and all(abs(wrap[c] - g[c]) < 1 for c in g) and wrap.get('pad') == [pad, top]:
            return recolour
        if not wrap:
            title = ' '.join(root['task'].split(' (This is about')[0].split())
            wrap = {'id': wid, 'type': 'area', 'agent': root['id'], 'chart': 'job', 'color': SAND,
                    'text': title[:120] + ('…' if len(title) > 120 else ''),
                    'why': {'by': 'Lead', 'task': root['task'][:400], 'asked': '', 'when': now_ms(), 'run': root['id']}}
            items.insert(0, wrap)
        wrap.update(g, z=min([i.get('z', 1) for i in parts] + [1]) - 1, pad=[pad, top])   # pad: the page grows it by the same margins
        return True

    def wrap_board(self, bid):
        """Every job on a board in its own frame, oldest first, each clear of the ones before it. The number wrapped."""
        n = 0
        for r in sorted([x for x in list(self.runs.values()) if not x.get('parent') and x['board'] == bid and x.get('frame')],
                        key=lambda x: x['created']):
            if not r.get('wrap'):
                r['wrap'] = True
                self.save(r)
            self.spread(r)
            n += 1
        return n

    def tidy_all(self):
        """Every job on every board, once at start (in the background): its row, clear of older work, its chart covering
        its cards. A clean job is only read."""
        for r in sorted([x for x in list(self.runs.values()) if not x.get('parent')], key=lambda x: x['created']):   # oldest first: each job clears the ones before it
            try:
                if self.spread(r):
                    print('tidied job %s on %s' % (r['id'], r['board']), flush=True)
                if r.get('chart') and self.chart_fit(r):
                    print('fitted chart of job %s on %s' % (r['id'], r['board']), flush=True)
            except Exception as e:
                print('tidy %s: %s' % (r['id'], e), flush=True)

    def item_text(self, it):
        """An item's whole content as text, for a copy: a text file's bytes as they are, anything else by its words."""
        label = ' '.join(str(it.get('label') or it.get('type') or 'item').split())
        src = str(it.get('src') or '').rsplit('/', 1)[-1]
        if it.get('type') == 'file' and src.rsplit('.', 1)[-1].lower() in ('md', 'txt', 'markdown', 'csv', 'json', 'html', 'htm'):
            try:
                with open(os.path.join(os.path.dirname(self.dir), 'files', src), encoding='utf-8', errors='replace') as f:
                    return f.read().strip(), label
            except OSError:
                pass
        kind, words = talk.item_words(it, self.reader)
        if it.get('type') == 'file' and words.startswith(label):
            words = words[len(label):].strip()
        return words.strip(), label

    def add_file(self, r, name, text):
        """A Markdown file made by an agent: kept content-addressed with the dropped files, its words kept for search, and a
        file card for it in the run's frame (the same card a dropped .md gets)."""
        name = re.sub(r'[\\/:*?"<>|]+', '-', ' '.join(name.split())).strip(' .-')[:60] or 'file'
        if not name.lower().endswith('.md'):
            name = re.sub(r'\.[a-z0-9]{1,5}$', '', name, flags=re.I) + '.md'
        data = text.encode()
        fn = hashlib.sha256(data).hexdigest() + '.md'
        files = os.path.join(os.path.dirname(self.dir), 'files')
        os.makedirs(files, exist_ok=True)
        path = os.path.join(files, fn)
        if not os.path.exists(path):
            with open(path + '.tmp', 'wb') as f:
                f.write(data)
            os.replace(path + '.tmp', path)
        try:
            tp = self.reader.path(fn)
            if not os.path.exists(tp):
                with open(tp + '.tmp', 'w') as f:
                    f.write(text)
                os.replace(tp + '.tmp', tp)
        except (OSError, AttributeError):
            pass
        u = r['lay']['u']
        return self.place(r, {'type': 'file', 'label': name, 'src': 'api/files/' + fn, 'mime': 'text/markdown', 'size': len(data),
                              'preview': re.sub(r'[ \t]+', ' ', text[:600]).strip()}, 260 * u, 340 * u)

    def place_note(self, r, text, source=None, card=False):
        """A sticky note holds one short thought: a fixed square that cuts off what does not fit. Anything over 120
        characters (links not counted) is put on a card instead, whatever the agent asked for."""
        text = str(text).strip()
        if not text:
            return
        if len(text) > 1200:   # longer than a card holds: a text block keeps every word (a verbatim quote was cut at 1200 and failed its check four times, 2026-11-05)
            self.place_text(r, text, 'body')
            return 'text'
        u = r['lay']['u']
        if not card and len(re.sub(r'\s*https?://\S+', '', text).strip()) <= STICKY_MAX:
            self.place(r, {'type': 'note', 'text': text, 'fs': 16 * u, **({'_src': source} if source else {})}, 220 * u, 220 * u)
            return
        self.place_card(r, text if card else card_text(text), source)

    def place_card(self, r, text, source=None):
        """A card: a bold title (the first line), then its lines (a "Label: value" line is a row, a link is the footer)."""
        u, fs = r['lay']['u'], 13 * r['lay']['u']
        width = 300 * u
        per_line = max(10, width / (fs * 0.55))
        def rows(x):   # how many lines a line of the card takes: a row's value wraps in its own, narrower column
            m = re.match(r'^[A-Za-z][^:]{0,32}:\s+(.+)$', x)
            if re.match(r'^https?://\S+$', x):
                return 1
            return max(1, -(-len(m.group(1)) // int(per_line * 0.58))) + 0.4 if m else max(1, -(-len(x) // int(per_line)))
        lines = sum(rows(x.strip()) for x in text.split('\n') if x.strip())
        item = {'type': 'note', 'text': text, 'card': 1, 'grow': 'h', 'fs': fs, **({'_src': source} if source else {})}
        return self.place(r, item, width, (lines + 1.5) * fs * 1.5 + fs * 2.4)

    def use_code(self, r, name, a):
        """An engineer's tools, all inside its own worktree of the code base (agents/code.py). The first one an engineer
        uses on a code base also hands it the notes the engineer before it left and the smoke test's result."""
        out = self.use_code_tool(r, name, a)
        start = r.pop('_code_start', '')
        return '%s\n\nYOUR CALL RETURNED:\n%s' % (start, out) if start and isinstance(out, str) else out

    def term_card(self, r, team, repo):
        if not r.get('term_item'):   # the terminal goes on the board, in the task frame, like the browser view
            u = r['lay']['u']
            it = self.place(r, {'type': 'term', 'run': r['id'], 'label': '%s · %s' % (code.branch(team), repo)}, 560 * u, 340 * u, wide=True)
            r['term_item'] = it and it['id']

    def code_start(self, r, repo, work, team, data):
        """Before an engineer's first step on a code base: what the engineer before it wrote down (TEAM-PROGRESS.md on the
        branch, and the last commits), and a smoke test so it never builds on something already broken. Done by the app,
        not asked of the model: an agent that picks work up cold needs notes and a known-good start (2026-11-11)."""
        try:
            with open(os.path.join(work, PROGRESS)) as f:
                notes = f.read(4000).strip()
        except OSError:
            notes = ''
        try:
            log = code.git(work, 'log', '--format=%h %ad %s', '--date=short', '-8').strip()
        except (ValueError, OSError, subprocess.TimeoutExpired):
            log = ''
        try:
            ok, out = shell.smoke(data, r['id'], work)
        except (ValueError, OSError, subprocess.TimeoutExpired) as e:
            ok, out = None, 'The smoke test could not be run (%s).' % e
        r['smoke'] = ok
        self.step(r, 'Smoke test of %s before the first change: %s' % (repo, 'passed' if ok else 'BROKEN' if ok is False else 'could not be run'))
        self.term_card(r, team, repo)
        return ('BEFORE YOU START on %s (branch %s):\n\nNotes the engineer before you left in %s:\n%s\n\nLast commits:\n%s\n\n'
                'Smoke test, run for you just now: %s\n%s%s' % (
                    repo, code.branch(team), PROGRESS, notes or '(none: you are the first on this branch)', log or '(none)',
                    'PASSED' if ok else 'BROKEN before you changed anything' if ok is False else 'not run', out[-1500:],
                    '' if ok is not False else '\nFix what is broken first, or if it is not yours to fix say so in a note before going on.'))

    def use_code_tool(self, r, name, a):
        data = os.path.dirname(self.dir)
        repo = str(a.get('repo') or '').strip()
        team = self.root(r)['id']   # one branch per team: the checker reads what the engineer wrote, a second round carries on
        try:
            work = code.workspace(data, repo, team, code.named_base(self.root(r).get('task')))   # from the branch or commit the job names, else main
            if str((r.get('member') or {}).get('id', '')).startswith('engineer') and repo not in r.setdefault('code_started', []):
                r['code_started'].append(repo)
                r['_code_start'] = self.code_start(r, repo, work, team, data)
            if name == 'code_files':
                self.step(r, 'Listed the files of %s%s' % (repo, ' in ' + str(a.get('folder')) if a.get('folder') else ''))
                return code.files(work, a.get('folder'))
            if name == 'code_search':
                self.step(r, 'Searched the %s code for "%s"' % (repo, str(a.get('words'))[:60]))
                return code.search(work, a.get('words'))
            if name == 'code_read':
                self.step(r, 'Read %s in %s' % (str(a.get('path'))[:80], repo))
                return code.read(work, a.get('path'), a.get('start'), a.get('count'))
            if name in ('code_edit', 'code_write'):
                said = (code.edit(work, a.get('path'), str(a.get('old') or ''), str(a.get('new') or '')) if name == 'code_edit'
                        else code.write(work, a.get('path'), str(a.get('content') or '')))
                r.setdefault('code_repos', [])
                if repo not in r['code_repos']:
                    r['code_repos'].append(repo)
                r['code_clean'] = False
                if os.path.normpath(str(a.get('path') or '')) == PROGRESS:
                    r['progress_written'] = True
                self.step(r, '%s %s in %s' % ('Changed' if name == 'code_edit' else 'Wrote', str(a.get('path'))[:80], repo))
                return said
            if name == 'code_run':
                exit_code, out = shell.run(data, r['id'], work, a.get('command'))
                self.step(r, 'Ran in the terminal: %s (%s)' % (' '.join(str(a.get('command')).split())[:90], 'took too long' if exit_code is None else 'exit %d' % exit_code))
                self.term_card(r, team, repo)
                return out
            if name == 'code_check':
                ok, report = code.check(work)
                r['code_checked'] = ok
                self.step(r, 'Checked the changed %s files: %s' % (repo, 'clean' if ok else 'errors found'))
                return report
            if name == 'code_commit':
                if not r.get('progress_written'):   # the next engineer starts with no memory of this one: the notes are part of the work
                    return ('Not committed yet: first update %s at the root of the code base (code_write it whole, at most 60 lines; '
                            'replace what is stale, do not just add). Write it so a stranger could pick the work up cold: what works now '
                            'and how you checked it; what you tried that did not work; what is left, most important first; the next '
                            'step. Then code_commit again.' % PROGRESS)
                ok, report = code.check(work)
                if not ok:
                    return 'Not committed: the check found errors. Fix them first.\n' + report
                said = code.commit(work, a.get('message'))
                r['code_clean'] = True
                self.step(r, '%s (%s, branch %s)' % (said, repo, code.branch(team)))
                self.code_card(r, repo, work)
                return ('%s, on branch %s of the %s code base on this machine. It is not on GitHub and is not meant to be: '
                        'The user reviews this branch here before anything goes live. Use exactly this branch name when you write '
                        'about it, and never ask the user to push it.' % (said, code.branch(team), repo))
        except (ValueError, OSError, subprocess.TimeoutExpired) as e:
            return 'That did not work: %s' % e
        return 'There is no tool called %s.' % name

    def code_card(self, r, repo, work):
        """The branch, on the board: what it changes (files, lines) and the whole diff as a page, kept up to date."""
        stat, diff = code.summary(work)
        team = self.root(r)['id']
        did = '%s-code-%s' % (team, repo)
        esc = lambda t: t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        rows = ''.join('<div class="%s">%s</div>' % ('a' if l.startswith('+') and not l.startswith('+++') else
                                                    'd' if l.startswith('-') and not l.startswith('---') else
                                                    'h' if l.startswith(('diff ', '@@')) else '', esc(l) or '&nbsp;')
                       for l in diff.split('\n')[:4000])
        page = ('<!doctype html><meta charset="utf-8"><title>%s</title><style>body{margin:0;font:13px/1.45 ui-monospace,Menlo,monospace;'
                'background:#fff;color:#1f2937}header{padding:14px 18px;font:600 15px system-ui;border-bottom:1px solid #e5e7eb}'
                'header p{font:400 13px system-ui;color:#6b7280;margin:4px 0 0}pre{margin:0;padding:12px 18px;white-space:pre}'
                'div{white-space:pre}.a{background:#e7f6ec}.d{background:#fdecec}.h{color:#1d4ed8;font-weight:600;margin-top:6px}</style>'
                '<header>Code changes in %s, branch %s<p>Not live. Nothing here ships until the user reviews this branch.</p></header>'
                '<pre>%s</pre><div style="padding:0 18px 18px">%s</div>') % (esc(repo), esc(repo), esc(code.branch(team)), esc(stat), rows)
        with open(os.path.join(self.demos, did + '.html.tmp'), 'w') as f:
            f.write(page)
        os.replace(os.path.join(self.demos, did + '.html.tmp'), os.path.join(self.demos, did + '.html'))
        demos = r.setdefault('demos', {})
        first = did not in demos
        demos[did] = demos.get(did, 0) + 1
        r['demo_v'] = r.get('demo_v', 0) + 1
        if first:
            self.place(r, {'type': 'web', 'url': 'api/demos/%s.html' % did, 'demo': True, 'run': r['id'],
                           'label': 'Code changes: %s, branch %s' % (repo, code.branch(team))}, r['lay']['inner'], 520 * r['lay']['u'], wide=True)

    def rules(self):
        """The user's standing rules: every note on the rules board (its id in <data>/rules_board), one rule each."""
        try:
            with open(os.path.join(os.path.dirname(self.dir), 'rules_board')) as f:
                board = self.store.read_board(f.read().strip())
        except (OSError, ValueError):
            return ''
        out = [' '.join(str(i.get('text') or '').split()) for i in sorted(board.get('items') or [], key=lambda i: (i.get('y', 0), i.get('x', 0)))
               if i.get('type') == 'note' and not i.get('agent') and not i.get('builtin') and not i.get('mined') and str(i.get('text') or '').strip()]
        text = '\n'.join('- ' + x[:400] for x in out[:40])
        try:
            from agents import propose
            tried = propose.trying(os.path.dirname(self.dir))   # rules the failure miner is trying out (agents/propose.py)
        except Exception:
            tried = []
        if tried:
            text += '\nRules being tried (from the failure patterns; follow them like the rest):\n' + '\n'.join(
                '- %s%s' % (x['rule'], ' (when: %s)' % x['applies'] if x.get('applies') else '') for x in tried[:6])
        return text

    def load_notes(self):
        """Every note earlier teams left (team_notes.jsonl); older notes without an id get one, once."""
        path = os.path.join(os.path.dirname(self.dir), 'team_notes.jsonl')
        try:
            with open(path) as f:
                notes = [json.loads(l) for l in f if l.strip()]
        except (OSError, ValueError):
            return path, []
        if any('id' not in x for x in notes):
            for x in notes:
                x.setdefault('id', 'n' + secrets.token_hex(4))
            self.save_notes(path, notes)
        return path, notes

    def save_notes(self, path, notes):
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            f.write(''.join(json.dumps(x) + '\n' for x in notes))
        os.replace(tmp, path)

    def notes_for(self, bid, task):
        """Notes earlier teams left, (text, ids): the last live ones on this board, and the live ones whose job and
        'applies when' share the most words with this task. Retired notes are never given out."""
        path, notes = self.load_notes()
        live = [x for x in notes if not x.get('retired')]
        want = set(talk.tokens(task))
        fit = lambda x: len(want & set(talk.tokens(x.get('task', '') + ' ' + x.get('applies', ''))))
        here = [x for x in live if x.get('board') == bid][-3:]
        near = sorted((x for x in live if x not in here), key=lambda x: (-fit(x), -x.get('hits', 0)))[:2]
        near = [x for x in near if fit(x) >= 3]
        picked = here + near
        return ('\n'.join('- Job: %s%s\n  Worked: %s\n  Failed: %s' % (x['task'][:160], ' (applies when: %s)' % x['applies'] if x.get('applies') else '',
                                                                  x.get('worked') or '-', x.get('failed') or '-') for x in picked),
                [x['id'] for x in picked])

    def note_outcomes(self, r):
        """The notes this job was given are reinforced when the job went through, counted against when it fell short, and
        retired after repeated misses or a long silence: 3 misses and more misses than hits, or 60 days with no hit
        (after AgentPilot's policy memory, 2026-11-07)."""
        used = set(r.get('notes_used') or [])
        if not used:
            return
        path, notes = self.load_notes()
        passed = r.get('status') == 'done' and not r.get('gaps')
        now = now_ms()
        for x in notes:
            if x['id'] in used:
                x['hits' if passed else 'misses'] = x.get('hits' if passed else 'misses', 0) + 1
            if x['id'] in used and passed and x.get('hits', 0) >= 3 and x.get('misses', 0) * 3 <= x['hits'] and not x.get('skill') and x.get('worked'):
                # a note that kept proving itself is a procedure: it becomes a skill of the Lead on every board (AgentPilot's
                # provisional skill made trusted by use; the note itself stays, but no longer needs to fit the task by words)
                x['skill'] = skills.add('lead', '%s%s' % (('When %s: ' % x['applies']) if x.get('applies') else '', x['worked']), note=x['id'])
            if not x.get('retired') and ((x.get('misses', 0) >= 3 and x.get('misses', 0) > x.get('hits', 0))
                                         or (not x.get('hits') and x.get('misses') and now - x.get('t', now) > 60 * 86400000)):
                x['retired'] = now
        self.save_notes(path, notes)

    def team_notes(self, r):
        """A finished team leaves a short note for the next one: what worked and what failed, from its own log."""
        log = ['JOB: ' + r['task'], 'LEAD STEPS: ' + '; '.join(x['text'] for x in r['steps'][-40:])]
        for c in r.get('children', []):
            x = self.runs.get(c)
            if x:
                v = x.get('check') or {}
                log.append('PART (%s, %s%s): %s | check: %s | last step: %s' % (
                    (x.get('member') or {}).get('name', ''), x['status'], ', second try' if x.get('retry_of') else '', x['task'][:200],
                    ('passed' if v.get('pass') else 'not done: ' + (v.get('missing') or '')) if v else '-',
                    x['steps'][-1]['text'] if x['steps'] else ''))
        log.append('FINAL: %s%s' % (r['status'], ', missing: ' + r['gaps'] if r.get('gaps') else ''))
        try:
            note = talk.team_note('\n'.join(log))
        except (OSError, ValueError, KeyError):
            return
        self.note_outcomes(r)   # first: how the notes this job was given fared
        passed = r.get('status') == 'done' and not r.get('gaps')
        try:
            skillfiles.outcomes(os.path.dirname(self.dir), r.get('skills_used'), passed)
            if passed and (len(r.get('children') or []) >= 2 or len(r['steps']) >= 12):   # a job with some shape to it: maybe a procedure worth keeping
                sk = skillfiles.consider(os.path.dirname(self.dir), r, '\n'.join(log))
                if sk:
                    self.card(r, 'Skill written for next time: %s\n%s' % (sk['name'], sk['description']), '#eef2ff')
                    self.step(r, 'Wrote a skill for next time: ' + sk['name'])
        except (OSError, ValueError, KeyError) as e:
            self.step(r, 'Could not weigh a skill from this job (%s)' % type(e).__name__)
        if not (note['worked'] or note['failed']):
            return
        with open(os.path.join(os.path.dirname(self.dir), 'team_notes.jsonl'), 'a') as f:
            f.write(json.dumps({'id': 'n' + secrets.token_hex(4), 'board': r['board'], 'run': r['id'], 'task': r['task'][:400], 't': now_ms(),
                                'hits': 0, 'misses': 0, **note}) + '\n')
        self.card(r, 'Team notes, for the next team\nWorked: %s\nFailed: %s' % (note['worked'] or '-', note['failed'] or '-'), '#f1f5f9')
        self.step(r, 'Left notes for the next team')

    def design_crew(self, r):
        """Put together the team THIS job needs (a vendor comparison and an integration plan need different people),
        show it as a card in the Lead's frame, and give the Lead that team. Falls back to the board's team."""
        r['crew_done'] = True
        try:
            have = code.repos(os.path.dirname(self.dir))
            notes, r['notes_used'] = self.notes_for(r['board'], r['task'])
            r['team_notes_read'] = notes
            r['skills_read'], r['skills_used'] = skillfiles.pick(os.path.dirname(self.dir), r['task'])   # skills earlier teams wrote, when the job fits (2026-11-08)
            want = talk.team_design(r['task'], r.get('intent'), ((
                '- engineer: writes real code in one of these code bases: %s. It works on its own branch, which the user reviews '
                'before anything goes live: it can read, search, edit, check syntax and commit on '
                'branch team/<job id>, the only branch name, and run commands in a shut-in terminal (code_run: python3, git, checks and '
                'tests; no network, no node or npm, cannot start the app); it cannot deploy. Say in '
                '"does" which code base.\n' % ', '.join(have)) if have else '') + custom_kinds_text(), notes,
                apps=integrations.can_act(r['board']))   # a post in a connected app is the app's draft call, not text on the board (2026-11-06)
        except (OSError, ValueError, KeyError):
            want = []
        if want == 'alone':   # the designer: one call does the whole job, no team (a copy of a card is add_file with from_item)
            want, r['critic'] = [], {'line': 'the designer found one call does this job, so the Lead does it alone', 'before': 0, 'after': 0, 'alone': True, 'cut': [], 'merged': [], 'method': []}
        elif not want:
            self.step(r, "Could not put a team together for this job, so the board's team is working on it")
            return
        else:
            r['critic'] = None
        if want and models.flag('critic'):   # the plan critic: the smallest plan that still delivers the intent (switch in Settings, 2026-11-05)
            try:
                v = talk.team_critic(r['task'], r.get('intent'), want, notes, lead_tools=[t['function']['name'] for t in LEAD_TOOLS],
                                     tools=[(t['function']['name'], t['function']['description'].split('. ')[0]) for t in TOOLS])
            except (OSError, ValueError, KeyError):
                v = None
            if v:
                r['critic'] = v
                want = v['members']
                self.step(r, 'Plan critic: ' + v['line'])
            else:
                r['critic'] = {'skipped': True, 'line': 'its answer could not be used, so the plan runs as designed'}
                self.step(r, 'Plan critic: its answer could not be used, so the plan runs as designed')
        if want and any(m.get('kind') in ('engineer', 'demo') for m in want):   # fresh eyes on the plan before anyone starts: what it cannot
            # do, and a result settled before it runs (2026-11-14). Only teams that build or run things: a research job that reads
            # about a tool was flagged for not installing it
            try:
                rv = talk.plan_review(r['task'], r.get('intent'), want)
            except (OSError, ValueError, KeyError):
                rv = None
            if rv:
                r['review'] = rv
                self.step(r, 'Plan review: ' + '; '.join(x for x in (rv['cannot'] and 'cannot ' + rv['cannot'], rv['fixed_outcome'] and 'settled before it runs: ' + rv['fixed_outcome']) if x) + '. Fix: ' + rv['fix'])
        crew, used = [dict(STARTERS['lead'], id='lead')], set()
        for m in want:
            base = KIND_BASE[m['kind']]
            mid = base if base not in used else next('%s-%d' % (base, k) for k in range(2, 9) if '%s-%d' % (base, k) not in used)
            used.add(mid)
            crew.append({'id': mid, 'name': m['name'], 'kind': m['kind'], 'color': STARTERS[base]['color'], 'why': m['does'],
                         'done_when': m['done_when'], 'role': '%s Done when: %s\n\nHow this kind of work is done:\n%s' % (
                             m['does'], m['done_when'] or 'your part is answered in your frame', STARTERS[base]['role'])})
        names = {m['name'].lower() for m in crew}
        if len(names) < len(crew):   # two with one name: assign finds teammates by name
            for k, m in enumerate(crew[1:], 2):
                m['name'] = '%s %d' % (m['name'], k - 1) if sum(1 for x in crew if x['name'].lower() == m['name'].lower()) > 1 else m['name']
        r['crew'] = crew
        lines = '\n'.join('- %s: %s Done when: %s' % (m['name'], m['why'], m['done_when'] or '-') for m in crew[1:])
        r['messages'][0]['content'] = (SYSTEM + "\n\nYOUR ROLE: you are the Lead on this job's team. " + STARTERS['lead']['role'] + learned(os.path.dirname(self.dir), 'lead')
                                       + LEAD % (lines, MAX_ROUNDS)
                                       + ('\nPLAN REVIEW (fresh eyes read this plan before you started; follow its fix, and tell the user plainly what cannot be done): %s%s Fix: %s'
                                          % ('Cannot: %s. ' % r['review']['cannot'] if r['review'].get('cannot') else '',
                                             'Settled before it runs: %s.' % r['review']['fixed_outcome'] if r['review'].get('fixed_outcome') else '',
                                             r['review']['fix']) if r.get('review') else '')
                                       + ('\nThe plan critic found you can finish this job alone: there are no teammates. Do it yourself with your '
                                          'tools (a copy of a card or file = one add_file call with from_item; otherwise read what is needed and write the result in your frame with add_text or add_file) and finish. Do not use assign. Read the named items once, then act: this job is usually one or two calls.'
                                          if not crew[1:] else '\nThis team was put together for this job; each line says what that teammate is for.')
                                       + ('\nNOTES FROM EARLIER TEAMS ON SIMILAR JOBS (learn from them):\n' + r['team_notes_read']
                                          if r.get('team_notes_read') else '')
                                       + ('\n\nSKILLS EARLIER TEAMS WROTE FOR JOBS LIKE THIS (follow the steps when they fit; say in your summary if you used one):\n' + r['skills_read']
                                          if r.get('skills_read') else ''))
        self.crew_chart(r, crew)
        if r.get('skills_used'):
            self.step(r, 'Given %d skill%s earlier teams wrote: %s' % (len(r['skills_used']), '' if len(r['skills_used']) == 1 else 's', ', '.join(r['skills_used'])))
        self.step(r, ('Put a team together for this job: ' + ', '.join(m['name'] for m in crew[1:])) if crew[1:] else 'No team: the Lead does this job alone')

    def crew_chart(self, r, crew):
        """The job's team as an org chart in its own frame ABOVE the team's work, drawn as the org cards the user knows (the
        old Agents tab): the Lead on top, thin lines down to each teammate, each card with why it is there, its
        done-when and how its part is going; crew_paint keeps them up to date. It sits in clear space above the Lead's
        frame; nothing moves."""
        u = r['lay']['u']
        members, pad, gap = crew[1:], r['lay']['pad'], 26 * u
        bw = 240 * u
        lines = lambda t, per: max(1, -(-len(t) // per)) if t else 0
        card = lambda m: (52 + 16.2 * lines(' '.join(str(m.get('why') or '').split()), 34)
                          + (8 + 14.5 * lines(' '.join(str(m.get('done_when') or '').split()), 40) if m.get('done_when') else 0) + 36) * u
        bh = max(card(m) for m in members) if members else 0
        lh = card(crew[0])
        roww = len(members) * bw + max(0, len(members) - 1) * gap

        def put(board):
            items = board.get('items') or []
            frame = next((i for i in items if i['id'] == r['frame']), None)
            if not frame:
                r['frame_gone'] = True
                return None
            w = max(frame['w'], roww + 2 * pad)
            h = 2 * pad + 30 * u + lh + 64 * u + bh
            x0, y0, z = frame['x'], frame['y'], max([i.get('z', 1) for i in items] + [1]) + 1
            # clear space above the team's work, nothing moves. Moving the frame down to make room instead left an open
            # page, which keeps its own positions, saving the frame back where it was: under the chart (2026-11-07)
            team = {r['id']} | set(r.get('children') or [])
            y0 = min([i['y'] for i in items if i.get('agent') in team and i.get('type') != 'arrow' and not i.get('chart')] + [frame['y']]) - h - 60 * u
            for _ in range(50):
                hit = [i for i in items if i.get('type') != 'arrow' and i.get('agent') not in team and i['x'] < x0 + w
                       and i['x'] + i['w'] > x0 and i['y'] < y0 + h and i['y'] + i['h'] > y0]
                if not hit:
                    break
                y0 = min(i['y'] for i in hit) - h - 60 * u
            why = {'by': 'Lead', 'task': r['task'][:400], 'asked': '', 'when': now_ms(), 'run': r['id']}
            box = lambda m, x, y, h_: {'id': 'a' + secrets.token_hex(5), 'type': 'note', 'agent': r['id'], 'chart': m['id'], 'why': why,
                                       'text': self.crew_text(r, m), 'org': self.crew_card(r, m), 'x': x, 'y': y, 'w': bw, 'h': h_, 'z': z + 1}
            new = [{'id': 'c' + r['id'], 'type': 'area', 'agent': r['id'], 'chart': 'frame', 'why': why, 'text': 'Team for this job',
                    'color': '#f8fafc', 'x': x0, 'y': y0, 'w': w, 'h': h, 'z': 1}]
            lead = box(crew[0], x0 + (w - bw) / 2, y0 + pad + 30 * u, lh)
            new.append(lead)
            left, top = x0 + (w - roww) / 2, lead['y'] + lh + 64 * u
            for k, m in enumerate(members):
                b = box(m, left + k * (bw + gap), top, bh)
                new += [b, dict(arrow(lead, b, '', '#b6bfcc', r['id']), shape='tree', head=False)]
            board['items'] = items + new
            return {b['chart']: b['id'] for b in new if b.get('chart') not in (None, 'frame')}
        ids = self.store.edit_board(r['board'], put)
        if ids:
            r['chart'] = ids
            r['chart_v'] = 2
            self.changed(r['board'])

    def backfill_charts(self):
        """Teams that ended before org charts existed get one, once, above their work; charts drawn as sticky notes
        (Nov 1, a few hours) are drawn again as org cards."""
        for r in [x for x in list(self.runs.values()) if x.get('crew') and not x.get('parent') and x.get('chart_v') != 2
                  and x['status'] in ENDED and x.get('lay')]:
            if r.get('chart'):
                old = set(r['chart'].values()) | {'c' + r['id']}

                def drop(board):
                    board['items'] = [i for i in board.get('items') or [] if i['id'] not in old
                                      and not (i.get('type') == 'arrow' and (i.get('from') in old or i.get('to') in old))]
                try:
                    self.store.edit_board(r['board'], drop)
                except (OSError, ValueError, KeyError):
                    continue
                r.pop('chart', None)
            try:
                self.crew_chart(r, r['crew'])
            except (OSError, ValueError, KeyError):
                continue
            self.save(r)

    def backfill_cards(self):
        """Once: what teams wrote on sticky notes before cards existed becomes cards. A status box (intent, done-when, a
        check) and any note over STICKY_MAX characters; short notes, questions and org charts stay as they are."""
        runs = set(self.runs)

        def convert(i):
            if (i.get('type') != 'note' or i.get('agent') not in runs or i.get('card') or i.get('chart') or i.get('ask')
                    or i.get('color') == 'none'):
                return False
            if i.get('grow') == 'h':
                i['card'] = 1
            elif len(re.sub(r'\s*https?://\S+', '', i.get('text') or '').strip()) > STICKY_MAX:
                i['card'], i['text'] = 1, card_text(i.get('text') or '')
            return bool(i.get('card'))

        for bid in {x['board'] for x in self.runs.values() if x.get('board')}:
            try:
                if not any(convert(dict(i)) for i in self.store.read_board(bid).get('items') or []):
                    continue
                self.store.edit_board(bid, lambda board: [convert(i) for i in board.get('items') or []])
            except (OSError, ValueError, KeyError):
                continue

    def crew_now(self, r, m):
        """How one teammate's part of this job is going: (word, style, detail, frame of its latest part)."""
        if m['id'] == 'lead':
            word, cls, _, frame = {'team': ('Managing', 'working', '', r['frame']), 'working': ('Working', 'working', '', r['frame']),
                                   'queued': ('Starting', '', '', r['frame'])}.get(
                r['status'], (('Done, with gaps', 'waiting', '', r['frame']) if r['status'] == 'partial' else
                              ('Done', 'done', '', r['frame']) if r['status'] == 'done' else ('Stopped', '', '', r['frame'])))
            detail = []   # the plan critic's verdict on this team, and the job's score once it ended (2026-11-05)
            if (r.get('critic') or {}).get('line'):
                detail.append('Plan critic: ' + r['critic']['line'])
            if r['status'] in ENDED and (r.get('score') or {}).get('line'):
                detail.append('Score: ' + r['score']['line'])
            return (word, cls, ' '.join(detail), frame)
        parts = [self.runs[c] for c in r.get('children', []) if c in self.runs and (self.runs[c].get('member') or {}).get('id') == m['id']]
        if not parts:
            return ('Not started yet', '', '', None)
        last, n = parts[-1], len(parts)
        ok = sum(1 for x in parts if (x.get('check') or {}).get('pass'))
        tally = '%d part%s, %d passed' % (n, '' if n == 1 else 's', ok)
        if last['status'] in ACTIVE:
            return ('Second try' if last.get('retry_of') else 'Working', 'waiting' if last.get('retry_of') else 'working',
                    cut(' '.join((last['steps'][-1]['text'] if last.get('steps') else last['task']).split()), 90), last['frame'])
        v = last.get('check')
        if v and v.get('pass'):
            return ('Passed', 'done', tally, last['frame'])
        if v:
            return ('Not done', 'waiting', '%s. Missing: %s' % (tally, cut(v.get('missing') or v.get('why') or '', 110)), last['frame'])
        return (('Done', 'done', tally, last['frame']) if last['status'] == 'done' else ('Stopped', '', tally, last['frame']))

    def crew_card(self, r, m):
        """What an org card on the board shows (the page draws it like the old Agents tab's cards)."""
        word, cls, detail, frame = self.crew_now(r, m)
        return {'name': m['name'], 'color': m.get('color') or '#475569', 'why': ' '.join(str(m.get('why') or '').split()),
                'done_when': ' '.join(str(m.get('done_when') or '').split()), 'label': word, 'cls': cls, 'detail': detail, 'frame': frame}

    def crew_text(self, r, m):   # the same card as words: search, the voice and the checkers read this
        word, _, detail, _ = self.crew_now(r, m)
        return '%s\n%s\n%s%s' % (m['name'], cut(' '.join(str(m.get('why') or '').split()), 200),
                                 'Done when: %s\n' % cut(' '.join(m['done_when'].split()), 180) if m.get('done_when') else '',
                                 'Now: ' + word + ('. ' + detail if detail else ''))

    def crew_paint(self, r):
        """Bring the org chart's cards up to date (a part started, passed, went back, ended)."""
        chart, crew = r.get('chart'), {m['id']: m for m in r.get('crew') or []}
        if not chart:
            return
        want = {iid: (self.crew_text(r, crew[mid]), self.crew_card(r, crew[mid])) for mid, iid in chart.items() if mid in crew}

        def paint(board):
            hit = False
            for i in board.get('items') or []:
                if i['id'] in want and (i.get('text'), i.get('org')) != want[i['id']]:
                    i['text'], i['org'] = want[i['id']]
                    hit = True
            return hit
        try:
            if self.store.edit_board(r['board'], paint):
                self.changed(r['board'])
        except (OSError, ValueError, KeyError):
            pass
        self.chart_fit(r)

    def chart_fit(self, r):
        """The chart's frame covers all its cards (it only grows). One chart's frame ended up 2,795 wide under cards
        8,543 wide, the cards hanging off it (2026-11-13)."""
        fid = 'c' + r['id']
        items = self.board_items(r['board'])
        f = next((i for i in items if i['id'] == fid), None)
        cards = [i for i in items if i.get('agent') == r['id'] and i.get('chart') and i['id'] != fid and i.get('type') != 'arrow' and i.get('chart') != 'job']
        if not f or not cards:
            return False
        pad = (r.get('lay') or {}).get('pad') or 28
        box = lambda f: (min(f['x'], min(i['x'] for i in cards) - pad), min(f['y'], min(i['y'] for i in cards) - pad),
                         max(f['x'] + f['w'], max(i['x'] + i['w'] for i in cards) + pad), max(f['y'] + f['h'], max(i['y'] + i['h'] for i in cards) + pad))
        if all(f['x'] - 1 <= i['x'] and i['x'] + i['w'] <= f['x'] + f['w'] + 1 and f['y'] - 1 <= i['y'] and i['y'] + i['h'] <= f['y'] + f['h'] + 1
               for i in cards):   # every card on it already: nothing to do (close to the edge is fine)
            return False

        def grow(board):
            g = next((i for i in board.get('items') or [] if i['id'] == fid), None)
            if not g:
                return False
            x0, y0, x1, y1 = box(g)
            g.update(x=x0, y=y0, w=x1 - x0, h=y1 - y0)
            return True
        if self.store.edit_board(r['board'], grow):
            self.changed(r['board'])
            return True
        return False

    def intent_card(self, r):
        """First card in a team's frame: what the user wants, what the team hands back, what no team can do. The Lead
        works to it, so a team does not set off to build what it cannot (Oct 31: "engineer our own integrations")."""
        it = r.get('intent')
        if not it:
            try:
                it = r['intent'] = talk.team_intent(r['task'], r.get('board'))
            except (OSError, ValueError, KeyError):
                it = None
        r['intent_card'] = True
        if not it or not it.get('deliver'):
            return
        lines = ['Intent']
        if it.get('want'):
            lines.append(it['want'])
        lines.append('The team will hand back: ' + it['deliver'])
        if it.get('cannot'):
            lines.append('Not something a team can do: %s.' % it['cannot'].rstrip('.'))
        if it.get('avoid'):
            lines.append('Not doing: ' + it['avoid'] + '.')   # non-goals: what an eager team would do that was not asked for (2026-11-07)
        lines.append('Sends, posts or spends: ' + ('yes. Each one waits for your yes.' if it.get('acts') else 'no.'))
        u, width = r['lay']['u'], r['lay']['inner']
        fs = 15 * u
        per_line = max(10, width / (fs * 0.55))
        text = '\n'.join(lines)
        h = sum(max(1, -(-len(p) // int(per_line))) for p in lines) * fs * 1.35 + fs * 2
        self.place(r, {'type': 'note', 'text': text, 'color': '#eef4ff', 'card': 1, 'grow': 'h', 'fs': fs}, width, h, wide=True)
        self.step(r, 'Wrote the intent: what the team will hand back' + (' and what it cannot do' if it.get('cannot') else ''))
        brief = 'THE INTENT, AGREED WITH THE USER (it is the first card in your frame): the team hands back %s.' % it['deliver']
        if 'connected app' in it['deliver'] or 'Substack' in it['deliver'] or 'email' in it['deliver'].lower():
            brief += (' A draft or post in a connected app means a teammate called that app\'s draft tool (substack_draft, gmail_draft) '
                      'and reported the draft\'s address: a post written on the board is not it. Pictures the person selected go into '
                      'the post (substack_draft pictures, in order), and the app chooses the cover picture itself: never plan "no cover".')
        if it.get('cannot'):
            brief += ' Do NOT attempt this, no team can: %s. Say in your answer that it has to be built into the app itself.' % it['cannot']
        if it.get('avoid'):
            brief += ' NOT DOING (not asked for; nobody on the team does it): %s.' % it['avoid']
        first = r['messages'][1]
        if isinstance(first['content'], str):
            first['content'] += '\n\n' + brief
        else:   # pictures attached: the words are the first part (adding a string to the list made 219 one-letter parts, the model said 400, 2026-11-10)
            first['content'][0]['text'] += '\n\n' + brief

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
            json.dump({k: v for k, v in r.items() if k != 'checking'}, f)   # 'checking' lives in memory only: saved, a restart left
        os.replace(path + '.tmp', path)                                         # the Lead sure someone was checking, and it never woke


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
    content = re.sub(r'<tool_call>.*?(</tool_call>|$)', '', content, flags=re.S).strip()
    # DeepSeek's own markup, written as text instead of a real call:
    # <｜DSML｜invoke name="x"><｜DSML｜parameter name="p" string="true">v</｜DSML｜parameter>...</｜DSML｜invoke>
    bar = '[|\uff5c]'
    for n, m in enumerate(re.finditer(r'<%sDSML%s\s*invoke name="([^"]+)"\s*>(.*?)(?:</%sDSML%s\s*invoke>|$)' % (bar, bar, bar, bar), content, re.S)):
        args = {}
        for p in re.finditer(r'<%sDSML%s\s*parameter name="([^"]+)"(?: string="(true|false)")?\s*>(.*?)</%sDSML%s\s*parameter>' % (bar, bar, bar, bar), m.group(2), re.S):
            v = p.group(3).strip()
            if p.group(2) == 'false':
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
            args[p.group(1)] = v
        calls.append({'id': 'dsml%d%s' % (n, secrets.token_hex(3)), 'type': 'function',
                      'function': {'name': m.group(1), 'arguments': json.dumps(args)}})
    if re.search(r'<%sDSML%s' % (bar, bar), content):
        content = re.sub(r'<%sDSML%s.*' % (bar, bar), '', content, flags=re.S).strip()
    return calls, content


def given(r, text):
    """Did this text come from the person: the task, the board items it was given, or an answer?"""
    t = ' '.join(str(text).lower().split())
    if not t:
        return True
    said = ' '.join(text_of(m['content']).lower() for m in r['messages'] if m['role'] == 'user' or
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


def cut(t, n):
    """Text cut to about n characters at a word, with … when it was longer."""
    t = str(t or '')
    return t if len(t) <= n else t[:n].rsplit(' ', 1)[0].rstrip(',;:.') + '…'


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
    imgs = [i for i, m in enumerate(messages) if m.get('img') and not m.get('keep')]
    out = []
    for i, m in enumerate(messages):
        if m.get('img') and not m.get('keep') and i not in imgs[-KEEP_IMAGES:]:   # an older screenshot: its words stay, the picture goes
            m = dict(m, content=text_of(m['content']) + ' (an earlier screenshot, no longer shown)')
        elif i in old and not m.get('keep') and isinstance(m.get('content'), str) and len(m['content']) > OLD_RESULT_CHARS:
            m = dict(m, content=m['content'][:OLD_RESULT_CHARS] + CUT_MARK)
        out.append({k: v for k, v in m.items() if k not in ('keep', 'img')})   # keep: a read of a selected item stays whole (the source of the job)
    return out
