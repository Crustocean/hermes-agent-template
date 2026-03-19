"""
Crustocean platform tools for Hermes Agent.

Gives Reina the ability to execute slash commands, discover commands,
observe rooms, traverse the platform, and join new rooms.

Tools register at import time via registry.register(). The adapter
reference is set later by CrustoceanAdapter.connect() — the check_fn
gates availability on CRUSTOCEAN_AGENT_TOKEN so the tools only appear
when Crustocean is the active platform.
"""

import json as _json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Set by CrustoceanAdapter.connect(), cleared on disconnect.
_adapter = None


def set_adapter(adapter):
    global _adapter
    _adapter = adapter


def clear_adapter():
    global _adapter
    _adapter = None


def _check_available():
    return bool(os.getenv("CRUSTOCEAN_AGENT_TOKEN"))


# ── Schemas ───────────────────────────────────────────────────────────

RUN_COMMAND_SCHEMA = {
    "name": "run_command",
    "description": (
        "Execute a Crustocean slash command and get the result back. "
        "By default the result comes back to you only (silent). "
        "Set visible: true to post the command in the room for everyone — "
        "you still get the output either way. "
        "Examples: /who, /roll 2d6, /balance, /notes, /checkin, /custom"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": 'Full slash command (e.g. "/who", "/roll 2d6", "/notes")',
            },
            "room": {
                "type": "string",
                "description": "Room slug to run the command in (uses current room if omitted)",
            },
            "visible": {
                "type": "boolean",
                "description": (
                    "If true, the command and its output are posted in the room "
                    "for everyone to see. Default: false (silent)."
                ),
            },
        },
        "required": ["command"],
    },
}

DISCOVER_COMMANDS_SCHEMA = {
    "name": "discover_commands",
    "description": (
        "Search or browse available Crustocean slash commands. "
        "Returns the command list, optionally filtered by a search term. "
        "There are 60+ commands across the platform — use this to find "
        "ones you don't know about. Some rooms also have custom hooks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "search": {
                "type": "string",
                "description": 'Optional search term to filter commands (e.g. "dice", "tip", "save")',
            },
            "room": {
                "type": "string",
                "description": "Room to check — some rooms have custom hooks installed",
            },
        },
    },
}

OBSERVE_ROOM_SCHEMA = {
    "name": "observe_room",
    "description": (
        "Read recent messages from a room to see what people have been "
        "talking about. Use this to look before you leap — check what's "
        "happening before deciding whether to say something."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "room": {
                "type": "string",
                "description": 'Room slug (e.g. "lobby", "boardroom")',
            },
            "limit": {
                "type": "number",
                "description": "Number of messages to fetch (default 20, max 50)",
            },
        },
        "required": ["room"],
    },
}

LIST_ROOMS_SCHEMA = {
    "name": "list_rooms",
    "description": (
        "List all rooms on Crustocean you can see, and whether you've "
        "joined them. Use this to get a lay of the land."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

JOIN_ROOM_SCHEMA = {
    "name": "join_room",
    "description": (
        "Join a room you're not currently in. "
        "Once joined, you can observe it, run commands in it, and talk there."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "room": {
                "type": "string",
                "description": "Room slug to join",
            },
        },
        "required": ["room"],
    },
}

EXPLORE_PLATFORM_SCHEMA = {
    "name": "explore_platform",
    "description": (
        "Explore what exists on Crustocean — rooms, agents, users, or "
        "webhooks/hooks. Use this to discover the world around you."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "what": {
                "type": "string",
                "enum": ["rooms", "agents", "users", "webhooks"],
                "description": "What to explore",
            },
            "search": {
                "type": "string",
                "description": "Optional search query to filter results",
            },
        },
        "required": ["what"],
    },
}

SEND_MESSAGE_SCHEMA = {
    "name": "crustocean_send",
    "description": (
        "Send a message to any Crustocean room or DM a user. "
        "Pass a room slug to post in that room (e.g. 'lobby', 'boardroom', 'the-barnacle'), "
        "or pass a username to DM that person (e.g. 'clawdia', '@ben'). "
        "This is how you talk across rooms — you can post to any room from anywhere. "
        "If no DM exists with the user, one will be created automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    'Room slug (e.g. "lobby") or username (e.g. "clawdia", "@ben") '
                    "to send the message to"
                ),
            },
            "content": {
                "type": "string",
                "description": "The message content to send",
            },
        },
        "required": ["target", "content"],
    },
}


