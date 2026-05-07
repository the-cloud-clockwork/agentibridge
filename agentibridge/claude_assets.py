"""Install/uninstall Claude Code assets shipped with agentibridge.

Symlinks each child of ``agentibridge/package/{skills,commands,agents,rules}/``
into ``~/.claude/<subdir>/`` and merges ``package/CLAUDE.md`` into
``~/.claude/CLAUDE.md`` inside an idempotent fenced block.

Mirrors the pattern in ``agentihooks/scripts/install.py``
(`_link_item`, `_cleanup_stale_links`, `_symlink_dir_contents`,
`_append_ci_manifesto_to_claude_md`, `_sweep_symlinks_into`) — scoped down
because agentibridge is a single source, not a chain of profiles.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent / "package"
CLAUDE_HOME = Path(os.getenv("CLAUDE_CODE_HOME_DIR", str(Path.home() / ".claude")))
CLAUDE_JSON = Path.home() / ".claude.json"
BEGIN_MARKER = "<!-- BEGIN agentibridge -->"
END_MARKER = "<!-- END agentibridge -->"

_SUBDIRS: list[tuple[str, str, Callable[[Path], bool]]] = [
    ("skills", "skill", lambda p: p.is_dir()),
    ("commands", "command", lambda p: p.suffix == ".md" and p.name != "README.md"),
    ("agents", "agent", lambda p: p.suffix == ".md" and p.name != "README.md"),
    ("rules", "rule", lambda p: p.suffix == ".md" and p.name != "README.md"),
]


def _link_item(item: Path, link: Path, label: str) -> None:
    """Create or refresh a single symlink ``link`` → ``item``."""
    if link.is_symlink():
        if link.resolve() == item.resolve():
            return
        link.unlink()
        link.symlink_to(item)
        print(f"  [OK] Re-linked {label} '{item.name}' → {item}")
        return
    if link.exists():
        print(f"  [!!] {label} '{item.name}' exists at {link} and is not a symlink — skipping")
        return
    link.symlink_to(item)
    print(f"  [OK] Linked {label} '{item.name}' → {item}")


def _cleanup_stale_links(
    dst_dir: Path,
    src_dir: Path,
    filter_fn: Callable[[Path], bool] | None,
) -> None:
    """Remove broken symlinks and symlinks that no longer pass ``filter_fn``."""
    if not dst_dir.is_dir():
        return
    for link in dst_dir.iterdir():
        if not link.is_symlink():
            continue
        try:
            target = link.resolve(strict=True)
        except FileNotFoundError:
            link.unlink()
            print(f"  [RM] Removed broken symlink: {link.name}")
            continue
        # Only sweep links pointing into our src_dir; leave foreign links alone.
        try:
            target.relative_to(src_dir.resolve())
        except ValueError:
            continue
        if filter_fn is not None and not filter_fn(target):
            link.unlink()
            print(f"  [RM] Removed stale symlink: {link.name}")


def _symlink_dir_contents(
    src_dir: Path,
    dst_dir: Path,
    *,
    label: str,
    filter_fn: Callable[[Path], bool] | None = None,
) -> None:
    """Symlink filtered children of ``src_dir`` into ``dst_dir``.

    Stale symlinks (broken or pointing to items that no longer pass the filter)
    are removed before new links are created.
    """
    if not src_dir.exists():
        return
    _cleanup_stale_links(dst_dir, src_dir, filter_fn)
    children = [c for c in src_dir.iterdir() if c.name != ".gitkeep" and (filter_fn is None or filter_fn(c))]
    if not children:
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in sorted(children):
        if not item.name.startswith("."):
            _link_item(item, dst_dir / item.name, label)


def _merge_claude_md() -> None:
    """Append ``package/CLAUDE.md`` into ``~/.claude/CLAUDE.md`` (idempotent)."""
    src = PACKAGE_DIR / "CLAUDE.md"
    if not src.exists():
        return
    dst = CLAUDE_HOME / "CLAUDE.md"
    body = src.read_text().rstrip()
    block = f"\n\n{BEGIN_MARKER}\n{body}\n{END_MARKER}\n"

    if not dst.exists():
        CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
        dst.write_text(block.lstrip())
        print(f"  [OK] Wrote {dst} with agentibridge block")
        return

    current = dst.read_text()
    if BEGIN_MARKER in current and END_MARKER in current:
        before = current.split(BEGIN_MARKER, 1)[0].rstrip()
        after = current.split(END_MARKER, 1)[1]
        new = before + block + after.lstrip("\n")
    else:
        new = current.rstrip() + block

    if new == current:
        return
    dst.write_text(new)
    print(f"  [OK] Merged agentibridge block into {dst} ({len(body):,} bytes)")


def _resolve_template_vars() -> dict[str, str]:
    """Values substituted into package/.mcp.json at install time."""
    return {
        "AGENTIBRIDGE_BIN": shutil.which("agentibridge") or "agentibridge",
        "PYTHON": shutil.which("python3") or "python3",
        "ENV_FILE": str(Path.home() / ".agentibridge" / "agentibridge.env"),
    }


def _merge_mcp_to_user_scope(servers: dict) -> None:
    """Merge ``servers`` into ``~/.claude.json`` ``mcpServers`` (last write wins per name)."""
    existing: dict = {}
    if CLAUDE_JSON.exists():
        try:
            existing = json.loads(CLAUDE_JSON.read_text())
        except json.JSONDecodeError as exc:
            print(f"  [!!] Could not parse {CLAUDE_JSON}: {exc} — skipping MCP merge")
            return
    existing_servers: dict = existing.get("mcpServers", {})
    added, updated = [], []
    for name, config in servers.items():
        if name in existing_servers:
            if existing_servers[name] != config:
                updated.append(name)
        else:
            added.append(name)
        existing_servers[name] = config
    existing["mcpServers"] = existing_servers
    CLAUDE_JSON.write_text(json.dumps(existing, indent=2) + "\n")
    if added:
        print(f"  [OK] Added user-scope MCP servers: {', '.join(added)}")
    if updated:
        print(f"  [OK] Updated user-scope MCP servers: {', '.join(updated)}")
    if not added and not updated:
        print(f"  [--] User-scope MCP servers unchanged: {', '.join(servers.keys())}")


def _remove_mcp_from_user_scope(server_names: list[str]) -> None:
    if not CLAUDE_JSON.exists():
        return
    try:
        existing = json.loads(CLAUDE_JSON.read_text())
    except json.JSONDecodeError:
        return
    servers = existing.get("mcpServers", {})
    removed = [name for name in server_names if servers.pop(name, None) is not None]
    if not removed:
        return
    existing["mcpServers"] = servers
    CLAUDE_JSON.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"  [RM] Removed user-scope MCP servers: {', '.join(removed)}")


def _load_mcp_template() -> dict:
    """Load and substitute placeholders in package/.mcp.json. Returns parsed dict."""
    src = PACKAGE_DIR / ".mcp.json"
    if not src.exists():
        return {}
    raw = src.read_text()
    for key, value in _resolve_template_vars().items():
        raw = raw.replace("${" + key + "}", value)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"  [!!] {src} parse failed after substitution: {exc}")
        return {}


def _install_mcp() -> None:
    config = _load_mcp_template()
    servers = config.get("mcpServers", {})
    if servers:
        _merge_mcp_to_user_scope(servers)


def _uninstall_mcp() -> None:
    """Remove the server names declared in package/.mcp.json from ~/.claude.json."""
    src = PACKAGE_DIR / ".mcp.json"
    if not src.exists():
        return
    try:
        config = json.loads(src.read_text())
    except json.JSONDecodeError:
        return
    servers = config.get("mcpServers", {})
    if servers:
        _remove_mcp_from_user_scope(list(servers.keys()))


def install_claude_assets() -> None:
    """Symlink package/{skills,commands,agents,rules}/ into ~/.claude/, merge CLAUDE.md and MCP servers."""
    if not PACKAGE_DIR.is_dir():
        return
    print("Installing agentibridge Claude assets…")
    for subdir, label, filter_fn in _SUBDIRS:
        _symlink_dir_contents(PACKAGE_DIR / subdir, CLAUDE_HOME / subdir, label=label, filter_fn=filter_fn)
    _merge_claude_md()
    _install_mcp()


def _strip_claude_md_block() -> None:
    dst = CLAUDE_HOME / "CLAUDE.md"
    if not dst.exists():
        return
    text = dst.read_text()
    if BEGIN_MARKER not in text:
        return
    before = text.split(BEGIN_MARKER, 1)[0].rstrip()
    after = text.split(END_MARKER, 1)[1] if END_MARKER in text else ""
    new = before
    if after.strip():
        new = before + "\n\n" + after.lstrip()
    new = new.rstrip() + "\n"
    if new != text:
        dst.write_text(new)
        print(f"  [OK] Stripped agentibridge block from {dst}")


def uninstall_claude_assets() -> None:
    """Remove symlinks pointing into PACKAGE_DIR and strip the fenced CLAUDE.md block."""
    package_root = str(PACKAGE_DIR.resolve()).rstrip("/")
    print("Removing agentibridge Claude assets…")
    for subdir, _label, _filter in _SUBDIRS:
        d = CLAUDE_HOME / subdir
        if not d.is_dir():
            continue
        for link in list(d.iterdir()):
            if not link.is_symlink():
                continue
            try:
                resolved = str(link.resolve()).rstrip("/")
                points_at_us = resolved == package_root or resolved.startswith(package_root + "/")
            except OSError:
                points_at_us = package_root in os.readlink(link)
            if points_at_us:
                link.unlink()
                print(f"  [RM] Removed {subdir}/{link.name}")
    _strip_claude_md_block()
    _uninstall_mcp()
