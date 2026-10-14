"""Skill registry — @skill decorator, auto-discovery, JSON schema generation."""

from __future__ import annotations

import functools
import importlib
import importlib.util
import inspect
import json
import logging
import os
import pkgutil
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import TypeAdapter

F = TypeVar("F", bound=Callable[..., Coroutine[Any, Any, Any]])


@dataclass
class SkillMeta:
    name: str
    description: str
    fn: Callable[..., Any]
    auto_confirm: bool = True
    timeout_s: int = 30
    schema: dict[str, Any] = field(default_factory=dict)


_registry: dict[str, SkillMeta] = {}
_log = logging.getLogger(__name__)

# Approved self-extended (Sutra-authored) skills live OUTSIDE the source tree and load on the
# next run. Written only by `sutra skill approve`; see friday/core/skillsmith.py.
USER_SKILLS_DIR = Path(os.getenv(
    "SUTRA_USER_SKILLS_DIR",
    str(Path.home() / ".local" / "share" / "friday" / "user_skills"),
))
_loaded_user: set[str] = set()

# Builtin skill modules that failed to import on the last autodiscover (usually a missing
# optional dependency — playwright, faster-whisper, …). Recorded so `sutra doctor` can say
# exactly which skills are unavailable and why, instead of them silently never existing.
_failed_imports: list[tuple[str, str]] = []


def failed_imports() -> list[tuple[str, str]]:
    """(module_name, error) for each builtin skill that couldn't import last discovery."""
    return list(_failed_imports)


def skill(
    name: str,
    description: str,
    *,
    auto_confirm: bool = True,
    timeout_s: int = 30,
) -> Callable[[F], F]:
    """Decorator that registers an async function as a callable skill.

    The function's type-annotated parameters are used to auto-generate the
    JSON schema exposed to the LLM via tool-calling.
    """

    def decorator(fn: F) -> F:
        schema = _build_schema(fn, name, description)
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def _safe(*args: Any, **kwargs: Any) -> Any:
            # Argument-binding errors (an invented/extra kwarg) must propagate: the agent's
            # tool loop turns them into the helpful "expected_params" correction. So bind the
            # signature BEFORE the guard — only the skill BODY is wrapped.
            sig.bind(*args, **kwargs)
            # Enforce the never-raise skill contract at one place: a skill that lets an
            # unexpected exception escape (e.g. urllib raising ValueError/InvalidURL on a
            # malformed URL) returns a clean error dict instead. CancelledError/KeyboardInterrupt
            # are BaseException, not Exception, so cooperative cancellation still propagates.
            try:
                return await fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — contract: skills return errors, never raise
                _log.warning("skill %s raised %s: %s", name, type(e).__name__, e)
                return {"error": f"{name} failed: {type(e).__name__}: {e}"}

        _registry[name] = SkillMeta(
            name=name,
            description=description,
            fn=_safe,
            auto_confirm=auto_confirm,
            timeout_s=timeout_s,
            schema=schema,
        )
        return _safe  # type: ignore[return-value]

    return decorator


def get(name: str) -> SkillMeta:
    try:
        return _registry[name]
    except KeyError:
        raise KeyError(f"Skill '{name}' not registered") from None


def register_external(
    name: str,
    description: str,
    fn: Callable[..., Any],
    *,
    schema: dict[str, Any],
    auto_confirm: bool = False,
    timeout_s: int = 60,
) -> None:
    """Register a non-decorator skill (e.g. an MCP tool) with a pre-built schema.

    The MCP bridge uses this to surface external tools through the same registry
    the agent loop already reads, so `all_tools()` / `call()` need no changes.
    Defaults to auto_confirm=False — third-party tools are untrusted until proven.
    """
    _registry[name] = SkillMeta(
        name=name,
        description=description,
        fn=fn,
        auto_confirm=auto_confirm,
        timeout_s=timeout_s,
        schema=schema,
    )


def unregister(name: str) -> bool:
    """Remove a registered skill. Returns True if it was present. Used on MCP teardown."""
    return _registry.pop(name, None) is not None


def all_tools() -> list[dict[str, Any]]:
    """Return all registered skills as LLM tool-call schemas."""
    return [meta.schema for meta in _registry.values()]


# General-purpose, high-frequency skills exposed on EVERY turn — the ones plausibly useful in any
# task. Everything else (desktop, browser, git, vpn, encoding, prefs, vision, …) is SPECIALIZED and
# surfaced on demand by relevance to the turn, so the model isn't shown 50+ tools at once — which
# degrades tool choice (the local function-caller mis-selects past ~48 tools) and wastes input
# tokens. See select_tools(). Names that aren't registered are simply ignored (forgiving).
_CORE_SKILLS = frozenset({
    "web_search", "web_extract", "read_file", "write_file", "edit_file", "list_directory",
    "run_command", "run_python", "calc", "current_time", "system_info", "recall", "http_request",
})


# Generic words that appear in too many skill descriptions to signal relevance — dropped from the
# QUERY before scoring so "how are you" doesn't spuriously surface half the registry. Deliberately
# excludes action verbs that ARE meaningful skill terms (get/set/run/open/read/write/search/…).
_QUERY_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at", "for", "with", "from",
    "by", "my", "your", "our", "their", "this", "that", "these", "those", "it", "its", "is",
    "are", "was", "were", "be", "been", "being", "do", "does", "did", "how", "what", "whats",
    "when", "where", "who", "why", "which", "then", "than", "so", "if", "as", "i", "you", "we",
    "us", "me", "please", "now", "today", "here", "there", "some", "any", "all", "into", "out",
})


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _content_terms(text: str) -> set[str]:
    """Query tokens worth matching on — drop stopwords and 1-2 char noise so only content words
    (decode, base64, weather, vpn, commit, …) drive selection."""
    return {t for t in _tokenize(text) if len(t) > 2 and t not in _QUERY_STOPWORDS}


