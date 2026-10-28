"""Rearranging a frame's cards for the voice: move one card to another place, sort the frame into themes, put it back.
The model only says which card goes where (or which cards make a theme); everything that moves is done here, in code,
so what the voice says it did is what the board shows."""
import json, os, re, secrets, time

import talk

HEAD_H, HEAD_FS = 56, 26   # a theme heading above its cards, in board units


def cards(board, frame):
    """The frame's cards in reading order: each a stack (a sticky note with the card under it), headings left out."""
    return talk.stacks([i for i in board.get('items') or [] if talk.inside(frame, i) and not i.get('agent') and not i.get('sortHead')])


def frame_text(board, frame, reader):
    """The frame as the voice sees it: its cards numbered in reading order, so "the fourth one in the top row" can be found."""
    cs = cards(board, frame)
    cols = len(grid(frame, cs)[0]) if cs else 0
    words = lambda i, n: ' '.join(talk.item_words(i, reader)[1].split()[:n])
    names = ['%d %s%s' % (k, words(s[0], 7), (' (card under it: %s)' % words(s[1], 6)) if len(s) > 1 else '') for k, s in enumerate(cs, 1)]
    return ('%d cards in rows of %d (place 1 is top left, then left to right, row by row; the next place is BESIDE, not below; '
            'each sticky note can have a card stacked under it). In order: %s' % (len(cs), cols, '; '.join(names)))


def grid(frame, cs):
    """Column lefts, row gap and the room below the last row, read from where the cards are now."""
    xs = []
    for x in sorted(s[0]['x'] for s in cs):
        if not xs or x - xs[-1] > 8:
            xs.append(x)
    rows = {}
    for s in cs:
        top = next((t for t in rows if abs(t - s[0]['y']) < 8), s[0]['y'])
        rows.setdefault(top, []).append(s)
    tops = sorted(rows)
    bottoms = [max(i['y'] + i['h'] for s in rows[t] for i in s) for t in tops]
    gaps = [tops[k + 1] - bottoms[k] for k in range(len(tops) - 1) if tops[k + 1] > bottoms[k]]
    pad = frame['y'] + frame['h'] - max(bottoms) if bottoms else 40
    return xs, (min(gaps) if gaps else 36), max(pad, 20)


def lay(board, frame, groups):
    """Put the cards back in the frame in this order: groups = [(heading or None, [stacks])], each group starting a new
    row under its heading. The frame grows (pushing what is below it down) when the cards need more room."""
    items = board['items']
    cs = [s for _, g in groups for s in g]
    xs, gap, pad = grid(frame, cs)
    heads = [i for i in items if i.get('sortHead') == frame['id']]
    y = min([s[0]['y'] for s in cs] + [h['y'] for h in heads])
    items[:] = [i for i in items if i.get('sortHead') != frame['id']]   # the last sort's headings make way for the new ones
    cw = max(i['x'] + i['w'] for s in cs for i in s) - xs[0]
    added, laid = [], {i['id'] for s in cs for i in s}
    for title, g in groups:
        if title:
            h = {'id': 'h' + secrets.token_hex(5), 'type': 'note', 'text': title, 'color': 'none', 'fs': HEAD_FS, 'fw': 700,
                 'grow': 'h', 'sortHead': frame['id'], 'agent': 'sort', 'x': xs[0], 'y': y, 'w': cw, 'h': HEAD_H,
                 'z': max([i.get('z', 1) for i in items] + [1]) + 1}
            items.append(h)
            added.append(h['id'])
            laid.add(h['id'])
            y += HEAD_H + 10
        for k in range(0, len(g), len(xs)):
            row = g[k:k + len(xs)]
            for col, s in enumerate(row):
                dx, dy = xs[col] - s[0]['x'], y - s[0]['y']
                for i in s:
                    i['x'], i['y'] = i['x'] + dx, i['y'] + dy
            y += max(i['y'] + i['h'] for s in row for i in s) - y + gap
    old_bottom = frame['y'] + frame['h']
    new_bottom = y - gap + pad
    if new_bottom > old_bottom:   # more rows than before: make room by moving everything below the frame down
        grow = new_bottom - old_bottom
        for i in items:
            if i is not frame and i['id'] not in laid and 'y' in i and i['y'] >= old_bottom - 1:
                i['y'] += grow
        frame['h'] += grow
    return added, heads


def keep_undo(data, bid, board, what):
    """Where everything was before a rearrangement, so "put it back" can undo it."""
    os.makedirs(os.path.join(data, 'undo'), exist_ok=True)
    pos = {i['id']: [i.get('x'), i.get('y'), i.get('w'), i.get('h')] for i in board.get('items') or [] if 'x' in i}
    heads = [i for i in board.get('items') or [] if i.get('sortHead')]   # an earlier sort's headings, which a new sort replaces
    with open(os.path.join(data, 'undo', bid + '.json'), 'w') as f:
        json.dump({'t': time.time(), 'what': what, 'pos': pos, 'heads': heads}, f)


