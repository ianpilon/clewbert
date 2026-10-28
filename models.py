"""Which model does which job, and the keys for outside providers (the Settings card).

Four jobs: voice (answers you, writes board briefs), agents (the usual teammates), builders (the Developer and the
designers), vision (looks at screenshots of demos). Each job runs on your own machines (the addresses set when the app
starts, as before) or on an outside provider with your key: OpenAI, xAI or Anthropic.

The rest of the app speaks one request shape (the chat/completions one the local servers use). OpenAI and xAI take it
as it is; for Anthropic it is translated to and from its own Messages API here (raw HTTP: the app has no outside
packages and runs on the Python that ships with macOS, older than the Anthropic SDK needs).

Keys live in settings.json in the data folder (owner-only), are never sent back to the browser, and go only to their
own provider.
"""
import base64, io, json, os, re, threading, urllib.error, urllib.request

SETTINGS = None   # set by server.py: <data>/settings.json


class NoModel(OSError):
    """No model is set up for this job yet: neither an address at startup nor a provider in Settings."""
    def __init__(self, job):
        super().__init__('No model is set up for %s yet. Open Settings (the gear on the home page) and pick one.' % job)
JOBS = ('voice', 'agents', 'builders', 'vision')
PROVIDERS = {
    'openai': {'name': 'OpenAI', 'url': 'https://api.openai.com/v1/chat/completions', 'models': 'https://api.openai.com/v1/models'},
    'xai': {'name': 'xAI (Grok)', 'url': 'https://api.x.ai/v1/chat/completions', 'models': 'https://api.x.ai/v1/models'},
    'anthropic': {'name': 'Anthropic', 'url': 'https://api.anthropic.com/v1/messages', 'models': 'https://api.anthropic.com/v1/models'},
}
# how hard Claude thinks per job: voice and vision answer fast, builders take their time
EFFORT = {'voice': 'low', 'vision': 'low', 'agents': 'medium', 'builders': 'high'}
# room for thinking plus the answer: a reasoning model spends part of max_tokens before it writes anything
FLOOR = {'voice': 4000, 'vision': 2000, 'agents': 8000, 'builders': 16000}
_lock = threading.Lock()
_cache = {'mtime': None, 'data': None}


def load():
    try:
        m = os.path.getmtime(SETTINGS)
    except (OSError, TypeError):
        return {'jobs': {}, 'keys': {}}
    if _cache['mtime'] != m:
        with open(SETTINGS) as f:
            _cache.update(mtime=m, data=json.load(f))
    return _cache['data']


def public():
    """What the Settings card shows: the choice per job, and for each key only whether it is set and its last 4 characters."""
    s = load()
    return {'jobs': {j: s.get('jobs', {}).get(j) or {'provider': 'local', 'model': ''} for j in JOBS},
            'keys': {p: ('…' + k[-4:]) if k else '' for p, k in ((p, s.get('keys', {}).get(p)) for p in PROVIDERS)},
            'providers': {p: v['name'] for p, v in PROVIDERS.items()}}


def save(change):
    """change = {'jobs': {job: {'provider', 'model'}}, 'keys': {provider: 'new key' | None to remove}}; keys left out stay."""
    with _lock:
        s = json.loads(json.dumps(load()))
        for j, v in (change.get('jobs') or {}).items():
            if j in JOBS and isinstance(v, dict) and v.get('provider') in ('local',) + tuple(PROVIDERS):
                s.setdefault('jobs', {})[j] = {'provider': v['provider'], 'model': str(v.get('model') or '').strip()[:100]}
                url = str(v.get('url') or '').strip()
                if v['provider'] == 'local' and v.get('model') and url.startswith('http'):   # a model on your own machines
                    s['jobs'][j]['url'] = url[:300]
        if isinstance(change.get('servers'), list):   # more of your own model servers to offer, beyond the startup ones
            s['servers'] = [str(u).strip().rstrip('/')[:300] for u in change['servers'] if str(u).strip().startswith('http')][:20]
        for p, k in (change.get('keys') or {}).items():
            if p in PROVIDERS:
                if k is None:
                    s.setdefault('keys', {}).pop(p, None)
                elif isinstance(k, str) and k.strip():
                    s.setdefault('keys', {})[p] = k.strip()
        tmp = SETTINGS + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, SETTINGS)
    return public()


def local(job):
    """(address, model) when this job uses a model picked on your own machines, else None (as set at startup)."""
    j = load().get('jobs', {}).get(job) or {}
    if j.get('provider') == 'local' and j.get('model') and j.get('url'):
        return j['url'], j['model']
    return None


