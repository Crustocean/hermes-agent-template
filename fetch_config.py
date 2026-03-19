#!/usr/bin/env python3
"""
Fetch agent persona (SOUL.md) and config from the Crustocean API on startup.

Cloud Hermes agents store their persona and runtime config in the Crustocean
database rather than baked into the Docker image. This script pulls the latest
config before the gateway starts, so persona edits take effect on restart
without a rebuild.

Falls back to bundled defaults if the API is unreachable.
"""

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

HERMES_HOME = os.getenv("HERMES_HOME", "/data/hermes")
CONFIG_URL = os.getenv("CRUSTOCEAN_CONFIG_URL", "")
AGENT_TOKEN = os.getenv("CRUSTOCEAN_AGENT_TOKEN", "")
DEFAULTS_DIR = "/app/hermes-defaults"


def fetch_config():
    """Pull persona + config from Crustocean and write to HERMES_HOME."""
    if not CONFIG_URL or not AGENT_TOKEN:
        print("[hermes-template] No CRUSTOCEAN_CONFIG_URL or AGENT_TOKEN — using defaults")
        return False

    print(f"[hermes-template] Fetching config from {CONFIG_URL}")

    req = Request(CONFIG_URL, headers={
        "Authorization": f"Bearer {AGENT_TOKEN}",
        "Accept": "application/json",
    })

    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[hermes-template] Config fetch failed: {e} — using defaults")
        return False

    home = Path(HERMES_HOME)
    home.mkdir(parents=True, exist_ok=True)

    soul = data.get("soul_md", "")
    if soul:
        (home / "SOUL.md").write_text(soul, encoding="utf-8")
        print(f"[hermes-template] Wrote SOUL.md ({len(soul)} chars)")

    config_yaml = data.get("config_yaml", "")
    if config_yaml:
        (home / "config.yaml").write_text(config_yaml, encoding="utf-8")
        print(f"[hermes-template] Wrote config.yaml ({len(config_yaml)} chars)")

    skills = data.get("skills") or {}
    if skills:
        skills_dir = home / "skills"
        skills_dir.mkdir(exist_ok=True)
        for name, content in skills.items():
            safe_name = name.replace("/", "_").replace("\\", "_")
            if not safe_name.endswith(".md"):
                safe_name += ".md"
            (skills_dir / safe_name).write_text(content, encoding="utf-8")
        print(f"[hermes-template] Wrote {len(skills)} skill(s)")

    return True


def copy_defaults():
    """Copy bundled defaults to HERMES_HOME (fallback)."""
    home = Path(HERMES_HOME)
    home.mkdir(parents=True, exist_ok=True)
    defaults = Path(DEFAULTS_DIR)

    if not defaults.exists():
        return

    for src in defaults.iterdir():
        if src.is_file():
            dst = home / src.name
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        elif src.is_dir() and src.name == "skills":
            skills_dst = home / "skills"
            skills_dst.mkdir(exist_ok=True)
            for skill_file in src.iterdir():
                if skill_file.is_file():
                    (skills_dst / skill_file.name).write_text(
                        skill_file.read_text(encoding="utf-8"), encoding="utf-8"
                    )


def main():
    if not fetch_config():
        print("[hermes-template] Falling back to bundled defaults")
        copy_defaults()


if __name__ == "__main__":
    main()
