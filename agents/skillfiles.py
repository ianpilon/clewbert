"""Skills the teams write for themselves, in the Agent Skills format (anthropics/skills: a folder with a SKILL.md whose
frontmatter has a name and a description that says what it does and when to use it, then the instructions).
After a job ends done with no gaps, a writer call reads the job's log and decides whether it holds a procedure a later
team could follow (not a one-off). If so it writes <data>/skills/<name>/SKILL.md, marked provisional. A later Lead whose
task fits the description gets the skill in its brief; the job's outcome counts for or against it; three hits with few
misses make it trusted, three misses or 60 days without a hit retire it (the same lifecycle as team notes). The folder is
plain Agent Skills, so any of them can be copied into another agent's skills folder as it is. 2026-11-08."""
import difflib, json, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import talk   # noqa: E402

MIN_FIT = 3           # task words the description must share before a skill is offered (trusted: 2)
MAX_PER_DAY = 3       # new skills a day at most, so what changed can be told apart
WRITE = """An AI agent team on the user's Clewbert boards just finished a job well. Decide whether its log holds a PROCEDURE a
later team could follow on a similar job: a way of doing a kind of task (which tools, in what order, what to check), not
the facts of this one job. Most jobs do not; say so. If it does, write it as an Agent Skill:
- "name": lowercase words joined by hyphens, 3 to 5 words, what the skill does ("transcribe-board-video").
- "description": what the skill does AND when to use it, in one paragraph under 500 characters, naming the kinds of
  asks that should trigger it ("Use when the person asks to ... or ...") and what it hands back. It is the only part a
  team reads before deciding to use the skill, so it carries every trigger.
- "body": the instructions in Markdown, under 1,500 characters: a "## Steps" list in the imperative naming the tools to
  call (read_item, search_board, transcribe, add_file, assign ...), a "## Check before finishing" list, and a "## Pitfalls"
  list of what went wrong or wasted time in this job. Nothing specific to this job's items, names or numbers.
Reply with JSON only: {"reusable": true or false, "why": "<one line>", "name": "", "description": "", "body": ""}"""


def root(data):
    return os.path.join(data, 'skills')


def load(data):
    """Every written skill: [{'name', 'description', 'body', meta...}], from the SKILL.md files."""
    out = []
    d = root(data)
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name, 'SKILL.md')
        if not os.path.exists(p):
            continue
        try:
            with open(p) as f:
                text = f.read()
            m = re.match(r'^---\n(.*?)\n---\n(.*)$', text, re.S)
            if not m:
                continue
            front, body = m.group(1), m.group(2).strip()
            x = {'name': name, 'body': body, 'path': p}
            for line in front.split('\n'):
                k, _, v = line.partition(':')
                k, v = k.strip(), v.strip()
                if k == 'description':
                    x['description'] = v.strip('"')
                elif k.startswith('  ') or not k:
                    continue
            mm = re.search(r'\nmetadata:\n((?:  .*\n?)+)', front + '\n')
            if mm:
                for line in mm.group(1).split('\n'):
                    k, _, v = line.strip().partition(':')
                    if k:
                        v = v.strip()
                        x[k] = int(v) if re.match(r'^-?\d+$', v) else v
            out.append(x)
        except OSError:
            continue
    return out


def save(data, x):
    """Write one skill's SKILL.md from its fields (our bookkeeping goes under metadata, as the format allows)."""
    d = os.path.join(root(data), x['name'])
    os.makedirs(d, exist_ok=True)
    meta = {k: x.get(k) for k in ('status', 'source', 'board', 't', 'hits', 'misses', 'last_hit', 'retired', 'trusted') if x.get(k) not in (None, '')}
    front = 'name: %s\ndescription: "%s"\nmetadata:\n%s' % (x['name'], x['description'].replace('"', "'"), '\n'.join('  %s: %s' % (k, v) for k, v in meta.items()))
    with open(os.path.join(d, 'SKILL.md.tmp'), 'w') as f:
        f.write('---\n%s\n---\n\n%s\n' % (front, x['body'].strip()))
    os.replace(os.path.join(d, 'SKILL.md.tmp'), os.path.join(d, 'SKILL.md'))


def consider(data, r, log):
    """After a job ended done with no gaps: write a skill from its log if it holds a procedure. Returns the skill or None."""
    have = load(data)
    today = time.strftime('%Y-%m-%d')
    if sum(1 for x in have if time.strftime('%Y-%m-%d', time.localtime(int(x.get('t') or 0) / 1000)) == today) >= MAX_PER_DAY:
        return None
    out = talk.chat([{'role': 'system', 'content': WRITE}, {'role': 'user', 'content': log[:14000]}], 1200, timeout=120)
    out = out if isinstance(out, dict) else {}
    if out.get('reusable') is not True:
        return None
    name = re.sub(r'[^a-z0-9]+', '-', str(out.get('name') or '').lower()).strip('-')[:48]
    desc = ' '.join(str(out.get('description') or '').split())[:600]
    body = str(out.get('body') or '').strip()[:2500]
    if len(name) < 5 or len(desc) < 40 or len(body) < 80:
        return None
    for x in have:   # the same procedure under other words is not written twice
        if x['name'] == name or difflib.SequenceMatcher(None, desc.lower(), (x.get('description') or '').lower()).ratio() >= 0.6:
            return None
    x = {'name': name, 'description': desc, 'body': body, 'status': 'provisional', 'source': r['id'], 'board': r.get('board'),
         't': int(time.time() * 1000), 'hits': 0, 'misses': 0}
    save(data, x)
    return x


def pick(data, task, limit=2):
    """(text for a Lead's brief, names used): the live skills whose description fits the task best."""
    want = set(talk.tokens(task))
    live = [x for x in load(data) if x.get('status') != 'retired']
    fit = lambda x: len(want & set(talk.tokens(x.get('description', '') + ' ' + x['name'].replace('-', ' '))))
    picked = sorted((x for x in live if fit(x) >= (2 if x.get('status') == 'trusted' else MIN_FIT)), key=lambda x: (-fit(x), -int(x.get('hits') or 0)))[:limit]
    if not picked:
        return '', []
    text = '\n\n'.join('### %s%s\n%s\n%s' % (x['name'], ' (trusted)' if x.get('status') == 'trusted' else ' (being tried)', x.get('description', ''), x['body'][:1800]) for x in picked)
    return text, [x['name'] for x in picked]


def outcomes(data, used, passed):
    """The skills a job was given: a hit when it went through, a miss when it fell short; trusted and retired as team notes are."""
    if not used:
        return
    now = int(time.time() * 1000)
    for x in load(data):
        if x['name'] not in used or x.get('status') == 'retired':
            continue
        x['hits' if passed else 'misses'] = int(x.get('hits' if passed else 'misses') or 0) + 1
        if passed:
            x['last_hit'] = now
        h, m = int(x.get('hits') or 0), int(x.get('misses') or 0)
        if x.get('status') == 'provisional' and h >= 3 and m * 3 <= h:
            x['status'], x['trusted'] = 'trusted', now
        elif (m >= 3 and m > h) or (not h and m and now - int(x.get('t') or now) > 60 * 86400000):
            x['status'], x['retired'] = 'retired', now
        save(data, x)


def listing(data):
    """For the Skills page: each written skill with its state."""
    return [{k: x.get(k) for k in ('name', 'description', 'status', 'source', 'board', 't', 'hits', 'misses', 'trusted', 'retired')}
            | {'lines': x['body'].count('\n') + 1} for x in load(data)]
