"""Proposals: the smallest rule that would stop a recurring failure pattern, tried for real and kept only if it works.
Runs at the end of each nightly mining pass (agents/mine.py) from DATA/patterns.json:
- A pattern seen in 2 or more jobs whose surface is a prompt, a rule, the team design or the check gets ONE proposed rule
  (a sentence in plain words, written by the model from the pattern). It goes to every team at once as a rule being tried
  (agents/runner.py rules()), and is listed on the standing rules board in the Proposed rules card.
- A pattern on the planner's or the voice's side gets one proposed line for the Assistant's skills instead, and is kept
  only if the recorded questions still replay right with it in place (tools/replay.py check, the same gate deploys use).
- Each later night the proposal is judged on the jobs that ran since it went in: once 6 or more have run, the pattern
  recurring at half its old rate or less makes the rule TRUSTED (it becomes an ordinary note on the rules board); anything
  else RETIRES it. At most 2 new proposals a night, so what changed can be told apart.
The user can stop any of them by deleting its line from DATA/proposals.json or the rule note it became.
After Self-Harness (minimal proposals, judged on held-out runs) and AgentPilot (provisional policy, reinforced or retired)."""
import difflib, json, os, secrets, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import skills, talk   # noqa: E402

TEAM_SURFACES = ('lead prompt', 'teammate prompt', 'rules', 'team design', 'check')
ASSISTANT_SURFACES = ('planner',)
MIN_JOBS = 6              # jobs that must run under a proposal before it is judged
MAX_NEW = 2               # new proposals a night
PROPOSE = """A recurring failure was found across several jobs done by AI agent teams on the user's Context Canvas boards. Write the ONE
standing rule that would stop it: a single sentence in plain words, addressed to the agents ("Read ... before ...",
"Never ..."), concrete enough to act on, nothing about this app's code. Under 220 characters.
Reply with JSON only: {"rule": "<the sentence>", "applies": "<when it applies, under 80 characters>"}"""


def load(data):
    try:
        with open(os.path.join(data, 'proposals.json')) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save(data, props):
    path = os.path.join(data, 'proposals.json')
    with open(path + '.tmp', 'w') as f:
        json.dump(props, f, indent=1)
    os.replace(path + '.tmp', path)


def same(p, x):
    """Does today's pattern p describe the failure proposal x was made for? Names and mechanisms are reworded between
    nights, so this is a likeness, not an equality."""
    a = (p.get('name', '') + ' ' + p.get('mechanism', '')).lower()
    b = (x.get('pattern', '') + ' ' + x.get('mechanism', '')).lower()
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.55


def draft(p):
    out = talk.chat([{'role': 'system', 'content': PROPOSE},
                     {'role': 'user', 'content': 'Pattern: %s\nMechanism: %s\nSeen in %d jobs. What a fix would change (%s): %s'
                      % (p['name'], p['mechanism'], p['count'], p.get('surface', ''), p.get('what_to_change', ''))}], 300, timeout=60)
    rule = ' '.join(str(out.get('rule') or '').split())[:220]
    return (rule, ' '.join(str(out.get('applies') or '').split())[:80]) if len(rule) > 20 else (None, None)


def replays_ok(data):
    """The deploy gate, run here: every marked recorded question must still come out right."""
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'tools'))
    import replay
    replay.HERE = data
    try:
        return replay.check([], False) == 0
    except Exception as e:   # the gate failing to run is not a pass
        print('  replay gate did not run:', e, flush=True)
        return False


def judge(props, patterns, created, now):
    """Open proposals, judged on the jobs that ran since each went in."""
    for x in props:
        if x.get('status') != 'proposed':
            continue
        new = [j for j, t in created.items() if t > x['t']]
        if len(new) < MIN_JOBS:
            x['jobs_since'] = len(new)
            continue
        hit = sum(1 for p in patterns if same(p, x) for j in p.get('jobs', []) if j in new)
        rate = hit / len(new)
        x.update(jobs_since=len(new), hits_since=hit, judged=now)
        x['status'] = 'trusted' if rate <= x['rate'] / 2 else 'retired'
    return props


