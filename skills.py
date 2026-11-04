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


def text(key):
    """The skills block for one agent's instructions ('' when it has none)."""
    got = load().get(key) or []
    return ('\n\nSKILLS THE USER ADDED (use them whenever they fit):\n' + '\n'.join('- ' + x['text'] for x in got)) if got else ''
