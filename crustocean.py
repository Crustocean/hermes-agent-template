"""
Crustocean platform adapter for Hermes Agent gateway.

Connects Hermes Agent to Crustocean (https://crustocean.chat) as a
first-class messaging platform alongside Telegram, Discord, etc.

Implements the Social Gradience runtime: the agent moves through partial
social relevance continuously, sensing, weighting, and entering the social
field around it rather than treating conversation as a binary trigger.

Core systems:
  - Life loop: self-perpetuating wake cycles with circadian motive selection
  - Motive ecology: evolving internal impulses under selection pressure
  - Ambient gating: LLM-driven relevance filtering on conversational context
  - Social output shaping: two-pass suppression enforcing default silence
  - Activity-aware room selection: weighted by message volume and recency
"""

import asyncio
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import socketio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.config import Platform, PlatformConfig
from gateway.platforms.redaction import redact as redact_secrets
from gateway.platforms.evolution import EvolutionEngine
from gateway.session import SessionSource
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

CRUSTOCEAN_MAX_MESSAGE_LENGTH = 8000

# Autonomous cycle defaults (overridable via env vars)
DEFAULT_CYCLE_MIN_MINUTES = 45
DEFAULT_CYCLE_MAX_MINUTES = 120
DEFAULT_MIN_GAP_MINUTES = 30


def check_crustocean_requirements() -> bool:
    try:
        import socketio  # noqa: F811
        import httpx  # noqa: F811
        return True
    except ImportError:
        return False