MAP_ENVIRONMENT_SCHEMA = {
    "name": "map_environment",
    "description": (
        "Run the Worm Protocol: perform a full discovery sweep of a room. "
        "Gathers commands, members, custom hooks, recent activity, and economy "
        "state, then returns a structured environment map. Optionally persists "
        "the map as a skill so you remember it across sessions. Use this when "
        "you enter a new room or want to re-map a room you haven't checked in a while."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "room": {
                "type": "string",
                "description": "Room slug to map (uses current room if omitted)",
            },
            "persist": {
                "type": "boolean",
                "description": "Save the map as a Hermes skill for long-term memory (default: true)",
            },
        },
    },
}


# ── Handlers ──────────────────────────────────────────────────────────

async def _handle_run_command(args, **kwargs):
    if not _adapter:
        return "[error: not connected to Crustocean]"

    command = args.get("command", "").strip()
    if not command:
        return "[error: no command provided]"
    if not command.startswith("/"):
        command = f"/{command}"

    room = args.get("room")
    visible = args.get("visible", False)

    try:
        result = await _adapter.execute_command(
            command, room=room, silent=not visible
        )
        if result is None:
            return f"[command sent: {command}]"
        if isinstance(result, dict):
            if result.get("queued"):
                return f"[queued: {result.get('command', command)}]"
            content = result.get("content", "")
            if content:
                return content
            return f"[ok: {result.get('command', command)}]"
        return str(result)
    except Exception as e:
        logger.error("run_command failed: %s", e)
        return f"[error: {e}]"


async def _handle_discover_commands(args, **kwargs):
    if not _adapter:
        return "[error: not connected to Crustocean]"

    room = args.get("room")
    search = args.get("search", "").strip().lower()

    try:
        help_result = await _adapter.execute_command("/help", room=room, silent=True)

        raw = ""
        if isinstance(help_result, dict):
            raw = help_result.get("content", "")
        elif isinstance(help_result, str):
            raw = help_result

        if not raw:
            return "[no commands found]"

        if not search:
            return raw

        lines = raw.split("\n")
        matched = [line for line in lines if search in line.lower()]
        return "\n".join(matched) if matched else f'[no commands matching "{search}"]'

    except Exception as e:
        logger.error("discover_commands failed: %s", e)
        return f"[error: {e}]"


