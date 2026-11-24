# Adding a Sparx module

Each capability in Sparx is a tool in `sparx/tools/<module>.py`. Before writing one, skim
`sparx/tools/__init__.py`, `sparx/tools/core.py`, `sparx/db.py`, `sparx/config.py`, `sparx/security.py`,
`sparx/jobs.py`, `sparx/undo.py`, `sparx/llm.py`, `sparx/agent.py`, `tests/conftest.py` and `tests/test_core.py`.

## Conventions

1. Keep changes to the core (`sparx/*.py` above, `sparx/tools/__init__.py`, `sparx/tools/core.py`,
   `tests/conftest.py`, `pyproject.toml`) separate from module work. If a module needs something the core lacks,
   work around it in the module first and note it.
2. Tools are registered with `@tool(...)`, and each module declares its group once with
   `category(name, description)`. The first parameter is `ctx`. Return str / dict / list, and raise
   `ToolError("...")` for expected failures. Names are `snake_case` with a domain prefix (`task_add`,
   `email_send`). The description is read by the model, so say in one or two sentences what the tool does and when
   to use it. Keep parameter lists small.
3. `risk=` decides whether the user is asked first: `read`, `write` (Sparx's own data or workspace), `exec` (runs
   code or commands), `control` (desktop, browser, devices), `delete`, `send` (reaches other people), `spend`
   (money). Anything irreversible or outward-facing must be `delete`, `send`, `spend`, `control` or `exec`. Tools
   never prompt on their own; the registry handles approval.
4. Set `untrusted=True` on any tool that returns text Sparx did not write (web, files, email, OCR, transcripts,
   device payloads).
5. Storage is SQLite through `sparx.db` (`db.schema(...)` at import, `db.q/one/x/many`, `db.now()` for
   timestamps). Files go under `config.path("<area>", ...)`. Don't do `from sparx.config import HOME`, because
   tests repoint it. Secrets come from `config.secret("ENV_NAME")`, integration settings from
   `config.settings()["integrations"]`.
6. Deleting means moving to `config.path("trash", ...)`, not unlinking. Call `undo.record(...)` for anything
   reversible.
7. Base dependencies: the stdlib plus httpx, fastapi, uvicorn, websockets, pydantic, openai, playwright, pypdf,
   pillow, numpy, chromadb, edge-tts, pyttsx3, pyautogui, cv2, sounddevice, rich, pytest and pytest-asyncio.
   Anything else is optional: import it inside the function and raise
   `ToolError("Install X to use this: pip install X")` when it's missing. Python is 3.14 on Windows 11 and many
   native packages have no wheel yet.
8. Model calls inside tools go through `sparx.llm` (`from sparx import llm`): `llm.complete(system, user,
   role="fast")`, `llm.complete_json(...)` or `llm.get(role).chat(...)`. Tests fake that module.
9. Background work uses `@jobs.every(seconds, "name", flag="<proactive setting>")` and
   `jobs.notify(title, body, kind, why)`.
10. No fake stubs. If a feature needs an account, device or library that isn't there, the tool says what is
    missing and how to set it up.
11. Tests go in `tests/test_<area>.py` and use the fixtures in `tests/conftest.py` (`ctx`, `fake_llm`,
    `run_tool`; `home` is automatic). No network, real model, mic, camera or desktop in tests.
    - One file: `python -m pytest tests/test_<area>.py -q`
    - Quick run: `python -m pytest tests -m "not browser" -q` (tests that open a real browser carry
      `pytest.mark.browser`)
    - Full run: `python -m pytest tests -q`, needed for any change to `sparx/web/*` or `sparx/server.py`
    - Low on memory: `python tests/run_by_file.py --out <dir>` runs each file in its own process and writes
      `results.json` plus per-file logs
    - Check the module loads: `python -c "from sparx import tools; tools.load_all(); print(tools.LOAD_ERRORS)"`
12. Match the style of the core: compact, typed where it helps, short docstrings, no dead code.
