# browser-pilot

Drive a real Chrome window with any LLM, from one command. A planner splits the job, several agents work in parallel tabs, and their reports are merged into one answer.

```
pilot "compare the price of the Sony WH-1000XM5 on amazon.in, flipkart.com and croma.com"
```

## Sparx

Sparx is a personal AI assistant built on this library, in the `sparx/` package. You use it from a terminal (`sparx`), a web UI that also works from a phone (`sparx serve`), or by voice (`sparx voice`). It works through tools for tasks, reminders, files, web search, email, smart-home devices and more. It asks before risky actions, keeps an audit log and can undo some changes. A background process (`sparx daemon`) handles reminders and monitors. It runs on Windows, uses a local Ollama model or a hosted provider, and stores its data in `~/.sparx`.

```
pip install -e .
sparx doctor
sparx
```

Hardware and account integrations (microphone, camera, desktop control, email, Telegram, Home Assistant, MQTT) have only been tested against fakes. See the full guide, including setup, permissions and known limits: [docs/SPARX.md](docs/SPARX.md).

## Personal assistant (`pilot chat`)

```
pilot chat
```

A chat with long-term memory that answers directly and hands web work to the browser agents when needed. It remembers what you tell it about yourself (`~/.browserpilot/memory.md`), carries the conversation across sessions, saves and runs workflows, and keeps notes in `~/.browserpilot/files/`.

Inside the chat: `/model gemini` (switch provider mid-conversation), `/models`, `/agents N`, `/yes on|off`, `/headless on|off`, `/memory`, `/forget TEXT`, `/new`, `/exit`.

`pilot ask "..."` runs a single assistant turn, for scripts and scheduled tasks. Rename the assistant with `BROWSERPILOT_ASSISTANT_NAME`.

## Setup

```
pip install -e .                       # already done on this machine
pip install -e .[all]                  # optional features (or one extra: docs, voice, vision, home)
copy .env.example %USERPROFILE%\.browserpilot\.env    # then add your API keys
pilot login https://mail.google.com    # sign in to the sites you want automated
pilot models                           # shows which providers have a key
```

Pilot uses its own Chrome profile (`~/.browserpilot/chrome-profile`), separate from your everyday one. Chrome refuses remote control of the default profile, so you sign in once with `pilot login` and those sessions are reused by every run.

## Models

`-m provider/model`, or set `BROWSERPILOT_MODEL`.

| Example | Needs |
| --- | --- |
| `groq/openai/gpt-oss-120b` (also `groq/gpt-oss-120b`) | `GROQ_API_KEY` |
| `gemini/gemini-2.5-flash` | `GEMINI_API_KEY` |
| `ollama/gpt-oss:latest` | Ollama running locally |
| `openai/<model>`, `anthropic/<model>`, `cerebras/<model>`, `mistral/<model>`, `deepseek/<model>`, `xai/<model>`, `together/<model>` | `<PROVIDER>_API_KEY` |
| `lmstudio/<model>` | LM Studio server running locally |
| `custom/<model>` | `BROWSERPILOT_BASE_URL` pointing at any OpenAI-compatible server |

Models without native tool calling are handled automatically through a JSON protocol. Small local models (3B-8B) get lost on real sites; use a 20B+ model or a hosted one for real work. For Ollama, raise the context window (`OLLAMA_CONTEXT_LENGTH=32768` before `ollama serve`), because page listings are long.

### Testing with Ollama

`pytest -m ollama` runs live checks against local Ollama models: a tool-call round trip, then a real headless-Chrome run (port 9444, throwaway profile) that clicks a button on a local page and reports what appears. Pick models with `OLLAMA_TEST_MODELS=gpt-oss:latest,qwen2.5-coder:14b`; the default is `gpt-oss:latest`. A missing model is skipped. These tests are excluded from a plain `pytest`, and take a few minutes on CPU.

## Commands

```
pilot "task"                      run a task
pilot save NAME "task {var}"      save a reusable workflow (options are saved too)
pilot run NAME var=value          run it
pilot list | rm NAME              manage workflows
pilot login [url]                 open pilot's Chrome to sign in
pilot stop                        close pilot's Chrome
pilot models                      providers and key status
```

Options: `-a/--agents N` (max parallel agents, 1 = no planning), `--planner-model`, `--max-steps`, `--url`, `--headless`, `-y/--yes`, `--vision`, `--keep-tabs`, `--out DIR`.

Each run writes `result.json`, screenshots, downloads and saved files to `~/.browserpilot/runs/<timestamp>/`.

## What the agents can do

navigate, go_back, click, type, select, hover, press, scroll, read_text, find, get_links, screenshot, run_js, new_tab / list_tabs / switch_tab / close_tab, wait, upload_file, save_file, get_console, get_network, ask_user, done.

## Safety

- Clicks on buttons labelled buy, pay, delete, send, post, submit, confirm and similar ask you first. `--yes` turns that off. With no terminal attached and no `--yes`, they are refused.
- Agents are told to treat page text as data, not instructions, but no prompt fully prevents a malicious page from steering a model. Don't run `--yes` on untrusted sites while signed in to accounts that matter.
- Chrome's debugging port (`127.0.0.1:9333`) lets any program on this PC control pilot's browser while it is open. `pilot stop` closes it.

## Layout

```
browserpilot/
  cli.py           commands and options
  orchestrator.py  plan -> parallel workers -> merge
  assistant.py     pilot chat / ask: memory, workflows, delegates to the orchestrator
  agent.py         one agent's observe/act loop
  browser.py       Chrome lifecycle, page snapshot, every tool
  llm.py           OpenAI-compatible client, native + JSON tool calling
  config.py        providers, paths, defaults
```

To add a tool, write a `t_<name>` method on `Session` in `browser.py` and add its schema to `TOOLS`.
