"""HTTP wrapper for transcribe-cli. Accepts multipart audio, returns JSON.

Usage: python3 serve.py [--port 10110] [--model path.gguf] [--bin path] [--token SECRET]
Endpoint: POST /transcribe  (multipart form, field "file")
Response: {"text": "...", "audio_duration_s": 1.5}
Health:   GET /health

Requires: ffmpeg, transcribe-cli binary, GGUF model file.
Auth: if --token or STT_TOKEN is set, requests must include Authorization: Bearer <token>.
"""

import argparse
import json
import os
import subprocess
import tempfile
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

DEFAULT_BIN = os.path.expanduser("~/transcribe.cpp/build/bin/transcribe-cli")
DEFAULT_MODEL = os.path.expanduser(
    "~/transcribe.cpp/models/parakeet-unified-en-0.6b/parakeet-unified-en-0.6b-Q8_0.gguf"
)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

_transcribe_lock = threading.Semaphore(1)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def transcribe(audio_bytes: bytes, suffix: str = ".ogg") -> dict:
    with tempfile.TemporaryDirectory(prefix="stt-") as tmp_dir:
        src = os.path.join(tmp_dir, f"input{suffix}")
        wav = os.path.join(tmp_dir, "input.wav")

        with open(src, "wb") as f:
            f.write(audio_bytes)

        r = subprocess.run(
            ["ffmpeg", "-i", src, "-ar", "16000", "-ac", "1", "-f", "wav", wav, "-y"],
            capture_output=True, timeout=10,
        )
        if r.returncode != 0:
            stderr = r.stderr.decode(errors="ignore")[:200]
            return {"text": "", "error": f"ffmpeg failed: {stderr}"}

        r = subprocess.run(
            [ARGS.bin, "-q", "-m", ARGS.model, wav],
            capture_output=True, timeout=60,
        )

        text = ""
        duration = 0.0
        for line in r.stdout.decode().splitlines():
            if line.startswith("text:"):
                t = line[5:].strip()
                if t != "(empty)":
                    text = t
            elif "duration:" in line and "s" in line:
                try:
                    duration = float(line.split("duration:")[1].strip().rstrip(" s"))
                except (ValueError, IndexError):
                    pass

        if not text and r.returncode != 0:
            stderr = r.stderr.decode(errors="ignore")[:200]
            return {"text": "", "error": f"transcribe-cli failed (rc={r.returncode}): {stderr}"}

        return {"text": text, "audio_duration_s": duration}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {"status": "ok", "model": os.path.basename(ARGS.model)})
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/transcribe":
            self.send_error(404)
            return

        if ARGS.token:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {ARGS.token}":
                self._json_response(401, {"error": "unauthorized"})
                return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_UPLOAD_BYTES:
            self._json_response(413, {"error": f"upload exceeds {MAX_UPLOAD_BYTES} bytes"})
            return

        content_type = self.headers.get("Content-Type", "")
        body = self.rfile.read(content_length)

        if "multipart/form-data" in content_type:
            boundary = content_type.split("boundary=")[1].strip()
            audio_bytes, suffix = self._parse_multipart(body, boundary)
        else:
            audio_bytes = body
            suffix = ".ogg"

        if not audio_bytes:
            self._json_response(400, {"error": "no audio data"})
            return

        acquired = _transcribe_lock.acquire(timeout=5)
        if not acquired:
            self._json_response(503, {"error": "busy, try again"})
            return
        try:
            result = transcribe(audio_bytes, suffix)
        finally:
            _transcribe_lock.release()

        self._json_response(200, result)

    def _parse_multipart(self, body: bytes, boundary: str) -> tuple:
        parts = body.split(f"--{boundary}".encode())
        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            header_end = part.find(b"\r\n\r\n")
            if header_end < 0:
                header_end = part.find(b"\n\n")
            if header_end < 0:
                continue
            header = part[:header_end].decode(errors="ignore")
            if 'name="file"' in header or 'name="audio"' in header:
                data = part[header_end:].strip()
                if data.endswith(b"--"):
                    data = data[:-2].strip()
                suffix = ".ogg"
                if 'filename="' in header:
                    fn = header.split('filename="')[1].split('"')[0]
                    if "." in fn:
                        suffix = "." + fn.rsplit(".", 1)[1]
                return data, suffix
        return None, ".ogg"

    def _json_response(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[stt] {args[0]}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="STT HTTP server wrapping transcribe-cli")
    parser.add_argument("--port", type=int, default=10110)
    parser.add_argument("--bin", default=DEFAULT_BIN)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--token", default=os.environ.get("STT_TOKEN", ""))
    ARGS = parser.parse_args()

    print(f"STT server starting on :{ARGS.port}")
    print(f"  bin:   {ARGS.bin}")
    print(f"  model: {ARGS.model}")
    print(f"  auth:  {'enabled' if ARGS.token else 'disabled'}")
    server = ThreadingHTTPServer(("0.0.0.0", ARGS.port), Handler)
    server.serve_forever()
