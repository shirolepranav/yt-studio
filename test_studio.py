"""Prove the studio's server and queue work, with two fake channels and no API calls.

    python3 test_studio.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import app

# A fake channel: "go" is a slow job that takes a second; anything else is fast.
ASSISTANT = """
import json, sys
text = sys.argv[-1]
print("routing log line")
print(json.dumps({"intent": "work" if text == "go" else "status", "args": {}, "reply": "", "slow": text == "go"}))
"""
CHAT_JOB = """
import json, os, sys, time
time.sleep(1)
line = json.dumps({"id": str(time.time_ns()), "ts": time.time(), "role": "ai", "html": "done"})
fd = os.open("runs/chat.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
os.write(fd, (line + "\\n").encode())
"""


def make_channel(root: Path, name: str) -> dict:
    repo = root / name
    (repo / "pipeline").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "venv" / "bin").mkdir(parents=True)
    (repo / "runs" / "r1" / "output").mkdir(parents=True)
    (repo / "venv" / "bin" / "python").symlink_to(sys.executable)
    (repo / "pipeline" / "__init__.py").write_text("")
    (repo / "pipeline" / "assistant.py").write_text(ASSISTANT)
    (repo / "tools" / "chat_job.py").write_text(CHAT_JOB)
    (repo / "runs" / "r1" / "output" / "clip.bin").write_text("0123456789")
    (repo / ".env").write_text("SECRET=1")
    return {"name": name, "path": str(repo)}


def request(url: str, data: dict | None = None, **headers) -> tuple[int, bytes]:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""


def messages(base: str, key: str) -> list[dict]:
    return json.loads(request(f"{base}/ch/{key}/messages")[1])["messages"]


def wait_for(check, seconds: float = 20) -> None:
    deadline = time.time() + seconds
    while not check():
        assert time.time() < deadline, "timed out"
        time.sleep(0.1)


def main() -> None:
    root = Path(tempfile.mkdtemp())
    for entry in (make_channel(root, "alpha"), make_channel(root, "beta")):
        channel = app.Channel(**entry)
        app.CHANNELS[channel.key] = channel

    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    port = server.server_address[1]
    app.ALLOWED_HOSTS = {f"127.0.0.1:{port}"}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=app.worker, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    # Files: byte ranges for the video player, nothing outside the channel's runs/.
    clip = f"{base}/ch/alpha/runs/r1/output/clip.bin"
    assert request(clip) == (200, b"0123456789")
    assert request(clip, Range="bytes=2-4") == (206, b"234")
    assert request(clip, Range="bytes=-3") == (206, b"789")
    assert request(clip, Range="bytes=20-")[0] == 416
    for escape in ("/ch/alpha/runs/../.env", "/ch/alpha/runs/%2e%2e/.env",
                   "/ch/alpha/runs/../../beta/.env", "/ch/nobody/runs/r1/output/clip.bin"):
        code = request(base + escape)[0]
        assert code == 404, f"{escape} gave {code}"
    assert request(clip, Host="evil.example")[0] == 403
    assert request(f"{base}/ch/alpha/send", {"text": "x"}, Origin="https://evil.example")[0] == 403
    print("files, ranges and isolation ok")

    # Queue: beta's job waits for alpha's, then both finish, in order.
    request(f"{base}/ch/alpha/send", {"text": "go"})
    wait_for(lambda: app.running is not None)
    request(f"{base}/ch/beta/send", {"text": "go"})
    wait_for(lambda: any("Queued behind" in m.get("html", "") for m in messages(base, "beta")))
    state = json.loads(request(f"{base}/channels")[1])
    assert state["running"]["channel"] == "alpha" and state["queued"][0]["channel"] == "beta", state
    wait_for(lambda: any(m.get("html") == "done" for m in messages(base, "beta")))
    done = {key: next(m["ts"] for m in messages(base, key) if m.get("html") == "done")
            for key in ("alpha", "beta")}
    assert done["alpha"] < done["beta"], done
    print("one job at a time, in order ok")

    # Stop takes a queued job out; a fast intent never queues.
    request(f"{base}/ch/alpha/send", {"text": "go"})
    wait_for(lambda: app.running is not None)
    request(f"{base}/ch/beta/send", {"text": "go"})
    wait_for(lambda: len(app.queue) == 1)
    request(f"{base}/ch/beta/send", {"text": "cmd:stop"})
    wait_for(lambda: any("Removed it from the queue" in m.get("html", "") for m in messages(base, "beta")))
    request(f"{base}/ch/beta/send", {"text": "status please"})
    wait_for(lambda: app.running is None)
    assert not app.queue

    # Stop kills a running job before it finishes.
    finished = sum(m.get("html") == "done" for m in messages(base, "alpha"))
    request(f"{base}/ch/alpha/send", {"text": "go"})
    wait_for(lambda: app.running is not None)
    request(f"{base}/ch/alpha/send", {"text": "cmd:stop"})
    wait_for(lambda: app.running is None, seconds=0.9)  # well inside the job's 1 s sleep
    time.sleep(1.5)
    assert sum(m.get("html") == "done" for m in messages(base, "alpha")) == finished, "killed job still finished"
    print("stop (queued and running) and fast routing ok")

    server.shutdown()
    print("\nSTUDIO TEST PASSED")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    main()
