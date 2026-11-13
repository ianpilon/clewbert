#!/usr/bin/env python3
"""Replay recorded questions against the live model before a prompt change goes out.
The server records every question with the very input the routing and the team planner saw (DATA/asks.jsonl). Mark the
ones that matter, then `check` replays them and fails on any that no longer come out right. Reads the app's data folder
(CLEWBERT_DATA, default ~/clewbert-data) on the machine that runs the app.
    tools/replay.py pull                       copy asks, verdicts, settings and skills from the data folder into .replay/
    tools/replay.py list [N]                   the last N recorded questions (default 30); * = marked
    tools/replay.py show ID                    one question in full, with what was made of it
    tools/replay.py mark ID good|bad [--route team|boards|chat|...] [--must w1,w2] [--must-not w3,w4] [--note "..."]
                                               good: the recorded route is expected. bad: say what right looks like.
    tools/replay.py check [ID,...] [--all]     replay the marked questions (or these ids; --all = every recorded one, just to look)
Exit code 1 from check when any marked question regresses: run it before you change a prompt in talk.py."""
import json, os, shutil, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(ROOT, '.replay')
DATA = os.environ.get('CLEWBERT_DATA') or os.path.expanduser('~/clewbert-data')


def jsonl(name):
    try:
        with open(os.path.join(HERE, name)) as f:
            return [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []


def pull():
    os.makedirs(HERE, exist_ok=True)
    for name in ('asks.jsonl', 'verdicts.jsonl', 'settings.json', 'skills.json'):
        src = os.path.join(DATA, name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(HERE, name))
        else:
            open(os.path.join(HERE, name), 'w').close()
    print('pulled %d questions, %d verdicts' % (len(jsonl('asks.jsonl')), len(jsonl('verdicts.jsonl'))))


def verdicts():
    out = {}
    for v in jsonl('verdicts.jsonl'):
        out[v['ask']] = v   # the last word on a question wins
    return out


def when(t):
    return time.strftime('%b %-d %H:%M', time.localtime(t / 1000))


def list_(n):
    vs = verdicts()
    for a in jsonl('asks.jsonl')[-n:]:
        v = vs.get(a['id'])
        print('%s %s %s %-6s %-60s %s' % ('*' if v else ' ', a['id'], when(a['t']), a.get('route') or '', a['q'][:60].replace('\n', ' '),
                                        ('-> ' + (a.get('task') or '')[:70]) if a.get('task') else ''))


def show(aid):
    a = next((x for x in jsonl('asks.jsonl') if x['id'] == aid), None)
    if not a:
        print('no question', aid)
        return 1
    print(json.dumps(a, indent=1)[:6000])
    v = verdicts().get(aid)
    print('VERDICT:', json.dumps(v) if v else 'none')


def mark(aid, verdict, args):
    a = next((x for x in jsonl('asks.jsonl') if x['id'] == aid), None)
    if not a:
        print('no question', aid, '(pull first?)')
        return 1
    opt = lambda k: args[args.index(k) + 1] if k in args else ''
    expect = {'route': opt('--route') or (a.get('route') if verdict == 'good' else ''),
              'must': [w.strip().lower() for w in opt('--must').split(',') if w.strip()],
              'must_not': [w.strip().lower() for w in opt('--must-not').split(',') if w.strip()]}
    if verdict == 'bad' and not (expect['route'] or expect['must'] or expect['must_not']):
        print('a bad mark needs what right looks like: --route, --must or --must-not')
        return 1
    v = {'ask': aid, 'verdict': verdict, 'expect': expect, 'note': opt('--note'), 't': int(time.time() * 1000)}
    line = json.dumps(v)
    for folder in (DATA, HERE):
        with open(os.path.join(folder, 'verdicts.jsonl'), 'a') as f:
            f.write(line + '\n')
    print('marked', aid, verdict, json.dumps(expect))


def check(ids, everything):
    sys.path.insert(0, ROOT)
    import models, skills, talk
    models.SETTINGS = os.path.join(HERE, 'settings.json')
    skills.DATA = HERE
    vs = verdicts()
    asks = [a for a in jsonl('asks.jsonl') if (a['id'] in ids if ids else (everything or a['id'] in vs))]
    if not asks:
        print('nothing to replay (mark some questions first)')
        return 0
    bad = 0
    # the gate checks what the model answers, not how fast: the voice's own 20 s limit timed out on a cold relay (the same
    # question took 0.9 s, 11 s and 20+ s within a minute on 2026-11-09), so replays wait up to 90 s
    real_chat = talk.chat
    talk.chat = lambda msgs, max_tokens, timeout=90, **kw: real_chat(msgs, max_tokens, timeout=max(timeout, 90), **kw)
    for a in asks:
        talk.apps_line = lambda bid, line=a.get('apps_line') or '\nCONNECTED APPS: none.': line
        t = time.time()
        try:
            route, _ = talk.small_talk(a['q'], a.get('history') or [], ['x'] if a.get('waiting') else [], a.get('here') or '', a.get('apps') or [], a.get('pending') or '')
            task = ''
            if route == 'team' or (vs.get(a['id']) or {}).get('expect', {}).get('route') == 'team':
                task = talk.team_plan(a['q'], a.get('history') or [], a.get('here') or '', a.get('spaces_text') or '', bid=a.get('board')).get('task') or ''
        except Exception as e:   # the replay must report, never crash on one question
            route, task = 'error: %s' % e, ''
        v = vs.get(a['id'])
        fails = []
        if v:
            e = v.get('expect') or {}
            if e.get('route') and route != e['route']:
                fails.append('route %s, expected %s' % (route, e['route']))
            for w in e.get('must') or []:
                if w not in task.lower():
                    fails.append('task lacks "%s"' % w)
            for w in e.get('must_not') or []:
                if w in task.lower():
                    fails.append('task has "%s"' % w)
        bad += bool(fails)
        print('%s %s %4.1fs %-6s %s' % ('FAIL' if fails else ('ok  ' if v else '    '), a['id'], time.time() - t, route, a['q'][:70].replace('\n', ' ')))
        if task:
            print('       task: ' + task[:200].replace('\n', ' '))
        for f in fails:
            print('       ' + f)
    print('%d replayed, %d regressed' % (len(asks), bad))
    return 1 if bad else 0


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd == 'pull':
        return pull()
    if cmd == 'list':
        return list_(int(args[1]) if len(args) > 1 else 30)
    if cmd == 'show':
        return show(args[1])
    if cmd == 'mark':
        return mark(args[1], args[2], args[3:])
    if cmd == 'check':
        ids = [x for x in (args[1].split(',') if len(args) > 1 and not args[1].startswith('--') else []) if x]
        return check(ids, '--all' in args)
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main() or 0)