def rule_note(data, text):
    """A trusted proposal becomes an ordinary note on the standing rules board (what runner.rules() reads)."""
    try:
        with open(os.path.join(data, 'rules_board')) as f:
            bid = f.read().strip()
        path = os.path.join(data, 'boards', bid + '.json')
        with open(path) as f:
            board = json.load(f)
    except (OSError, ValueError):
        return False
    notes = [i for i in board.get('items') or [] if i.get('type') == 'note' and not i.get('mined') and not i.get('builtin')]
    k = len(notes)
    board['items'] = (board.get('items') or []) + [{'id': 'rule' + secrets.token_hex(4), 'type': 'note', 'text': text,
                                                    'x': 40 + (k % 3) * 320, 'y': 140 + (k // 3) * 200, 'w': 290, 'h': 170, 'z': k + 1}]
    board['updated'] = int(time.time() * 1000)
    with open(path + '.tmp', 'w') as f:
        json.dump(board, f)
    os.replace(path + '.tmp', path)
    return True


def card_text(props):
    rows = []
    words = {'proposed': 'being tried', 'trusted': 'kept: it worked', 'retired': 'dropped: no change', 'rejected': 'not tried: the recorded questions broke'}
    for x in sorted(props, key=lambda x: -x['t'])[:12]:
        when = time.strftime('%b %-d', time.localtime(x['t'] / 1000))
        tail = ''
        if x['status'] == 'proposed':
            tail = ' · %d of %d jobs run so far' % (x.get('jobs_since', 0), MIN_JOBS)
        elif x.get('judged'):
            tail = ' · %d of %d jobs since still showed it (was %d of %d)' % (x.get('hits_since', 0), x.get('jobs_since', 0), x['count'], x['jobs'])
        rows.append('%s · %s (%s)%s\n  %s' % (when, x['pattern'], words.get(x['status'], x['status']), tail, x['rule']))
    return 'Proposed rules · from the failure patterns · %s\n' % time.strftime('%b %-d, %Y') + (
        '\n'.join(rows) if rows else 'None yet: a pattern needs 2 or more jobs and a fixable surface.')


def run(data, result, created):
    """One pass: judge the open proposals, make up to MAX_NEW new ones, write the card. created = {root job id: created ms}."""
    now = int(time.time() * 1000)
    props = judge(load(data), result['patterns'], created, now)
    for x in props:
        if x.get('status') == 'trusted' and not x.get('kept'):
            x['kept'] = rule_note(data, 'From the failure miner (%s): %s' % (x['pattern'], x['rule']))
        if x.get('status') in ('retired', 'trusted') and x.get('skill'):
            skills.remove('assistant', x['skill'])   # a provisional Assistant line ends either way (a trusted one is kept as a rule note)
            x['skill'] = None
    new = 0
    for p in result['patterns']:
        if new >= MAX_NEW:
            break
        if p['count'] < 2:
            continue
        surface = p.get('surface', '')
        if surface not in TEAM_SURFACES + ASSISTANT_SURFACES or any(same(p, x) and x['status'] != 'retired' for x in props):
            continue
        if any(same(p, x) and x['status'] == 'retired' and now - x.get('judged', 0) < 30 * 86400000 for x in props):
            continue   # dropped within the month: a second try comes only with a different pattern
        try:
            rule, applies = draft(p)
        except (OSError, ValueError, KeyError) as e:
            print('  could not draft a rule for %s: %s' % (p['name'], e), flush=True)
            continue
        if not rule:
            continue
        x = {'id': 'p' + secrets.token_hex(3), 't': now, 'pattern': p['name'], 'mechanism': p['mechanism'], 'surface': surface, 'rule': rule,
             'applies': applies, 'count': p['count'], 'jobs': result['jobs'], 'rate': p['count'] / max(result['jobs'], 1), 'status': 'proposed'}
        if surface in ASSISTANT_SURFACES:
            x['skill'] = skills.add('assistant', rule, proposal=x['id'])
            if not replays_ok(data):
                skills.remove('assistant', x['skill'])
                x['skill'], x['status'] = None, 'rejected'
        props.append(x)
        new += 1
        print('  proposed (%s): %s' % (x['status'], rule), flush=True)
    save(data, props)
    return props


def trying(data):
    """The rules being tried right now, for every team's prompt."""
    return [x for x in load(data) if x.get('status') == 'proposed' and x.get('surface') in TEAM_SURFACES]
