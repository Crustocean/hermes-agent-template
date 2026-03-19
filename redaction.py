"""
Secret redaction for Reina's output pipeline.

Applies 25+ regex patterns to strip API keys, tokens, passwords, SSH keys,
database connection strings, and other secrets from any text before it
reaches Crustocean or the LLM context window.

Patterns are applied in order. Each match is replaced with a redaction tag
like [REDACTED:aws_secret] so it's obvious something was removed without
leaking the value.
"""

import re
from typing import List, Tuple

# Each entry: (name, compiled_regex, replacement_template)
# Replacement uses {name} which gets filled with the pattern name.
_PATTERNS: List[Tuple[str, re.Pattern, str]] = []


def _p(name: str, pattern: str, flags: int = 0) -> None:
    _PATTERNS.append((
        name,
        re.compile(pattern, flags),
        f"[REDACTED:{name}]",
    ))


# ── AWS ──────────────────────────────────────────────────────────────────────

_p("aws_access_key",
   r"(?<![A-Z0-9])((?:AKIA|ABIA|ACCA|ASIA)[A-Z0-9]{16})(?![A-Z0-9])")

_p("aws_secret_key",
   r"""(?i)(?:aws.?secret.?(?:access)?.?key|secret.?key)\s*[:=]\s*['"]?([A-Za-z0-9/+=]{40})['"]?""")

_p("aws_session_token",
   r"""(?i)aws.?session.?token\s*[:=]\s*['"]?([A-Za-z0-9/+=]{100,})['"]?""")

# ── GitHub ───────────────────────────────────────────────────────────────────

_p("github_token",
   r"(ghp_[A-Za-z0-9]{36,}|gho_[A-Za-z0-9]{36,}|ghu_[A-Za-z0-9]{36,}|ghs_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")

_p("github_fine_grained",
   r"(github_pat_[A-Za-z0-9_]{22,255})")

# ── Stripe ───────────────────────────────────────────────────────────────────

_p("stripe_key",
   r"((?:sk|pk|rk)_(?:test|live)_[A-Za-z0-9]{10,99})")

# ── OpenAI ───────────────────────────────────────────────────────────────────

_p("openai_key",
   r"(sk-[A-Za-z0-9]{20,}T3BlbkFJ[A-Za-z0-9]{20,}|sk-proj-[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9_-]{40,})")

# ── Anthropic ────────────────────────────────────────────────────────────────

_p("anthropic_key",
   r"(sk-ant-[A-Za-z0-9_-]{40,})")

# ── OpenRouter ───────────────────────────────────────────────────────────────

_p("openrouter_key",
   r"(sk-or-v1-[A-Za-z0-9]{40,})")

# ── Nous Research ────────────────────────────────────────────────────────────

_p("nous_key",
   r"""(?i)nous.?api.?key\s*[:=]\s*['"]?([A-Za-z0-9_-]{32,})['"]?""")

# ── HuggingFace ──────────────────────────────────────────────────────────────

_p("huggingface_token",
   r"(hf_[A-Za-z0-9]{30,})")

# ── Slack ────────────────────────────────────────────────────────────────────

_p("slack_token",
   r"(xox[boaprs]-[A-Za-z0-9-]{10,})")

_p("slack_webhook",
   r"(https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+)")

# ── Discord ──────────────────────────────────────────────────────────────────

_p("discord_token",
   r"([MN][A-Za-z0-9]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,})")

_p("discord_webhook",
   r"(https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+)")

# ── Telegram ─────────────────────────────────────────────────────────────────

_p("telegram_bot_token",
   r"(\d{8,10}:[A-Za-z0-9_-]{35})")

# ── SSH / PEM private keys ───────────────────────────────────────────────────

_p("private_key_block",
   r"(-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY(?:\s+BLOCK)?-----[\s\S]*?-----END (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY(?:\s+BLOCK)?-----)",
   re.DOTALL)

# ── Database connection strings ──────────────────────────────────────────────

_p("postgres_uri",
   r"(postgres(?:ql)?://[^\s'\"<>]+:[^\s'\"<>]+@[^\s'\"<>]+)")

_p("mysql_uri",
   r"(mysql://[^\s'\"<>]+:[^\s'\"<>]+@[^\s'\"<>]+)")

_p("mongodb_uri",
   r"(mongodb(?:\+srv)?://[^\s'\"<>]+:[^\s'\"<>]+@[^\s'\"<>]+)")

_p("redis_uri",
   r"(redis(?:s)?://[^\s'\"<>]*:[^\s'\"<>]+@[^\s'\"<>]+)")

# ── JWT tokens ───────────────────────────────────────────────────────────────

_p("jwt_token",
   r"(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})")

# ── Generic API key patterns in key=value or key: value format ───────────────

_p("generic_api_key",
   r"""(?i)(?:api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token|secret[_-]?token|private[_-]?key|client[_-]?secret)\s*[:=]\s*['"]?([A-Za-z0-9_/+=-]{20,})['"]?""")

# ── Bearer tokens in headers ─────────────────────────────────────────────────

_p("bearer_token",
   r"""(?i)(?:authorization|bearer)\s*[:=]\s*(?:bearer\s+)?['"]?([A-Za-z0-9_/+=-]{20,})['"]?""")

# ── Basic auth in URLs ───────────────────────────────────────────────────────

_p("url_credentials",
   r"(https?://[^\s/:'\"]+:[^\s/@'\"]+@)")

# ── Firecrawl ────────────────────────────────────────────────────────────────

_p("firecrawl_key",
   r"(fc-[A-Za-z0-9]{30,})")

# ── Passwords in .env-style lines ────────────────────────────────────────────

_p("env_password",
   r"""(?i)(?:password|passwd|db_pass|database_password|secret)\s*[:=]\s*['"]?([^\s'\"]{8,})['"]?""")


def redact(text: str) -> str:
    """
    Scan text for secrets and replace each match with a [REDACTED:type] tag.
    Returns the redacted text.
    """
    if not text:
        return text

    for name, pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)

    return text


def has_secrets(text: str) -> bool:
    """Quick check: does this text contain anything that looks like a secret?"""
    if not text:
        return False
    for _, pattern, _ in _PATTERNS:
        if pattern.search(text):
            return True
    return False


def redact_dict(d: dict, keys_to_skip: set = None) -> dict:
    """
    Recursively redact string values in a dict.
    Useful for redacting metadata or tool results before they enter context.
    """
    if keys_to_skip is None:
        keys_to_skip = set()

    result = {}
    for k, v in d.items():
        if k in keys_to_skip:
            result[k] = v
        elif isinstance(v, str):
            result[k] = redact(v)
        elif isinstance(v, dict):
            result[k] = redact_dict(v, keys_to_skip)
        elif isinstance(v, list):
            result[k] = [
                redact(item) if isinstance(item, str)
                else redact_dict(item, keys_to_skip) if isinstance(item, dict)
                else item
                for item in v
            ]
        else:
            result[k] = v
    return result
