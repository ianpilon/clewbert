"""Weakness mining: what keeps going wrong across agent jobs, found from the records, not from one incident at a time.
Runs nightly at 03:45 as a child process of the server (server.py mine_nightly, log mine.log) or by hand:
    /usr/bin/python3 agents/mine.py DATA_DIR [--days 14] [--max 40] [--dry]
Reads: DATA/runs (every job and part), DATA/team_notes.jsonl, DATA/asks.jsonl + DATA/verdicts.jsonl (questions and how
they were judged). A job counts as a case when a signal says something went wrong: it ended stopped, failed, partial or
unfinished; a part failed its check; the same job was asked again on that board within 90 minutes; the question that
started it was marked bad; or it was a one-agent job over 30 model calls. Each case gets a failure signature from the
model (cause, behaviour, mechanism, surface), cached in DATA/mined/<run>.json so a job is read once. The signatures are
grouped into patterns, written to DATA/patterns.json and to one card on the standing rules board, next to the built-ins
frame. Nothing else changes: a fix is still a prompt, rule or tool change, tried on the recorded questions first.
After Self-Harness (Zhang et al., 2026): cluster failures by what was rejected, what the agent did, and the reusable
mechanism, so that fixes target mechanisms rather than single mistakes."""
import glob, json, os, re, secrets, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import models, skills, talk   # noqa: E402
from agents import propose   # noqa: E402

ENDED = ('done', 'partial', 'failed', 'stopped', 'unfinished')
SIGNATURE = """You are reading the record of one job an AI agent team did on the user's Clewbert boards, with the signals that
say something went wrong. Name the failure the way a harness engineer would, so that jobs with the same failure can be
grouped: separate what was wrong at the end from what the agents did, and from the reusable mechanism behind it.
Reply with JSON only:
{"real": true or false (false when the signals are a false alarm and the job was in fact fine; say why in "cause"),
 "cause": "<what was wrong at the end, as the checker or the user would see it, one line>",
 "behaviour": "<what the agent or team did that led there, one line, naming the step or part>",
 "mechanism": "<the reusable mechanism, 3 to 8 words, lower case, no job details: 'planner reused a finished task',
               'teammate read the wrong item id', 'lead spent many calls on a one-call job', 'team never asked what the
               person meant'>",
 "surface": "<one of: planner, team design, lead prompt, teammate prompt, check, rules, tool, model, not fixable>",
 "evidence": "<one line pointing at the step, part or signal that shows it>"}"""
GROUP = """Below are failure signatures from recent agent jobs on the user's Clewbert boards, one per job. Group the jobs that
share a mechanism into patterns: at most 8, biggest first, each with every job that belongs to it (a job belongs to one
pattern). Leave out one-off failures that match nothing else unless they would plainly recur.
Reply with JSON only:
{"patterns": [{"name": "<3 to 6 words>", "mechanism": "<one sentence, plain words>", "jobs": ["<job id>", ...],
               "surface": "<planner | team design | lead prompt | teammate prompt | check | rules | tool | model | not fixable>",
               "what_to_change": "<the smallest change to that surface that would stop it, one or two sentences, plain words>"}]}"""


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_jsonl(path):
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []


def write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def team_of(runs, root):
    team, todo = [root], list(root.get('children') or [])
    while todo:
        c = todo.pop()
        if c in runs and runs[c] not in team:
            team.append(runs[c])
            todo += runs[c].get('children') or []
    return team


def bare(task):
    return ' '.join(str(task or '').split(' (This is about')[0].split())[:120]


