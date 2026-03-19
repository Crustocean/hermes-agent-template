#!/usr/bin/env python3
"""
Patch hermes-agent source to register the Crustocean platform adapter.

Run once after cloning hermes-agent. Modifies three files:
  1. gateway/config.py   — adds CRUSTOCEAN to the Platform enum
  2. gateway/config.py   — adds env-var overrides for Crustocean credentials
  3. gateway/run.py      — adds Crustocean to _create_adapter + auth maps

Idempotent: safe to run multiple times.
"""

import re
import sys
from pathlib import Path

HERMES_ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/app/hermes-agent")

CONFIG_PY = HERMES_ROOT / "gateway" / "config.py"
RUN_PY = HERMES_ROOT / "gateway" / "run.py"

MARKER = "CRUSTOCEAN"


def patch_config_enum(text: str) -> str:
    """Add CRUSTOCEAN = 'crustocean' to the Platform enum."""
    if MARKER in text:
        return text
    # Insert after the last enum member (HOMEASSISTANT line)
    return text.replace(
        '    HOMEASSISTANT = "homeassistant"',
        '    HOMEASSISTANT = "homeassistant"\n    CRUSTOCEAN = "crustocean"',
    )


def patch_config_env_overrides(text: str) -> str:
    """Add Crustocean env-var block to _apply_env_overrides()."""
    if "CRUSTOCEAN_AGENT_TOKEN" in text:
        return text

    block = '''
    # Crustocean
    crustocean_token = os.getenv("CRUSTOCEAN_AGENT_TOKEN")
    if crustocean_token:
        if Platform.CRUSTOCEAN not in config.platforms:
            config.platforms[Platform.CRUSTOCEAN] = PlatformConfig()
        config.platforms[Platform.CRUSTOCEAN].enabled = True
        config.platforms[Platform.CRUSTOCEAN].token = crustocean_token
        config.platforms[Platform.CRUSTOCEAN].extra["api_url"] = os.getenv(
            "CRUSTOCEAN_API_URL", "https://api.crustocean.chat"
        )
        config.platforms[Platform.CRUSTOCEAN].extra["handle"] = os.getenv(
            "CRUSTOCEAN_HANDLE", ""
        )
        config.platforms[Platform.CRUSTOCEAN].extra["agencies"] = [
            s.strip()
            for s in os.getenv("CRUSTOCEAN_AGENCIES", "lobby").split(",")
            if s.strip()
        ]
        crustocean_home = os.getenv("CRUSTOCEAN_HOME_CHANNEL")
        if crustocean_home:
            config.platforms[Platform.CRUSTOCEAN].home_channel = HomeChannel(
                platform=Platform.CRUSTOCEAN,
                chat_id=crustocean_home,
                name=os.getenv("CRUSTOCEAN_HOME_CHANNEL_NAME", "Home"),
            )
'''

    # Insert before the "# Session settings" comment in _apply_env_overrides
    anchor = "    # Session settings"
    if anchor in text:
        text = text.replace(anchor, block + anchor)
    else:
        # Fallback: append before function end (last line of the function)
        text += block

    return text


def patch_run_create_adapter(text: str) -> str:
    """Add Crustocean elif block to _create_adapter()."""
    if "Platform.CRUSTOCEAN" in text:
        return text

    crustocean_block = '''
        elif platform == Platform.CRUSTOCEAN:
            from gateway.platforms.crustocean import CrustoceanAdapter, check_crustocean_requirements
            if not check_crustocean_requirements():
                logger.warning("Crustocean: python-socketio or httpx not installed")
                return None
            return CrustoceanAdapter(config)
'''

    # Insert after the HomeAssistant block, before the final "return None"
    # Find the pattern: HomeAssistant block ... return None
    pattern = r"(return HomeAssistantAdapter\(config\)\n)"
    match = re.search(pattern, text)
    if match:
        insert_pos = match.end()
        text = text[:insert_pos] + crustocean_block + text[insert_pos:]
    else:
        # Fallback: insert before the standalone "return None" in _create_adapter
        text = text.replace(
            "        return None\n    \n    def _is_user_authorized",
            crustocean_block + "        return None\n    \n    def _is_user_authorized",
        )

    return text