def undo(data, bid, board):
    """Put the last rearrangement back. Returns what it was, or None."""
    path = os.path.join(data, 'undo', bid + '.json')
    try:
        with open(path) as f:
            u = json.load(f)
    except (OSError, ValueError):
        return None
    old = u.get('pos') or {}
    board['items'] = [i for i in board.get('items') or [] if not (i.get('sortHead') and i['id'] not in old)]
    have = {i['id'] for i in board['items']}
    board['items'] += [h for h in u.get('heads') or [] if h['id'] not in have]
    for i in board['items']:
        if i['id'] in old:
            i['x'], i['y'], i['w'], i['h'] = old[i['id']]
    os.remove(path)
    return u.get('what') or 'the last change'


def move(board, frame, frm, to, reader):
    """Card at place frm goes to place to; the cards between shift along one place. Returns (moved card's words, error)."""
    cs = cards(board, frame)
    if not cs:
        return None, 'That frame has no cards.'
    try:
        frm, to = int(frm), int(to)
    except (TypeError, ValueError):
        return None, 'I did not get which place the card should go to.'
    if not (1 <= frm <= len(cs) and 1 <= to <= len(cs)):
        return None, 'That frame has %d cards, so there is no place %d.' % (len(cs), max(frm, to))
    if any(i.get('sortHead') == frame['id'] for i in board['items']):   # its themes would be laid over
        return None, 'This frame is sorted into themes, so I did not move single cards in it. Say "put it back" first.'
    order = cs[:]
    card = order.pop(frm - 1)
    order.insert(to - 1, card)
    lay(board, frame, [(None, order)])
    return ' '.join(talk.item_words(card[0], reader)[1].split()[:8]), None


SORT = """You sort the cards of one frame on a canvas board into themes. Each card is a note someone saved, with what it
links to. Make between 3 and 8 themes, each with a short plain title (2 to 5 words, from what the cards are about). Every
card goes in exactly one theme; only a few odd ones (at most 5) go in "Other". No theme holds more than half the cards.
Put a card with the theme of what it is about, not of a word in it (a call about a contract is legal, not jobs).
Reply with JSON only: {"themes": [{"title": "...", "cards": [card numbers]}]}"""


def themes(board, frame, reader):
    """One model call: the frame's cards sorted into themes, checked here. Returns ([(title, [stacks])], error)."""
    cs = cards(board, frame)
    if len(cs) < 4:
        return None, 'That frame has too few cards to sort into themes.'
    lines = ['%d. %s' % (k, ' | '.join(' '.join(talk.item_words(i, reader)[1].split()[:18]) for i in s)) for k, s in enumerate(cs, 1)]
    note = ''
    for _ in range(2):
        out = talk.chat([{'role': 'system', 'content': SORT}, {'role': 'user', 'content': 'CARDS:\n' + '\n'.join(lines) + note}], 1500, timeout=120)
        groups, seen = [], set()
        for t in out.get('themes') or []:
            if not isinstance(t, dict):
                continue
            ks = []
            for k in t.get('cards') or []:
                try:
                    k = int(k)
                except (TypeError, ValueError):
                    continue
                if 1 <= k <= len(cs) and k not in seen:
                    seen.add(k)
                    ks.append(k)
            if ks and str(t.get('title') or '').strip():
                groups.append([str(t['title']).strip()[:60], ks])
        missing = [k for k in range(1, len(cs) + 1) if k not in seen]
        if missing:   # every card stays on the board: ones the model left out go under Other
            other = next((g for g in groups if g[0].lower() == 'other'), None)
            if other:
                other[1] += missing
            else:
                groups.append(['Other', missing])
        biggest = max((len(g[1]) for g in groups), default=0)
        if len(groups) >= 2 and biggest <= len(cs) * .6:
            return [(t, [cs[k - 1] for k in ks]) for t, ks in groups], None
        note = '\n\nYour last answer put %d of %d cards in one theme. Split them into smaller themes.' % (biggest, len(cs))
    return None, 'I could not get a sensible set of themes for that frame, so I left it as it was.'


# words that say the board changed: without a real edit they are replaced ("Done. The fourth card is back in place 4")
CLAIM = re.compile(r"^\W*done\b|\bI(?:'ve| have)? (?:just )?(?:moved|rearranged|reordered|sorted|grouped|swapped|put (?:it|them|the))\b|"
                   r"\b(?:is|are) (?:now |back )(?:in |grouped|sorted|moved|where|organi[sz]ed|arranged)", re.I)