def signals(runs, root, roots, bad_asks):
    """Why this job is worth a look; [] when nothing says it went wrong."""
    out = []
    if root.get('status') in ('failed', 'stopped', 'partial', 'unfinished'):
        out.append('ended as %s%s' % (root['status'], ', missing: ' + root['gaps'][:200] if root.get('gaps') else ''))
    for x in team_of(runs, root)[1:]:
        v = x.get('check') or {}
        if v and v.get('pass') is False:
            out.append('part by %s did not pass its check: %s' % ((x.get('member') or {}).get('name', '?'), (v.get('missing') or '')[:160]))
    key = bare(root.get('task'))
    for o in roots:
        if o is not root and o.get('board') == root.get('board') and bare(o.get('task')) == key and 0 < o['created'] - root['created'] <= 90 * 60000:
            out.append('the same job was asked again %d minutes later (job %s)' % ((o['created'] - root['created']) // 60000, o['id']))
    for a in bad_asks:
        if a.get('board') == root.get('board') and a.get('task') and str(root.get('task', '')).startswith(a['task'][:100]):
            out.append('the question that started it was marked bad%s' % (': ' + a['note'] if a.get('note') else ''))
    sc = root.get('score') or {}
    if sc.get('model_calls', 0) >= 30 and not sc.get('members'):
        out.append('one agent, %d model calls' % sc['model_calls'])
    elif sc.get('model_calls', 0) >= 60:
        out.append('%d model calls' % sc['model_calls'])
    return out


def case_log(runs, root, sig, names):
    log = ['JOB %s on board "%s", %s' % (root['id'], names.get(root.get('board'), root.get('board')), time.strftime('%Y-%m-%d', time.localtime(root['created'] / 1000))),
           'TASK: ' + str(root.get('task', ''))[:600], 'SIGNALS: ' + ' | '.join(sig), 'STATUS: ' + str(root.get('status'))]
    if root.get('critic'):
        log.append('PLAN CRITIC: ' + json.dumps(root['critic'])[:300])
    if (root.get('score') or {}).get('line'):
        log.append('COST: ' + root['score']['line'])
    log.append('LEAD STEPS: ' + '; '.join(s.get('text', '') for s in (root.get('steps') or [])[-30:])[:3000])
    for x in team_of(runs, root)[1:]:
        v = x.get('check') or {}
        log.append('PART (%s, %s%s): %s | check: %s | last step: %s' % (
            (x.get('member') or {}).get('name', ''), x.get('status'), ', second try' if x.get('retry_of') else '', str(x.get('task', ''))[:200],
            ('passed' if v.get('pass') else 'not done: ' + (v.get('missing') or '')) if v else '-',
            x['steps'][-1].get('text', '') if x.get('steps') else ''))
    if root.get('summary'):
        log.append('LEAD SUMMARY: ' + str(root['summary'])[:500])
    return '\n'.join(log)[:9000]


def signature(log):
    out = talk.chat([{'role': 'system', 'content': SIGNATURE}, {'role': 'user', 'content': log}], 400, timeout=120)
    if not isinstance(out, dict) or not out.get('mechanism'):
        return None
    clean = lambda k, n: ' '.join(str(out.get(k) or '').split())[:n]
    return {'real': out.get('real') is not False, 'cause': clean('cause', 300), 'behaviour': clean('behaviour', 300),
            'mechanism': clean('mechanism', 80).lower(), 'surface': clean('surface', 30).lower(), 'evidence': clean('evidence', 300)}


def group(cases):
    lines = ['- job %s (%s, %s): mechanism: %s | cause: %s | behaviour: %s | surface: %s' % (
        c['id'], c['board_name'], c['date'], c['sig']['mechanism'], c['sig']['cause'], c['sig']['behaviour'], c['sig']['surface']) for c in cases]
    out = talk.chat([{'role': 'system', 'content': GROUP}, {'role': 'user', 'content': '\n'.join(lines)[:30000]}], 1500, timeout=180)
    pats = out.get('patterns') if isinstance(out, dict) else None
    if not isinstance(pats, list):
        return None
    ids = {c['id'] for c in cases}
    good = []
    for p in pats:
        if not isinstance(p, dict):
            continue
        jobs = [j for j in (p.get('jobs') or []) if j in ids]
        if not jobs:
            continue
        good.append({'name': ' '.join(str(p.get('name') or '').split())[:60], 'mechanism': ' '.join(str(p.get('mechanism') or '').split())[:300],
                     'jobs': jobs, 'count': len(jobs), 'surface': ' '.join(str(p.get('surface') or '').split())[:30].lower(),
                     'what_to_change': ' '.join(str(p.get('what_to_change') or '').split())[:400]})
    return sorted(good, key=lambda p: -p['count'])


def card_text(result):
    head = 'Failure patterns · last %d days · %d jobs read, %d with problems · %s' % (
        result['days'], result['jobs'], result['cases'], time.strftime('%b %-d, %Y', time.localtime(result['t'] / 1000)))
    if not result['patterns']:
        return head + '\nNo recurring pattern found.'
    rows = []
    for k, p in enumerate(result['patterns'], 1):
        rows.append('%d. %s ×%d: %s Jobs: %s. Fix would touch the %s: %s' % (
            k, p['name'], p['count'], p['mechanism'].rstrip('.') + '.', ', '.join(p['jobs'][:6]) + (' and more' if len(p['jobs']) > 6 else ''),
            p['surface'] or 'surface', p['what_to_change']))
    return head + '\n' + '\n'.join(rows)


def put_card(data, text, cid='mined-patterns', under=None):
    """One card on the standing rules board, right of the built-ins frame (or under another card); rewritten each run."""
    try:
        with open(os.path.join(data, 'rules_board')) as f:
            bid = f.read().strip()
    except OSError:
        return False
    path = os.path.join(data, 'boards', bid + '.json')
    board = load_json(path, None)
    if not board:
        return False
    items = [i for i in board.get('items') or [] if i.get('id') != cid]
    frame = next((i for i in items if i.get('id') == 'builtin-frame'), None)
    x, y = (frame['x'] + frame['w'] + 60, frame['y']) if frame else (40, -500)
    above = next((i for i in items if i.get('id') == under), None) if under else None
    if above:
        x, y = above['x'], above['y'] + above.get('h', 0) + 40
    w = 380
    h = 44 + 19 * sum(1 + len(l) // 34 for l in text.split('\n'))
    items.append({'id': cid, 'type': 'note', 'mined': 1, 'card': 1, 'grow': 'h', 'color': '#fdecec' if cid == 'mined-patterns' else '#eef2ff', 'fs': 13,
                  'x': x, 'y': y, 'w': w, 'h': h, 'z': 2, 'text': text})
    board['items'] = items
    board['updated'] = int(time.time() * 1000)
    write_json(path, board)
    return True


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    data = args[0]
    days = int(args[args.index('--days') + 1]) if '--days' in args else 14
    cap = int(args[args.index('--max') + 1]) if '--max' in args else 40
    dry = '--dry' in args
    models.SETTINGS = os.path.join(data, 'settings.json')
    skills.DATA = data
    os.makedirs(os.path.join(data, 'mined'), exist_ok=True)
    runs = {}
    for f in glob.glob(os.path.join(data, 'runs', '*.json')):
        r = load_json(f, None)
        if r and r.get('id'):
            runs[r['id']] = r
    since = int(time.time() * 1000) - days * 86400000
    roots = [r for r in runs.values() if not r.get('parent') and r.get('created', 0) >= since and r.get('status') in ENDED]
    names = {}
    for f in glob.glob(os.path.join(data, 'boards', '*.json')):
        b = load_json(f, None)
        if b:
            names[b['id']] = b.get('name', '')
    verdicts = {v['ask']: v for v in load_jsonl(os.path.join(data, 'verdicts.jsonl')) if v.get('ask')}
    asks = {a['id']: a for a in load_jsonl(os.path.join(data, 'asks.jsonl')) if a.get('id')}
    bad_asks = [dict(asks[k], note=v.get('note', '')) for k, v in verdicts.items() if v.get('verdict') == 'bad' and k in asks]
    cases = []
    for r in sorted(roots, key=lambda r: -r['created']):
        sig = signals(runs, r, roots, bad_asks)
        if sig:
            cases.append({'id': r['id'], 'board': r.get('board'), 'board_name': names.get(r.get('board'), ''),
                          'date': time.strftime('%Y-%m-%d', time.localtime(r['created'] / 1000)), 'signals': sig, 'run': r})
    cases = cases[:cap]
    print('%s mining: %d jobs in the last %d days, %d with signals' % (time.strftime('%Y-%m-%d %H:%M:%S'), len(roots), days, len(cases)), flush=True)
    mined = []
    for c in cases:
        cache = os.path.join(data, 'mined', c['id'] + '.json')
        sig = load_json(cache, None)
        if not sig:
            if dry:
                print('  would read', c['id'], '|', ' | '.join(c['signals']))
                continue
            try:
                sig = signature(case_log(runs, c['run'], c['signals'], names))
            except (OSError, ValueError, KeyError) as e:
                print('  could not read', c['id'], e)
                continue
            if not sig:
                continue
            sig['signals'] = c['signals']
            sig['t'] = int(time.time() * 1000)
            write_json(cache, sig)
            print('  %s: %s [%s]%s' % (c['id'], sig['mechanism'], sig['surface'], '' if sig['real'] else ' (false alarm)'), flush=True)
        if sig.get('real', True):
            mined.append(dict(c, sig=sig))
    if dry:
        return 0
    patterns = group(mined) if mined else []
    if patterns is None:
        print('grouping failed: the model did not return patterns')
        return 1
    result = {'t': int(time.time() * 1000), 'days': days, 'jobs': len(roots), 'cases': len(mined), 'patterns': patterns,
              'false_alarms': [c['id'] for c in cases if not (load_json(os.path.join(data, 'mined', c['id'] + '.json'), {}) or {}).get('real', True)]}
    write_json(os.path.join(data, 'patterns.json'), result)
    text = card_text(result)
    print(text, flush=True)
    print('card written' if put_card(data, text) else 'card not written (no rules board yet)')
    try:   # the proposal step: one rule per big pattern, tried for real, judged on the jobs that follow
        props = propose.run(data, result, {r['id']: r['created'] for r in roots})
        print('proposals: %s' % ', '.join('%s %s' % (x['status'], x['pattern']) for x in props[-6:]) if props else 'proposals: none', flush=True)
        put_card(data, propose.card_text(props), cid='mined-proposals', under='mined-patterns')
    except Exception as e:   # a failed proposal pass never loses the night's patterns
        print('proposal step failed: %s: %s' % (type(e).__name__, e), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
