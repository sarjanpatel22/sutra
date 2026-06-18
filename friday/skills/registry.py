"""Skill registry — @skill decorator, auto-discovery, JSON schema generation."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import logging
import os
import pkgutil
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
        _registry[name] = SkillMeta(
            name=name,
            description=description,
            fn=fn,
            auto_confirm=auto_confirm,
            timeout_s=timeout_s,
            schema=schema,
        )
        return fn

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


def list_skills() -> list[str]:
    return list(_registry.keys())


async def call(name: str, **kwargs: Any) -> Any:
    """Invoke a registered skill by name with keyword arguments."""
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
    """Import all modules in the skills package to trigger @skill registration."""
    pkg = importlib.import_module(package)
    pkg_path = Path(pkg.__file__).parent  # type: ignore[arg-type]
    for _, module_name, _ in pkgutil.iter_modules([str(pkg_path)]):
        if module_name.startswith("_"):
            continue
        importlib.import_module(f"{package}.{module_name}")
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
