"""Inspect actual Grok discovery without exposing config, hook bodies, or secrets."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile

CLAUDE_SURFACES = ("skills", "rules", "agents", "mcps", "hooks", "sessions")
SHARED_SKILLS = ("ai-media", "grok-imagine")
MAX_INSPECT_BYTES = 4 * 1024 * 1024


def analyze(data: dict, home: Path | None = None) -> dict:
    """Only allow-listed findings leave this function. Discovery is not execution."""
    home = home or Path.home()
    if not isinstance(data, dict):
        return {"status": "unverified", "problems": ["inspect_schema_unknown"]}
    compat = data.get("externalCompat")
    cells = compat.get("cells") if isinstance(compat, dict) else None
    if not isinstance(cells, list) or not all(isinstance(data.get(k), list) for k in
                                              ("skills", "hooks", "projectInstructions", "agents", "mcpServers")):
        return {"status": "unverified", "problems": ["inspect_schema_unknown"]}
    if any(not isinstance(row, dict) for field in
           ("skills", "hooks", "projectInstructions", "agents", "mcpServers") for row in data[field]):
        return {"status": "unverified", "problems": ["inspect_schema_unknown"]}
    selected = [c for c in cells if isinstance(c, dict) and c.get("vendor") == "claude"]
    settings = {}
    for surface in CLAUDE_SURFACES:
        values = [c.get("enabled") for c in selected if c.get("surface") == surface]
        settings[surface] = values[0] if len(values) == 1 and type(values[0]) is bool else None
    enabled = [s for s, value in settings.items() if value is True]
    unknown = [s for s, value in settings.items() if value is None]
    if any(c.get("surface") not in CLAUDE_SURFACES for c in selected):
        unknown.append("unknown_surface")
    imported = sum(
        isinstance(row, dict) and row.get("vendor") == "claude"
        and row.get("compatibilityStatus") not in ("disabled", "blocked")
        for field in ("skills", "hooks", "projectInstructions", "agents", "mcpServers")
        for row in data[field]
    )
    visible = {}
    for name in SHARED_SKILLS:
        expected = home / ".agents" / "skills" / name / "SKILL.md"
        visible[name] = any(
            isinstance(row, dict) and row.get("name") == name
            and isinstance(row.get("source"), dict)
            and row["source"].get("path") == str(expected)
            and row.get("compatibilityStatus") not in ("disabled", "blocked")
            for row in data["skills"]
        )
    problems = []
    if unknown:
        problems.append("compat_surface_unverified")
    if enabled or imported:
        problems.append("claude_import_enabled")
    if not all(visible.values()):
        problems.append("shared_skill_not_discovered")
    return {"status": "unverified" if unknown else "attention" if problems else "healthy",
            "problems": problems, "claude_surfaces": settings,
            "enabled_import_count": imported, "shared_skills": visible,
            "evidence": "discovery_only"}


def inspect() -> dict:
    # Files avoid unbounded PIPE buffers; neither stderr nor raw JSON is reported.
    with tempfile.TemporaryFile() as output:
        try:
            completed = subprocess.run(["grok", "inspect", "--json"], stdout=output,
                                       stderr=subprocess.DEVNULL, timeout=20)
            if completed.returncode:
                return {"status": "unverified", "problems": ["inspect_failed"]}
            if output.tell() > MAX_INSPECT_BYTES:
                return {"status": "unverified", "problems": ["inspect_output_too_large"]}
            output.seek(0)
            data = json.loads(output.read(MAX_INSPECT_BYTES))
        except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError):
            return {"status": "unverified", "problems": ["inspect_unavailable"]}
    return analyze(data)


if __name__ == "__main__":
    report = inspect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "healthy" else 65)