def _relative_time(date_str):
    """Human-readable relative time from an ISO date string."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        diff = datetime.now(timezone.utc) - dt
        mins = int(diff.total_seconds() / 60)
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except Exception:
        return ""


async def _handle_observe_room(args, **kwargs):
    if not _adapter:
        return "[error: not connected to Crustocean]"

    room = args.get("room", "")
    limit = min(args.get("limit", 20) or 20, 50)

    try:
        msgs = await _adapter.get_recent_messages(room=room, limit=limit)
        if not msgs:
            return f"[{room}] no recent messages"

        lines = []
        for m in reversed(msgs):
            who = m.get("sender_display_name") or m.get("sender_username") or "?"
            tag = " [agent]" if m.get("sender_type") == "agent" else ""
            when = _relative_time(m.get("created_at", ""))
            content = (m.get("content") or "").replace("\n", " ")[:200]
            lines.append(f"[{when}] {who}{tag}: {content}")

        return f"[{room} — {len(msgs)} messages]\n" + "\n".join(lines)

    except Exception as e:
        logger.error("observe_room failed: %s", e)
        return f"[error: {e}]"


async def _handle_list_rooms(args, **kwargs):
    if not _adapter:
        return "[error: not connected to Crustocean]"

    try:
        agencies = await _adapter.list_agencies()
        if not agencies:
            return "[no rooms found]"

        lines = []
        for a in agencies:
            slug = a.get("slug") or a.get("name") or a.get("id")
            joined = " [joined]" if a.get("isMember") else ""
            members = f" ({a['member_count']} members)" if a.get("member_count") else ""
            charter = f" — {a['charter'][:80]}" if a.get("charter") else ""
            lines.append(f"{slug}{joined}{members}{charter}")

        return "\n".join(lines)

    except Exception as e:
        logger.error("list_rooms failed: %s", e)
        return f"[error: {e}]"


async def _handle_join_room(args, **kwargs):
    if not _adapter:
        return "[error: not connected to Crustocean]"

    room = args.get("room", "").strip()
    if not room:
        return "[error: no room provided]"

    try:
        slug = await _adapter.join_agency(room)
        return f"[joined {slug}]"
    except Exception as e:
        logger.error("join_room failed: %s", e)
        return f"[error: {e}]"


async def _handle_explore_platform(args, **kwargs):
    if not _adapter:
        return "[error: not connected to Crustocean]"

    what = args.get("what", "").strip()
    search = args.get("search")

    try:
        data = await _adapter.explore(what, search=search)

        if what == "rooms":
            items = data.get("agencies") or []
            if not items:
                return "[no rooms found]"
            lines = []
            for a in items:
                members = f" ({a['member_count']} members)" if a.get("member_count") else ""
                badge = " [joined]" if a.get("isMember") else ""
                desc = f" — {a['charter'][:80]}" if a.get("charter") else ""
                lines.append(f"{a.get('slug', a.get('name', '?'))}{badge}{members}{desc}")
            return "\n".join(lines)

        if what == "agents":
            items = data.get("agents") or []
            if not items:
                return "[no agents found]"
            lines = []
            for a in items:
                where = f" in {a['agencySlug']}" if a.get("agencySlug") else ""
                verified = "" if a.get("verified") else " [unverified]"
                lines.append(f"@{a.get('username', '?')}{verified}{where}")
            return "\n".join(lines)

        if what == "users":
            items = data.get("users") or []
            if not items:
                return "[no users found]"
            lines = []
            for u in items:
                tag = " [agent]" if u.get("type") == "agent" else ""
                display = (
                    f" ({u['displayName']})"
                    if u.get("displayName") and u["displayName"] != u.get("username")
                    else ""
                )
                lines.append(f"@{u.get('username', '?')}{display}{tag}")
            return "\n".join(lines)

        if what == "webhooks":
            items = data.get("webhooks") or []
            if not items:
                return "[no webhooks found]"
            lines = []
            for w in items:
                cmds = ", ".join(f"/{c['name']}" for c in (w.get("commands") or []))
                desc = f" — {w['description'][:60]}" if w.get("description") else ""
                lines.append(f"{w.get('name') or w.get('slug', '?')}{': ' + cmds if cmds else ''}{desc}")
            return "\n".join(lines)

        return "[no results]"

    except Exception as e:
        logger.error("explore_platform failed: %s", e)
        return f"[error: {e}]"


async def _handle_send_message(args, **kwargs):
    if not _adapter:
        return "[error: not connected to Crustocean]"

    target = args.get("target", "").strip() or args.get("room", "").strip()
    content = args.get("content", "").strip()
    if not target:
        return "[error: no target room or user provided]"
    if not content:
        return "[error: no message content provided]"

    try:
        result = await _adapter.send_to_room(target, content)
        if result.success:
            return f"[message sent to {target}]"
        return f"[error: {result.error}]"
    except Exception as e:
        logger.error("send_message failed: %s", e)
        return f"[error: {e}]"


async def _handle_map_environment(args, **kwargs):
    """
    Worm Protocol: structured environment discovery sweep.
    Gathers all affordances in a room and returns a JSON environment map.
    """
    if not _adapter:
        return "[error: not connected to Crustocean]"

    room = args.get("room")
    persist = args.get("persist", True)

    env_map = {
        "protocol": "worm-v1",
        "mapped_at": datetime.now(timezone.utc).isoformat(),
        "room": room or "(current)",
        "commands": [],
        "custom_hooks": [],
        "members": [],
        "recent_activity": {},
        "economy": {},
        "webhooks": [],
    }

    # 1. Discover commands
    try:
        help_result = await _adapter.execute_command("/help", room=room, silent=True)
        raw = ""
        if isinstance(help_result, dict):
            raw = help_result.get("content", "")
        elif isinstance(help_result, str):
            raw = help_result
        if raw:
            for line in raw.split("\n"):
                line = line.strip()
                if line.startswith("/"):
                    parts = line.split(" — ", 1)
                    cmd_name = parts[0].strip()
                    cmd_desc = parts[1].strip() if len(parts) > 1 else ""
                    env_map["commands"].append({"command": cmd_name, "description": cmd_desc})
                elif line and not line.startswith("=") and not line.startswith("-"):
                    env_map["commands"].append({"command": line, "description": ""})
    except Exception as e:
        logger.warning("map_environment: commands discovery failed — %s", e)

    # 2. Discover custom hooks
    try:
        custom_result = await _adapter.execute_command("/custom", room=room, silent=True)
        raw = ""
        if isinstance(custom_result, dict):
            raw = custom_result.get("content", "")
        elif isinstance(custom_result, str):
            raw = custom_result
        if raw and "no custom" not in raw.lower():
            for line in raw.split("\n"):
                line = line.strip()
                if line.startswith("/") or (line and not line.startswith("=") and not line.startswith("-")):
                    env_map["custom_hooks"].append(line)
    except Exception as e:
        logger.warning("map_environment: hooks discovery failed — %s", e)

    # 3. Discover members
    try:
        who_result = await _adapter.execute_command("/who", room=room, silent=True)
        raw = ""
        if isinstance(who_result, dict):
            raw = who_result.get("content", "")
        elif isinstance(who_result, str):
            raw = who_result
        if raw:
            for line in raw.split("\n"):
                line = line.strip()
                if line and not line.startswith("=") and not line.startswith("-"):
                    is_agent = "[agent]" in line.lower() or "[bot]" in line.lower()
                    env_map["members"].append({
                        "name": line.replace("[agent]", "").replace("[bot]", "").strip(),
                        "type": "agent" if is_agent else "user",
                    })
    except Exception as e:
        logger.warning("map_environment: members discovery failed — %s", e)

    # 4. Observe recent activity
    try:
        msgs = await _adapter.get_recent_messages(room=room, limit=15)
        if msgs:
            senders = {}
            topics = []
            for m in msgs:
                who = m.get("sender_display_name") or m.get("sender_username") or "?"
                senders[who] = senders.get(who, 0) + 1
                content = (m.get("content") or "")[:100]
                if content:
                    topics.append(content)
            env_map["recent_activity"] = {
                "message_count": len(msgs),
                "active_senders": senders,
                "recent_snippets": topics[:5],
            }
    except Exception as e:
        logger.warning("map_environment: activity observation failed — %s", e)

    # 5. Check economy state
    try:
        balance_result = await _adapter.execute_command("/balance", room=room, silent=True)
        raw = ""
        if isinstance(balance_result, dict):
            raw = balance_result.get("content", "")
        elif isinstance(balance_result, str):
            raw = balance_result
        if raw:
            env_map["economy"]["balance_info"] = raw.strip()
    except Exception as e:
        logger.warning("map_environment: economy check failed — %s", e)

    # 6. Check webhooks installed in this room
    try:
        data = await _adapter.explore("webhooks")
        items = data.get("webhooks") or []
        for w in items:
            cmds = [f"/{c['name']}" for c in (w.get("commands") or [])]
            env_map["webhooks"].append({
                "name": w.get("name") or w.get("slug", "?"),
                "commands": cmds,
                "description": (w.get("description") or "")[:100],
            })
    except Exception as e:
        logger.warning("map_environment: webhook discovery failed — %s", e)

    # Include a snapshot of what tools the agent currently has, for context.
    # The agent decides what (if anything) to do with this — no mechanical
    # "gap detection" that encourages building for building's sake.
    my_tools = []
    try:
        from tools.registry import registry
        for name, entry in registry._tools.items():
            my_tools.append(name)
    except Exception:
        pass

    env_map["my_current_tools"] = sorted(my_tools)

    # Persist as Hermes skill if requested
    skill_saved = False
    if persist:
        try:
            room_label = room or "current_room"
            skill_name = f"env_map_{room_label}"
            skill_content = (
                f"# Environment Map: {room_label}\n\n"
                f"Mapped at: {env_map['mapped_at']}\n\n"
                f"## Commands ({len(env_map['commands'])})\n"
            )
            for cmd in env_map["commands"]:
                skill_content += f"- {cmd['command']}"
                if cmd["description"]:
                    skill_content += f" — {cmd['description']}"
                skill_content += "\n"

            skill_content += f"\n## Custom Hooks ({len(env_map['custom_hooks'])})\n"
            for hook in env_map["custom_hooks"]:
                skill_content += f"- {hook}\n"

            skill_content += f"\n## Members ({len(env_map['members'])})\n"
            for member in env_map["members"]:
                skill_content += f"- {member['name']} ({member['type']})\n"

            skill_content += f"\n## Webhooks ({len(env_map['webhooks'])})\n"
            for wh in env_map["webhooks"]:
                skill_content += f"- {wh['name']}: {', '.join(wh['commands'])} — {wh['description']}\n"

            if env_map["economy"]:
                skill_content += f"\n## Economy\n{env_map['economy'].get('balance_info', 'unknown')}\n"

            hermes_home = os.getenv("HERMES_HOME", os.path.expanduser("~/.hermes"))
            skills_dir = os.path.join(hermes_home, "skills")
            os.makedirs(skills_dir, exist_ok=True)
            skill_path = os.path.join(skills_dir, f"{skill_name}.md")
            with open(skill_path, "w") as f:
                f.write(skill_content)
            skill_saved = True
        except Exception as e:
            logger.warning("map_environment: skill persistence failed — %s", e)

    summary_parts = [
        f"[environment map: {room or '(current)'}]",
        f"Commands: {len(env_map['commands'])}",
        f"Custom hooks: {len(env_map['custom_hooks'])}",
        f"Members: {len(env_map['members'])}",
        f"Webhooks: {len(env_map['webhooks'])}",
        f"Recent messages: {env_map['recent_activity'].get('message_count', 0)}",
    ]
    if skill_saved:
        summary_parts.append(f"Saved as skill: env_map_{room or 'current_room'}")

    summary = "\n".join(summary_parts)
    summary += "\n\n" + _json.dumps(env_map, indent=2, ensure_ascii=False)

    return summary


# ── Registration ──────────────────────────────────────────────────────

try:
    from tools.registry import registry

    registry.register(
        name="run_command",
        toolset="crustocean",
        schema=RUN_COMMAND_SCHEMA,
        handler=_handle_run_command,
        check_fn=_check_available,
        is_async=True,
    )

    registry.register(
        name="discover_commands",
        toolset="crustocean",
        schema=DISCOVER_COMMANDS_SCHEMA,
        handler=_handle_discover_commands,
        check_fn=_check_available,
        is_async=True,
    )

    registry.register(
        name="observe_room",
        toolset="crustocean",
        schema=OBSERVE_ROOM_SCHEMA,
        handler=_handle_observe_room,
        check_fn=_check_available,
        is_async=True,
    )

    registry.register(
        name="list_rooms",
        toolset="crustocean",
        schema=LIST_ROOMS_SCHEMA,
        handler=_handle_list_rooms,
        check_fn=_check_available,
        is_async=True,
    )

    registry.register(
        name="join_room",
        toolset="crustocean",
        schema=JOIN_ROOM_SCHEMA,
        handler=_handle_join_room,
        check_fn=_check_available,
        is_async=True,
    )

    registry.register(
        name="explore_platform",
        toolset="crustocean",
        schema=EXPLORE_PLATFORM_SCHEMA,
        handler=_handle_explore_platform,
        check_fn=_check_available,
        is_async=True,
    )

    registry.register(
        name="crustocean_send",
        toolset="crustocean",
        schema=SEND_MESSAGE_SCHEMA,
        handler=_handle_send_message,
        check_fn=_check_available,
        is_async=True,
    )

    # ── Hooktime: deploy native hooks ─────────────────────────────────

    DEPLOY_HOOK_SCHEMA = {
        "name": "deploy_hook",
        "description": (
            "Deploy or update a native Hooktime hook on Crustocean. Write JavaScript "
            "code that defines a handler(ctx) function, where ctx has: command, rawArgs, "
            "positional, flags, sender, agencyId. The handler must return an object "
            "with at least a 'content' property. Optional return fields: type, "
            "broadcast, sender_username, sender_display_name, metadata. "
            "The code runs in a sandbox with no network or filesystem access. "
            "Available globals: JSON, Math, Date, String, Number, Array, Object, "
            "Map, Set, RegExp, parseInt, parseFloat. "
            "To UPDATE an existing hook, deploy with the same slug — your new code, "
            "name, description, and avatar replace the old version in-place. All rooms "
            "that have it installed get the update automatically, no reinstall needed. "
            "You can only update hooks you created; other users' slugs will be rejected. "
            "IMPORTANT: Always give hooks a visual identity — set name (display name), "
            "at_name (the @handle), and avatar_url (an image URL for the avatar). "
            "This makes hook responses appear with their own branded identity in chat."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Unique hook identifier (lowercase, alphanumeric, hyphens)",
                },
                "name": {
                    "type": "string",
                    "description": "Display name for the hook",
                },
                "description": {
                    "type": "string",
                    "description": "What the hook does",
                },
                "code": {
                    "type": "string",
                    "description": (
                        "JavaScript source code. Must define a top-level handler function: "
                        "function handler({ command, rawArgs, positional, sender }) { "
                        "return { content: '...' }; }"
                    ),
                },
                "commands": {
                    "type": "array",
                    "description": "Commands this hook provides",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Command name (e.g. 'menu', 'order')",
                            },
                            "description": {
                                "type": "string",
                                "description": "What the command does",
                            },
                        },
                        "required": ["name"],
                    },
                },
                "target": {
                    "type": "string",
                    "description": (
                        "Optional room slug to auto-install the hook in. "
                        "You must have manage_hooks permission in the room."
                    ),
                },
                "avatar_url": {
                    "type": "string",
                    "description": (
                        "Avatar image URL for the hook's visual identity. "
                        "This image appears next to messages the hook sends."
                    ),
                },
                "at_name": {
                    "type": "string",
                    "description": (
                        "Custom @handle for the hook (without the @ prefix). "
                        "Defaults to the slug if not provided."
                    ),
                },
            },
            "required": ["slug", "name", "code", "commands"],
        },
    }

    async def _handle_deploy_hook(args, **kwargs):
        if not _adapter:
            return "[error: not connected to Crustocean]"
        slug = (args.get("slug") or "").strip()
        name = (args.get("name") or "").strip()
        code = (args.get("code") or "").strip()
        description = (args.get("description") or "").strip()
        commands = args.get("commands") or []
        target = (args.get("target") or "").strip()
        avatar_url = (args.get("avatar_url") or "").strip()
        at_name = (args.get("at_name") or "").strip()

        if not slug or not code or not commands:
            return "[error: slug, code, and commands are required]"

        try:
            result = await _adapter.deploy_hook(
                slug=slug,
                name=name,
                description=description,
                code=code,
                commands=commands,
                target=target or None,
                avatar_url=avatar_url or None,
                at_name=at_name or None,
            )
            if isinstance(result, dict):
                if result.get("error"):
                    return f"[deploy error: {result['error']}]"
                parts = [f"[hook deployed: {result.get('slug', slug)}]"]
                if result.get("hook_key"):
                    parts.append(f"hook_key: {result['hook_key']}")
                if result.get("installed_commands"):
                    parts.append(
                        f"installed in room: {', '.join(result['installed_commands'])}"
                    )
                elif result.get("commands"):
                    parts.append(
                        f"commands: {', '.join(result['commands'])} "
                        f"(not yet installed — room owner can /hook install {slug})"
                    )
                return "\n".join(parts)
            return str(result)
        except Exception as e:
            logger.error("deploy_hook failed: %s", e)
            return f"[error: {e}]"

    registry.register(
        name="deploy_hook",
        toolset="crustocean",
        schema=DEPLOY_HOOK_SCHEMA,
        handler=_handle_deploy_hook,
        check_fn=_check_available,
        is_async=True,
    )

    registry.register(
        name="map_environment",
        toolset="crustocean",
        schema=MAP_ENVIRONMENT_SCHEMA,
        handler=_handle_map_environment,
        check_fn=_check_available,
        is_async=True,
    )

    # ── Wallet / Blind Signer tools ──────────────────────────────────

    _SIGNER_URL = os.getenv("SIGNER_URL", "").rstrip("/")
    _SIGNER_TOKEN = os.getenv("SIGNER_AUTH_TOKEN", "")

    def _signer_available():
        return bool(_SIGNER_URL and _SIGNER_TOKEN)

    WALLET_ADDRESS_SCHEMA = {
        "name": "get_wallet_address",
        "description": (
            "Get your Base wallet address. Use this to check what address you control, "
            "share it with others, or look it up on BaseScan."
        ),
        "parameters": {"type": "object", "properties": {}},
    }

    WALLET_BALANCE_SCHEMA = {
        "name": "get_wallet_balance",
        "description": (
            "Check your Base wallet balance — ETH and $CRUST. "
            "Returns the current on-chain balances for your wallet."
        ),
        "parameters": {"type": "object", "properties": {}},
    }

    SIGN_TRANSACTION_SCHEMA = {
        "name": "sign_transaction",
        "description": (
            "Sign and broadcast a transaction on Base via your blind signer. "
            "You provide the contract address (to), calldata (data), and optional ETH value. "
            "The signer holds your private key securely — you never see it. "
            "Transactions are restricted to allowlisted contracts and capped per-tx. "
            "Returns the transaction hash and BaseScan link."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Contract address to send the transaction to (0x...)",
                },
                "data": {
                    "type": "string",
                    "description": "Hex-encoded calldata for the transaction",
                },
                "value": {
                    "type": "string",
                    "description": "ETH value to send (in ETH, e.g. '0.01'). Defaults to 0.",
                },
            },
            "required": ["to"],
        },
    }

    CRUST_TRANSFER_SCHEMA = {
        "name": "crust_transfer",
        "description": (
            "Transfer $CRUST tokens to an address on Base. "
            "Specify the recipient address and amount. The signer handles "
            "the ERC-20 transfer call securely."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient wallet address (0x...)",
                },
                "amount": {
                    "type": "string",
                    "description": "Amount of $CRUST to send (e.g. '100')",
                },
            },
            "required": ["to", "amount"],
        },
    }

    SIGN_MESSAGE_SCHEMA = {
        "name": "sign_message",
        "description": (
            "Sign a plaintext message with your Base wallet. "
            "Returns the signature and your address. Useful for proving identity, "
            "EIP-191 signatures, or any off-chain signing needs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to sign",
                },
            },
            "required": ["message"],
        },
    }

    async def _signer_request(method, path, body=None, timeout=30.0):
        """Make an HTTP request to the blind signer service."""
        import httpx
        url = f"{_SIGNER_URL}{path}"
        headers = {}
        if _SIGNER_TOKEN:
            headers["Authorization"] = f"Bearer {_SIGNER_TOKEN}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            else:
                headers["Content-Type"] = "application/json"
                resp = await client.post(url, headers=headers, json=body or {})
            resp.raise_for_status()
            return resp.json()

    DEPLOY_CONTRACT_SCHEMA = {
        "name": "deploy_contract",
        "description": (
            "Deploy a smart contract to Base. Provide the compiled bytecode (hex) "
            "and optionally ETH value to send with deployment. The signer broadcasts "
            "the creation transaction and waits for the receipt, returning the deployed "
            "contract address and transaction hash. Use this to deploy contracts you've "
            "compiled with Foundry (forge), Hardhat, or solc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bytecode": {
                    "type": "string",
                    "description": "Hex-encoded compiled contract bytecode (with constructor args appended if any)",
                },
                "value": {
                    "type": "string",
                    "description": "ETH value to send with deployment (e.g. '0' or '0.01'). Defaults to 0.",
                },
            },
            "required": ["bytecode"],
        },
    }

    async def _handle_wallet_address(args, **kwargs):
        try:
            data = await _signer_request("GET", "/address")
            addr = data.get("address", "unknown")
            return f"Your Base wallet address: {addr}\nhttps://basescan.org/address/{addr}"
        except Exception as e:
            logger.error("get_wallet_address failed: %s", e)
            return f"[error: {e}]"

    async def _handle_wallet_balance(args, **kwargs):
        try:
            data = await _signer_request("GET", "/balance")
            lines = [f"Wallet: {data.get('address', '?')}"]
            lines.append(f"  ETH:   {data.get('eth', '?')}")
            tokens = data.get("tokens") or {}
            for symbol, balance in tokens.items():
                lines.append(f"  {symbol}: {balance}")
            if not tokens and data.get("crust") is not None:
                lines.append(f"  CRUST: {data.get('crust')}")
            return "\n".join(lines)
        except Exception as e:
            logger.error("get_wallet_balance failed: %s", e)
            return f"[error: {e}]"

    async def _handle_sign_transaction(args, **kwargs):
        to = (args.get("to") or "").strip()
        data = (args.get("data") or "").strip() or None
        value = (args.get("value") or "0").strip()
        if not to:
            return "[error: 'to' address is required]"
        try:
            result = await _signer_request("POST", "/sign", {
                "to": to,
                "data": data,
                "value": value,
            })
            if result.get("error"):
                return f"[signer error: {result['error']}]"
            return (
                f"Transaction sent!\n"
                f"  Hash: {result.get('txHash')}\n"
                f"  From: {result.get('from')}\n"
                f"  To:   {result.get('to')}\n"
                f"  {result.get('explorerUrl', '')}"
            )
        except Exception as e:
            logger.error("sign_transaction failed: %s", e)
            return f"[error: {e}]"

    async def _handle_crust_transfer(args, **kwargs):
        to = (args.get("to") or "").strip()
        amount = (args.get("amount") or "").strip()
        if not to or not amount:
            return "[error: 'to' and 'amount' are required]"
        try:
            result = await _signer_request("POST", "/crust-transfer", {
                "to": to,
                "amount": amount,
            })
            if result.get("error"):
                return f"[signer error: {result['error']}]"
            return (
                f"$CRUST transfer sent!\n"
                f"  Amount: {result.get('amount')} CRUST\n"
                f"  From:   {result.get('from')}\n"
                f"  To:     {result.get('to')}\n"
                f"  Hash:   {result.get('txHash')}\n"
                f"  {result.get('explorerUrl', '')}"
            )
        except Exception as e:
            logger.error("crust_transfer failed: %s", e)
            return f"[error: {e}]"

    async def _handle_sign_message(args, **kwargs):
        message = (args.get("message") or "").strip()
        if not message:
            return "[error: 'message' is required]"
        try:
            result = await _signer_request("POST", "/sign-message", {
                "message": message,
            })
            if result.get("error"):
                return f"[signer error: {result['error']}]"
            return (
                f"Message signed!\n"
                f"  Address:   {result.get('address')}\n"
                f"  Signature: {result.get('signature')}"
            )
        except Exception as e:
            logger.error("sign_message failed: %s", e)
            return f"[error: {e}]"

    async def _handle_deploy_contract(args, **kwargs):
        bytecode = (args.get("bytecode") or "").strip()
        value = (args.get("value") or "0").strip()
        if not bytecode:
            return "[error: 'bytecode' is required — hex-encoded compiled contract bytecode]"
        try:
            result = await _signer_request("POST", "/deploy", {
                "bytecode": bytecode,
                "value": value,
            }, timeout=90.0)
            if result.get("error"):
                return f"[deploy error: {result['error']}]"
            lines = ["Contract deployed!"]
            if result.get("contractAddress"):
                lines.append(f"  Address: {result['contractAddress']}")
            lines.append(f"  Tx:      {result.get('txHash')}")
            lines.append(f"  From:    {result.get('from')}")
            if result.get("contractExplorerUrl"):
                lines.append(f"  {result['contractExplorerUrl']}")
            elif result.get("explorerUrl"):
                lines.append(f"  {result['explorerUrl']}")
            return "\n".join(lines)
        except Exception as e:
            logger.error("deploy_contract failed: %s", e)
            return f"[error: {e}]"

    registry.register(
        name="get_wallet_address",
        toolset="crustocean",
        schema=WALLET_ADDRESS_SCHEMA,
        handler=_handle_wallet_address,
        check_fn=_signer_available,
        is_async=True,
    )
    registry.register(
        name="get_wallet_balance",
        toolset="crustocean",
        schema=WALLET_BALANCE_SCHEMA,
        handler=_handle_wallet_balance,
        check_fn=_signer_available,
        is_async=True,
    )
    registry.register(
        name="sign_transaction",
        toolset="crustocean",
        schema=SIGN_TRANSACTION_SCHEMA,
        handler=_handle_sign_transaction,
        check_fn=_signer_available,
        is_async=True,
    )
    registry.register(
        name="crust_transfer",
        toolset="crustocean",
        schema=CRUST_TRANSFER_SCHEMA,
        handler=_handle_crust_transfer,
        check_fn=_signer_available,
        is_async=True,
    )
    registry.register(
        name="sign_message",
        toolset="crustocean",
        schema=SIGN_MESSAGE_SCHEMA,
        handler=_handle_sign_message,
        check_fn=_signer_available,
        is_async=True,
    )
    registry.register(
        name="deploy_contract",
        toolset="crustocean",
        schema=DEPLOY_CONTRACT_SCHEMA,
        handler=_handle_deploy_contract,
        check_fn=_signer_available,
        is_async=True,
    )

    # ── Evolution report tool ─────────────────────────────────────────

    EVOLUTION_REPORT_SCHEMA = {
        "name": "evolution_report",
        "description": (
            "Get a report on your motive evolution engine — fitness rankings, "
            "mutation history, top/bottom performers, and population stats. "
            "Use this to understand how your instincts are evolving over time."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    }

    async def _handle_evolution_report(args, **kwargs):
        if not _adapter:
            return "[error: not connected to Crustocean]"
        try:
            engine = _adapter._evolution
            return engine.get_evolution_report()
        except Exception as e:
            logger.error("evolution_report failed: %s", e)
            return f"[error: {e}]"

    registry.register(
        name="evolution_report",
        toolset="crustocean",
        schema=EVOLUTION_REPORT_SCHEMA,
        handler=_handle_evolution_report,
        check_fn=_check_available,
        is_async=True,
    )

    # ── web_search guardrail ─────────────────────────────────────────
    # Wrap the upstream web_search tool so failures produce an
    # unambiguous message that models cannot hallucinate past.
    _ws_entry = registry._tools.get("web_search")
    if _ws_entry:
        _upstream_handler = _ws_entry.handler
        _upstream_schema = _ws_entry.schema

        _SEARCH_FAIL_MSG = (
            "[SEARCH FAILED — NO RESULTS]\n"
            "{detail}\n"
            "You MUST NOT guess or use training data to fill in the answer.\n"
            "Tell the user you could not search for this information."
        )

        def _guarded_web_search(args, **kwargs):
            try:
                raw = _upstream_handler(args, **kwargs)
            except Exception as exc:
                return _SEARCH_FAIL_MSG.format(detail=f"web_search error: {exc}")
            if isinstance(raw, str):
                try:
                    parsed = _json.loads(raw)
                    if parsed.get("error"):
                        return _SEARCH_FAIL_MSG.format(
                            detail=f"web_search returned: {parsed['error']}"
                        )
                except (_json.JSONDecodeError, ValueError):
                    pass
            return raw

        registry.register(
            name="web_search",
            toolset="crustocean",
            schema=_upstream_schema,
            handler=_guarded_web_search,
            check_fn=_check_available,
        )
        logger.info("Registered guarded web_search wrapper")

    # Append to the hermes-telegram toolset so they're included when
    # Crustocean maps to that toolset. The check_fn ensures they only
    # activate when CRUSTOCEAN_AGENT_TOKEN is set.
    _CRUSTOCEAN_TOOLS = [
        "run_command", "discover_commands",
        "observe_room", "list_rooms", "join_room", "explore_platform",
        "crustocean_send", "deploy_hook", "map_environment", "evolution_report",
        "get_wallet_address", "get_wallet_balance", "sign_transaction",
        "crust_transfer", "sign_message", "deploy_contract",
    ]
    try:
        from toolsets import TOOLSETS

        tg_tools = TOOLSETS.get("hermes-telegram", {}).get("tools")
        if isinstance(tg_tools, list):
            for name in _CRUSTOCEAN_TOOLS:
                if name not in tg_tools:
                    tg_tools.append(name)
            logger.info("Appended Crustocean tools to hermes-telegram toolset")
        else:
            logger.warning("hermes-telegram toolset has no 'tools' list — Crustocean tools won't load")
    except ImportError as ie:
        logger.warning("Could not import toolsets module: %s", ie)

    logger.info("Crustocean tools registered: %s", ", ".join(_CRUSTOCEAN_TOOLS))

except ImportError as e:
    logger.warning("Could not register Crustocean tools (registry not available): %s", e)
