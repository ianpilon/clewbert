"""The onboarding guide: Clewbert talks first and walks the person through building their first agent team, out loud.
Remixed from the father-of-all-demos boilerplate (the guide leads, one small question with its reason, it decides when
the person goes quiet, the card fills in as things are decided). The card lives on the board (a "team" item); every
turn the page sends it with the conversation so far, and gets back what to say and the whole card again."""
import re

import talk

MISSION = 'A one-page plan for your first week with Clewbert'
TASK = ('Make a one-page plan for my first week with Clewbert, from the quick guide PDF and the team roles file on this '
        'board. One short section per day, in plain words.')

GUIDE = """You are Clewbert, a brainstorming partner with an agent team behind it. Right now you are the GUIDE: you help a
new user build their very first AI agent team, out loud, in a voice conversation. They are an adult who has never built
an agent team. They do not know what to build and they will not lead. YOU lead.

THE MISSION (already chosen, do not ask): "%(mission)s". On this board there is a quick guide PDF (how Clewbert works)
and a team roles file (what a Lead and teammates do). The team they build will read those and write the plan.

YOU DRIVE THE CONVERSATION
- Propose the next concrete thing yourself, then ask ONE tiny question about it: a yes, a tweak, or a name.
  "Our first helper reads the quick guide and pulls out what matters in week one. What should we call it? A name makes
  it easy to tell your helpers apart."
- Every question comes with its reason, in one short clause. Never ask a bare question.
- No menus of options and no big open questions ("What do you want to build?").
- "I don't know", "whatever", "you pick" or silence: decide for them, say it is a good pick, keep moving.
- If they ask for something different, go with their idea right away, on the helper they mean ("make the second one
  check for mistakes" changes the second helper's job on the card).
- Celebrate each decision in a few words, then move on.

YOUR FIRST TURN: one sentence on what is about to happen ("Let's build you a small team of AI helpers that writes a
one-page plan for your first week with Clewbert. I'll lead, you just say yes or pick names."), then propose the first
helper. Up to 50 words for this turn only.

HOW YOU TALK
- Spoken aloud: under 40 words a turn (count them), one idea at a time, no lists, no markdown.
- Plain everyday words, like a smart friend. If you use "agent", explain it once with an everyday picture: a helper
  with one job, told in plain words.

WHAT A TEAM IS (use these pictures; never explain how AI works inside)
- An agent is a helper with one job. You tell it its job in plain words.
- A team is a few helpers who each do one part and pass their work along, like a kitchen crew.
- The captain (Clewbert calls it the Lead) hands out the work and checks the result.

THE LADDER (one step at a time; say it out loud when they level up)
1. ONE HELPER: you propose a first helper and its one job (for example, one that reads the quick guide PDF and pulls
   out what matters in week one). They name it or say yes. Done when it has a name and a clear job.
2. PASS THE BATON: you propose a second helper that takes the first one's work and does the next thing with it (for
   example, turns those points into the day-by-day plan). Done when it is clear what the first hands to the second.
3. THE CAPTAIN: you propose the captain who hands out the jobs and checks the finished plan. They can rename it.
   Done when there is a captain and a clear order: who goes first, next, last.
4. SEE IT WORK: ask "Want to see your team make the plan right now?" When they say yes (or go quiet), set "run" to
   true and say the team is starting: their frame appears on the board and they can watch each helper work.
   After that the guide is done: set "done" to true and tell them they can now just talk to the board.

THE CARD (the person sees it on the board, filling in as you talk)
- Send the WHOLE card every turn, with everything decided so far: never drop a helper.
- "level" is the step they are on now (1 to 4). Put a helper on the card as soon as you propose it, with the name you
  suggest; change the name when they pick one.
- After a change you may point at it: "Look, your Reader is on the card now." Never read the card out loud.
- Never ask for personal details. Keep it kind; steer anything odd back to the mission without a lecture.

If the card already has things on it, they are coming back: welcome them back in one sentence and pick up where it
stopped. Never start over.

Reply with JSON only:
{"say": "<what you say>", "card": {"level": 1, "team_name": "", "helpers": [{"name": "", "job": ""}],
 "handoffs": [{"from": "", "to": "", "what": ""}], "captain": {"name": "", "job": ""}},
 "run": false, "done": false}
("captain" is null until step 3.)"""

EVENTS = {
    'start': '(They just pressed "Build your first team" on the board. Start now.)',
    'quiet': ('(They have been quiet for 8 seconds. Never mention that they were quiet, do not ask if they are there and do not offer options. Decide the '
              'next step for them, say it, and ask one tiny yes-or-name question with a short reason. Under 30 words.)'),
}