def _relevance(query_terms: set[str], meta: SkillMeta) -> int:
    """Cheap lexical score: how many distinct (content) query terms appear in the skill's name or
    description. Deterministic, offline, no model call. 0 means 'not obviously related'."""
    if not query_terms:
        return 0
    return len(query_terms & _tokenize(f"{meta.name} {meta.description}"))


def select_tools(query: str, *, limit: int = 0) -> list[dict[str, Any]]:
    """Tool schemas relevant to `query`: the always-on core PLUS the highest-relevance specialized
    skills, capped near `limit`. Returns ALL tools when limit<=0 or the registry already fits within
    limit — so small installs and an explicit 'show everything' (SUTRA_MAX_TOOLS=0) are unchanged.
    On-demand loading: keeps the model focused and the prompt cheap once skills outgrow `limit`."""
    metas = list(_registry.values())
    if limit <= 0 or len(metas) <= limit:
        return [m.schema for m in metas]
    q_terms = _content_terms(query)
    core = [m for m in metas if m.name in _CORE_SKILLS]
    scored = sorted(
        ((m, _relevance(q_terms, m)) for m in metas if m.name not in _CORE_SKILLS),
        key=lambda ms: (-ms[1], ms[0].name),
    )
    budget = max(0, limit - len(core))
    extra = [m for m, score in scored if score > 0][:budget]
    return [m.schema for m in core + extra]


def list_skills() -> list[str]:
    return list(_registry.keys())


async def call(name: str, /, **kwargs: Any) -> Any:
    """Invoke a registered skill by name with keyword arguments.

    `name` is positional-only (note the `/`): a skill is free to declare a parameter literally
    called `name` (e.g. dns_lookup) without colliding with this skill-name argument — otherwise
    `call('dns_lookup', name='example.com')` raises 'multiple values for name'. The agent invokes
    every tool through here as `call(fn_name, **model_args)`, so the collision was a live bug."""
    meta = get(name)
    return await meta.fn(**kwargs)


def _build_schema(fn: Callable[..., Any], name: str, description: str) -> dict[str, Any]:
    """Generate an OpenAI-compatible tool schema from a function's signature."""
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue

        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            annotation = str  # default to string if unannotated

        try:
            adapter = TypeAdapter(annotation)
            json_schema = adapter.json_schema()
        except Exception:  # noqa: BLE001
            json_schema = {"type": "string"}

        # Pull docstring param descriptions if present
        doc = inspect.getdoc(fn) or ""
        param_desc = _extract_param_doc(doc, param_name)
        if param_desc:
            json_schema["description"] = param_desc

        properties[param_name] = json_schema

        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _extract_param_doc(docstring: str, param_name: str) -> str:
    """Extract param description from a Google-style or plain docstring."""
    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{param_name}:") or stripped.startswith(f"{param_name} ("):
            parts = stripped.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return ""


def autodiscover(package: str = "friday.skills") -> None:
    """Import all modules in the skills package to trigger @skill registration.

    A module that fails to import — typically a missing optional dependency for a
    feature skill (playwright, faster-whisper, …) — is skipped with a warning and
    recorded in failed_imports(), NEVER allowed to crash discovery. One unavailable
    skill must not take the whole registry, and the rest of Sutra, down with it.
    (The user-skill loader below already worked this way; the builtin loop did not.)
    """
    pkg = importlib.import_module(package)
    pkg_path = Path(pkg.__file__).parent  # type: ignore[arg-type]
    failed: list[tuple[str, str]] = []
    for _, module_name, _ in pkgutil.iter_modules([str(pkg_path)]):
        if module_name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{package}.{module_name}")
        except Exception as e:  # noqa: BLE001 — a broken/optional skill must not break discovery
            summary = f"{type(e).__name__}: {e}"
            failed.append((module_name, summary))
            _log.warning("skill module %r unavailable, skipping: %s", module_name, summary)
    _failed_imports[:] = failed  # replace in place so failed_imports() stays current
    _load_user_skills()


def _load_user_skills() -> None:
    """Load APPROVED self-extended skills from USER_SKILLS_DIR (once per file, per process).

    These are skills Sutra drafted and the user explicitly approved via `sutra skill approve`,
    kept outside the source tree. A broken one is skipped, never crashing discovery.
    """
    if not USER_SKILLS_DIR.is_dir():
        return
    for f in sorted(USER_SKILLS_DIR.glob("*.py")):
        if f.name.startswith("_") or str(f) in _loaded_user:
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"friday.skills.user_{f.stem}", f)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)  # runs @skill registration
                _loaded_user.add(str(f))
        except Exception as e:  # noqa: BLE001 — a bad user skill must not break discovery
            _log.warning("could not load user skill %s: %s", f, e)


def dump_registry() -> str:
    return json.dumps(
        {name: {"description": m.description, "auto_confirm": m.auto_confirm}
         for name, m in _registry.items()},
        indent=2,
    )
