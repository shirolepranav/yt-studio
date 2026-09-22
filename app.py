"""YT Studio - one chat app for every YouTube channel on this Mac.

    make app          (or: python3 app.py)

Opens http://127.0.0.1:8765. Pick a channel in the header and chat with it.
The studio knows nothing about how a channel makes videos: each channel is its
own repo that honours CHANNEL_CONTRACT.md, and the studio just

  * routes each message through that repo's `pipeline.assistant`, in that
    repo's own venv, so every channel keeps its own code and dependencies;
  * runs slow work (topics, script, build, upload) as that repo's
    `tools/chat_job.py`, one job at a time across ALL channels, so two renders
    never fight over the GPU;
  * shows the repo's `runs/chat.jsonl` and serves files from its `runs/`.

Standard library only. Bound to 127.0.0.1, and refuses requests from other
sites' pages, so nothing but a browser tab on this Mac can drive it.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
PORT = int(os.getenv("STUDIO_PORT", "8765"))
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ROUTE_TIMEOUT = 300  # a routing call may ask a model; a stuck one must not hang forever


class Channel:
    def __init__(self, name: str, path: str, emoji: str = "📺"):
        self.name, self.emoji = name, emoji
        self.key = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        self.repo = Path(path).expanduser().resolve()
        self.runs = self.repo / "runs"
        self.log = self.runs / "chat.jsonl"
        self.python = str(self.repo / "venv" / "bin" / "python")

    def say(self, html: str, buttons=None, role: str = "ai", **extra) -> None:
        """Append a message to this channel's chat, in the same format its pipeline writes."""
        record = {"id": str(time.time_ns()), "ts": time.time(), "role": role,
                  "html": html, "buttons": buttons, **extra}
        self.runs.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(record) + "\n").encode())
        finally:
            os.close(fd)

    def lines(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


def load_channels() -> dict[str, Channel]:
    entries = json.loads((HERE / "channels.json").read_text())
    return {c.key: c for c in (Channel(**entry) for entry in entries)}


CHANNELS: dict[str, Channel] = {}


# ---------------------------------------------------------------------------
# The job queue - one job at a time, across every channel
# ---------------------------------------------------------------------------

# ponytail: one queue for all jobs, even text-only ones (topics, script) that
# don't need the GPU. Split into a GPU queue and a text queue if waiting annoys.
queue: list[dict] = []
running: dict | None = None
lock = threading.Condition()


def enqueue(channel: Channel, intent: str, args: dict, ack: str = "") -> None:
    (channel.runs / "last_job.json").write_text(json.dumps({"intent": intent, "args": args}))
    with lock:
        job = {"channel": channel, "intent": intent, "args": args, "ack": ack}
        if running or queue:
            ahead = len(queue) + (1 if running else 0)
            first = running or queue[0]
            channel.say(f"⏳ Queued behind <b>{first['channel'].name} · {first['intent']}</b> "
                        f"({ahead} ahead). I'll start as soon as it finishes.")
            job["queued"] = True
        queue.append(job)
        lock.notify()


def worker() -> None:
    global running
    while True:
        with lock:
            while not queue:
                lock.wait()
            job = queue.pop(0)
            channel = job["channel"]
            if job.get("queued"):
                channel.say(f"▶️ Starting <b>{job['intent']}</b> now.")
            elif job["ack"]:
                channel.say(f"👍 {escape(job['ack'])}")
            else:
                channel.say(f"👍 Starting <b>{job['intent']}</b>…")
            with open(channel.runs / "job.log", "ab") as out:
                out.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} {job['intent']} {job['args']}\n".encode())
                proc = subprocess.Popen(
                    [channel.python, "tools/chat_job.py", "--intent", job["intent"],
                     "--args", json.dumps(job["args"]), "--run", "latest"],
                    cwd=channel.repo, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                )
            running = {**job, "proc": proc, "started": time.time()}
        proc.wait()  # failures report themselves in the chat via chat.failed
        with lock:
            running = None


def stop(channel: Channel) -> str:
    """Kill this channel's running job, or drop its queued ones."""
    with lock:
        if running and running["channel"] is channel:
            try:
                os.killpg(running["proc"].pid, signal.SIGTERM)  # the job and any render it started
            except ProcessLookupError:
                pass
            return "running"
        mine = [job for job in queue if job["channel"] is channel]
        for job in mine:
            queue.remove(job)
        return "queued" if mine else ""


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# One message from the page
# ---------------------------------------------------------------------------

