"""A shell for the engineer, shut in: commands run in the team's worktree under macOS sandbox-exec.

What a command can do: read and write the worktree and its own scratch folder, read the code base's git history, run
the system's tools (python3, git, grep, the JavaScript checker jsc ...). What it cannot: use the network, read anything
else under /Users or /Volumes (the boards, keys, other apps), write anywhere else, signal or inspect other processes,
or reach the system services that start programs or hold passwords. It gets a bare environment (none of the server's
settings) and 60 seconds. Everything typed and printed is appended to <data>/terms/<run id>.log, which the board shows
as the agent's terminal. 2026-11-11."""
import os, re, signal, subprocess, time

TIMEOUT = 60            # seconds one command may run
MAX_OUT = 200_000       # bytes of output kept from one command
MAX_LOG = 400_000       # bytes of terminal log kept per run (the oldest part is dropped)
BACK = 6000             # characters of output the agent gets back
JSC = '/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc'
try:   # the real python3 and git: the ones in /usr/bin are shims that start xcodebuild (1.5 s and warnings each time)
    TOOLS = os.path.dirname(subprocess.run(['/usr/bin/xcrun', '-f', 'python3'], capture_output=True, text=True, timeout=20).stdout.strip())
except (OSError, subprocess.SubprocessError):
    TOOLS = ''

PROFILE = """(version 1)
(allow default)
(deny network*)
(deny mach-lookup)
(deny signal)
(allow signal (target same-sandbox))
(deny process-info* (target others))
(deny file-write*)
(allow file-write* (subpath "{work}") (subpath "{tmp}")
  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (literal "/dev/dtracehelper") (regex #"^/dev/fd/"))
(deny file-read* (subpath "/Users") (subpath "/Volumes") (subpath "/private/var/root") (subpath "/Library/Keychains")
  (subpath "/private/tmp") (subpath "/private/var/folders") (subpath "/private/var/tmp"))
(allow file-read-metadata (subpath "/Users"))
(allow file-read* (subpath "{work}") (subpath "{repo}") (subpath "{tmp}"))
(deny process-exec (literal "/bin/launchctl") (literal "/usr/bin/osascript") (literal "/usr/bin/open")
  (literal "/usr/bin/security") (literal "/usr/bin/ssh") (literal "/usr/bin/sudo") (literal "/usr/bin/su")
  (literal "/usr/bin/at") (literal "/usr/bin/crontab") (literal "/usr/bin/sandbox-exec"))
"""


def log_path(data_dir, rid):
    return os.path.join(data_dir, 'terms', rid + '.log')


