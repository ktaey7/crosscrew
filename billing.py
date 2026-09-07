"""Detect known API-auth overrides without reading or printing credentials.

Absence of these indicators is not proof of subscription billing. Native CLIs
may retain API authentication or custom providers in other locations.
"""
from pathlib import Path
import json
import os
import tomllib

COMMON = ('OPENAI_API_KEY','CODEX_API_KEY','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN',
          'XAI_API_KEY','GROK_API_KEY','GROK_API_TOKEN','GEMINI_API_KEY','GOOGLE_API_KEY',
          'OPENAI_BASE_URL','ANTHROPIC_BASE_URL','CLAUDE_CODE_USE_BEDROCK',
          'CLAUDE_CODE_USE_VERTEX','CLAUDE_CODE_USE_FOUNDRY','GOOGLE_GENAI_USE_VERTEXAI')


def inspect(provider, target, *, env=None, home=None):
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    indicators = ['env:' + name for name in COMMON if env.get(name) not in (None, '', '0', 'false')]
    configs = []
    if provider == 'claude':
        settings = [home / '.claude/settings.json', home / '.claude/settings.local.json']
        target = Path(target).resolve()
        for parent in (target, *target.parents):
            settings += [parent / '.claude/settings.json', parent / '.claude/settings.local.json']
        for path in dict.fromkeys(settings):
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text())
                if not isinstance(data, dict):
                    raise ValueError()
                if data.get('apiKeyHelper'):
                    configs.append('claude:apiKeyHelper')
                configured = data.get('env', {})
                if not isinstance(configured, dict):
                    raise ValueError()
                configs += ['claude:settings.env.' + k for k in COMMON if configured.get(k) not in (None, '', '0', 'false')]
            except (OSError, ValueError):
                configs.append('claude:settings_unreadable')
    if provider == 'codex':
        path = Path(env.get('CODEX_HOME', home / '.codex')) / 'config.toml'
        if path.exists():
            try:
                data = tomllib.loads(path.read_text())
                if data.get('model_provider', 'openai') != 'openai':
                    configs.append('codex:custom_model_provider')
                if data.get('forced_login_method') == 'api':
                    configs.append('codex:api_login')
            except (OSError, ValueError):
                configs.append('codex:config_unreadable')
    indicators = sorted(set(indicators + configs))
    return {'status': 'blocked_known_override' if indicators else 'native_cli_auth_unverified',
            'indicators': indicators}


def refusal(provider, target):
    report = inspect(provider, target)
    if not report['indicators']:
        return None
    return {'status': 'billing_configuration_blocked', 'exit_code': 78,
            'provider': provider, 'indicators': report['indicators'],
            'hint': 'Known API/cloud authentication overrides detected. Review native CLI authentication in a clean environment. Crosscrew does not change it or fall back to an API.'}
