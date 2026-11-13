"""The pieces of a big dropped file, taken by a process of their own: python3 uploads.py PORT DATA_DIR

A 1 GB video arrives as 20 MB pieces (PUT api/uploads/<id>?at=<offset>). server.py used to take them itself, in the same
process that answers the voice and runs the teams; a question asked during an upload waited behind them (2026-11-10:
33 s before the model was even called). Optional: a reverse proxy can send exactly that path here (a rule such as
path ^/api/uploads/[0-9a-f]{12}$ -> this port, placed before the canvas line) and server.py keeps the two small calls,
start (POST api/uploads) and done (POST api/uploads/<id>/done), reading the same DATA_DIR/uploads folder.
The rules are server.py's own PUT rules, kept the same here: a piece that did not arrive whole is refused, a piece sent
twice is not added twice, a hole answers with where to carry on.
"""
import json, os, re, sys, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT, DATA = int(sys.argv[1]), sys.argv[2]
UPLOADS = os.path.join(DATA, 'uploads')
PATH = re.compile(r'^/api/uploads/([0-9a-f]{12})$')
MAX_BODY, MAX_FILE = 25 * 1024 * 1024, 4 * 1024 ** 3


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        path, _, query = self.path.partition('?')
        m = PATH.match(path)
        part = os.path.join(UPLOADS, m.group(1)) if m else ''
        n = int(self.headers.get('Content-Length') or 0)
        if not m or not os.path.exists(part):
            self.close_connection = True   # the body is not read: do not take the next request from its middle
            return self.reply(404, {'error': 'no such upload'})
        if n > MAX_BODY:
            self.close_connection = True
            return self.reply(413, {'error': 'too large'})
        data = self.rfile.read(n)
        have = os.path.getsize(part)
        if len(data) != n:   # the connection broke mid-piece
            return self.reply(400, {'error': 'the piece did not arrive whole; send it again', 'size': have})
        at = (urllib.parse.parse_qs(query).get('at') or [None])[0]
        if at is not None:
            try:
                at = int(at)
            except ValueError:
                return self.reply(400, {'error': 'bad request'})
            if at < have:   # already here (the browser sent it twice)
                return self.reply(200, {'size': have})
            if at > have:   # a piece is missing before this one
                return self.reply(409, {'error': 'a piece of the file is missing; carry on from size', 'size': have})
        if have + len(data) > MAX_FILE:
            os.remove(part)
            return self.reply(413, {'error': 'too large'})
        with open(part, 'ab') as f:
            f.write(data)
        self.reply(200, {'size': os.path.getsize(part)})


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