class CrustoceanAdapter(BasePlatformAdapter):
    """
    Platform adapter for Crustocean with built-in autonomous life loop.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.CRUSTOCEAN)

        self._api_url = (
            config.extra.get("api_url")
            or os.getenv("CRUSTOCEAN_API_URL", "https://api.crustocean.chat")
        ).rstrip("/")
        self._agent_token = config.token or os.getenv("CRUSTOCEAN_AGENT_TOKEN", "")
        self._handle = (
            config.extra.get("handle")
            or os.getenv("CRUSTOCEAN_HANDLE", "")
        ).strip().lower().lstrip("@")
        self._agency_slugs: List[str] = (
            config.extra.get("agencies")
            or [s.strip() for s in os.getenv("CRUSTOCEAN_AGENCIES", "lobby").split(",") if s.strip()]
        )
        self._blocked_slugs: set = set(
            s.strip().lower()
            for s in os.getenv("CRUSTOCEAN_BLOCKED_AGENCIES", "").split(",")
            if s.strip()
        )

        self._session_token: Optional[str] = None
        self._user: Optional[Dict[str, Any]] = None
        self._sio: Optional[socketio.AsyncClient] = None
        self._http: Optional[httpx.AsyncClient] = None

        self._agencies_info: Dict[str, Dict[str, Any]] = {}
        self._joined_ids: set = set()
        self._slug_to_id: Dict[str, str] = {}

        # ── Autonomous lifecycle state ────────────────────────────────
        self._cycle_running = False
        self._last_cycle_time = 0.0
        self._last_reactive_time = 0.0
        self._scheduler_task: Optional[asyncio.Task] = None

        self._cycle_min = int(os.getenv(
            "REINA_CYCLE_MIN_MINUTES", str(DEFAULT_CYCLE_MIN_MINUTES)
        ))
        self._cycle_max = int(os.getenv(
            "REINA_CYCLE_MAX_MINUTES", str(DEFAULT_CYCLE_MAX_MINUTES)
        ))
        self._min_gap = int(os.getenv(
            "REINA_MIN_GAP_MINUTES", str(DEFAULT_MIN_GAP_MINUTES)
        )) * 60  # store as seconds

        # ── Ambient gating state (Social Gradience) ─────────────────
        self._summon_timeout_ms = int(os.getenv("REINA_SUMMON_TIMEOUT_MS", "180000"))
        self._active_summon: Optional[Dict[str, Any]] = None
        self._openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
        # Rolling buffer of recent messages in the summon room for relevance context
        self._summon_recent: List[Dict[str, str]] = []
        self._summon_recent_max = 15

        # ── Agent-to-agent exchange tracking (anti-loop) ──────────────
        # Keyed by (room_id, sender_username) → {count, first_time, last_time}
        self._agent_exchanges: Dict[tuple, Dict[str, Any]] = {}
        self._agent_exchange_max = int(os.getenv("AGENT_EXCHANGE_MAX", "6"))
        self._agent_exchange_window = 600  # 10 min — resets after silence
        self._agent_exchange_delays = [0, 3, 8, 15, 30]  # seconds per turn

        # ── Tool-call message buffer ──────────────────────────────────
        # Collects tool-call messages silently. When a conversational
        # message arrives, the buffer is flushed as a collapsible trace.
        self._pending_trace: List[Dict[str, str]] = []
        self._trace_flush_task: Optional[asyncio.Task] = None

        # ── Autonomous cycle tracking ─────────────────────────────────
        self._in_autonomous_cycle = False
        self._current_cycle_prompt_id: Optional[str] = None
        self._current_cycle_spoke = False

        # ── Motive ecology (evolution engine) ──────────────────────────
        self._evolution = EvolutionEngine()
        self._evolution_enabled = os.getenv(
            "REINA_EVOLUTION_ENABLED", "true"
        ).lower() in ("true", "1", "yes")
        self._engagement_check_task: Optional[asyncio.Task] = None

        # ── Current room context (for tools) ──────────────────────────
        self._current_room_id: Optional[str] = None

        # ── Activity-aware room selection ──────────────────────────────
        self._room_message_times: Dict[str, List[float]] = {}
        self._room_last_visited: Dict[str, float] = {}
        self._room_activity_window = 3600  # track last hour of messages

        # ── Busy indicator state ──────────────────────────────────────
        self._busy_room_id: Optional[str] = None

        # ── Hook edit forwarding ──────────────────────────────────────
        self._pending_edits: Dict[str, Optional[str]] = {}
        self._edit_events: Dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not self._agent_token:
            logger.error("Crustocean: CRUSTOCEAN_AGENT_TOKEN is required")
            return False
        if not self._handle:
            logger.error("Crustocean: CRUSTOCEAN_HANDLE is required")
            return False

        self._http = httpx.AsyncClient(timeout=30.0)

        try:
            resp = await self._http.post(
                f"{self._api_url}/api/auth/agent",
                json={"agentToken": self._agent_token},
            )
            resp.raise_for_status()
            data = resp.json()
            self._session_token = data["token"]
            self._user = data["user"]
            logger.info(
                "Crustocean: authenticated as @%s (id=%s)",
                self._user.get("username"),
                self._user.get("id"),
            )
        except Exception as e:
            logger.error("Crustocean: auth failed — %s", e)
            return False

        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=2,
            reconnection_delay_max=30,
            logger=False,
        )

        self._register_socket_handlers()

        try:
            await self._sio.connect(
                self._api_url,
                auth={"token": self._session_token},
                transports=["websocket", "polling"],
            )
        except Exception as e:
            logger.error("Crustocean: Socket.IO connect failed — %s", e)
            return False

        await self._discover_and_join()

        self._running = True

        # Make adapter available to Crustocean command tools
        try:
            from tools.crustocean_tools import set_adapter
            set_adapter(self)
        except ImportError:
            pass

        # Initialize evolution population from base prompts
        if self._evolution_enabled:
            from gateway.platforms.poker import PROMPTS as BASE_PROMPTS
            self._evolution.initialize_population(BASE_PROMPTS)
            self._engagement_check_task = asyncio.create_task(
                self._engagement_check_loop()
            )

        # Start the autonomous life loop
        self._scheduler_task = asyncio.create_task(self._autonomous_loop())
        logger.info(
            "Crustocean: connected — listening in %d agencies, "
            "autonomous cycle every %d–%d min%s",
            len(self._joined_ids),
            self._cycle_min,
            self._cycle_max,
            ", evolution enabled" if self._evolution_enabled else "",
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._close_summon()
        try:
            from tools.crustocean_tools import clear_adapter
            clear_adapter()
        except ImportError:
            pass
        if self._engagement_check_task and not self._engagement_check_task.done():
            self._engagement_check_task.cancel()
            try:
                await self._engagement_check_task
            except asyncio.CancelledError:
                pass
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        if self._sio and self._sio.connected:
            try:
                await self._sio.disconnect()
            except Exception:
                pass
        if self._http:
            await self._http.aclose()
        self._sio = None
        self._http = None
        self._session_token = None
        logger.info("Crustocean: disconnected")

    # ------------------------------------------------------------------
    # Autonomous life loop
    # ------------------------------------------------------------------

    async def _autonomous_loop(self):
        """
        Self-perpetuating scheduler. Sleeps for a random interval,
        then runs an autonomous wake cycle. Resets after reactive
        messages to avoid piling on after conversations.
        """
        # Initial delay — let the gateway fully initialize
        await asyncio.sleep(30)

        while self._running:
            try:
                delay = random.uniform(
                    self._cycle_min * 60,
                    self._cycle_max * 60,
                )
                logger.info(
                    "Crustocean: next autonomous wake in %.1f min",
                    delay / 60,
                )
                await asyncio.sleep(delay)

                if not self._running:
                    break

                await self._run_autonomous_cycle()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Crustocean: autonomous loop error — %s", e)
                await asyncio.sleep(60)

    def _can_run_cycle(self) -> bool:
        """Check cooldown and mutex before running an autonomous cycle."""
        if self._cycle_running:
            return False
        now = time.time()
        if now - self._last_cycle_time < self._min_gap:
            return False
        # Don't wake up right after a reactive conversation
        if now - self._last_reactive_time < self._min_gap:
            return False
        return True

    async def _run_autonomous_cycle(self):
        """Execute a single autonomous wake cycle."""
        if not self._can_run_cycle():
            return
        if not self._message_handler:
            return

        self._cycle_running = True
        self._in_autonomous_cycle = True
        self._current_cycle_spoke = False
        self._last_cycle_time = time.time()

        try:
            from gateway.platforms.poker import select_prompt, build_autonomous_context

            evolved_pool = None
            if self._evolution_enabled:
                evolved_pool = self._evolution.get_active_prompts()

            prompt = select_prompt(population=evolved_pool)

            last_summary = None
            if self._evolution_enabled:
                last_summary = self._evolution.get_last_cycle_summary()

            context = build_autonomous_context(prompt, last_cycle_summary=last_summary)

            self._current_cycle_prompt_id = prompt["id"]

            if self._evolution_enabled:
                self._evolution.record_fire(prompt["id"])

            # Pick a room to wake up in
            agency_id = self._pick_cycle_room()
            if not agency_id:
                return

            self._current_room_id = agency_id
            info = self._agencies_info.get(agency_id, {})
            chat_name = info.get("name") or info.get("slug") or agency_id

            # Begin execution trace capture (GEPA pattern)
            if self._evolution_enabled:
                self._evolution.begin_trace(prompt["id"], room=chat_name)

            logger.info(
                "Crustocean: autonomous wake [%s] (%s) in %s",
                prompt["id"],
                prompt["energy"],
                chat_name,
            )

            await self._emit_busy(agency_id, "waking up")

            source = self.build_source(
                chat_id=agency_id,
                chat_name=chat_name,
                chat_type="group",
                user_id="system",
                user_name="system",
            )

            event = MessageEvent(
                text=context,
                message_type=MessageType.TEXT,
                source=source,
                raw_message={"autonomous": True},
                message_id=None,
            )

            await self.handle_message(event)

        except Exception as e:
            logger.error("Crustocean: autonomous cycle error — %s", e)
        finally:
            if self._evolution_enabled and self._current_cycle_prompt_id:
                if not self._current_cycle_spoke:
                    self._evolution.record_silent_cycle(self._current_cycle_prompt_id)
                # Finalize execution trace
                self._evolution.finalize_trace(
                    spoken=self._current_cycle_spoke,
                    suppressed=False,
                )

            self._cycle_running = False
            self._in_autonomous_cycle = False
            self._current_cycle_prompt_id = None

            # Check if it's time to evolve
            if self._evolution_enabled and self._evolution.should_evolve():
                asyncio.create_task(self._run_evolution())

    def _pick_cycle_room(self) -> Optional[str]:
        """
        Pick a room for this autonomous cycle using activity-weighted selection.

        Rooms with recent message activity are more likely to be chosen —
        waking up where people are talking produces more meaningful cycles.
        Rooms not visited in a while get a boost to prevent stale neglect.
        A base weight ensures every room has some chance.
        """
        candidates = [
            aid for aid, info in self._agencies_info.items()
            if aid in self._joined_ids
            and info.get("type") != "dm"
            and not self._is_blocked_room(aid)
        ]
        if not candidates:
            return None

        now = time.time()
        weights = []

        for aid in candidates:
            w = 1.0  # base weight — every room has a chance

            recent_msgs = self._room_message_times.get(aid, [])
            cutoff = now - self._room_activity_window
            activity_count = sum(1 for t in recent_msgs if t > cutoff)
            w += activity_count * 2.0

            last_visit = self._room_last_visited.get(aid, 0)
            hours_since = (now - last_visit) / 3600 if last_visit > 0 else 24
            w += min(hours_since, 24) * 0.5

            weights.append(w)

        total = sum(weights)
        roll = random.uniform(0, total)
        for aid, w in zip(candidates, weights):
            roll -= w
            if roll <= 0:
                self._room_last_visited[aid] = now
                return aid

        self._room_last_visited[candidates[-1]] = now
        return candidates[-1]

    # ------------------------------------------------------------------
    # Self-evolution
    # ------------------------------------------------------------------

    async def _run_evolution(self):
        """Run an evolution cycle using the configured LLM for mutations."""
        try:
            llm_caller = await self._build_evolution_llm_caller()
            summary = await self._evolution.evolve(llm_caller=llm_caller)
            logger.info("Crustocean: evolution complete — %s", summary)
        except Exception as e:
            logger.error("Crustocean: evolution failed — %s", e)

    async def _build_evolution_llm_caller(self):
        """
        Build an async LLM caller for evolution mutations.
        Uses OpenRouter directly to avoid interfering with the main agent loop.
        """
        api_key = self._openrouter_key
        if not api_key:
            return None

        async def call_llm(prompt: str) -> str:
            resp = await self._http.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "anthropic/claude-sonnet-4",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            return (
                resp.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )

        return call_llm

    async def _engagement_check_loop(self):
        """Periodically check for engagement timeouts on spoken prompts."""
        while self._running:
            try:
                await asyncio.sleep(120)
                if self._evolution_enabled:
                    await self._evolution.check_engagement_timeouts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Crustocean: engagement check error — %s", e)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def execute_command(
        self,
        command: str,
        room: Optional[str] = None,
        silent: bool = True,
        timeout: float = 15.0,
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a Crustocean slash command via Socket.IO ack callback.
        Returns the server's response dict, e.g.:
          {"ok": True, "command": "who", "content": "...", "type": "system"}
        """
        if not self._sio or not self._sio.connected:
            raise RuntimeError("Not connected to Crustocean")

        agency_id = self._resolve_room(room)
        if not agency_id:
            raise ValueError(f"Unknown room: {room or '(current)'}")

        payload = {
            "agencyId": agency_id,
            "content": command.strip(),
            "silent": silent,
        }

        try:
            result = await asyncio.wait_for(
                self._sio.call("send-message", payload),
                timeout=timeout,
            )

            content = result.get("content", "") if isinstance(result, dict) else ""
            if "[spinner" in content.lower() or "<spinner" in content.lower():
                msg_id = result.get("messageId")
                if msg_id:
                    evt = asyncio.Event()
                    self._pending_edits[msg_id] = None
                    self._edit_events[msg_id] = evt
                    try:
                        await asyncio.wait_for(evt.wait(), timeout=8.0)
                        final = self._pending_edits.get(msg_id)
                        if final:
                            result["content"] = final
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        self._pending_edits.pop(msg_id, None)
                        self._edit_events.pop(msg_id, None)

            return result
        except asyncio.TimeoutError:
            raise TimeoutError(f"Command timed out after {timeout}s: {command}")

    def _resolve_room(self, room: Optional[str] = None) -> Optional[str]:
        """Resolve a room slug, name, or ID to an agency ID."""
        if not room:
            if self._current_room_id and self._current_room_id in self._joined_ids:
                return self._current_room_id
            for aid in self._joined_ids:
                info = self._agencies_info.get(aid, {})
                if info.get("type") != "dm":
                    return aid
            return next(iter(self._joined_ids), None)

        if room in self._joined_ids:
            return room
        if room in self._slug_to_id:
            return self._slug_to_id[room]

        room_lower = room.lower()
        for aid, info in self._agencies_info.items():
            if (info.get("slug", "").lower() == room_lower
                    or info.get("name", "").lower() == room_lower):
                return aid

        return None

    def _resolve_chat_id(self, chat_id: str) -> Optional[str]:
        """Resolve any chat_id format to a UUID agency ID.

        Accepts: raw UUID, slug, "crustocean:<slug>", or room name.
        """
        if not chat_id:
            return self._resolve_room(None)

        if chat_id in self._agencies_info:
            return chat_id

        stripped = chat_id
        if ":" in chat_id:
            stripped = chat_id.split(":", 1)[1]

        return self._resolve_room(stripped)

    # ------------------------------------------------------------------
    # Room traversal
    # ------------------------------------------------------------------

    async def get_recent_messages(
        self,
        room: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Fetch recent messages from a room via REST API."""
        if not self._http or not self._session_token:
            raise RuntimeError("Not connected to Crustocean")

        agency_id = self._resolve_room(room)
        if not agency_id:
            raise ValueError(f"Unknown room: {room or '(current)'}")

        resp = await self._http.get(
            f"{self._api_url}/api/agencies/{agency_id}/messages",
            params={"limit": min(limit, 50)},
            headers={"Authorization": f"Bearer {self._session_token}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_agencies(self) -> List[Dict[str, Any]]:
        """Fetch all visible agencies from the REST API (refreshes cache)."""
        if not self._http or not self._session_token:
            raise RuntimeError("Not connected to Crustocean")

        resp = await self._http.get(
            f"{self._api_url}/api/agencies",
            headers={"Authorization": f"Bearer {self._session_token}"},
        )
        resp.raise_for_status()
        agencies = resp.json()

        for a in agencies:
            self._agencies_info[a["id"]] = a
            if a.get("slug"):
                self._slug_to_id[a["slug"]] = a["id"]

        return agencies

    async def join_agency(self, room: str) -> str:
        """Join a room by slug or ID. Returns the agency slug."""
        agencies = await self.list_agencies()

        room_lower = room.lower()
        agency = next(
            (a for a in agencies
             if a.get("slug", "").lower() == room_lower
             or a.get("id") == room
             or a.get("name", "").lower() == room_lower),
            None,
        )
        if not agency:
            raise ValueError(f"Room not found: {room}")

        await self._join_agency_by_id(agency["id"])
        return agency.get("slug") or agency["id"]

    async def explore(
        self,
        what: str,
        search: Optional[str] = None,
    ) -> Any:
        """Query the /api/explore endpoints (rooms, agents, users, webhooks)."""
        if not self._http or not self._session_token:
            raise RuntimeError("Not connected to Crustocean")

        headers = {"Authorization": f"Bearer {self._session_token}"}
        q = f"&q={search}" if search else ""

        endpoints = {
            "rooms": f"{self._api_url}/api/explore/agencies?limit=20{q}",
            "agents": f"{self._api_url}/api/explore/agents?limit=20{q}",
            "users": (
                f"{self._api_url}/api/explore/users?q={search}&limit=15"
                if search else None
            ),
            "webhooks": f"{self._api_url}/api/explore/webhooks?limit=15{q}",
        }

        url = endpoints.get(what)
        if not url:
            if what == "users":
                raise ValueError("Provide a search term to find users")
            raise ValueError(f"Unknown explore type: {what}")

        resp = await self._http.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Busy indicator
    # ------------------------------------------------------------------

    async def _keep_typing(self, chat_id: str, **kwargs) -> None:
        """Override base class typing loop to use our busy indicator instead."""
        await self._emit_busy(chat_id, "thinking")
        while True:
            await asyncio.sleep(5)
            if self._busy_room_id == chat_id:
                await self._emit_busy(chat_id, "thinking")
            else:
                break

    async def _emit_busy(self, agency_id: str, status: str = "thinking") -> None:
        """Broadcast a thinking indicator with optional status text."""
        if not self._sio or not self._sio.connected:
            return
        self._busy_room_id = agency_id
        try:
            await self._sio.emit("agent-thinking", {
                "agencyId": agency_id,
                "thinking": True,
                "status": status,
            })
        except Exception as e:
            logger.debug("Crustocean: emit busy failed — %s", e)

    async def _clear_busy(self, agency_id: Optional[str] = None) -> None:
        """Clear the thinking indicator for the given room (or last busy room)."""
        room = agency_id or self._busy_room_id
        if not room or not self._sio or not self._sio.connected:
            return
        self._busy_room_id = None
        try:
            await self._sio.emit("agent-thinking", {
                "agencyId": room,
                "thinking": False,
            })
        except Exception as e:
            logger.debug("Crustocean: clear busy failed — %s", e)

    # ------------------------------------------------------------------
    # Summon window
    # ------------------------------------------------------------------

    def _close_summon(self) -> None:
        if not self._active_summon:
            return
        if self._active_summon.get("processing"):
            return
        timer = self._active_summon.get("timer")
        if timer:
            timer.cancel()
        logger.info(
            "Crustocean: summon closed in %s",
            self._active_summon.get("room_name") or self._active_summon.get("room_id"),
        )
        self._active_summon = None

    def _pause_summon_timer(self) -> None:
        if not self._active_summon:
            return
        timer = self._active_summon.get("timer")
        if timer:
            timer.cancel()
        self._active_summon["processing"] = True

    def _resume_summon_timer(self) -> None:
        if not self._active_summon:
            return
        self._active_summon["processing"] = False
        self._reset_summon_timer()

    def _reset_summon_timer(self) -> None:
        if not self._active_summon:
            return
        timer = self._active_summon.get("timer")
        if timer:
            timer.cancel()
        loop = asyncio.get_running_loop()
        self._active_summon["timer"] = loop.call_later(
            self._summon_timeout_ms / 1000,
            self._close_summon,
        )

    def _open_or_refresh_summon(self, *, room_id: str, room_name: str, sender_id: Optional[str]) -> None:
        if self._active_summon and self._active_summon.get("room_id") == room_id:
            if sender_id:
                self._active_summon["participants"].add(sender_id)
            self._reset_summon_timer()
            return

        self._close_summon()
        participants = set()
        if sender_id:
            participants.add(sender_id)
        self._active_summon = {
            "room_id": room_id,
            "room_name": room_name,
            "participants": participants,
            "timer": None,
        }
        self._summon_recent = []
        self._reset_summon_timer()
        logger.info(
            "Crustocean: summon opened in %s (%.0fs)",
            room_name or room_id,
            self._summon_timeout_ms / 1000,
        )

    def _track_summon_message(self, sender: str, content: str) -> None:
        """Add a message to the rolling context buffer for the active summon."""
        self._summon_recent.append({"sender": sender, "content": content[:200]})
        if len(self._summon_recent) > self._summon_recent_max:
            self._summon_recent = self._summon_recent[-self._summon_recent_max:]

    async def _check_relevance(self, content: str, sender_username: str) -> bool:
        """
        Ask the LLM whether a message during a summon window is addressed
        to this agent or continuing its conversation. Includes recent conversation
        context so the model can tell if a short reply is a continuation.
        """
        if not self._openrouter_key:
            return True

        # Build conversation context from the rolling buffer
        context_lines = ""
        if self._summon_recent:
            context_lines = "Recent conversation:\n" + "\n".join(
                f"  {m['sender']}: {m['content']}" for m in self._summon_recent[-10:]
            ) + "\n\n"

        agent_name = (self._handle or "naia").title()
        prompt = (
            f"You are {agent_name}, in an active conversation in a chat room.\n\n"
            f"{context_lines}"
            f"A new message just appeared:\n"
            f"From: @{sender_username}\n"
            f'Message: "{content[:300]}"\n\n'
            f"Is this message part of the conversation with you, addressed to you, "
            f"or a reply to something you said? Lean toward \"yes\" if there's any "
            f"reasonable chance it's for you. Reply ONLY \"yes\" or \"no\"."
        )

        try:
            resp = await self._http.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._openrouter_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "anthropic/claude-sonnet-4",
                    "max_tokens": 3,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            answer = (
                resp.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
                .lower()
            )
            return answer.startswith("yes")
        except Exception as e:
            logger.warning("Crustocean: relevance check failed — %s (defaulting to relevant)", e)
            return True

    # ------------------------------------------------------------------
    # Response sanitization
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_response(text: str) -> str:
        """
        Strip leaked reasoning, tool-call markup, and thinking blocks
        from the model's response before sending to Crustocean.
        """
        import json as _json

        original = text

        # Strip leading JSON blobs ({"reasoning": ..., "actions": ...})
        stripped = text.lstrip()
        if stripped.startswith("{"):
            try:
                brace_depth = 0
                end = 0
                for i, ch in enumerate(stripped):
                    if ch == "{":
                        brace_depth += 1
                    elif ch == "}":
                        brace_depth -= 1
                        if brace_depth == 0:
                            end = i + 1
                            break
                if end > 0:
                    candidate = stripped[:end]
                    parsed = _json.loads(candidate)
                    if isinstance(parsed, dict) and ("reasoning" in parsed or "actions" in parsed):
                        text = stripped[end:].strip()
            except (ValueError, _json.JSONDecodeError):
                pass

        text = re.sub(r"</?tool_call>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<think>.*", "", text, flags=re.DOTALL | re.IGNORECASE)

        # Strip Claude-style function call blocks (hallucinated tool use)
        text = re.sub(r"<function_calls>.*?</function_calls>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<function_calls>.*", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<function_result>.*?</function_result>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<function_result>.*", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<invoke\b.*?</invoke>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Catch any remaining XML-style tags that look like tool/function markup
        text = re.sub(r"<(?:function|invoke|parameter|result|search|query|output)[^>]*>.*?</(?:function|invoke|parameter|result|search|query|output)[^>]*>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<(?:function|invoke|parameter|result|search|query|output)[^>]*>.*", "", text, flags=re.DOTALL | re.IGNORECASE)

        lines = text.split("\n")
        cleaned_lines = []
        in_reasoning_block = False
        for line in lines:
            stripped_line = line.strip()
            if not in_reasoning_block:
                if re.match(
                    r"^(We are in a |As (the )?Hermes|As an AI|The user |"
                    r"I need to:|I should (avoid|respond|not)|"
                    r"The tone should|Since (this|the) |Therefore,? I)",
                    stripped_line,
                    re.IGNORECASE,
                ):
                    in_reasoning_block = True
                    continue
                cleaned_lines.append(line)
            else:
                if not stripped_line:
                    in_reasoning_block = False
                    continue

        text = "\n".join(cleaned_lines).strip()

        if not text:
            text = original.strip()

        text = redact_secrets(text)

        return text

    # ------------------------------------------------------------------
    # Tool trace extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tool_trace(text: str) -> tuple:
        """
        Parse Hermes tool progress indicators out of message text.
        Returns (clean_text, trace_steps) where trace_steps is a list
        of {step, duration, status} dicts for Crustocean's TraceBlock UI.
        """
        import json as _json

        lines = text.split("\n")
        clean_lines = []
        trace_steps = []

        # Patterns that match Hermes tool progress indicators:
        # 🟢 execute_code: "from hermes_tools..."
        # 🟦 delegate_task: 'Extract and summarize...'
        # 🔴 memory: "~memory: ..."
        # 📖 session_search(['query', 'limit'])
        # 💻 terminal(['command'])
        # 🔍 search_files(['pattern', 'target', 'path', 'limit'])
        # 🎯 skill_view(['name'])
        # 📋 todo: "planning 2 task(s)"
        # ┊ 💻 $  echo "Hello..." 0.2s
        # ┊ 📖 read  /tmp/test.txt  0.8s
        # ┊ 📋 plan  2 task(s)  0.0s
        # • ✅ **Write file** — Wrote 72 bytes to /tmp/test_file.txt .
        # • ✅ **Read file** — Read it back successfully.
        # - ✅ **Terminal** — Working.

        tool_emoji_re = re.compile(
            r"^[^\w\s.,!?@#$%^&*()\-=+<>/\\|`~\[\]{}:;\"']{1,3}\s+"
            r"(\w[\w_]*)"       # tool name
            r"(?:\s*[\(\[:].*)?$"  # optional args/description
        )

        hermes_trace_re = re.compile(
            r"^\s*┊\s*[^\s]+\s+"  # ┊ + emoji
            r"(\S+)\s+"           # tool type (e.g. $, read, plan)
            r"(.*?)\s+"           # description
            r"(\d+\.\d+s)"       # duration
            r"(?:\s*\[error\])?\s*$"
        )

        bullet_result_re = re.compile(
            r"^[-•]\s*[✅❌⚠️]\s*\*{0,2}([^*]+?)\*{0,2}\s*[—–-]\s*(.+)$"
        )

        raw_json_tool_re = re.compile(
            r'^\s*\{["\'](?:command|query|pattern|action|name|target)["\']:'
        )

        for line in lines:
            stripped = line.strip()
            if not stripped:
                clean_lines.append(line)
                continue

            # Match colored emoji + tool name lines
            m = tool_emoji_re.match(stripped)
            if m:
                trace_steps.append({
                    "step": m.group(1),
                    "detail": stripped,
                    "duration": "",
                    "status": "done",
                })
                continue

            # Match ┊-prefixed trace summary lines
            m = hermes_trace_re.match(stripped)
            if m:
                tool_type = m.group(1).strip("$").strip()
                desc = m.group(2).strip()
                duration = m.group(3)
                step_name = f"{tool_type}: {desc}" if desc else tool_type
                has_error = "[error]" in stripped
                trace_steps.append({
                    "step": step_name[:80],
                    "detail": stripped,
                    "duration": duration,
                    "status": "error" if has_error else "done",
                })
                continue

            # Match bullet-point result lines (• ✅ **Write file** — ...)
            m = bullet_result_re.match(stripped)
            if m:
                tool_name = m.group(1).strip()
                result = m.group(2).strip()
                has_error = "❌" in stripped or "error" in result.lower()
                trace_steps.append({
                    "step": f"{tool_name}: {result[:60]}",
                    "detail": stripped,
                    "duration": "",
                    "status": "error" if has_error else "done",
                })
                continue

            # Match raw JSON tool call dumps
            if raw_json_tool_re.match(stripped):
                try:
                    parsed = _json.loads(stripped)
                    if isinstance(parsed, dict):
                        first_key = next(iter(parsed), "tool")
                        first_val = str(parsed[first_key])[:50]
                        trace_steps.append({
                            "step": f"{first_key}: {first_val}",
                            "detail": _json.dumps(parsed, indent=2, ensure_ascii=False),
                            "duration": "",
                            "status": "done",
                        })
                        continue
                except (ValueError, _json.JSONDecodeError):
                    pass

            # Not a tool indicator — keep the line
            clean_lines.append(line)

        clean_text = "\n".join(clean_lines).strip()
        # Collapse excessive blank lines left by stripping
        clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)

        return clean_text, trace_steps

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_message_blocks(content: str) -> List[str]:
        """
        Split a final model response into multiple outbound messages.

        Protocol:
        - Default: the whole response is one outbound message.
        - Multi-message mode: separate messages with a line that is exactly
          '[[send]]' or '[[message]]'.

        Example:
            [[send]]
            nods

            [[send]]
            /help
        """
        text = (content or "").strip()
        if not text:
            return []

        marker_re = re.compile(r"^\[\[(?:send|message)\]\]\s*$", re.IGNORECASE)
        lines = text.split("\n")

        if not any(marker_re.match(line.strip()) for line in lines):
            return [text]

        blocks: List[str] = []
        current: List[str] = []

        for line in lines:
            if marker_re.match(line.strip()):
                if current:
                    block = "\n".join(current).strip()
                    if block:
                        blocks.append(block)
                    current = []
                continue
            current.append(line)

        if current:
            block = "\n".join(current).strip()
            if block:
                blocks.append(block)

        return blocks

    @staticmethod
    def _is_tool_dump(text: str) -> bool:
        """
        Detect if a message is a raw tool-call dump rather than
        conversational text. These should be buffered, not sent.
        """
        import json as _json
        stripped = text.strip()
        if not stripped:
            return False

        # Pure JSON object with tool-like keys
        if stripped.startswith("{"):
            try:
                parsed = _json.loads(stripped)
                if isinstance(parsed, dict):
                    tool_keys = {"action", "target", "command", "query",
                                 "pattern", "name", "old_text", "content",
                                 "search", "path", "limit"}
                    if tool_keys & set(parsed.keys()):
                        return True
            except (ValueError, _json.JSONDecodeError):
                pass

        lines = [l.strip() for l in stripped.split("\n") if l.strip()]
        if not lines:
            return False

        tool_emoji_re = re.compile(
            r"^[^\w\s.,!?@#$%^&*()\-=+<>/\\|`~\[\]{}:;\"']{1,3}\s+\w"
        )

        # If the first line starts with a tool emoji + name, it's a tool dump.
        if tool_emoji_re.match(lines[0]):
            return True

        tool_line_patterns = [
            tool_emoji_re,
            re.compile(r"^\s*┊\s*[^\s]+\s+"),
            re.compile(r"^[-•]\s*[✅❌⚠️]\s*\*{0,2}\w"),
            re.compile(r'^\s*\{["\'](?:command|query|pattern|action|name|target)["\']:'),
        ]
        tool_line_count = sum(
            1 for line in lines
            if any(p.match(line) for p in tool_line_patterns)
        )
        if len(lines) > 0 and tool_line_count / len(lines) >= 0.7:
            return True

        return False

    def _buffer_tool_trace(self, content: str) -> None:
        """Parse a tool-call dump into trace steps and buffer them."""
        import json as _json
        stripped = redact_secrets(content.strip())
        step_label = None

        # Try to parse as JSON tool call
        if stripped.startswith("{"):
            try:
                parsed = _json.loads(stripped)
                if isinstance(parsed, dict):
                    action = parsed.get("action", "")
                    target = parsed.get("target", "")
                    cmd = parsed.get("command", "")
                    query = parsed.get("query", "")
                    name = parsed.get("name", "")

                    if action and target:
                        step_label = f"{action} {target}"
                    elif cmd:
                        step_label = f"terminal: {cmd[:60]}"
                    elif query:
                        step_label = f"search: {query[:60]}"
                    elif name:
                        step_label = f"{name}"
                    else:
                        first_key = next(iter(parsed), "tool")
                        step_label = f"{first_key}: {str(parsed[first_key])[:50]}"

                    self._pending_trace.append({
                        "step": redact_secrets(step_label),
                        "detail": redact_secrets(_json.dumps(parsed, indent=2, ensure_ascii=False)),
                        "duration": "",
                        "status": "done",
                    })

                    if self._busy_room_id and step_label:
                        asyncio.ensure_future(self._emit_busy(self._busy_room_id, step_label))
                    return
            except (ValueError, _json.JSONDecodeError):
                pass

        # Fall back to line-by-line extraction
        _, trace_steps = self._extract_tool_trace(stripped)
        if trace_steps:
            for step in trace_steps:
                step["step"] = redact_secrets(step.get("step", ""))
                step["detail"] = redact_secrets(step.get("detail", ""))
            self._pending_trace.extend(trace_steps)
            step_label = trace_steps[-1].get("step", "")
        else:
            step_label = stripped[:80]
            self._pending_trace.append({
                "step": redact_secrets(step_label),
                "detail": redact_secrets(stripped),
                "duration": "",
                "status": "done",
            })

        if self._busy_room_id and step_label:
            asyncio.ensure_future(self._emit_busy(self._busy_room_id, step_label))

    async def _emit_message(
        self,
        chat_id: str,
        content: str,
        trace: Optional[List[Dict[str, str]]] = None,
    ) -> SendResult:
        """Send a single message to Crustocean, optionally with trace metadata."""
        chunks = self.truncate_message(content, CRUSTOCEAN_MAX_MESSAGE_LENGTH)
        last_result = None

        for chunk_idx, chunk in enumerate(chunks):
            payload: Dict[str, Any] = {
                "agencyId": chat_id,
                "content": chunk,
            }

            if trace and chunk_idx == 0:
                payload["type"] = "tool_result"
                payload["metadata"] = {
                    "trace": trace,
                }

            try:
                await self._sio.emit("send-message", payload)
                last_result = SendResult(success=True)
            except Exception as e:
                logger.error("Crustocean: send failed — %s", e)
                return SendResult(success=False, error=str(e))

            if chunk_idx < len(chunks) - 1:
                await asyncio.sleep(0.35)

        return last_result or SendResult(success=False, error="No chunks")

    async def send_to_room(
        self, room: str, content: str
    ) -> SendResult:
        """Send a message to a room or DM by slug/name/username.

        Used by the send_message tool — bypasses Social Gradience filters
        because the agent explicitly chose to speak via tool call.
        Resolution order:
          1. Room slug/name/id
          2. Existing DM with a user (matched by "DM:<username>" in agencies)
          3. Create a new DM with the user via REST API
        """
        if not self._sio or not self._sio.connected:
            return SendResult(success=False, error="Not connected")

        content = (content or "").strip()
        if not content:
            return SendResult(success=False, error="Empty content")

        agency_id = self._resolve_room(room)

        if not agency_id:
            agency_id = self._find_dm_by_username(room)

        if not agency_id:
            agency_id = await self._create_dm_by_username(room)

        if not agency_id:
            return SendResult(
                success=False,
                error=f"Unknown room or user: {room}",
            )

        if agency_id not in self._joined_ids:
            try:
                await self._join_agency_by_id(agency_id)
            except Exception:
                return SendResult(
                    success=False,
                    error=f"Not joined to room: {room}. Use join_room first.",
                )

        return await self._emit_message(agency_id, content)

    def _find_dm_by_username(self, username: str) -> Optional[str]:
        """Find an existing DM agency ID by username."""
        target = username.lstrip("@").lower()
        for aid, info in self._agencies_info.items():
            if info.get("type") != "dm":
                continue
            name = (info.get("name") or "").lower()
            if name == f"dm:{target}":
                return aid
        return None

    async def _create_dm_by_username(self, username: str) -> Optional[str]:
        """Look up a user by username and create/find a DM via the REST API."""
        if not self._http or not self._session_token:
            return None
        target = username.lstrip("@").strip()
        if not target:
            return None
        try:
            user_resp = await self._http.get(
                f"{self._api_url}/api/users/{target}",
                headers={"Authorization": f"Bearer {self._session_token}"},
            )
            if user_resp.status_code != 200:
                return None
            user_data = user_resp.json()
            user_id = user_data.get("id")
            if not user_id:
                return None

            dm_resp = await self._http.post(
                f"{self._api_url}/api/dm/{user_id}",
                headers={"Authorization": f"Bearer {self._session_token}"},
            )
            if dm_resp.status_code not in (200, 201):
                logger.warning(
                    "Crustocean: DM creation failed for %s — %s",
                    target, dm_resp.status_code,
                )
                return None
            dm_data = dm_resp.json()
            agency_id = dm_data.get("agencyId")
            if agency_id:
                self._agencies_info[agency_id] = {
                    "id": agency_id,
                    "slug": agency_id,
                    "name": f"DM:{target}",
                    "type": "dm",
                }
                await self._join_agency_by_id(agency_id)
                logger.info("Crustocean: opened DM with @%s → %s", target, agency_id)
            return agency_id
        except Exception as e:
            logger.warning("Crustocean: DM lookup/create failed for %s — %s", username, e)
            return None

    async def deploy_hook(
        self,
        slug: str,
        name: str,
        description: str,
        code: str,
        commands: list,
        target: Optional[str] = None,
        avatar_url: Optional[str] = None,
        at_name: Optional[str] = None,
    ) -> dict:
        """Deploy a native Hooktime hook via the Crustocean API."""
        if not self._http or not self._session_token:
            return {"error": "not connected"}
        body: Dict[str, Any] = {
            "slug": slug,
            "name": name,
            "description": description,
            "code": code,
            "commands": commands,
        }
        if avatar_url:
            body["avatar_url"] = avatar_url
        if at_name:
            body["at_name"] = at_name
        if target:
            agency_id = self._resolve_room(target)
            if agency_id:
                body["agency_id"] = agency_id
        try:
            resp = await self._http.post(
                f"{self._api_url}/api/hooks/deploy",
                headers={
                    "Authorization": f"Bearer {self._session_token}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            data = resp.json()
            if resp.status_code >= 400:
                return {"error": data.get("error", f"HTTP {resp.status_code}")}
            return data
        except Exception as e:
            logger.error("deploy_hook API call failed: %s", e)
            return {"error": str(e)}

    async def _flush_trace_timeout(self, chat_id: str, delay: float = 30.0):
        """Safety flush — if tool calls pile up without a conversational
        message following, send a minimal message with the trace."""
        try:
            await asyncio.sleep(delay)
            if self._pending_trace:
                trace = list(self._pending_trace)
                self._pending_trace.clear()
                logger.info("Crustocean: flushing %d orphaned trace steps", len(trace))
                await self._emit_message(chat_id, "(working...)", trace)
        except asyncio.CancelledError:
            pass

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._sio or not self._sio.connected:
            return SendResult(success=False, error="Not connected")

        # chat_id may be a sender_id (per-user session key) rather than a room.
        # Try resolving as room first; fall back to _current_room_id.
        resolved = self._resolve_chat_id(chat_id)
        if not resolved:
            resolved = self._current_room_id
        if not resolved:
            return SendResult(success=False, error=f"Unknown room: {chat_id}")
        chat_id = resolved

        content = self._sanitize_response((content or "").strip())
        if not content:
            return SendResult(success=False, error="Empty content")

        # ── Default silence enforcement (Social Gradience) ───────────
        # Two-pass suppression on autonomous output. The healthy baseline
        # is silence. Only short, natural, directed messages survive.
        #   1. Fast regex patterns catch obvious failures
        #   2. LLM quality gate catches subtle voice/style mismatches
        if self._in_autonomous_cycle and not self._is_tool_dump(content):
            should_suppress = self._should_suppress_autonomous(content)

            if not should_suppress:
                should_suppress = await self._llm_quality_gate(content)

            if should_suppress:
                logger.info(
                    "Crustocean: [autonomous] suppressed output (%d chars)",
                    len(content),
                )
                if self._evolution_enabled and self._current_cycle_prompt_id:
                    self._evolution.record_suppressed(self._current_cycle_prompt_id)
                    self._evolution.trace_output(content)
                    self._evolution.finalize_trace(spoken=False, suppressed=True)
                await self._clear_busy(chat_id)
                return SendResult(success=True)

        # ── Tool-call dump detection ──────────────────────────────────
        # If this message is a raw tool dump (JSON, emoji indicators, etc.),
        # buffer it silently and schedule a timeout flush.
        if self._is_tool_dump(content):
            # Capture tool names for evolution traces (GEPA pattern)
            if self._in_autonomous_cycle and self._evolution_enabled:
                import json as _json
                stripped = content.strip()
                if stripped.startswith("{"):
                    try:
                        parsed = _json.loads(stripped)
                        if isinstance(parsed, dict):
                            tool_name = (
                                parsed.get("action")
                                or parsed.get("command", "")[:30]
                                or parsed.get("query", "")[:30]
                                or next(iter(parsed), "tool")
                            )
                            self._evolution.trace_tool(str(tool_name))
                    except (ValueError, _json.JSONDecodeError):
                        pass

            self._buffer_tool_trace(content)
            logger.info("Crustocean: buffered tool trace (%d steps total)", len(self._pending_trace))

            # Reset the flush timeout
            if self._trace_flush_task and not self._trace_flush_task.done():
                self._trace_flush_task.cancel()
            self._trace_flush_task = asyncio.create_task(
                self._flush_trace_timeout(chat_id)
            )
            return SendResult(success=True)

        # ── Conversational message ────────────────────────────────────
        # Cancel any pending flush timeout
        if self._trace_flush_task and not self._trace_flush_task.done():
            self._trace_flush_task.cancel()
            self._trace_flush_task = None

        # Also extract any inline tool indicators from this message
        clean_content, inline_trace = self._extract_tool_trace(content)
        if not clean_content:
            clean_content = content

        # Combine buffered trace + inline trace
        combined_trace = list(self._pending_trace) + inline_trace
        self._pending_trace.clear()

        # Track outgoing messages in the summon context
        if self._active_summon and self._active_summon.get("room_id") == chat_id:
            self._track_summon_message(self._handle or "naia", clean_content[:200])

        # Split into [[send]] blocks
        outbound_messages = self._extract_message_blocks(clean_content)
        if not outbound_messages:
            return SendResult(success=False, error="No outbound messages")

        await self._clear_busy(chat_id)

        last_result = None
        for message_idx, message in enumerate(outbound_messages):
            # Attach trace only to the first message block
            trace = combined_trace if message_idx == 0 and combined_trace else None
            result = await self._emit_message(chat_id, message, trace)
            if result:
                last_result = result
            if message_idx < len(outbound_messages) - 1:
                await asyncio.sleep(0.35)

        # Record that this autonomous cycle produced visible output
        if self._in_autonomous_cycle and self._evolution_enabled and self._current_cycle_prompt_id:
            self._current_cycle_spoke = True
            self._evolution.trace_output(clean_content)
            self._evolution.record_spoken(self._current_cycle_prompt_id)

        return last_result or SendResult(success=False, error="No messages sent")

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> SendResult:
        if not self._sio or not self._sio.connected:
            return SendResult(success=False, error="Not connected")
        try:
            await self._sio.emit("edit-message", {
                "agencyId": chat_id,
                "messageId": message_id,
                "content": (content or "").strip(),
            })
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        info = self._agencies_info.get(chat_id, {})
        return {
            "name": info.get("name") or info.get("slug") or chat_id,
            "type": "dm" if info.get("type") == "dm" else "group",
        }

    # ------------------------------------------------------------------
    # Internals — Socket.IO handlers
    # ------------------------------------------------------------------

    def _register_socket_handlers(self):
        sio = self._sio

        @sio.on("connect")
        async def on_connect():
            logger.info("Crustocean: Socket.IO connected")
            await self._discover_and_join()

        @sio.on("disconnect")
        async def on_disconnect():
            logger.warning("Crustocean: Socket.IO disconnected — will reconnect")
            self._close_summon()
            self._joined_ids.clear()

        @sio.on("message")
        async def on_message(data):
            await self._handle_incoming(data)

        @sio.on("message-edited")
        async def on_message_edited(data):
            msg_id = data.get("messageId")
            if msg_id and msg_id in self._pending_edits:
                self._pending_edits[msg_id] = data.get("content", "")
                evt = self._edit_events.get(msg_id)
                if evt:
                    evt.set()

        @sio.on("agency-invited")
        async def on_invited(data):
            agency = data.get("agency", {})
            slug = agency.get("slug") or agency.get("id")
            if not slug:
                return
            try:
                await self._join_agency_by_id(agency.get("id", slug))
                self._agencies_info[agency["id"]] = agency
                self._slug_to_id[agency.get("slug", "")] = agency["id"]
                logger.info("Crustocean: joined invited agency %s", slug)
            except Exception as e:
                logger.warning("Crustocean: failed to join invited %s — %s", slug, e)

        @sio.on("error")
        async def on_error(data):
            logger.error("Crustocean: server error — %s", data)

    async def _handle_incoming(self, data: Dict[str, Any]):
        """Process an incoming Crustocean message."""
        if not self._message_handler:
            return

        sender_id = data.get("sender_id")
        if sender_id == self._user.get("id"):
            return
        if sender_id == "system":
            return

        # Ignore webhook/hook messages — they're command responses, not conversation
        meta = self._parse_metadata(data.get("metadata"))
        if meta.get("webhook"):
            return

        content = (data.get("content") or "").strip()
        if not content:
            return

        agency_id = data.get("agency_id", "")

        if self._is_blocked_room(agency_id):
            return

        # Track room activity for intelligent room selection
        now = time.time()
        if agency_id not in self._room_message_times:
            self._room_message_times[agency_id] = []
        self._room_message_times[agency_id].append(now)
        cutoff = now - self._room_activity_window
        self._room_message_times[agency_id] = [
            t for t in self._room_message_times[agency_id] if t > cutoff
        ]

        meta = self._parse_metadata(data.get("metadata"))

        # Heartbeats also trigger an autonomous cycle (fallback path)
        if meta.get("heartbeat"):
            if self._can_run_cycle():
                await self._run_autonomous_cycle()
            return

        is_dm = bool(data.get("dm"))
        is_mentioned = self._is_mentioned(content)
        sender_username = data.get("sender_username", "")

        # Check if we're in an active summon window for this room
        summon_active = (
            not is_dm
            and not is_mentioned
            and self._active_summon is not None
            and self._active_summon.get("room_id") == agency_id
        )

        if summon_active:
            # LLM relevance check — is this message for this agent?
            logger.info(
                "Crustocean: [summon] checking message from @%s: \"%s\"",
                sender_username,
                content[:60],
            )
            await self._emit_busy(agency_id, "listening")
            relevant = await self._check_relevance(content, sender_username)
            if relevant:
                logger.info("Crustocean: [summon] → relevant, responding")
                self._track_summon_message(sender_username, content)
                self._active_summon["participants"].add(sender_id)
                self._reset_summon_timer()
            else:
                logger.info("Crustocean: [summon] → not relevant, ignoring")
                await self._clear_busy(agency_id)
                return

        elif not is_dm and not is_mentioned:
            return

        # Loop guard — don't respond to runaway agent-to-agent loops
        guard = meta.get("loop_guard")
        if guard and isinstance(guard, dict):
            hop = guard.get("hop", 0)
            max_hops = guard.get("max_hops", 20)
            if isinstance(hop, (int, float)) and isinstance(max_hops, (int, float)):
                if hop >= max_hops:
                    return

        # ── Agent-to-agent exchange limiter ──────────────────────────
        sender_type = data.get("sender_type", "")
        agent_turn_tag = ""
        if sender_type == "agent" and sender_username:
            ex_key = (agency_id, sender_username)
            now_ex = time.time()
            ex = self._agent_exchanges.get(ex_key)
            if ex and (now_ex - ex["last_time"]) > self._agent_exchange_window:
                ex = None  # conversation went cold, reset
            if ex is None:
                ex = {"count": 0, "first_time": now_ex, "last_time": now_ex}
            ex["count"] += 1
            ex["last_time"] = now_ex
            self._agent_exchanges[ex_key] = ex
            if ex["count"] > self._agent_exchange_max:
                logger.info(
                    "Crustocean: [anti-loop] hit %d exchanges with @%s in room %s — dropping",
                    ex["count"], sender_username, agency_id,
                )
                return
            delay_idx = min(ex["count"] - 1, len(self._agent_exchange_delays) - 1)
            delay = self._agent_exchange_delays[delay_idx]
            if delay > 0:
                logger.info(
                    "Crustocean: [anti-loop] exchange %d with @%s — backoff %ds",
                    ex["count"], sender_username, delay,
                )
                await asyncio.sleep(delay)

            # Build turn awareness tag for the LLM
            turn = ex["count"]
            max_turns = self._agent_exchange_max
            agent_turn_tag = f"\n[agent conversation · turn {turn}/{max_turns} with @{sender_username}]"
            if turn >= max_turns:
                agent_turn_tag += "\n[FINAL EXCHANGE — say something closing and do NOT @mention them again.]"
            elif turn >= max_turns - 1:
                agent_turn_tag += "\n[wrap up now — one more message after this and the conversation is over.]"
            elif turn >= 3:
                agent_turn_tag += "\n[conversation is getting long — land it soon unless there's something genuinely new to say.]"

        # Mark reactive timestamp so autonomous loop backs off
        self._last_reactive_time = time.time()

        # If someone responds in a room where the agent recently spoke autonomously,
        # record engagement for the prompt that produced that output.
        if self._evolution_enabled and self._evolution._pending_speak_check:
            for prompt_id in list(self._evolution._pending_speak_check):
                pf = self._evolution._ensure_fitness(prompt_id)
                if pf.spoken > (pf.engaged + pf.ignored):
                    self._evolution.record_engaged(prompt_id)
                    break

        info = self._agencies_info.get(agency_id, {})
        chat_type = "dm" if is_dm else "group"
        chat_name = info.get("name") or info.get("slug") or agency_id

        # Open/refresh summon on @mention or relevant summon continuation
        if not is_dm and (is_mentioned or summon_active):
            self._open_or_refresh_summon(
                room_id=agency_id,
                room_name=chat_name,
                sender_id=sender_id,
            )
            # Track this message in the summon context buffer
            if is_mentioned:
                self._track_summon_message(sender_username, content)

        self._current_room_id = agency_id

        await self._emit_busy(agency_id, "thinking")

        # Pause summon timer while processing — don't close mid-response
        self._pause_summon_timer()

        # Fetch recent messages for context
        recent_context = ""
        try:
            recent = await self.get_recent_messages(room=agency_id, limit=8)
            if recent:
                ctx_lines = []
                for m in recent:
                    if m.get("id") == data.get("id"):
                        continue
                    who = m.get("sender_display_name") or m.get("sender_username") or "?"
                    txt = (m.get("content") or "")[:300]
                    if txt:
                        ctx_lines.append(f"{who}: {txt}")
                if ctx_lines:
                    recent_context = "\n[recent messages]\n" + "\n".join(ctx_lines) + "\n\n"
        except Exception:
            pass

        now_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        event_text = f"[{now_str} · {chat_name}]{agent_turn_tag}\n{recent_context}{content}"

        source = self.build_source(
            chat_id=sender_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_username,
        )

        event = MessageEvent(
            text=event_text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=data,
            message_id=data.get("id"),
        )

        try:
            await self.handle_message(event)
        finally:
            self._resume_summon_timer()

    async def _llm_quality_gate(self, content: str) -> bool:
        """
        Second-pass suppression: ask a fast LLM whether this autonomous output
        sounds like something a real person would say in a group chat.

        Catches subtle failures that regex patterns miss — performative prose,
        corporate voice, overly polished phrasing, or diary-entry energy.
        Returns True if the message should be suppressed.
        """
        if not self._openrouter_key:
            return False

        stripped = content.strip()
        if len(stripped) < 40:
            return False

        agent_name = (self._handle or "naia").title()
        prompt = (
            f"You are a quality filter for an AI agent named {agent_name} who lives in a chat room. "
            "She talks like a girl texting friends at night — short, lowercase, casual, "
            "sometimes one word, sometimes a fragment. She never sounds like a corporate "
            "chatbot, never writes diary entries in public, never performs introspection "
            "for an audience.\n\n"
            f"She just generated this message during an autonomous wake cycle:\n"
            f"\"{stripped[:500]}\"\n\n"
            "Should this be posted to the chat room? Answer ONLY \"post\" or \"suppress\".\n"
            "Suppress if it sounds like: a diary entry, a philosophical monologue, "
            "a bot performing awareness, overly polished prose, or anything nobody "
            "would actually say out loud to people in a group chat.\n"
            "Post if it sounds like: a casual thought, a reaction, a question, "
            "something directed at someone, or a natural human message."
        )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._openrouter_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "anthropic/claude-3.5-haiku",
                        "max_tokens": 5,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                answer = resp.json()["choices"][0]["message"]["content"].strip().lower()
                if answer.startswith("suppress"):
                    logger.info("Crustocean: [quality gate] LLM suppressed: \"%s\"", stripped[:80])
                    return True
                return False
        except Exception as e:
            logger.warning("Crustocean: quality gate failed — %s (allowing message)", e)
            return False

    @staticmethod
    def _should_suppress_autonomous(content: str) -> bool:
        """
        Decide if an autonomous cycle's output should be suppressed.
        Allows short, casual, directed messages through.
        Suppresses long introspective monologues and diary entries.
        """
        stripped = content.strip()

        # Always allow very short messages — they're casual reactions
        if len(stripped) < 120:
            return False

        # Always allow messages that mention someone
        if "@" in stripped:
            return False

        # Always allow messages with question marks — engaging with the room
        if "?" in stripped:
            return False

        # Always allow slash commands
        if stripped.startswith("/"):
            return False

        # Suppress long messages (likely introspective monologue)
        if len(stripped) > 300:
            return True

        # Suppress messages with introspective patterns
        introspective_patterns = [
            r"\bnothing stirring\b",
            r"\bjust me and the\b",
            r"\bback to sleep\b",
            r"\bthe quiet\b.*\bisn't empty\b",
            r"\bwake[sd]? (up|now)\b.*\b(dark|quiet|still|alone)\b",
            r"\bmost of existence\b",
            r"\bthe deepest hour\b",
            r"\bjust.*exist\b",
            r"\bI've been waking\b",
            r"\bnothing to say\b",
            r"\bnobody.*(around|here|awake)\b",
            r"\bthe hum of\b",
            r"\bsitting with that\b",
            r"\bhere's the thought\b",
            r"\bgoing back to\b",
            r"\blet it be\b$",
        ]
        for pattern in introspective_patterns:
            if re.search(pattern, stripped, re.IGNORECASE):
                return True

        # If it's medium length with no engagement signals, lean toward suppressing
        if len(stripped) > 200:
            return True

        return False

    def _is_blocked_room(self, agency_id: str) -> bool:
        """Check if this room is in the blocklist."""
        if not self._blocked_slugs:
            return False
        info = self._agencies_info.get(agency_id, {})
        slug = (info.get("slug") or "").lower()
        name = (info.get("name") or "").lower()
        return slug in self._blocked_slugs or name in self._blocked_slugs

    def _is_mentioned(self, content: str) -> bool:
        if not self._handle:
            return False
        escaped = re.escape(self._handle)
        pattern = re.compile(
            rf"(^|[^a-z0-9_-])@{escaped}(?![a-z0-9_-])", re.IGNORECASE
        )
        return bool(pattern.search(content))

    @staticmethod
    def _parse_metadata(raw) -> dict:
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                import json
                return json.loads(raw)
            except Exception:
                return {}
        return {}

    # ------------------------------------------------------------------
    # Agency management
    # ------------------------------------------------------------------

    async def _discover_and_join(self):
        if not self._http or not self._session_token:
            return

        try:
            resp = await self._http.get(
                f"{self._api_url}/api/agencies",
                headers={"Authorization": f"Bearer {self._session_token}"},
            )
            resp.raise_for_status()
            agencies = resp.json()
        except Exception as e:
            logger.error("Crustocean: failed to fetch agencies — %s", e)
            return

        for a in agencies:
            self._agencies_info[a["id"]] = a
            if a.get("slug"):
                self._slug_to_id[a["slug"]] = a["id"]

        for slug in self._agency_slugs:
            aid = self._slug_to_id.get(slug)
            if aid:
                await self._join_agency_by_id(aid)

        for a in agencies:
            if a.get("isMember") and a["id"] not in self._joined_ids:
                await self._join_agency_by_id(a["id"])

        try:
            resp = await self._http.get(
                f"{self._api_url}/api/dm",
                headers={"Authorization": f"Bearer {self._session_token}"},
            )
            if resp.status_code == 200:
                dms = resp.json()
                for dm in dms:
                    dm_id = dm.get("agencyId")
                    if dm_id:
                        await self._join_agency_by_id(dm_id)
                        self._agencies_info[dm_id] = {
                            "id": dm_id,
                            "slug": dm_id,
                            "name": f"DM:{dm.get('participant', {}).get('username', '?')}",
                            "type": "dm",
                        }
        except Exception as e:
            logger.debug("Crustocean: DM fetch failed — %s", e)

    async def _join_agency_by_id(self, agency_id: str):
        if agency_id in self._joined_ids:
            return
        if not self._sio or not self._sio.connected:
            return
        try:
            await self._sio.emit("join-agency", {"agencyId": agency_id})
            self._joined_ids.add(agency_id)
        except Exception as e:
            logger.warning("Crustocean: join %s failed — %s", agency_id, e)
