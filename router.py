"""Two background helpers for agent work, after DeepSeek's Super-agent design (2026-11-01 article, dsh-superagent-prototype):

Watchdog: every minute it looks at the agent teams. A team that makes no progress for 15 minutes gets one push to the user and a
line in its frame. A top-level team that stops unfinished or with an error is started again once, from what it already
put in its frame. A team the user stopped is left alone, and so is anything that ended before this server started.

Router: every few minutes it reads what is new in the user's Gmail inbox and Google Calendar (the next 3 days). Code
rules drop most of it (no-reply senders, mail already seen); the rest goes to the model with the user's rules, the note on
the Incoming board, which the user edits in words. Each item is ignored (only counted), noted (a card), or turned into a
suggested task (a card with a "Start this task" in its menu, and a push). It is record-only: it never starts a task,
replies or sends anything itself. Email is judged by the models on your own machines only, never an outside provider.
"""
import json, os, re, secrets, threading, time, urllib.request

import integrations, models, skills
from agents import runner as agents

STALL_MIN = 15
RETRY_NOTE = ('The watchdog started you again: you stopped before finishing (%s). Pick up from what is already in your frame, '
              'do not redo the parts that are done, and finish what the task asks.')


class Watchdog:
    def __init__(self, runner, push):
        self.runner, self.push, self.since = runner, push, time.time() * 1000
        self.last, self.flagged, self.retried, self.events = None, 0, 0, []   # for the agent library: when it last looked, what it did since the start
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            time.sleep(60)
            try:
                self.check()
                self.last = time.time() * 1000
            except Exception as e:   # the watchdog itself must never stop
                print('watchdog:', e, flush=True)

    def check(self):
        now = time.time() * 1000
        retry, stalled = [], []
        with self.runner.lock:
            for r in self.runner.runs.values():
                last = max([s['t'] for s in r.get('steps') or []] + [r.get('created') or 0])
                if r['status'] == 'working' and now - last > STALL_MIN * 60000 and not r.get('watch_stall'):
                    r['watch_stall'] = now
                    stalled.append(r)
                elif (r['status'] in ('unfinished', 'failed') and not r.get('parent') and not r.get('watch_retry')
                      and (r.get('ended') or 0) > self.since):
                    r['watch_retry'] = now
                    retry.append(r)
        self.flagged += len(stalled)
        self.retried += len(retry)
        self.events = (self.events + [{'id': r['id'], 'task': r['task'], 'board': r['board'], 'boardName': '', 'when': now,
                                       'status': 'stuck' if r in stalled else 'restarted'} for r in stalled + retry])[-200:]
        for r in stalled:
            self.runner.step(r, 'Watchdog: no progress for %d minutes' % STALL_MIN)
            self.runner.save(r)
            self.runner.changed(r['board'])
            self.push.send({'title': 'A team is stuck', 'body': '%s: no progress for %d minutes.' % (r['task'][:80], STALL_MIN),
                            'url': './#agents/' + r['id'], 'tag': 'stall-' + r['id']})
        for r in retry:
            why = r.get('error') or ([s['text'] for s in r.get('steps') or []] or ['it ran out of steps'])[-1]
            self.runner.follow_up(r['id'], RETRY_NOTE % str(why)[:200])


FIRST_RULES = """What matters to me (edit this note in your own words; the router reads it every time):
- A real person writing to me directly, a customer, a partner or a broker: note it.
- Anything asking me to do something, decide, sign, pay or reply by a date: suggest a task.
- A meeting added or moved in the next 3 days: note it; if it needs preparation, suggest a task to prepare.
- Newsletters, receipts, notifications, marketing and automatic mail: ignore."""
JUDGE = ('You sort what just came into the user\'s email and calendar. Follow their rules:\n\n%s\n\nFor each item answer with one '
         'object: {"id": the item id, "decision": "ignore" | "note" | "task", "summary": one plain sentence of what it is, '
         '"why": a few words on which rule decided it, "task": only for "task", what an agent team should do, starting with '
         'a verb, and never send, reply, pay or sign: a team prepares and the user decides}. Answer with a JSON list only.\n\n'
         'Items:\n%s')   # the router's prompt (also shown on its profile page)