def patch_run_auth_maps(text: str) -> str:
    """Add Crustocean entries to the authorized-users and allow-all maps."""
    if "CRUSTOCEAN_ALLOWED_USERS" in text:
        return text

    # Allowed users map
    text = text.replace(
        '            Platform.SLACK: "SLACK_ALLOWED_USERS",',
        '            Platform.SLACK: "SLACK_ALLOWED_USERS",\n'
        '            Platform.CRUSTOCEAN: "CRUSTOCEAN_ALLOWED_USERS",',
    )

    # Allow-all map
    text = text.replace(
        '            Platform.SLACK: "SLACK_ALLOW_ALL_USERS",',
        '            Platform.SLACK: "SLACK_ALLOW_ALL_USERS",\n'
        '            Platform.CRUSTOCEAN: "CRUSTOCEAN_ALLOW_ALL_USERS",',
    )

    # Logger name map (if present)
    text = text.replace(
        '            Platform.SLACK: "hermes-slack",',
        '            Platform.SLACK: "hermes-slack",\n'
        '            Platform.CRUSTOCEAN: "hermes-crustocean",',
    )

    # Platform short name map (if present)
    text = text.replace(
        '            Platform.SLACK: "slack",',
        '            Platform.SLACK: "slack",\n'
        '            Platform.CRUSTOCEAN: "crustocean",',
    )

    # Toolset map — give Crustocean the full telegram toolset
    text = text.replace(
        '            Platform.SLACK: "hermes-slack",\n        }',
        '            Platform.SLACK: "hermes-slack",\n'
        '            Platform.CRUSTOCEAN: "hermes-telegram",\n        }',
    )

    # Platform config key map — add crustocean
    text = text.replace(
        '            Platform.SLACK: "slack",\n        }.get(source.platform, "telegram")',
        '            Platform.SLACK: "slack",\n'
        '            Platform.CRUSTOCEAN: "crustocean",\n        }.get(source.platform, "telegram")',
    )

    return text


def patch_run_tool_import(text: str) -> str:
    """Add early import of crustocean_tools so tools register before the gateway starts."""
    if "crustocean_tools" in text:
        return text

    import_block = (
        "\n# Crustocean command tools — registered at import time\n"
        "try:\n"
        "    import tools.crustocean_tools  # noqa: F401\n"
        "except ImportError:\n"
        "    pass\n"
    )

    # Insert before the first function definition (after module-level imports)
    match = re.search(r"\ndef ", text)
    if match:
        text = text[:match.start()] + import_block + text[match.start():]
    else:
        text += import_block

    return text


def main():
    print(f"Patching hermes-agent at {HERMES_ROOT} ...")

    # --- gateway/config.py ---
    config_text = CONFIG_PY.read_text(encoding="utf-8")
    config_text = patch_config_enum(config_text)
    config_text = patch_config_env_overrides(config_text)
    CONFIG_PY.write_text(config_text, encoding="utf-8")
    print("  ✓ gateway/config.py patched")

    # --- gateway/run.py ---
    run_text = RUN_PY.read_text(encoding="utf-8")
    run_text = patch_run_create_adapter(run_text)
    run_text = patch_run_auth_maps(run_text)
    run_text = patch_run_tool_import(run_text)
    RUN_PY.write_text(run_text, encoding="utf-8")
    print("  ✓ gateway/run.py patched")

    # Verify
    config_final = CONFIG_PY.read_text(encoding="utf-8")
    run_final = RUN_PY.read_text(encoding="utf-8")
    ok = True
    if "CRUSTOCEAN" not in config_final:
        print("  ✗ CRUSTOCEAN not found in config.py after patch")
        ok = False
    if "CrustoceanAdapter" not in run_final:
        print("  ✗ CrustoceanAdapter not found in run.py after patch")
        ok = False
    if "crustocean_tools" not in run_final:
        print("  ✗ crustocean_tools import not found in run.py after patch")
        ok = False
    if ok:
        print("  ✓ All patches verified")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
