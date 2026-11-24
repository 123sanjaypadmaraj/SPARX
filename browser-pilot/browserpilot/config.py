import os
from dataclasses import dataclass
from pathlib import Path

HOME = Path(os.environ.get("BROWSERPILOT_HOME") or Path.home() / ".browserpilot")


def load_env():
    """Load KEY=VALUE lines from ~/.browserpilot/.env and ./.env (real env vars win)."""
    for path in (Path.cwd() / ".env", HOME / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()

PROFILE_DIR = HOME / "chrome-profile"
WORKFLOWS = HOME / "workflows.json"
RUNS_DIR = HOME / "runs"
MEMORY_FILE = HOME / "memory.md"
CHAT_FILE = HOME / "chat.json"
FILES_DIR = HOME / "files"
ASSISTANT_NAME = os.environ.get("BROWSERPILOT_ASSISTANT_NAME", "Pilot")
CDP_PORT = int(os.environ.get("BROWSERPILOT_PORT", "9333"))
MAX_ELEMENTS = int(os.environ.get("BROWSERPILOT_MAX_ELEMENTS", "150"))
DEFAULT_MODEL = os.environ.get("BROWSERPILOT_MODEL", "groq/openai/gpt-oss-120b")

# provider -> (base_url, api key env var, default model)
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "gemini-2.5-flash"),
    "ollama": (os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"), None, "gpt-oss:latest"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4.1-mini"),
    "anthropic": ("https://api.anthropic.com/v1/", "ANTHROPIC_API_KEY", "claude-sonnet-5-5"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "gpt-oss-120b"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", ""),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY", ""),
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY", ""),
    "lmstudio": (os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"), None, ""),
    # any other OpenAI-compatible server: set BROWSERPILOT_BASE_URL (+ BROWSERPILOT_API_KEY)
    "custom": (os.environ.get("BROWSERPILOT_BASE_URL", ""), "BROWSERPILOT_API_KEY", ""),
}


def resolve_model(spec: str):
    """'groq/openai/gpt-oss-120b' -> (provider, base_url, api_key, model). Bare 'groq' uses its default."""
    provider, _, model = spec.partition("/")
    if provider not in PROVIDERS:
        raise SystemExit(f"Unknown provider '{provider}' in model '{spec}'. Use one of: {', '.join(PROVIDERS)} (as provider/model).")
    base_url, key_env, default = PROVIDERS[provider]
    model = model or default
    if provider == "groq" and model.startswith("gpt-oss"):
        model = "openai/" + model  # groq/gpt-oss-120b shorthand
    if not base_url:
        raise SystemExit("Set BROWSERPILOT_BASE_URL to use the 'custom' provider.")
    if not model:
        raise SystemExit(f"No model given for provider '{provider}' (use {provider}/<model>).")
    key = os.environ.get(key_env, "") if key_env else "none"
    if key_env and not key:
        if provider != "custom":
            raise SystemExit(f"{key_env} is not set. Put it in {HOME / '.env'} or your environment.")
        key = "none"
    return provider, base_url, key, model


@dataclass
class RunConfig:
    model: str = DEFAULT_MODEL
    planner_model: str | None = None
    agents: int = 4
    max_steps: int = 30
    headless: bool = False
    yes: bool = False
    vision: bool = False
    url: str | None = None
    out: Path | None = None
    keep_tabs: bool = False
    local_roots: tuple = ()      # folders whose .html files may be opened as file: pages
    confirm: object = None       # async (label, page url) -> bool; when set it replaces the terminal prompt for risky clicks
    ask: object = None           # async (question) -> str | None; when set it replaces the terminal prompt for ask_user
