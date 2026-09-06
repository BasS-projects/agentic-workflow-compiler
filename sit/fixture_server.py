"""Local vendor portal and transient HTTP outage fixture, in its own process."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import urllib.parse


FORM = b'''<!doctype html><html lang="en"><meta charset="utf-8">
<title>SIT vendor quote intake</title><body>
<h1>Vendor quote intake</h1><form id="quote">
<label>Vendor <input id="vendor" required></label>
<label>Amount THB <input id="amount" type="number" step="0.01" required></label>
<button id="submit" type="submit">Submit quote</button></form>
<p id="receipt" role="status">No quote submitted</p>
<a id="download" href="/receipt.txt" download="receipt.txt">Download receipt</a>
<script>document.querySelector('#quote').addEventListener('submit', async (event) => {
event.preventDefault();
const response = await fetch('/quote', {method:'POST', headers:{'Content-Type':'application/json'},
body:JSON.stringify({vendor:document.querySelector('#vendor').value,amount:document.querySelector('#amount').value})});
const result=await response.json(); document.querySelector('#receipt').textContent=result.receipt;
});</script></body></html>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    counts = {}
    deliveries = {}
    delivery_attempts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def send(self, body, status=200, content_type='application/json'):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path)
            if path.path == '/healthz':
                return self.send({'ok': True})
            if path.path == '/vendor-quote':
                return self.send(FORM, content_type='text/html; charset=utf-8')
            if path.path == '/receipt.txt':
                receipt = args.state / 'receipt.txt'
                return self.send(receipt.read_bytes() if receipt.exists() else b'No receipt',
                                 content_type='text/plain')
            if path.path == '/retry':
                key = urllib.parse.parse_qs(path.query).get('channel', ['default'])[0]
                with lock:
                    counts[key] = counts.get(key, 0) + 1
                    attempt = counts[key]
                    (args.state / 'request-counts.json').write_text(json.dumps(counts))
                if attempt == 1:
                    return self.send({'error': 'temporary vendor outage'}, status=503)
                return self.send({'quote_id': 'Q-2026-001', 'amount_thb': 1250, 'attempt': attempt})
            return self.send({'error': 'not found'}, status=404)

        def do_POST(self):
            if self.path == '/publish':
                data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                key = self.headers.get('Idempotency-Key')
                if not key:
                    return self.send({'error': 'idempotency key required'}, status=400)
                with lock:
                    delivery_attempts.append({'key': key, 'invoice': data['invoice']})
                    deliveries.setdefault(key, {'invoice': data['invoice'], 'receipt': 'DELIVERED-001'})
                    (args.state / 'delivery-ledger.json').write_text(json.dumps({
                        'attempts': delivery_attempts, 'deliveries': list(deliveries.values())}))
                    result = deliveries[key]
                return self.send(result)
            if self.path != '/quote':
                return self.send({'error': 'not found'}, status=404)
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            receipt = f"Accepted {data['vendor']} quote: {float(data['amount']):.2f} THB"
            (args.state / 'receipt.txt').write_text(receipt, encoding='utf-8')
            self.send({'receipt': receipt})

    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