SKIP = re.compile(r'no-?reply|notifications?@|mailer-daemon|newsletter|news@|updates?@|marketing@|info@', re.I)
COLORS = {'note': '#3b82f6', 'task': '#f59e0b'}


class Router:
    def __init__(self, data, board, read_board, edit_board, runner, push):
        self.dir = os.path.join(data, 'router')
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, 'state.json')
        self.board, self.read_board, self.edit_board, self.runner, self.push = board, read_board, edit_board, runner, push
        try:
            with open(self.state_path) as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}
        threading.Thread(target=self.loop, daemon=True).start()

    def save(self):
        with open(self.state_path + '.tmp', 'w') as f:
            json.dump(self.state, f)
        os.replace(self.state_path + '.tmp', self.state_path)

    def loop(self):
        time.sleep(30)
        while True:
            try:
                if (integrations.load().get('google') or {}).get('refresh_token'):
                    self.once()
            except Exception as e:   # Google or the model was away: try again next time
                print('router:', e, flush=True)
            time.sleep(180)

    # ---------- sources: what is new since last time ----------
    def new_mail(self):
        ids = [m['id'] for m in integrations._google('GET', 'https://gmail.googleapis.com/gmail/v1/users/me/messages?' + integrations._q(
            q='in:inbox -from:me newer_than:2d', maxResults=20)).get('messages') or []]
        seen = set(self.state.get('mail') or [])
        first = 'mail' not in self.state   # the first look only sets the starting point
        self.state['mail'] = (ids + [i for i in self.state.get('mail') or [] if i not in ids])[:500]
        out = []
        for mid in ids if not first else []:
            if mid in seen:
                continue
            x = integrations._google('GET', 'https://gmail.googleapis.com/gmail/v1/users/me/messages/%s?format=metadata'
                                     '&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date' % mid)
            sender = integrations._header(x, 'From')
            if SKIP.search(sender) or {'CATEGORY_PROMOTIONS', 'CATEGORY_SOCIAL'} & set(x.get('labelIds') or []):
                self.count('skipped')
                continue
            out.append({'id': 'mail:' + mid, 'kind': 'email', 'from': sender, 'subject': integrations._header(x, 'Subject'),
                        'date': integrations._header(x, 'Date'), 'text': x.get('snippet', ''),
                        'link': 'https://mail.google.com/mail/u/0/#all/' + mid})
        return out

    def new_events(self):
        iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + 'Z'
        now = time.time()
        ev = integrations._google('GET', 'https://www.googleapis.com/calendar/v3/calendars/primary/events?' + integrations._q(
            timeMin=iso(now), timeMax=iso(now + 3 * 86400), singleEvents='true', orderBy='startTime', maxResults=50)).get('items') or []
        seen = self.state.get('events') or {}
        first = 'events' not in self.state
        self.state['events'] = {e['id']: e.get('updated', '') for e in ev}
        out = []
        for e in ev if not first else []:
            if seen.get(e['id']) == e.get('updated', ''):
                continue
            start = (e.get('start') or {}).get('dateTime') or (e.get('start') or {}).get('date', '')
            out.append({'id': 'event:%s:%s' % (e['id'], e.get('updated', '')), 'kind': 'calendar event',
                        'change': 'moved or changed' if e['id'] in seen else 'added', 'subject': e.get('summary', '(no title)'),
                        'date': start, 'with': ', '.join(a.get('email', '') for a in e.get('attendees') or [])[:300],
                        'text': (e.get('description') or '')[:400], 'link': e.get('htmlLink', '')})
        return out

    # ---------- the judgement ----------
    def rules(self):
        b = self.read_board(self.board)
        note = next((i for i in b.get('items') or [] if i.get('id') == 'routerrules'), None)
        return (note or {}).get('text') or FIRST_RULES

    def judge(self, items):
        prompt = JUDGE % (self.rules() + skills.text('router'), json.dumps(items, ensure_ascii=False))
        url, name = models.local('agents') or (agents.MODEL_URL, agents.MODEL)   # your own machines only, never an outside provider
        body = {'model': name, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 2500, 'temperature': 0.1,
                'chat_template_kwargs': {'enable_thinking': False}}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=300) as f:
            text = json.load(f)['choices'][0]['message'].get('content') or ''
        m = re.search(r'\[.*\]', text, re.S)
        got = {d.get('id'): d for d in json.loads(m.group(0)) if isinstance(d, dict)} if m else {}
        return [(it, got.get(it['id']) or {'decision': 'note', 'summary': it['subject'], 'why': 'the model did not judge it'}) for it in items]

    # ---------- what the user sees ----------
    def count(self, what):
        day = time.strftime('%Y-%m-%d')
        c = self.state.setdefault('counts', {})
        if c.get('day') != day:
            c.clear()
            c['day'] = day
        c[what] = c.get(what, 0) + 1

    def once(self):
        items = self.new_mail() + self.new_events()
        self.state['last'] = int(time.time() * 1000)
        self.save()
        if not items:
            return
        cards = []
        for it, d in self.judge(items):
            dec = d.get('decision') if d.get('decision') in ('ignore', 'note', 'task') else 'note'
            self.count(dec)
            if dec == 'ignore':
                continue
            lines = ['%s: %s' % ('Email' if it['kind'] == 'email' else 'Calendar', it['subject'] or '(no subject)')]
            lines.append('From: %s' % it['from'] if it.get('from') else 'When: %s (%s)' % (it['date'], it.get('change', '')))
            lines.append('What: %s' % (d.get('summary') or it['text'][:200]))
            lines.append('Why: %s' % (d.get('why') or ''))
            if dec == 'task' and d.get('task'):
                lines.append('Suggested task: %s' % d['task'])
            lines.append(it['link'])
            cards.append((dec, d.get('task') if dec == 'task' else None, '\n'.join(lines), it))
        self.save()
        self.put(cards)   # also when all were ignored: the counts in the title change
        for dec, task, text, it in cards:
            if task:
                self.push.send({'title': 'Suggested task', 'body': '%s: %s' % (it['subject'][:60], task[:120]),
                                'url': './#' + self.board, 'tag': 'router-' + it['id'][:60]})

    def put(self, cards):
        c = self.state.get('counts') or {}

        def edit(board):
            items = board.setdefault('items', [])
            mine = [i for i in items if i.get('router')]
            for k, (dec, task, text, it) in enumerate(cards):
                n = len(mine) + k   # newest go on the next free spot, 4 to a row under the rules
                card = {'id': 'in' + secrets.token_hex(5), 'type': 'note', 'card': 1, 'grow': 'h', 'fs': 13, 'router': it['id'],
                        'color': COLORS[dec], 'text': text, 'x': 380 + (n % 4) * 340, 'y': 140 + (n // 4) * 260, 'w': 310, 'h': 220,
                        'z': 10 + n, 'why': {'by': 'Router', 'task': 'sorting new email and calendar events', 'when': int(time.time() * 1000)}}
                if task:
                    card['suggest'] = task
                items.append(card)
            for i in items:
                if i.get('id') == 'routertitle':
                    i['text'] = ('Incoming: the router reads new email and calendar events every few minutes and sorts them with the '
                                 'rules on the left. It only records; it never starts, replies or sends. Today: %d noted, %d suggested '
                                 'tasks, %d ignored.' % (c.get('note', 0), c.get('task', 0), c.get('ignore', 0) + c.get('skipped', 0)))
        self.edit_board(self.board, edit)
