"""Serve sentence embeddings to the aligner from a machine that keeps the model loaded.

The aligner spends nearly all its time in the encoder, and on a laptop CPU the
first alignment in a process also pays to import torch and load the weights.
Run this on a faster machine on the LAN and point ``alignment.embed_url`` in
``app_config.json`` at it; ``src/embed_client.py`` is the other end.

Standalone on purpose: the serving machine has no checkout of the repo. It
needs only sentence-transformers. Pin torch and sentence-transformers to the
versions the laptop has: the vectors then agree with the laptop's to float
tolerance (about 1e-7 an element across devices), not bit for bit.

    python embed_server.py --key-file ~/embed/api-key.txt

    GET  /health  -> {"model": ..., "device": ...}
    POST /embed   {"model": ..., "texts": [...], "normalize": true}
                  -> the vectors as .npy bytes (float32, one row per text)

Every request needs ``Authorization: Bearer <key>``. A request naming a model
other than the one loaded is refused with 409 rather than answered with
vectors from the wrong space.
"""

import argparse
import hmac
import io
import json
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
MAX_BODY_BYTES = 32 * 1024 * 1024
SOCKET_TIMEOUT_S = 30
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _pick_device(torch):
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def build_handler(model, model_name, device, key, batch_size):
    # One forward pass at a time: the model is not safe to share across
    # threads, and the threads exist only so /health answers during an encode.
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # On socket reads and writes only, never on the encode: a caller that
        # announces a body and stops sending cannot hold its thread for good.
        timeout = SOCKET_TIMEOUT_S

        def log_message(self, fmt, *args):
            # A request line is the caller's text; keep its control characters out of the log.
            message = _CONTROL_CHARS.sub(lambda m: "\\x%02x" % ord(m.group()), fmt % args)
            sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))

        def _send(self, status, body, content_type="application/json"):
            if isinstance(body, dict):
                body = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            given = self.headers.get("Authorization", "")
            return hmac.compare_digest(given.encode("utf-8"), ("Bearer " + key).encode("utf-8"))

        def do_GET(self):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            self._send(200, {"model": model_name, "device": device})

        def do_POST(self):
            # The key comes first: a caller without it does not get its body read.
            # Wherever the body is left unread, the connection cannot be reused.
            if not self._authorized():
                self.close_connection = True
                return self._send(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self.close_connection = True
                if length < 0:
                    return self._send(400, {"error": "bad Content-Length"})
                return self._send(413, {"error": "body too large"})
            raw = self.rfile.read(length)
            if self.path != "/embed":
                return self._send(404, {"error": "not found"})
            try:
                body = json.loads(raw)
                texts = body["texts"]
                if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                    raise ValueError("texts must be a list of strings")
                normalize = body.get("normalize", True)
                if not isinstance(normalize, bool):
                    raise ValueError("normalize must be a boolean")
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, {"error": str(e)})
            wanted = body.get("model")
            if wanted and wanted != model_name:
                return self._send(409, {"error": f"serving {model_name}, not {wanted}"})

            import numpy as np

            t0 = time.perf_counter()
            try:
                with lock:
                    vectors = model.encode(
                        texts,
                        batch_size=batch_size,
                        normalize_embeddings=normalize,
                        show_progress_bar=False,
                        convert_to_numpy=True,
                    )
                buf = io.BytesIO()
                np.save(buf, np.asarray(vectors, dtype=np.float32), allow_pickle=False)
            except Exception:
                # Answer, so the client can tell a failed encode from a dead server.
                traceback.print_exc()
                return self._send(500, {"error": "encode failed"})
            self._send(200, buf.getvalue(), "application/octet-stream")
            self.log_message("encoded %d texts in %.2fs", len(texts), time.perf_counter() - t0)

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default="", help="mps, cuda or cpu; default picks the best available")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--key-file", required=True, help="file holding the bearer key")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--threads", type=int, default=0, help="torch CPU threads; 0 keeps the default")
    args = ap.parse_args()

    key = Path(args.key_file).expanduser().read_text(encoding="utf-8-sig").strip()
    if not key:
        sys.exit(f"{args.key_file} is empty")

    import torch
    from sentence_transformers import SentenceTransformer

    if args.threads:
        torch.set_num_threads(args.threads)
    device = args.device or _pick_device(torch)
    model = SentenceTransformer(args.model, device=device)
    # The first forward pass pays for lazy allocation (and, on MPS, kernel
    # compilation); spend it here, not on the first request.
    model.encode(["Una frase corta para calentar.", "A short sentence to warm up."])

    server = ThreadingHTTPServer(
        (args.host, args.port), build_handler(model, args.model, device, key, args.batch_size)
    )
    print(f"serving {args.model} on {device} at {args.host}:{args.port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
