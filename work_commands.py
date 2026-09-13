"""Caller-specified exact AGY commands; never infer grants from a worker brief."""
from __future__ import annotations

import re

MAX_COMMANDS = 16
MAX_COMMAND_LENGTH = 4096


def validate(value: object, provider: str, profile: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_COMMANDS:
        raise ValueError("allow_commands must be a list of at most 16 commands")
    if value and (provider != "agy" or profile != "work"):
        raise ValueError("--allow-command requires agy --profile work")
    commands = []
    for command in value:
        if (not isinstance(command, str) or not command.strip()
                or len(command) > MAX_COMMAND_LENGTH
                or any(ord(char) < 32 or ord(char) == 127 for char in command)):
            raise ValueError("commands must be nonempty, single-line strings of at most 4096 characters")
        if command not in commands:
            commands.append(command)
    return commands


def permission_rules(commands: list[str]) -> list[str]:
    rules = []
    for command in validate(commands, "agy", "work"):
        # Escape regex metacharacters only: backslash-space from re.escape is
        # not portable across native regex engines. No prefix/wildcard grants.
        literal = re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", command)
        rules.extend(f"{action}(regex:^{literal}$)" for action in ("command", "unsandboxed"))
    return rules
