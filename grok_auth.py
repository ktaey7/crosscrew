"""Refresh an expiring native login before entering Grok's strict sandbox.

The strict profile cannot update the global auth cache. Use the provider's
model-list command, which makes no model generation request. Never export a
credential or implement token refresh ourselves.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import time


def needs_refresh() -> bool:
    source = Path(os.environ.get('GROK_HOME', str(Path.home() / '.grok'))) / 'auth.json'
    try:
        if source.stat().st_size > 1024 * 1024:
            return False
        data = json.loads(source.read_text())
        if not isinstance(data, dict):
            return False
        expiries = []
        for entry in data.values():
            if not isinstance(entry, dict) or not isinstance(entry.get('key'), str):
                continue
            parts = entry['key'].split('.')
            if len(parts) != 3:
                continue
            try:
                claims = json.loads(base64.urlsafe_b64decode(parts[1] + '==='))
                expiry = claims.get('exp') if isinstance(claims, dict) else None
                if isinstance(expiry, (int, float)) and not isinstance(expiry, bool):
                    expiries.append(expiry)
            except (ValueError, UnicodeError):
                continue
        # Unknown credentials and API-key logins remain native CLI concerns.
        return bool(expiries) and all(expiry <= time.time() + 300 for expiry in expiries)
    except (OSError, ValueError, UnicodeError):
        return False


def prepare(command: str) -> str | None:
    if not needs_refresh():
        return None
    try:
        result = subprocess.run([command, 'models'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 'auth_preflight_failed'
    if result.returncode:
        return 'auth_preflight_failed'
    return 'auth_expired' if needs_refresh() else None
