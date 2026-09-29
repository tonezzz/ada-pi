"""Drop-in tool loader — backend/tools.d/.

A tool is one module file plus one manifest line; no edits to
tool_runner.py or realtime_provider.py are needed to add a tool.

Module contract (backend/tools.d/<module>.py):

    DECLARATION = {"name": "ada_x", "description": "...", "parameters": {...}}

    async def run(runner, **args) -> Any:
        ...  # runner: the shared ToolRunner — .mddb, .banks,
             # .context.ha_client, ._memory_identity()

manifest.yml:

    tools:
      ada_x:
        module: ada_x            # tools.d/ada_x.py
        policy: read             # read | confirmed | owner_only
        secondary_allowed: false # deny to non-owner speakers (default)
        timeout_s: 20

A bad module must never break the backend: load errors are logged and the
tool is skipped (its declaration is never offered to the model).
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

logger = logging.getLogger("tools.loader")

TOOLS_DIR = Path(
    os.environ.get("ADA_TOOLS_DIR")
    or Path(__file__).resolve().parent / "tools.d"
)

VALID_POLICIES = {"read", "confirmed", "owner_only"}
DEFAULT_TIMEOUT_S = 20.0


@dataclass
class DynamicTool:
    name: str
    declaration: dict[str, Any]
    run: Callable[..., Any]
    policy: str = "read"
    secondary_allowed: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S
    module: str = ""


@dataclass
class ToolRegistry:
    tools: dict[str, DynamicTool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def declarations(self, excluded: set[str] | None = None) -> list[dict[str, Any]]:
        return [
            dict(t.declaration) for n, t in self.tools.items()
            if not excluded or n not in excluded
        ]


def _load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"tools_d.{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path.name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _validate(name: str, cfg: dict[str, Any], mod: Any) -> DynamicTool:
    decl = getattr(mod, "DECLARATION", None)
    if not isinstance(decl, dict) or not decl.get("name"):
        raise ValueError("missing DECLARATION dict with a 'name'")
    if decl["name"] != name:
        raise ValueError(f"DECLARATION name {decl['name']!r} != manifest key {name!r}")
    if not isinstance(decl.get("description"), str) or not decl["description"]:
        raise ValueError("DECLARATION needs a non-empty description")
    run = getattr(mod, "run", None)
    if not callable(run) or not inspect.iscoroutinefunction(run):
        raise ValueError("missing 'async def run(runner, **args)'")
    policy = str(cfg.get("policy") or "read")
    if policy not in VALID_POLICIES:
        raise ValueError(f"policy {policy!r} not in {sorted(VALID_POLICIES)}")
    return DynamicTool(
        name=name,
        declaration=decl,
        run=run,
        policy=policy,
        secondary_allowed=bool(cfg.get("secondary_allowed", False)),
        timeout_s=float(cfg.get("timeout_s") or DEFAULT_TIMEOUT_S),
        module=str(cfg.get("module") or name),
    )


def load(tools_dir: Path | None = None) -> ToolRegistry:
    """Load manifest + modules. Never raises — bad entries land in .errors."""
    reg = ToolRegistry()
    root = Path(tools_dir) if tools_dir else TOOLS_DIR
    manifest_path = root / "manifest.yml"
    if not manifest_path.is_file():
        return reg
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        reg.errors.append(f"manifest parse failed: {exc}")
        logger.error("tools.d manifest parse failed: %s", exc)
        return reg
    for name, cfg in (manifest.get("tools") or {}).items():
        name = str(name)
        cfg = cfg or {}
        try:
            if cfg.get("enabled") is False:
                continue
            mod_path = root / f"{cfg.get('module') or name}.py"
            mod = _load_module(mod_path)
            reg.tools[name] = _validate(name, cfg, mod)
        except Exception as exc:
            reg.errors.append(f"{name}: {exc}")
            logger.error("tools.d tool %r failed to load: %s", name, exc)
    if reg.tools:
        logger.info("tools.d: loaded %d tool(s): %s",
                    len(reg.tools), ", ".join(sorted(reg.tools)))
    for err in reg.errors:
        logger.error("tools.d: %s", err)
    return reg


_cache: tuple[float, ToolRegistry] | None = None


def registry(tools_dir: Path | None = None) -> ToolRegistry:
    """Process-wide registry, reloaded when manifest.yml changes."""
    global _cache
    root = Path(tools_dir) if tools_dir else TOOLS_DIR
    try:
        mtime = (root / "manifest.yml").stat().st_mtime
    except OSError:
        mtime = -1.0
    if _cache is None or _cache[0] != mtime:
        _cache = (mtime, load(root))
    return _cache[1]
