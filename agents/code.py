"""A team's code workspace: its own git branch of one of the user's code bases, on this machine.

The engineer reads, searches, edits, checks and commits there. There is no shell, no push and no deploy: the branch
stays here until the user (or a coding assistant for them) reviews it. Code bases are bare repos in <data>/code/<name>.git, put there by
tools/deploy/push-agent-repos.sh; each run gets a worktree in <data>/code/work/<run id>-<name> on branch team/<run id>.
"""
import ast, json, os, re, subprocess

MAX_FILE = 400_000      # bytes an engineer may write in one file
ME = ['-c', 'user.name=Clewbert team', '-c', 'user.email=team@clewbert.local']


def root(data_dir):
    return os.path.join(data_dir, 'code')


def repos(data_dir):
    """The code bases a team may work on, by name."""
    try:
        return sorted(n[:-4] for n in os.listdir(root(data_dir)) if n.endswith('.git'))
    except OSError:
        return []


def git(where, *args, timeout=60):
    p = subprocess.run(['git', '-C', where] + list(args), capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise ValueError((p.stderr or p.stdout).strip()[:400])
    return p.stdout


def branch(rid):
    return 'team/' + rid


def workspace(data_dir, repo, rid):
    """The run's worktree of that code base (made on first use, from main)."""
    if repo not in repos(data_dir):
        raise ValueError('no code base called %r; there are: %s' % (repo, ', '.join(repos(data_dir)) or 'none'))
    bare = os.path.join(root(data_dir), repo + '.git')
    work = os.path.join(root(data_dir), 'work', '%s-%s' % (rid, repo))
    if not os.path.isdir(work):
        os.makedirs(os.path.dirname(work), exist_ok=True)
        git(bare, 'worktree', 'add', '-b', branch(rid), work, 'main')
    return work


def inside(work, path):
    """The real path of a file in the workspace, or ValueError: never outside it, never in .git."""
    path = str(path or '').strip().lstrip('/')
    full = os.path.realpath(os.path.join(work, path))
    base = os.path.realpath(work)
    if not path or not full.startswith(base + os.sep) or '/.git/' in '/' + full[len(base) + 1:] + '/':
        raise ValueError('that path is outside the code base')
    return full


def files(work, prefix=''):
    names = git(work, 'ls-files').splitlines()
    prefix = str(prefix or '').strip().lstrip('/')
    hits = [n for n in names if n.startswith(prefix)] if prefix else names
    return '%d files%s:\n%s' % (len(hits), ' under ' + prefix if prefix else '', '\n'.join(hits[:300])) + (
        '\n(first 300 shown; give a folder to see the rest)' if len(hits) > 300 else '')


def search(work, words):
    words = str(words or '').strip()
    if not words:
        raise ValueError('give the words to look for')
    try:
        out = git(work, 'grep', '-n', '-I', '-F', '-e', words)
    except ValueError:
        return 'Nothing in the code base matches "%s".' % words
    lines = out.splitlines()
    return '\n'.join(l[:240] for l in lines[:60]) + ('\n(%d more matches)' % (len(lines) - 60) if len(lines) > 60 else '')


def read(work, path, start=1, count=150):
    full = inside(work, path)
    with open(full, errors='replace') as f:
        lines = f.read().split('\n')
    start = max(1, int(start or 1))
    count = max(1, min(int(count or 150), 400))
    shown = lines[start - 1:start - 1 + count]
    return '%s, lines %d to %d of %d:\n%s' % (path, start, start + len(shown) - 1, len(lines),
                                             '\n'.join('%d: %s' % (start + k, l) for k, l in enumerate(shown)))


def edit(work, path, old, new):
    """Replace one exact stretch of a file. old must appear exactly once."""
    full = inside(work, path)
    with open(full) as f:
        text = f.read()
    n = text.count(old) if old else 0
    if n != 1:
        raise ValueError('the old text appears %d times in %s; it must appear exactly once (copy it from code_read, '
                         'with a little more around it)' % (n, path))
    with open(full, 'w') as f:
        f.write(text.replace(old, new, 1))
    return 'Changed %s.' % path


def write(work, path, content):
    """A new file, or a whole file replaced."""
    full = inside(work, path)
    if len(content.encode()) > MAX_FILE:
        raise ValueError('that file is too big to write in one go')
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w') as f:
        f.write(content)
    return 'Wrote %s.' % path


def changed(work):
    """Files changed on this branch so far, committed or not."""
    names = set(git(work, 'diff', '--name-only', 'main').splitlines()) | set(git(work, 'ls-files', '--others', '--exclude-standard').splitlines())
    return sorted(n for n in names if n)


def check(work):
    """Syntax checks of the changed files (nothing is run): Python parsed, JSON loaded. -> (all good, report)."""
    out, ok = [], True
    for n in changed(work):
        full = os.path.join(work, n)
        if not os.path.isfile(full):
            out.append('%s: deleted' % n)
            continue
        try:
            with open(full) as f:
                text = f.read()
            if n.endswith('.py'):
                ast.parse(text, n)
                out.append('%s: Python parses' % n)
            elif n.endswith('.json'):
                json.loads(text)
                out.append('%s: JSON loads' % n)
            else:
                out.append('%s: no syntax check for this kind of file' % n)
        except SyntaxError as e:
            ok = False
            out.append('%s: SYNTAX ERROR line %s: %s' % (n, e.lineno, e.msg))
        except ValueError as e:
            ok = False
            out.append('%s: BROKEN JSON: %s' % (n, e))
    return ok, '\n'.join(out) or 'No files changed yet.'


def commit(work, message):
    git(work, 'add', '-A')
    if not git(work, 'status', '--porcelain').strip():
        return 'Nothing to commit.'
    git(work, *ME, 'commit', '-q', '-m', str(message or 'Team change')[:300])
    return 'Committed: ' + git(work, 'log', '-1', '--format=%h %s').strip()


def summary(work):
    """What the branch changes against main: the files with lines added and taken out, and the whole diff."""
    stat = git(work, 'diff', '--stat', 'main...HEAD').strip()
    diff = git(work, 'diff', 'main...HEAD')
    return stat, diff
