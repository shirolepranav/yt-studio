# YT Studio

One chat app for every YouTube channel on this Mac.

```bash
make app        # opens http://127.0.0.1:8765
```

Pick a channel in the header and chat with it: start a video, pick a topic,
edit the script, listen to the narration (if the channel stops there for it),
watch the finished video, change the title, thumbnail or pinned
comment, then **Accept & upload**.

Before the render, the build stops and posts the **footage list**: every shot
next to its line of script, marked stock, archive (NASA/Commons), AI, or
chart/card, with the reason it was picked. Swap any shot for a fresh library
search or an AI image, optionally saying what you want, then tap **Render**.
Only the two-minute segments you changed are re-rendered. Every channel keeps its own conversation; a
dot marks one with new messages.

**One job runs at a time across all channels.** A second channel's build waits
in the queue ("⏳ Queued behind Deep Earth · approve_script") and starts on its
own when the first finishes, so two renders never fight over the GPU. **⏹ Stop**
kills the channel's running job, or takes its job out of the queue; **🔁 Retry**
re-runs its last job, skipping every stage that already finished.

The Mac won't idle-sleep while the studio is open (it may still sleep if you
close the lid on battery). Standard library only: no install beyond Python 3.

## How it works

The studio knows nothing about how a channel makes videos. Each channel is its
own repo, with its own code, prompts, renderer, `.env` and YouTube sign-in, that
honours [CHANNEL_CONTRACT.md](CHANNEL_CONTRACT.md). The studio runs that repo's
commands in that repo's own `venv`, shows its `runs/chat.jsonl`, and serves files
from its `runs/` folder only.

| File | What it is |
|---|---|
| `channels.json` | the channels: name, emoji, repo path |
| `app.py` | the server and the job queue |
| `ui.html` | the page |
| `test_studio.py` | `make test` — two fake channels, no API calls |

## Adding a channel

1. **Copy the closest existing channel**, without its history:
   ```bash
   cd ~/coding/projects
   rsync -a --exclude venv --exclude runs --exclude .git --exclude .env \
     --exclude node_modules --exclude graphify-out yt-boring-docs/ yt-<name>/
   cd yt-<name> && git init && make setup && cp .env.example .env
   ```
2. **Make it that channel.** Edit `config/channel.json`, `persona.md`,
   `brand.json`, `topic_backlog.csv` and `prompts/`. Change pipeline stages only
   where this channel really makes videos differently (fewer charts, no stock,
   more real-world data, another renderer); the contract doesn't care.
3. **Keys.** Fill in `.env`. Run `python tools/youtube_auth.py` and sign in as
   **this** channel. Each channel needs its own refresh token. If several
   channels upload on the same day, give each its own Google Cloud project,
   because the API's daily quota is per project.
4. **Add one line** to `channels.json`:
   ```json
   {"name": "My Channel", "emoji": "🎙", "path": "~/coding/projects/yt-<name>"}
   ```
5. Restart the studio, then run `python tools/selftest.py` in the new repo.

Fixes to the shared pieces (`pipeline/chat.py`, `brain.py`, `assistant.py`,
`tools/chat_job.py`, the review helpers in `publish.py`) are copied between repos
by hand. That's cheap at a handful of channels, and it's what lets each channel
change its own pipeline freely.
