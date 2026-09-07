#!/usr/bin/env python3
"""Verify the external behaviours this design leans on.

Several load-bearing facts belong to other tools, not to us: Grok reading
Claude's skill directory, Codex allowing loopback out of its sandbox, Codex
exposing its image generator under a particular skill name. They are documented
by those tools but they are not contracts, and if one changes the failure shows
up somewhere far away — a skill that silently stops being offered, an escalation
that silently reverts to manual.

Each assumption is declared in ``backends.json`` with a cheap check and the
consequence of it breaking, so a drift is reported as itself rather than as a
mystery downstream.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CROSSCREW_BACKENDS_CONFIG", ROOT / "backends.json"))


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _expand(raw: str) -> Path:
    return Path(os.path.expanduser(raw))


def _dig(data, dotted: str):
    current = data
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _check_path_exists(spec: dict) -> dict:
    path = _expand(spec.get("path", ""))
    ok = path.exists()
    return {"holds": ok, "detail": f"{'있음' if ok else '없음'}: {path}"}


def _check_toml_key_true(spec: dict) -> dict:
    path = _expand(spec.get("path", ""))
    key = spec.get("key", "")
    if tomllib is None:
        return {"holds": None, "detail": "tomllib 없음 — 확인 불가"}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return {"holds": None, "detail": f"{path} 읽기 실패: {exc}"}
    value = _dig(data, key)
    if value is None:
        return {"holds": False, "detail": f"{key} 항목 자체가 없음 ({path})"}
    return {"holds": value is True, "detail": f"{key} = {value!r}"}


def _check_json_key_exists(spec: dict) -> dict:
    path = _expand(spec.get("path", ""))
    key = spec.get("key", "")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"holds": None, "detail": f"{path} 읽기 실패: {exc}"}
    value = _dig(data, key)
    return {"holds": value is not None, "detail": f"{key} {'존재' if value is not None else '부재'}"}


def _check_command_help_lacks(spec: dict) -> dict:
    command = spec.get("command", "")
    pattern = str(spec.get("pattern", "")).lower()
    if not shutil.which(command):
        return {"holds": None, "detail": f"{command} 미설치 — 확인 불가"}
    try:
        completed = subprocess.run(
            [command, "--help"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"holds": None, "detail": f"{command} --help 실패: {exc}"}
    text = (completed.stdout + completed.stderr).lower()
    present = pattern in text
    return {
        "holds": not present,
        "detail": f"'{pattern}' {'발견 — 전제 변경' if present else '없음'} ({command} --help)",
    }


CHECKS = {
    "path_exists": _check_path_exists,
    "toml_key_true": _check_toml_key_true,
    "json_key_exists": _check_json_key_exists,
    "command_help_lacks": _check_command_help_lacks,
}


def verify(config: dict | None = None) -> dict:
    config = config if config is not None else load_config()
    results = []
    for item in config.get("external_assumptions", {}).get("items", []):
        spec = item.get("check", {})
        handler = CHECKS.get(spec.get("type"))
        if handler is None:
            outcome = {"holds": None, "detail": f"알 수 없는 검사 유형: {spec.get('type')}"}
        else:
            outcome = handler(spec)
        results.append(
            {
                "id": item["id"],
                "statement": item.get("statement"),
                "impact": item.get("impact"),
                "holds": outcome["holds"],
                "detail": outcome["detail"],
            }
        )
    broken = [r["id"] for r in results if r["holds"] is False]
    unverified = [r["id"] for r in results if r["holds"] is None]
    return {
        "items": results,
        "broken": broken,
        "unverified": unverified,
        "status": "drifted" if broken else "holding",
    }


def main() -> int:
    report = verify()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "holding" else 65


if __name__ == "__main__":
    raise SystemExit(main())