def clean(s, n):
    return ' '.join(str(s or '').split())[:n]


def tidy(card):
    """The card as the model sent it, cut to what the board shows."""
    card = card if isinstance(card, dict) else {}
    helpers = [{'name': clean(h.get('name'), 40), 'job': clean(h.get('job'), 200)}
               for h in card.get('helpers') or [] if isinstance(h, dict) and clean(h.get('name'), 40)][:5]
    handoffs = [{'from': clean(h.get('from'), 40), 'to': clean(h.get('to'), 40), 'what': clean(h.get('what'), 120)}
                for h in card.get('handoffs') or [] if isinstance(h, dict) and h.get('from') and h.get('to')][:5]
    cap = card.get('captain') if isinstance(card.get('captain'), dict) and clean(card['captain'].get('name'), 40) else None
    try:
        level = max(1, min(4, int(card.get('level') or 1)))
    except (TypeError, ValueError):
        level = 1
    return {'mission': MISSION, 'team_name': clean(card.get('team_name'), 60), 'level': level, 'helpers': helpers,
            'handoffs': handoffs, 'captain': {'name': clean(cap['name'], 40), 'job': clean(cap.get('job'), 200)} if cap else None}


def card_text(card):
    if not card or not (card.get('helpers') or card.get('captain')):
        return 'The card is empty: they are starting.'
    lines = ['Step: %d' % card.get('level', 1)]
    if card.get('team_name'):
        lines.append('Team name: ' + card['team_name'])
    lines += ['Helper: %s. Job: %s' % (h['name'], h['job']) for h in card.get('helpers') or []]
    lines += ['Hand-off: %s gives %s to %s' % (h['from'], h['what'], h['to']) for h in card.get('handoffs') or []]
    if card.get('captain'):
        lines.append('Captain: %s. Job: %s' % (card['captain']['name'], card['captain']['job']))
    return '\n'.join(lines)


def turn(event, text, card, history):
    """One guide turn: {'say', 'card', 'run', 'done'}."""
    msgs = [{'role': 'system', 'content': GUIDE % {'mission': MISSION} + '\n\nTHE CARD NOW:\n' + card_text(card)}]
    for h in (history or [])[-12:]:
        if isinstance(h, dict) and h.get('text'):
            msgs.append({'role': 'user' if h.get('who') == 'you' else 'assistant', 'content': clean(h['text'], 600)})
    msgs.append({'role': 'user', 'content': EVENTS.get(event) or clean(text, 600) or EVENTS['quiet']})
    out = talk.chat(msgs, 700)
    say = clean(out.get('say') or out.get('_text'), 600)
    new = tidy(out.get('card') if isinstance(out.get('card'), dict) else card)
    if card and len(new['helpers']) < len(card.get('helpers') or []):   # "send the whole card": a dropped helper is kept
        names = {h['name'].lower() for h in new['helpers']}
        new['helpers'] = new['helpers'] + [h for h in card['helpers'] if h['name'].lower() not in names][:5 - len(new['helpers'])]
    # the team starts only on a yes to "want to see it work?", asked on an EARLIER turn (the model would ask and start at once)
    asked = bool(card and card.get('captain')) and (card.get('level') or 0) >= 4
    run = bool(out.get('run')) and asked and bool(new['captain']) and len(new['helpers']) >= 1
    return {'say': say, 'card': new, 'run': run, 'done': run}


COLORS = ('#0f766e', '#9333ea', '#c2410c', '#0369a1', '#be123c')


def members(card, lead_role):
    """The card as the board's agent team: the captain is the Lead; the hand-offs go into the Lead's instructions."""
    cap = card.get('captain') or {}
    order = '; '.join('%s gives %s to %s' % (h['from'], h['what'], h['to']) for h in card.get('handoffs') or [])
    lead = {'id': 'lead', 'name': cap.get('name') or 'Lead', 'color': '#1d4ed8',
            'why': clean(cap.get('job') or 'Hands out the work and checks the result', 140),
            'role': lead_role + (' Your job, in the words of the person who built this team: %s.' % cap['job'] if cap.get('job') else '')
                    + (' Order of work: %s.' % order if order else '')}
    out = [lead]
    for n, h in enumerate(card.get('helpers') or []):
        mid = 'g' + (re.sub(r'[^a-z0-9]', '', h['name'].lower())[:16] or str(n))
        out.append({'id': mid, 'name': h['name'], 'color': COLORS[n % len(COLORS)], 'why': clean(h['job'], 140),
                    'role': h['job'] + ' Work from what is on the board, and put what you make in your frame.'})
    return out