def _append(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a') as f:
        f.write(text)
    if os.path.getsize(path) > MAX_LOG:   # keep the newest part, from a line start
        with open(path, 'rb') as f:
            f.seek(-MAX_LOG // 2, os.SEEK_END)
            keep = f.read().split(b'\n', 1)[-1]
        with open(path + '.tmp', 'wb') as f:
            f.write(b'[earlier lines dropped]\n' + keep)
        os.replace(path + '.tmp', path)


def run(data_dir, rid, work, command, shown=None):
    """Run one command line in the worktree, shut in. -> (exit code or None on timeout, output as the agent sees it).
    shown: the line the terminal shows in place of the command (the smoke test)."""
    command = str(command or '').strip()
    if not command:
        raise ValueError('no command given')
    if len(command) > 4000:
        raise ValueError('the command is too long (4,000 characters at most): put it in a file with code_write and run the file')
    work = os.path.realpath(work)
    with open(os.path.join(work, '.git')) as f:   # a worktree's .git is a file that names the code base it belongs to
        repo = os.path.realpath(re.sub(r'/worktrees/[^/]+$', '', f.read().split('gitdir:', 1)[1].strip()))
    tmp = os.path.realpath(os.path.join(data_dir, 'terms', 'tmp-' + rid))
    os.makedirs(tmp, exist_ok=True)
    profile = PROFILE.format(work=work, repo=repo, tmp=tmp)
    env = {'PATH': (TOOLS + ':' if TOOLS else '') + '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': tmp, 'TMPDIR': tmp, 'LANG': 'en_US.UTF-8', 'TERM': 'dumb',
           'PYTHONDONTWRITEBYTECODE': '1', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'JSC': JSC}
    log = log_path(data_dir, rid)
    _append(log, '$ %s\n' % (shown or command))
    t0 = time.time()
    p = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile, '/bin/bash', '-c', command], cwd=work, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=TIMEOUT)
        code = p.returncode
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        code = None
    text = out[:MAX_OUT].decode('utf-8', 'replace')
    text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', text).replace('\r\n', '\n').replace('\r', '\n')   # no colour codes: the card shows plain text
    if len(out) > MAX_OUT:
        text += '\n[output cut at %d KB]' % (MAX_OUT // 1000)
    tail = ('' if not text or text.endswith('\n') else '\n') + ('[stopped after %d s: took too long]\n' % TIMEOUT if code is None
                                                                else '[exit %d]\n' % code if code else '')
    _append(log, text + tail)
    seen = text if len(text) <= BACK else text[:BACK // 3] + '\n[... %d characters left out ...]\n' % (len(text) - BACK) + text[-2 * BACK // 3:]
    return code, '%s%s(%s, %.1f s)' % (seen, tail, 'timed out' if code is None else 'exit %d' % code, time.time() - t0)


SMOKE = r'''import json, os, re, subprocess, sys
files = [f for f in subprocess.run(['git', 'ls-files'], capture_output=True, text=True).stdout.split('\n') if f and os.path.isfile(f)]
jsc, tmp, n, bad = os.environ['JSC'], os.environ['TMPDIR'], {'py': 0, 'js': 0, 'json': 0}, []
def js(src, label):
    if re.search(r'^\s*(import|export)\s', src, re.M):   # a module: Function() cannot parse it
        return
    p = os.path.join(tmp, 'smoke.js')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(src)
    r = subprocess.run([jsc, '-e', "new Function(read('%s'))" % p], capture_output=True, text=True, timeout=30)
    if r.returncode:
        bad.append('%s: %s' % (label, (r.stdout + r.stderr).strip().split('\n')[0][:200]))
    else:
        n['js'] += 1
for f in files[:3000]:
    try:
        if f.endswith('.py'):
            compile(open(f, encoding='utf-8').read(), f, 'exec'); n['py'] += 1
        elif f.endswith('.json'):
            json.load(open(f, encoding='utf-8')); n['json'] += 1
        elif f.endswith('.js') and not f.endswith('.min.js') and os.path.getsize(f) < 3000000:
            js(open(f, encoding='utf-8').read(), f)
        elif f.endswith(('.html', '.htm')):
            for i, m in enumerate(re.finditer(r'<script(\s[^>]*)?>(.*?)</script>', open(f, encoding='utf-8').read(), re.S | re.I)):
                attrs = m.group(1) or ''
                if 'src=' in attrs or ('type=' in attrs and 'javascript' not in attrs) or not m.group(2).strip():
                    continue
                js(m.group(2), '%s, script block %d' % (f, i + 1))
    except SyntaxError as e:
        bad.append('%s: line %s: %s' % (f, e.lineno, e.msg))
    except ValueError as e:
        bad.append('%s: %s' % (f, str(e)[:160]))
print('%d Python files compile, %d scripts parse, %d JSON files load' % (n['py'], n['js'], n['json']))
if bad:
    print('BROKEN (%d):' % len(bad)); print('\n'.join(bad[:30])); sys.exit(1)
'''


def smoke(data_dir, rid, work):
    """The smoke test an engineer's work starts with: every tracked Python file compiles, every script (a .js file, a
    script block in a page) parses, every JSON file loads. Nothing is run. -> (passed, what it printed)."""
    tmp = os.path.join(data_dir, 'terms', 'tmp-' + rid)
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, 'smoke.py'), 'w') as f:
        f.write(SMOKE)
    code, out = run(data_dir, rid, work, 'python3 "$TMPDIR/smoke.py"', shown='smoke test: every Python file compiles, every script parses, every JSON file loads')
    return code == 0, out
