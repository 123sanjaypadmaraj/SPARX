"""Output styles: a named paragraph appended to the system prompt that changes how Sparx answers.

Built in: default, concise, explanatory, learning. Your own: ~/.sparx/output-styles/<name>.md (or <repo>/.sparx/ and
.claude/output-styles); the file body is the style, `description:` in the frontmatter is the one-liner. Choose with
`/output-style NAME` (saved in the `output_style` setting)."""
from . import config, skills

BUILTIN = {
    "default": ("Normal replies.", ""),
    "concise": ("Shortest useful answer.", "Answer in as few words as possible: no preamble, no recap, no offers of "
                "further help. Code and results first."),
    "explanatory": ("Explain the why as you work.", "While working, add short 'Insight' notes explaining why you chose an "
                    "approach, what the codebase does there, and what alternatives exist."),
    "learning": ("Teach: leave small pieces for the user.", "Work as a teacher. For small, meaningful pieces of code, "
                 "leave a clearly marked TODO(human) with a hint instead of writing it, explain what you did write, and "
                 "ask the user to try the part you left."),
}


def styles() -> dict[str, tuple[str, str]]:
    out = dict(BUILTIN)
    repo = skills.repo_root()
    for root in (config.HOME, repo / ".claude", repo / ".sparx"):
        base = root / "output-styles"
        for f in sorted(base.glob("*.md")) if base.is_dir() else []:
            try:
                meta, body = skills.parse(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            out[(meta.get("name") or f.stem).lower()] = (meta.get("description", "custom style"), body)
    return out


def current() -> str:
    name = str(config.settings().get("output_style", "default")).lower()
    return name if name in styles() else "default"


def prompt_block() -> str:
    body = styles()[current()][1]
    return f"\n\nOutput style ({current()}): {body}" if body else ""


def set_style(name: str) -> str:
    name = name.strip().lower()
    if name not in styles():
        return "Unknown style. Available: " + ", ".join(styles())
    config.set_key("output_style", name)
    return f"Output style: {name}"


def listing() -> str:
    return "\n".join(f"{'*' if n == current() else ' '} {n:<12} {d}" for n, (d, _b) in styles().items())
