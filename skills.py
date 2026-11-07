"""Skills the user adds to an agent from its profile page (Upskill), in plain words: {agent key: [{'id', 'text', 'added'}]} in
DATA/skills.json. Every agent that runs on a model reads its own into its instructions: the teammates (agents/runner.py),
the Assistant (talk.py), the Router (router.py) and the Board brief writer (briefs.py). The server sets DATA at start."""
import json, os

DATA = None


def load():
    try:
        with open(os.path.join(DATA, 'skills.json')) as f:
            return json.load(f)
    except (OSError, TypeError, ValueError):
        return {}


def save(got):
    path = os.path.join(DATA, 'skills.json')
    with open(path + '.tmp', 'w') as f:
        json.dump(got, f, indent=1)
    os.replace(path + '.tmp', path)


def add(key, text, **meta):
    """A skill added by code (a proposal being tried, a note that proved itself): its id. Meta says where it came from."""
    got = load()
    x = {'id': 's' + os.urandom(3).hex(), 'text': ' '.join(str(text).split())[:400], 'added': int(__import__('time').time() * 1000), **meta}
    got[key] = (got.get(key) or []) + [x]
    save(got)
    return x['id']


def remove(key, skill_id):
    got = load()
    got[key] = [x for x in got.get(key) or [] if x.get('id') != skill_id]
    save(got)


def text(key):
    """The skills block for one agent's instructions ('' when it has none)."""
    got = load().get(key) or []
    return ('\n\nSKILLS THE USER ADDED (use them whenever they fit):\n' + '\n'.join('- ' + x['text'] for x in got)) if got else ''