def route(channel: Channel, text: str) -> dict | None:
    """Ask the channel's own assistant. Its last stdout line is the decision."""
    try:
        result = subprocess.run([channel.python, "-m", "pipeline.assistant", "--text", text],
                                cwd=channel.repo, capture_output=True, text=True, timeout=ROUTE_TIMEOUT)
        return json.loads(result.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        detail = f"no answer after {ROUTE_TIMEOUT} s"
    except (OSError, IndexError, ValueError) as error:
        detail = (result.stderr or result.stdout)[-600:] if "result" in locals() else str(error)
    channel.say(f"❌ I couldn't work that out.\n\n<code>{escape(detail)}</code>")
    return None


def handle(channel: Channel, text: str) -> None:
    channel.say("", role="user", text=text)
    clean = text.strip().lower()

    if clean in ("cmd:stop", "stop job"):
        stopped = stop(channel)
        if stopped == "running":
            channel.say("⏹ Stopped. Tap Retry to carry on from the last finished stage.",
                        buttons=[[["🔁 Retry", "retry"]]])
        else:
            channel.say("Removed it from the queue." if stopped else "Nothing is running.")
        return
    if clean == "retry":
        last = channel.runs / "last_job.json"
        if last.exists():
            job = json.loads(last.read_text())
            enqueue(channel, job["intent"], job["args"], f"Retrying {job['intent']}.")
        else:
            channel.say("There's nothing to retry.")
        return

    decision = route(channel, text)
    if not decision:
        return
    if decision["intent"] == "cancel":
        stop(channel)
    elif decision.get("slow"):
        enqueue(channel, decision["intent"], decision.get("args", {}), decision.get("reply", ""))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def job_view(job: dict | None) -> dict | None:
    return job and {"channel": job["channel"].key, "name": job["channel"].name,
                    "intent": job["intent"], "started": job.get("started")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # the page polls every 2 s - keep the terminal quiet
        pass

    def trusted(self) -> bool:
        """Only this page, on this Mac. Blocks DNS rebinding and cross-site POSTs."""
        origin = self.headers.get("Origin")
        return (self.headers.get("Host") in ALLOWED_HOSTS
                and (origin is None or urlparse(origin).netloc in ALLOWED_HOSTS))

    def send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def channel_route(self, path: str) -> tuple[Channel | None, str]:
        """'/ch/<key>/<rest>' -> (channel, '<rest>')."""
        match = re.fullmatch(r"/ch/([a-z0-9-]+)/(.*)", path)
        return (CHANNELS.get(match[1]), match[2]) if match else (None, "")

    def do_GET(self) -> None:
        if not self.trusted():
            return self.send_error(403)
        url = urlparse(self.path)
        if url.path == "/":
            return self.serve(HERE / "ui.html")
        if url.path == "/channels":
            with lock:
                state = {"running": job_view(running), "queued": [job_view(j) for j in queue]}
            return self.send_json({
                "channels": [{"key": c.key, "name": c.name, "emoji": c.emoji, "count": len(c.lines())}
                             for c in CHANNELS.values()],
                **state,
            })

        channel, rest = self.channel_route(unquote(url.path))
        if not channel:
            return self.send_error(404)
        if rest == "messages":
            query = parse_qs(url.query).get("after", ["0"])[0]
            after = int(query) if query.isdigit() else 0
            lines = channel.lines()
            return self.send_json({"messages": [json.loads(line) for line in lines[after:] if line.strip()],
                                   "next": len(lines)})
        if rest.startswith("runs/"):
            path = (channel.repo / rest).resolve()
            if not path.is_relative_to(channel.runs) or not path.is_file():
                return self.send_error(404)
            return self.serve(path)
        self.send_error(404)

    def do_POST(self) -> None:
        channel, rest = self.channel_route(urlparse(self.path).path)
        if not self.trusted() or not channel or rest != "send":
            return self.send_error(403)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        text = str(body.get("text", "")).strip()
        if text:
            threading.Thread(target=handle, args=(channel, text), daemon=True).start()
        self.send_json({"ok": True})

    def serve(self, path: Path) -> None:
        """Send a file, honouring Range - browsers need it to play and seek a video."""
        size = path.stat().st_size
        start, end = 0, size - 1
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if match and (match[1] or match[2]):
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), size - 1) if match[2] else size - 1
            else:  # "bytes=-500" means the last 500 bytes
                start = max(0, size - int(match[2]))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)

        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in (".srt", ".md", ".txt", ".log"):
            kind = "text/plain"  # shown in the tab, not downloaded or rendered
        if kind.startswith("text/"):
            kind += "; charset=utf-8"
        self.send_header("Content-Type", kind)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        remaining = end - start + 1
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    chunk = handle.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the video player skipped ahead and dropped this request


def main() -> None:
    CHANNELS.update(load_channels())
    for channel in CHANNELS.values():
        if not Path(channel.python).exists():
            print(f"warning: {channel.name} has no venv at {channel.python} - run `make setup` there")
        if not channel.log.exists():
            channel.say(f"👋 <b>{channel.emoji} {escape(channel.name)}.</b> Say <code>new video</code> "
                        "to start one, or ask me anything.",
                        buttons=[[["🎬 New video", "cmd:new"], ["📊 Status", "cmd:status"]]])

    threading.Thread(target=worker, daemon=True).start()
    # Keep the Mac from idle-sleeping for as long as the studio is open.
    subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Studio running at {url} with {', '.join(c.name for c in CHANNELS.values())} - Ctrl+C to quit.",
          flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        with lock:
            if running:
                os.killpg(running["proc"].pid, signal.SIGTERM)


if __name__ == "__main__":
    main()
