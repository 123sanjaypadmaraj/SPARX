"""Working tools: a session todo list, structured questions for the user, and notebook editing."""
import json

from . import A, I, S, ToolError, category, tool
from .coding import _file, _require_read, _write

category("session", "Working aids for long tasks: a todo list for the current job and structured questions for the user")

STATUSES = ("pending", "in_progress", "completed")
_TODOS: dict[str, list[dict]] = {}          # session -> todo list (kept across turns of one conversation)
_MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def _render(items: list[dict]) -> str:
    return "\n".join(f"{_MARK[t['status']]} {t['content']}" for t in items) or "(no todos)"


@tool("todo_write", "Keep a todo list for the job you are doing: replace the whole list with `todos`, each with `content` "
      "and `status` (pending | in_progress | completed). Use it for any task of three or more steps; keep exactly one "
      "item in_progress and mark items completed as soon as they are done. This is your working list, not the user's "
      "personal tasks (task_add).",
      {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": S, "status": S}}}},
      ["todos"], category="session", risk="read")
def todo_write(ctx, todos: list):
    if not isinstance(todos, list):
        raise ToolError("todos must be a list of {content, status}.")
    items = []
    for t in todos:
        if not isinstance(t, dict) or not str(t.get("content", "")).strip():
            raise ToolError("every todo needs a non-empty `content`.")
        status = str(t.get("status") or "pending")
        if status not in STATUSES:
            raise ToolError(f"status must be one of {', '.join(STATUSES)}.")
        items.append({"content": str(t["content"]).strip()[:300], "status": status})
    if sum(t["status"] == "in_progress" for t in items) > 1:
        raise ToolError("only one todo can be in_progress at a time.")
    _TODOS[ctx.session] = items
    ctx.event(type="todos", todos=items)
    done = sum(t["status"] == "completed" for t in items)
    return f"Todos updated ({done}/{len(items)} done):\n{_render(items)}"


@tool("todo_read", "Show your current todo list for this conversation.", category="session", risk="read")
def todo_read(ctx):
    return _render(_TODOS.get(ctx.session, []))


@tool("ask_user", "Ask the user a question you cannot settle yourself, with optional choices. This ends your turn: "
      "the question is shown as your reply and their answer arrives as their next message. Ask only when the answer "
      "changes what you do and no sensible default exists.",
      {"question": S, "options": {**A, "description": "2-4 short choices (the user may also answer freely)"}},
      ["question"], category="session", risk="read")
def ask_user(ctx, question: str, options: list | None = None):
    question = question.strip()
    if not question:
        raise ToolError("question is empty.")
    opts = [str(o).strip() for o in (options or []) if str(o).strip()][:4]
    shown = question + "".join(f"\n  {i}. {o}" for i, o in enumerate(opts, 1))
    return (f"Question for the user:\n{shown}\n\nEnd your turn now: reply with exactly this question (and the choices) "
            "and wait for their answer. Do not call more tools or guess the answer.")


def _lines(source: str) -> list[str]:
    return source.splitlines(keepends=True)


@tool("notebook_edit", "Edit one cell of a Jupyter notebook (.ipynb): replace its source, insert a new cell after it, or "
      "delete it. Read the notebook with code_read first. Cell indices are the numbers code_read shows.",
      {"path": S, "cell": {**I, "description": "cell index; for insert the new cell goes after it (-1 = at the start)"},
       "action": {**S, "description": "replace (default) | insert | delete"},
       "source": {**S, "description": "new cell source (replace and insert)"},
       "cell_type": {**S, "description": "code (default) or markdown; keeps the current type on replace"}},
      ["path", "cell"], category="coding", risk="write")
def notebook_edit(ctx, path: str, cell: int, action: str = "replace", source: str = "", cell_type: str = ""):
    p = _file(path)
    if p.suffix.lower() != ".ipynb":
        raise ToolError(f"{p.name} is not a .ipynb file; use code_edit for other files.")
    if action not in ("replace", "insert", "delete"):
        raise ToolError("action must be replace, insert or delete.")
    if cell_type and cell_type not in ("code", "markdown"):
        raise ToolError("cell_type must be code or markdown.")
    _require_read(ctx, p)
    try:
        nb = json.loads(p.read_text(encoding="utf-8"))
        cells = nb["cells"]
    except (ValueError, KeyError, OSError):
        raise ToolError(f"{p.name} is not a valid Jupyter notebook.")
    cell = int(cell)
    if action == "insert":
        if not -1 <= cell < len(cells):
            raise ToolError(f"cell must be between -1 and {len(cells) - 1}.")
        kind = cell_type or "code"
        new = {"cell_type": kind, "metadata": {}, "source": _lines(source)}
        if kind == "code":
            new.update(execution_count=None, outputs=[])
        cells.insert(cell + 1, new)
    else:
        if not 0 <= cell < len(cells):
            raise ToolError(f"cell must be between 0 and {len(cells) - 1}.")
        if action == "delete":
            del cells[cell]
        else:
            c = cells[cell]
            c["source"] = _lines(source)
            if cell_type and cell_type != c.get("cell_type"):
                c["cell_type"] = cell_type
                if cell_type == "markdown":
                    c.pop("outputs", None), c.pop("execution_count", None)
                else:
                    c.update(execution_count=None, outputs=[])
            elif c.get("cell_type") == "code":
                c.update(execution_count=None, outputs=[])      # old outputs no longer match the source
    return _write(ctx, p, json.dumps(nb, indent=1, ensure_ascii=False) + "\n", f"Edited cell {cell} of")
