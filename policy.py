#!/usr/bin/env python3
"""Evaluate whether the registry's role rules and assumptions have gone stale.

Role restrictions and capability workarounds are judgements made at a point in
time — "agy has no frontier code model", "codex is the better image model",
"agy cannot hold a session". Providers move; the judgements do not. Written as
prose in four entry-point documents, they aged silently and nothing pointed at
them again.

So each rule records its basis, the basis it has already outlived, and the
condition under which it should be revisited. Where that condition is
machine-checkable this module checks it and reports the ones that have fired.
Where it is not, the rule is still surfaced so it gets read periodically instead
of never.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CROSSCREW_BACKENDS_CONFIG", ROOT / "backends.json"))

MODEL_LIST_COMMAND = {
    "agy": ["agy", "models"],
    "grok": ["grok", "models"],
}
# gemini-3.6-flash-high / gemini-3.1-pro-low / grok-4.5 ...
MODEL_GENERATION = re.compile(r"(?P<stem>[a-z]+)-(?P<major>\d+)(?:\.(?P<minor>\d+))?(?P<rest>[-\w]*)")


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _provider_models(provider: str) -> tuple[list[str] | None, str]:
    """Return (models, reason). A watchman that cannot check must say why."""
    command = MODEL_LIST_COMMAND.get(provider)
    if not command:
        return None, f"{provider}에 모델 목록 명령이 등록돼 있지 않음"
    resolved = shutil.which(command[0])
    if not resolved:
        return None, f"{command[0]} 실행파일을 PATH에서 찾지 못함 (PATH={os.environ.get('PATH','')[:120]})"
    # These CLIs are occasionally slow to answer on a cold start, which showed up
    # as a spurious "cannot verify" during development. One retry separates a
    # transient stall from a real change, and an unverified result never counts as
    # a fired trigger either way.
    attempts = 2
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            completed = subprocess.run(
                [resolved, *command[1:]],
                capture_output=True,
                text=True,
                timeout=90,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            last = f"{command[0]} {command[1]} 90초 초과 (시도 {attempt}/{attempts})"
            continue
        except OSError as exc:
            return None, f"{command[0]} 실행 실패: {exc}"
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            last = (
                f"{command[0]} {command[1]} 종료코드 {completed.returncode}"
                + (f" — {detail[0][:120]}" if detail else "")
                + f" (시도 {attempt}/{attempts})"
            )
            continue
        models = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if models:
            return models, "ok"
        last = f"{command[0]} {command[1]} 출력이 비어 있음 (시도 {attempt}/{attempts})"
    return None, last


def _check_model_generation_above(check: dict) -> dict:
    provider = check.get("provider", "")
    family = str(check.get("family", "")).lower()
    recorded = int(check.get("recorded_major", 0))
    models, reason = _provider_models(provider)
    if models is None:
        return {"state": "unverified", "detail": f"{provider} 모델 목록 조회 실패 — {reason}"}
    newer = []
    for name in models:
        lowered = name.lower()
        if family and family not in lowered:
            continue
        match = MODEL_GENERATION.match(lowered)
        if match and int(match.group("major")) > recorded:
            newer.append(name)
    if newer:
        return {
            "state": "fired",
            "detail": f"{family} 계열 신세대 발견: {', '.join(sorted(newer)[:5])} (기록된 세대 {recorded})",
        }
    return {
        "state": "holds",
        "detail": f"{family} 계열 최신이 여전히 세대 {recorded} 이하 ({len(models)}개 조회)",
    }


def _check_no_other_provider_declares_kind(check: dict, config: dict) -> dict:
    kind = check.get("kind")
    expected = set(check.get("expected_providers", []))
    actual = {
        name
        for name, entry in config.get("providers", {}).items()
        if kind in (entry.get("media_capabilities", {}).get("kinds") or [])
    }
    if actual != expected:
        return {
            "state": "fired",
            "detail": f"{kind} 선언 provider가 {sorted(actual)} 로 변경됨 (기록: {sorted(expected)})",
        }
    return {"state": "holds", "detail": f"{kind}는 여전히 {sorted(expected)} 전용"}


def evaluate_check(check: dict, config: dict) -> dict:
    kind = (check or {}).get("type", "manual")
    if kind == "provider_model_generation_above":
        return _check_model_generation_above(check)
    if kind == "no_other_provider_declares_kind":
        return _check_no_other_provider_declares_kind(check, config)
    if kind == "manual":
        result = {"state": "manual", "detail": "사람이 판단해야 하는 조건"}
        if check.get("how"):
            result["how"] = check["how"]
        return result
    return {"state": "unverified", "detail": f"알 수 없는 검사 유형: {kind}"}


def review(config: dict | None = None) -> dict:
    config = config if config is not None else load_config()
    rules = []
    for rule in config.get("role_policy", {}).get("rules", []):
        verdict = evaluate_check(rule.get("revisit_when", {}).get("check", {}), config)
        rules.append(
            {
                "id": rule["id"],
                "kind": rule.get("kind"),
                "providers": rule.get("providers", []),
                "statement": rule.get("statement"),
                "basis": rule.get("basis"),
                "obsolete_basis": rule.get("obsolete_basis", []),
                "recorded_at": rule.get("recorded_at"),
                "revisit_human": rule.get("revisit_when", {}).get("human"),
                "verdict": verdict,
            }
        )
    assumptions = []
    for item in config.get("capability_assumptions", {}).get("items", []):
        verdict = evaluate_check(item.get("revisit_when", {}).get("check", {}), config)
        assumptions.append(
            {
                "id": item["id"],
                "provider": item.get("provider"),
                "affects": item.get("affects"),
                "statement": item.get("statement"),
                "observed_at": item.get("observed_at"),
                "revisit_human": item.get("revisit_when", {}).get("human"),
                "verdict": verdict,
            }
        )
    fired = [r["id"] for r in rules + assumptions if r["verdict"]["state"] == "fired"]
    outlived = [r["id"] for r in rules if r["obsolete_basis"]]
    return {
        "rules": rules,
        "assumptions": assumptions,
        "fired": fired,
        "rules_with_outlived_basis": outlived,
        "status": "revisit_due" if fired else "current",
    }


def main() -> int:
    report = review()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "current" else 65


if __name__ == "__main__":
    raise SystemExit(main())
