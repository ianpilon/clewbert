# Context Canvas

Boards you can talk to, with a team of AI agents that works on them.

Paste screenshots, links, PDFs and any file onto an endless canvas, add sticky notes and frames, and pan and zoom
like Miro. Then ask the board questions out loud or by typing, or hand a task to a team of agents: the task becomes a
frame on the board, and the agents fill it with notes, links, working demos and questions for you.

## What it does

- **Canvas:** sticky notes, text, arrows, frames, drawing, emoji, images, videos, PDFs and any dropped file. Copy and paste (also between boards), Option-drag to copy. Boards live in spaces.
- **Talk to your boards:** questions are answered only from what is on the board, with the items it used highlighted. Works from one board, one space, or all boards.
- **Agent teams:** each board has a team: a Lead and teammates with roles in plain words, in an org chart you can rearrange. A task goes to the Lead, who hands parts to teammates (each part is its own frame, joined by an arrow) and writes the final answer. Agents can search the web, read pages and code, use a real browser, and build demos you can open. They ask before anything that sends, buys, books, posts or signs up.
- **Your models, your choice:** in Settings (the gear), pick the model for each job (voice, agents, builders, vision): a model server on your own machine, or OpenAI, xAI or Anthropic with your own key. Keys stay on your server.
- **Miro import:** New board > Import from Miro.

## Run it

Needs macOS (pictures and PDFs are read with Apple's on-device text recognition) and the Python that comes with it. No packages to install.

```
swiftc -O tools/textof.swift -o bin/textof
python3 server.py 8135 ~/context-canvas-data
```

Open http://localhost:8135, click the gear, and pick a model for each job.

## Settings at startup (optional)

| Variable | What it is |
|---|---|
| `CANVAS_MODEL_URL`, `CANVAS_MODEL` | A chat/completions model on your own machine (Ollama, LM Studio, vLLM...) for the voice |
| `AGENT_MODEL_URL`, `AGENT_MODEL` | The same for the agents (it must support tool calls) |
| `AGENT_CODE_MODELS` | Stronger models for the Developer and designers: `url|model;url|model`, tried in order |
| `AGENT_VISION_MODEL_URL`, `AGENT_VISION_MODEL` | A model that can see, to check demo screenshots |
| `AGENT_WORKERS` | Tasks at once (default 3) |
| `SEARXNG_URL` | Your own SearXNG for web search (json format on) |
| `BROWSER_URL`, `BROWSER_TOKEN` | A real browser for agents: run `browser/service.py PORT` on any machine with Playwright |
| `CANVAS_TTS_URL` | A text-to-speech server for spoken answers |
| `CF_ACCESS_TEAM`, `CF_ACCESS_AUD` | If the app sits behind Cloudflare Access |

Choices made in Settings win over these. Voice input needs a speech-to-text service at `/ears` next to the app;
without one, type your questions in the voice card.

## Your data

Everything is stored as plain files in the data folder you pass to `server.py`: boards, pictures, files, agent runs,
conversations, and `settings.json` (your provider keys, readable only by your user account). `backup.sh` makes a nightly copy.

## Licence

Apache License 2.0. See `LICENSE` and `NOTICE`.