def choice(job):
    """(provider, model, key) when this job runs on an outside provider, else None (your own machines)."""
    s = load()
    j = s.get('jobs', {}).get(job) or {}
    p, key = j.get('provider'), s.get('keys', {}).get(j.get('provider'))
    if p in PROVIDERS and key and j.get('model'):
        return p, j['model'], key
    return None


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=dict(headers, **{'Content-Type': 'application/json'}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return json.load(f)
    except urllib.error.HTTPError as e:   # the provider's own words: a wrong key or model name reads plainly
        try:
            msg = json.load(e).get('error', {})
            msg = msg.get('message') if isinstance(msg, dict) else str(msg)
        except ValueError:
            msg = ''
        raise OSError('%s answered %d%s' % (url.split('/')[2], e.code, ': ' + msg[:300] if msg else ''))


def complete(job, body, timeout=120):
    """Send one chat/completions-shaped request for this job to its provider; the reply comes back in the same shape."""
    p, model, key = choice(job)
    want = max(int(body.get('max_tokens') or 0), FLOOR[job])
    if p == 'anthropic':
        return _anthropic(job, model, key, body, want, timeout)
    out = {'model': model, 'messages': [_clean(m) for m in body['messages']]}
    if body.get('tools'):
        out['tools'] = body['tools']
    # no temperature: the reasoning models on both reject anything but their default
    out['max_completion_tokens' if p == 'openai' else 'max_tokens'] = want
    return _post(PROVIDERS[p]['url'], out, {'Authorization': 'Bearer ' + key}, timeout)


def _clean(m):
    """A message as OpenAI and xAI accept it: only the standard fields (the runner keeps its own notes on them)."""
    return {k: v for k, v in m.items() if k in ('role', 'content', 'tool_calls', 'tool_call_id', 'name')}


def _anthropic(job, model, key, body, want, timeout):
    system, msgs = [], []
    for m in body['messages']:
        role = m['role']
        if role == 'system':
            if not msgs:
                system.append(m.get('content') or '')
            else:   # a later instruction from the app: told as a note in the conversation
                msgs.append({'role': 'user', 'content': '[Note from the app] ' + (m.get('content') or '')})
        elif role == 'tool':
            msgs.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': str(m.get('content') or '')}]})
        elif role == 'assistant':
            msgs.append({'role': 'assistant', 'content': _assistant_blocks(m)})
        else:
            msgs.append({'role': 'user', 'content': _user_blocks(m.get('content'))})
    if not msgs or msgs[0]['role'] != 'user':
        msgs.insert(0, {'role': 'user', 'content': 'Begin.'})
    out = {'model': model, 'max_tokens': want, 'messages': msgs}
    if system:
        out['system'] = '\n\n'.join(system)
    if body.get('tools'):
        out['tools'] = [{'name': t['function']['name'], 'description': t['function'].get('description', ''),
                         'input_schema': t['function'].get('parameters') or {'type': 'object', 'properties': {}}} for t in body['tools']]
    family5 = model.startswith(('claude-opus-5', 'claude-sonnet-5', 'claude-haiku-5', 'claude-fable-5', 'claude-mythos-5'))
    if family5:
        out['output_config'] = {'effort': EFFORT[job]}
    headers = {'x-api-key': key, 'anthropic-version': '2023-06-01'}
    # a declined request is re-run on Anthropic's recommended model instead of coming back empty (no Haiku fallback exists)
    if family5 and not model.startswith('claude-haiku'):
        try:
            r = _post(PROVIDERS['anthropic']['url'], dict(out, fallbacks='default'), dict(headers, **{'anthropic-beta': 'server-side-fallback-2026-07-01'}), timeout)
        except OSError as e:
            if 'fallback' not in str(e).lower():
                raise
            r = _post(PROVIDERS['anthropic']['url'], out, headers, timeout)
    else:
        r = _post(PROVIDERS['anthropic']['url'], out, headers, timeout)
    blocks = [b for b in r.get('content') or [] if b.get('type') != 'fallback']
    text = '\n'.join(b['text'] for b in blocks if b.get('type') == 'text').strip()
    calls = [{'id': b['id'], 'type': 'function', 'function': {'name': b['name'], 'arguments': json.dumps(b.get('input') or {})}}
             for b in blocks if b.get('type') == 'tool_use']
    if r.get('stop_reason') == 'refusal':
        text, calls = 'The model declined this request.', []
    msg = {'role': 'assistant', 'content': text or None, '_raw': blocks}   # _raw: Claude's own blocks, thinking included, sent back as they were
    if calls:
        msg['tool_calls'] = calls
    return {'choices': [{'message': msg, 'finish_reason': 'length' if r.get('stop_reason') == 'max_tokens' else 'stop'}]}


def _assistant_blocks(m):
    calls = m.get('tool_calls') or []
    raw = m.get('_raw')
    if raw and {b['id'] for b in raw if b.get('type') == 'tool_use'} == {c['id'] for c in calls}:
        return raw
    blocks = [{'type': 'text', 'text': m['content']}] if m.get('content') else []
    for c in calls:
        try:
            args = json.loads(c['function'].get('arguments') or '{}')
        except ValueError:
            args = {}
        blocks.append({'type': 'tool_use', 'id': c['id'], 'name': c['function']['name'], 'input': args if isinstance(args, dict) else {}})
    return blocks or [{'type': 'text', 'text': '(no reply)'}]


def _user_blocks(content):
    if not isinstance(content, list):
        return content or '(empty)'
    out = []
    for c in content:
        if c.get('type') == 'text':
            out.append({'type': 'text', 'text': c['text']})
        elif c.get('type') == 'image_url':
            url = c['image_url']['url']
            if url.startswith('data:'):
                head, data = url.split(',', 1)
                out.append({'type': 'image', 'source': {'type': 'base64', 'media_type': head[5:].split(';')[0], 'data': data}})
            else:
                out.append({'type': 'image', 'source': {'type': 'url', 'url': url}})
    return out


def list_models(provider, newest=False):
    """The model names this key can use: A to Z for the Settings card's list, or newest first."""
    key = load().get('keys', {}).get(provider)
    if provider not in PROVIDERS or not key:
        return []
    headers = {'x-api-key': key, 'anthropic-version': '2023-06-01'} if provider == 'anthropic' else {'Authorization': 'Bearer ' + key}
    with _open(PROVIDERS[provider]['models'] + ('?limit=100' if provider == 'anthropic' else ''), None, headers, 15) as f:
        data = json.load(f).get('data', [])
    if newest:
        return [m['id'] for m in sorted(data, key=lambda m: str(m.get('created_at') or '') if provider == 'anthropic' else m.get('created') or 0, reverse=True)]
    return sorted(m['id'] for m in data)


def test(job):
    """One tiny request on this job's choice: the reply, or the provider's error in its own words."""
    if not choice(job):
        return {'ok': False, 'error': 'Pick a provider and a model, and save a key for that provider first.'}
    try:
        r = complete(job, {'messages': [{'role': 'user', 'content': 'Reply with the single word: ready'}], 'max_tokens': 20}, 60)
        return {'ok': True, 'reply': (r['choices'][0]['message'].get('content') or '').strip()[:80]}
    except (OSError, ValueError, KeyError) as e:
        return {'ok': False, 'error': str(e)[:300]}


# ---------- hearing and speaking: an OpenAI or xAI key also turns speech into text and text into speech ----------
SPEECH = {
    'openai': {'hear': 'https://api.openai.com/v1/audio/transcriptions', 'speak': 'https://api.openai.com/v1/audio/speech',
               'voices': ['coral', 'alloy', 'ash', 'ballad', 'echo', 'fable', 'nova', 'onyx', 'sage', 'shimmer', 'verse']},
    'xai': {'hear': 'https://api.x.ai/v1/stt', 'speak': 'https://api.x.ai/v1/tts', 'voices': 'https://api.x.ai/v1/tts/voices'},
}
RATE = 24000   # both give raw 16-bit PCM at this rate, which the page plays as it arrives


def speech():
    """(provider, key) for hearing and speaking: the voice job's provider when it can, else any saved key that can, else None."""
    s = load()
    keys = s.get('keys', {})
    for p in ((s.get('jobs', {}).get('voice') or {}).get('provider'), 'openai', 'xai'):
        if p in SPEECH and keys.get(p):
            return p, keys[p]
    return None


def _form(fields, wav):
    """multipart/form-data with the audio last (xAI requires the file to be the last field)."""
    b = 'clewbert' + os.urandom(8).hex()
    out = b''.join(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode() for k, v in fields.items())
    out += ('--%s\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\nContent-Type: audio/wav\r\n\r\n' % b).encode()
    return out + wav + ('\r\n--%s--\r\n' % b).encode(), 'multipart/form-data; boundary=' + b


def _open(url, data, headers, timeout):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get('error', {})
            msg = msg.get('message') if isinstance(msg, dict) else str(msg)
        except ValueError:
            msg = ''
        raise OSError('%s answered %d%s' % (url.split('/')[2], e.code, ': ' + msg[:300] if msg else ''))


def hear(wav):
    """The words in one spoken question (a WAV file from the page)."""
    p, key = speech()
    body, ctype = _form({'model': 'gpt-4o-mini-transcribe'} if p == 'openai' else {}, wav)
    with _open(SPEECH[p]['hear'], body, {'Authorization': 'Bearer ' + key, 'Content-Type': ctype}, 60) as f:
        return str(json.load(f).get('text') or '').strip()


def speak(text, voice):
    """A readable stream of raw 16-bit PCM at RATE: OpenAI streams it as it is made, xAI sends it whole."""
    p, key = speech()
    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
    if p == 'openai':
        body = {'model': 'gpt-4o-mini-tts', 'input': text, 'voice': voice or 'coral', 'response_format': 'pcm'}
        return _open(SPEECH[p]['speak'], json.dumps(body).encode(), headers, 120)
    body = {'text': text, 'voice_id': voice or 'eve', 'language': 'auto', 'output_format': {'codec': 'pcm', 'sample_rate': RATE}}
    with _open(SPEECH[p]['speak'], json.dumps(body).encode(), headers, 120) as f:
        data, json_reply = f.read(), 'json' in (f.headers.get('Content-Type') or '')
    return io.BytesIO(base64.b64decode(json.loads(data)['audio']) if json_reply else data)


def voices():
    """{'voices': [{'id', 'name'}]} for the voice card's list; the first one is the default."""
    p, key = speech()
    if p == 'openai':
        return {'voices': [{'id': v, 'name': v.title()} for v in SPEECH['openai']['voices']]}
    with _open(SPEECH['xai']['voices'], None, {'Authorization': 'Bearer ' + key}, 15) as f:
        vs = [{'id': v['voice_id'], 'name': v.get('name') or v['voice_id']} for v in json.load(f).get('voices', [])]
    return {'voices': sorted(vs, key=lambda v: v['id'] != 'eve')}


# ---------- first run: one key sets up every job ----------
# per provider, the kinds of model each job gets, best first; a job takes the newest model that matches
PICK = {
    'openai': {'fast': [r'^gpt-\d+(\.\d+)?-mini$', r'^gpt-4o-mini$'], 'strong': [r'^gpt-\d+(\.\d+)?$', r'^gpt-4o$']},
    'xai': {'fast': [r'non-reasoning$', r'^grok-[\d.]+-fast', r'^grok-[\d.]+-mini'], 'strong': [r'^grok-\d+(\.\d+)?$']},   # voice: no thinking, ~1 s
    'anthropic': {'fast': [r'^claude-haiku-'], 'strong': [r'^claude-sonnet-'], 'best': [r'^claude-opus-', r'^claude-sonnet-']},
}
JOB_KIND = {'voice': 'fast', 'vision': 'fast', 'agents': 'strong', 'builders': 'best'}
NOT_CHAT = re.compile(r'audio|realtime|tts|transcribe|search|image|imagine|embed|instruct|moderation|codex|nano|video|voice')


def pick(provider, ids):
    """{job: model} from the model names this key can use, newest first."""
    ids = [i for i in ids if not NOT_CHAT.search(i) and not i.startswith('grok-build')]
    if provider == 'openai':   # dated snapshots (gpt-4o-2024-08-06) duplicate the plain names
        ids = [i for i in ids if not re.search(r'\d{4}-\d{2}-\d{2}$', i)]
    out = {}
    for job, kind in JOB_KIND.items():
        pats = PICK[provider].get(kind, []) + PICK[provider]['strong']   # no fast model: the strong one does it
        out[job] = next((i for pat in pats for i in ids if re.search(pat, i)), ids[0] if ids else '')
    return out


def setup(provider, key):
    """Save the key, give every job a model from it, and prove it answers; on any failure nothing is kept."""
    if provider not in PROVIDERS or not key.strip():
        return {'ok': False, 'error': 'Pick a provider and paste its key.'}
    before = json.loads(json.dumps(load()))
    save({'keys': {provider: key}})
    try:
        chosen = pick(provider, list_models(provider, newest=True))
        if not chosen['voice']:
            raise OSError('This key has no chat models.')
        save({'jobs': {j: {'provider': provider, 'model': m} for j, m in chosen.items()}})
        r = test('voice')
        if not r['ok']:
            raise OSError(r['error'])
    except (OSError, ValueError, KeyError) as e:
        with _lock:
            tmp = SETTINGS + '.tmp'
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(before, f, indent=1)
            os.replace(tmp, SETTINGS)
        return {'ok': False, 'error': plain_error(str(e))}
    return {'ok': True, 'models': chosen, 'speech': provider in SPEECH}


def plain_error(e):
    """The provider's error, said so anyone can act on it."""
    low = e.lower()
    if ' 401' in low or 'invalid' in low and 'key' in low or 'incorrect api key' in low:
        return 'That key was not accepted. Check that you copied all of it.'
    if ' 402' in low or ' 429' in low and ('quota' in low or 'credit' in low or 'billing' in low) or 'credit' in low:
        return 'The key works, but the account has no credit left. Add credit with the provider, then try again.'
    if ' 403' in low:
        return 'The key works, but it is not allowed to use these models. Check its permissions with the provider.'
    if 'timed out' in low or 'urlopen error' in low:
        return 'Could not reach the provider. Check the internet connection and try again.'
    return e[:300]
