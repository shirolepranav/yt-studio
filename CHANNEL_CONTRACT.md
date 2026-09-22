# The channel contract

The studio drives any repo that provides these five things. How the repo makes
its videos (stages, renderer, how many charts, stock or real-world data) is its
own business.

| # | The repo provides | Used for |
|---|---|---|
| 1 | `venv/bin/python` | every command below runs with the channel's own Python and dependencies, with the repo root as the working directory |
| 2 | `python -m pipeline.assistant --text "<message>"` | routing a message. It answers quick things itself (status, title/tags/description/pinned-comment edits, thumbnail pick, questions) and its **last stdout line** is JSON: `{"intent": "...", "args": {...}, "reply": "...", "slow": true/false}` |
| 3 | `python tools/chat_job.py --intent <intent> --args '<json>' --run latest` | slow work: `new`, `pick`, `edit_script`, `replace_script`, `approve_script`, `redo`, `publish`. The studio runs one at a time across all channels, and kills the process group on Stop |
| 4 | `runs/chat.jsonl` | the conversation. The repo appends one JSON object per line: `{"id", "ts", "role": "ai", "html", "buttons": [[[label, code], ...]], "image"?, "video"?, "file"?, "name"?, "edit"?}`. Media paths start with `/runs/`. The studio adds the user's lines (`role: "user"`, `text`) and queue notices |
| 5 | `runs/<id>/output/` | what gets reviewed: `video.mp4`, `thumbnail.jpg` + `_b` + `_c`, `subtitles.srt`, `UPLOAD.md` |

Button codes are just message text (`cmd:new`, `cmd:approve`, `cmd:publish`,
`pick:3`, `thumb:b`, `redo:stock`), so tapping and typing share one path. The
studio itself handles `cmd:stop` and `retry`; retry re-runs `runs/last_job.json`.

A repo that follows this can be copied from `yt-deep-earth`: `pipeline/chat.py`,
`pipeline/brain.py`, `pipeline/assistant.py` and `tools/chat_job.py`, plus the
review helpers in `pipeline/publish.py`.
