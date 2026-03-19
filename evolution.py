"""
Motive ecology and evolutionary tuning for Social Gradience.

Manages a living population of internal motives (impulses that drive
autonomous wake cycles) under selection pressure from live social
engagement signals. Motives compete for cycles, get scored on fitness,
reproduce through LLM-driven mutation, and get culled when weak.

Implements patterns from Nous Research's hermes-agent-self-evolution
(DSPy + GEPA): constraint-gated mutations, execution trace-aware
reflective analysis, fitness scoring with length penalties, tournament
selection, and population management. Applied here as a live runtime
component rather than offline batch optimization.

Architecture (from Nous PLAN.md):
  Read current prompt ──► Collect execution traces
                                  │
                                  ▼
                             GEPA-style optimizer ◄── Trace analysis
                                  │                        ▲
                                  ▼                        │
                             Candidate variants ──► Constraint gates
                                  │
                             Population update

Engagement signals tracked per prompt:
  - fired:       prompt was selected for a wake cycle
  - spoken:      cycle produced visible output (message sent to room)
  - suppressed:  output was caught by the introspection filter
  - engaged:     someone responded within 10 minutes of Reina's message
  - ignored:     Reina spoke but nobody responded within 10 minutes

Constraint gates (every variant must pass ALL):
  1. Size limit: prompt text ≤ 500 chars
  2. Growth limit: ≤ 40% larger than parent
  3. Non-empty: must contain actionable text
  4. Energy preservation: energy level cannot drift
  5. Semantic coherence: must still be a wake-cycle motivation prompt
"""

import asyncio
import json
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = os.path.join(
    os.getenv("HERMES_HOME", "/data/hermes"), "evolution"
)

POPULATION_FILE = "population.json"
FITNESS_LOG_FILE = "fitness_log.jsonl"
TRACES_FILE = "traces.jsonl"

MIN_SAMPLES_FOR_MUTATION = 10
EVOLUTION_INTERVAL_HOURS = 24
MAX_VARIANTS_PER_PROMPT = 3
MAX_POPULATION_SIZE = 120
MUTATION_BATCH_SIZE = 5
ENGAGEMENT_WINDOW_SECONDS = 600  # 10 minutes

# Constraint constants (from Nous constraints.py patterns)
MAX_PROMPT_SIZE = 500           # chars — prompts are injected into context every cycle
MAX_GROWTH_RATIO = 0.40         # variant can't exceed parent by more than 40%
LENGTH_PENALTY_THRESHOLD = 0.85 # penalty starts at 85% of max size
LENGTH_PENALTY_MAX = 0.25       # max penalty applied to fitness

# Trace buffer
MAX_TRACES_PER_PROMPT = 5       # keep last N execution traces per prompt


# ── Constraint gates ─────────────────────────────────────────────────────


@dataclass
class ConstraintResult:
    """Result of a single constraint check."""
    passed: bool
    name: str
    message: str


def validate_variant(
    variant: dict,
    parent: Optional[dict] = None,
) -> List[ConstraintResult]:
    """
    Run all constraint gates on a candidate variant.
    Every gate must pass for the variant to be accepted.
    Modeled after Nous constraints.py — size, growth, non-empty, structural.
    """
    results = []
    text = variant.get("text", "")

    # Gate 1: Size limit
    size = len(text)
    if size <= MAX_PROMPT_SIZE:
        results.append(ConstraintResult(
            True, "size_limit",
            f"Size OK: {size}/{MAX_PROMPT_SIZE} chars",
        ))
    else:
        results.append(ConstraintResult(
            False, "size_limit",
            f"Size exceeded: {size}/{MAX_PROMPT_SIZE} chars ({size - MAX_PROMPT_SIZE} over)",
        ))

    # Gate 2: Growth limit (if parent provided)
    if parent:
        parent_size = max(1, len(parent.get("text", "")))
        growth = (size - parent_size) / parent_size
        if growth <= MAX_GROWTH_RATIO:
            results.append(ConstraintResult(
                True, "growth_limit",
                f"Growth OK: {growth:+.1%} (max {MAX_GROWTH_RATIO:+.1%})",
            ))
        else:
            results.append(ConstraintResult(
                False, "growth_limit",
                f"Growth exceeded: {growth:+.1%} (max {MAX_GROWTH_RATIO:+.1%})",
            ))

    # Gate 3: Non-empty with actionable content
    stripped = text.strip()
    if len(stripped) >= 15:
        results.append(ConstraintResult(True, "non_empty", "Content present"))
    else:
        results.append(ConstraintResult(
            False, "non_empty",
            f"Too short or empty ({len(stripped)} chars)",
        ))

    # Gate 4: Energy preservation — energy level must match parent
    if parent and variant.get("energy") != parent.get("energy"):
        results.append(ConstraintResult(
            False, "energy_preservation",
            f"Energy drifted: {parent.get('energy')} → {variant.get('energy')}",
        ))
    elif variant.get("energy") in ("low", "medium", "high"):
        results.append(ConstraintResult(
            True, "energy_preservation", "Energy level valid",
        ))
    else:
        results.append(ConstraintResult(
            False, "energy_preservation",
            f"Invalid energy level: {variant.get('energy')}",
        ))

    # Gate 5: Semantic coherence — must look like a motivation prompt
    lower = stripped.lower()
    instruction_signals = [
        "you", "your", "check", "look", "observe", "explore", "try",
        "talk", "say", "ask", "run", "think", "write", "share", "find",
        "join", "wander", "notice", "react", "start", "pick", "exist",
        "feel", "drift", "settle", "poke", "discover", "search", "learn",
    ]
    has_signal = any(word in lower for word in instruction_signals)
    if has_signal:
        results.append(ConstraintResult(
            True, "semantic_coherence", "Reads as a motivation prompt",
        ))
    else:
        results.append(ConstraintResult(
            False, "semantic_coherence",
            "Doesn't read as a wake-cycle motivation prompt",
        ))

    return results


def passes_all_gates(results: List[ConstraintResult]) -> bool:
    return all(r.passed for r in results)


# ── Execution traces ─────────────────────────────────────────────────────


@dataclass
class CycleTrace:
    """Execution trace for a single autonomous wake cycle.
    Captures what the prompt actually produced so the mutator
    can understand WHY things failed — not just that they failed.
    (GEPA pattern from Nous PLAN.md)
    """
    prompt_id: str
    timestamp: str = ""
    room: str = ""
    tools_used: List[str] = field(default_factory=list)
    output_text: str = ""
    was_spoken: bool = False
    was_suppressed: bool = False
    was_engaged: bool = False
    was_ignored: bool = False

    def to_dict(self) -> dict:
        return {
            "prompt_id": self.prompt_id,
            "timestamp": self.timestamp,
            "room": self.room,
            "tools_used": self.tools_used,
            "output_text": self.output_text[:300],
            "was_spoken": self.was_spoken,
            "was_suppressed": self.was_suppressed,
            "was_engaged": self.was_engaged,
            "was_ignored": self.was_ignored,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CycleTrace":
        return cls(
            prompt_id=d.get("prompt_id", ""),
            timestamp=d.get("timestamp", ""),
            room=d.get("room", ""),
            tools_used=d.get("tools_used", []),
            output_text=d.get("output_text", ""),
            was_spoken=d.get("was_spoken", False),
            was_suppressed=d.get("was_suppressed", False),
            was_engaged=d.get("was_engaged", False),
            was_ignored=d.get("was_ignored", False),
        )

    def summary(self) -> str:
        """One-line summary for the mutation prompt."""
        outcome = "silent"
        if self.was_suppressed:
            outcome = "SUPPRESSED (introspection filter caught it)"
        elif self.was_spoken and self.was_engaged:
            outcome = "ENGAGED (someone responded)"
        elif self.was_spoken and self.was_ignored:
            outcome = "IGNORED (nobody responded)"
        elif self.was_spoken:
            outcome = "spoken (awaiting response)"

        tools = ", ".join(self.tools_used[:5]) if self.tools_used else "none"
        output_preview = self.output_text[:100].replace("\n", " ") if self.output_text else "(no output)"
        return f"  Room: {self.room} | Tools: {tools} | Outcome: {outcome}\n  Output: \"{output_preview}\""


# ── Fitness tracking ─────────────────────────────────────────────────────


class PromptFitness:
    """Tracks engagement signals for a single prompt variant."""

    __slots__ = ("fired", "spoken", "suppressed", "engaged", "ignored",
                 "last_fired", "last_engaged")

    def __init__(self):
        self.fired: int = 0
        self.spoken: int = 0
        self.suppressed: int = 0
        self.engaged: int = 0
        self.ignored: int = 0
        self.last_fired: float = 0.0
        self.last_engaged: float = 0.0

    @property
    def engagement_rate(self) -> float:
        if self.spoken == 0:
            return 0.0
        return self.engaged / self.spoken

    @property
    def speak_rate(self) -> float:
        if self.fired == 0:
            return 0.0
        return self.spoken / self.fired

    @property
    def suppression_rate(self) -> float:
        total_output = self.spoken + self.suppressed
        if total_output == 0:
            return 0.0
        return self.suppressed / total_output

    def fitness(self, prompt_size: int = 0) -> float:
        """
        Multi-dimensional fitness with length penalty.
        Modeled after Nous fitness.py composite scoring.

        fitness = (engagement_rate × 0.5 + speak_penalty × 0.3 + suppression_bonus × 0.2)
                  - length_penalty
        """
        if self.fired < 3:
            return 0.5  # insufficient data — neutral prior

        er = self.engagement_rate
        sr = self.speak_rate
        supr = self.suppression_rate

        speak_penalty = 1.0 if sr >= 0.05 else (sr / 0.05)
        suppression_bonus = 1.0 - supr

        raw = er * 0.5 + speak_penalty * 0.3 + suppression_bonus * 0.2

        # Length penalty (from Nous fitness.py pattern)
        length_pen = 0.0
        if prompt_size > 0:
            ratio = prompt_size / MAX_PROMPT_SIZE
            if ratio > LENGTH_PENALTY_THRESHOLD:
                length_pen = min(
                    LENGTH_PENALTY_MAX,
                    (ratio - LENGTH_PENALTY_THRESHOLD) * (LENGTH_PENALTY_MAX / (1.0 - LENGTH_PENALTY_THRESHOLD)),
                )

        return max(0.0, raw - length_pen)

    def to_dict(self) -> dict:
        return {
            "fired": self.fired,
            "spoken": self.spoken,
            "suppressed": self.suppressed,
            "engaged": self.engaged,
            "ignored": self.ignored,
            "last_fired": self.last_fired,
            "last_engaged": self.last_engaged,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PromptFitness":
        pf = cls()
        pf.fired = d.get("fired", 0)
        pf.spoken = d.get("spoken", 0)
        pf.suppressed = d.get("suppressed", 0)
        pf.engaged = d.get("engaged", 0)
        pf.ignored = d.get("ignored", 0)
        pf.last_fired = d.get("last_fired", 0.0)
        pf.last_engaged = d.get("last_engaged", 0.0)
        return pf


# ── Evolution engine ─────────────────────────────────────────────────────


class EvolutionEngine:
    """
    Motive ecology manager. Drives the evolutionary loop that makes
    Social Gradience adaptive: motives that produce suppressed or ignored
    output lose fitness; motives that spark engagement survive and
    reproduce through mutation.

    Integrates patterns from Nous hermes-agent-self-evolution:
      - Constraint-gated mutations (constraints.py)
      - Execution trace-aware reflective analysis (GEPA)
      - LLM-as-judge feedback loop (fitness.py)
      - Length penalties to prevent prompt bloat
      - Population management with variant limits

    Lifecycle:
      1. record_fire(prompt_id)     — prompt selected for wake cycle
      2. record_trace(trace)        — execution trace captured
      3. record_spoken(prompt_id)   — cycle produced visible output
      4. record_suppressed(prompt_id) — introspection filter caught output
      5. record_engaged(prompt_id)  — someone responded to Reina
      6. record_ignored(prompt_id)  — nobody responded within window

    Every EVOLUTION_INTERVAL_HOURS, evolve() runs:
      - Tournament selection → failure analysis → trace-aware mutation →
        constraint gates → population injection
    """

    def __init__(self, data_dir: Optional[str] = None):
        self._data_dir = data_dir or DEFAULT_DATA_DIR
        self._fitness: Dict[str, PromptFitness] = {}
        self._population: List[dict] = []
        self._last_evolution: float = 0.0
        self._evolution_lock = asyncio.Lock()

        self._pending_speak_check: Dict[str, float] = {}

        # Execution trace buffer — keyed by prompt_id, stores last N traces
        self._traces: Dict[str, deque] = {}
        self._current_trace: Optional[CycleTrace] = None

        os.makedirs(self._data_dir, exist_ok=True)
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────

    def _population_path(self) -> str:
        return os.path.join(self._data_dir, POPULATION_FILE)

    def _fitness_log_path(self) -> str:
        return os.path.join(self._data_dir, FITNESS_LOG_FILE)

    def _traces_path(self) -> str:
        return os.path.join(self._data_dir, TRACES_FILE)

    def _load(self) -> None:
        path = self._population_path()
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                self._population = data.get("population", [])
                self._last_evolution = data.get("last_evolution", 0.0)
                for pid, fd in data.get("fitness", {}).items():
                    self._fitness[pid] = PromptFitness.from_dict(fd)

                for pid, trace_list in data.get("traces", {}).items():
                    self._traces[pid] = deque(
                        (CycleTrace.from_dict(t) for t in trace_list),
                        maxlen=MAX_TRACES_PER_PROMPT,
                    )

                logger.info(
                    "Evolution: loaded %d prompts, %d fitness records, %d trace buffers",
                    len(self._population), len(self._fitness), len(self._traces),
                )
            except Exception as e:
                logger.warning("Evolution: failed to load state — %s", e)

    def _save(self) -> None:
        data = {
            "population": self._population,
            "fitness": {k: v.to_dict() for k, v in self._fitness.items()},
            "traces": {
                k: [t.to_dict() for t in v]
                for k, v in self._traces.items()
            },
            "last_evolution": self._last_evolution,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            path = self._population_path()
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Evolution: failed to save state — %s", e)

    def _log_event(self, event: dict) -> None:
        """Append a fitness event to the log for observability."""
        event["timestamp"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(self._fitness_log_path(), "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception:
            pass

    # ── Population management ────────────────────────────────────────────

    def initialize_population(self, base_prompts: List[dict]) -> None:
        """Seed from base poker prompts if no evolved population exists."""
        if self._population:
            return

        for p in base_prompts:
            entry = {
                "id": p["id"],
                "energy": p["energy"],
                "text": p["text"],
                "generation": 0,
                "parent": None,
                "mutated": False,
            }
            self._population.append(entry)
            if p["id"] not in self._fitness:
                self._fitness[p["id"]] = PromptFitness()

        self._save()
        logger.info("Evolution: initialized population with %d base prompts", len(base_prompts))

    def get_population(self) -> List[dict]:
        return list(self._population)

    def get_active_prompts(self) -> List[dict]:
        """Return prompts in the format poker.py expects."""
        return [
            {"id": p["id"], "energy": p["energy"], "text": p["text"]}
            for p in self._population
        ]

    def _find_prompt(self, prompt_id: str) -> Optional[dict]:
        for p in self._population:
            if p["id"] == prompt_id:
                return p
        return None

    # ── Execution trace capture ──────────────────────────────────────────

    def begin_trace(self, prompt_id: str, room: str = "") -> None:
        """Start capturing an execution trace for the current cycle."""
        self._current_trace = CycleTrace(
            prompt_id=prompt_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            room=room,
        )

    def trace_tool(self, tool_name: str) -> None:
        """Record a tool use in the current trace."""
        if self._current_trace:
            self._current_trace.tools_used.append(tool_name)

    def trace_output(self, text: str) -> None:
        """Record the output text from the current cycle."""
        if self._current_trace:
            self._current_trace.output_text = text[:300]

    def finalize_trace(
        self,
        spoken: bool = False,
        suppressed: bool = False,
    ) -> None:
        """Finalize and store the current execution trace."""
        if not self._current_trace:
            return

        trace = self._current_trace
        trace.was_spoken = spoken
        trace.was_suppressed = suppressed
        self._current_trace = None

        pid = trace.prompt_id
        if pid not in self._traces:
            self._traces[pid] = deque(maxlen=MAX_TRACES_PER_PROMPT)
        self._traces[pid].append(trace)

        # Also append to the traces log file for long-term observability
        try:
            with open(self._traces_path(), "a") as f:
                f.write(json.dumps(trace.to_dict()) + "\n")
        except Exception:
            pass

    def mark_trace_engaged(self, prompt_id: str) -> None:
        """Mark the most recent trace for a prompt as engaged."""
        if prompt_id in self._traces and self._traces[prompt_id]:
            self._traces[prompt_id][-1].was_engaged = True

    def mark_trace_ignored(self, prompt_id: str) -> None:
        """Mark the most recent trace for a prompt as ignored."""
        if prompt_id in self._traces and self._traces[prompt_id]:
            self._traces[prompt_id][-1].was_ignored = True

    def get_traces(self, prompt_id: str) -> List[CycleTrace]:
        """Get recent execution traces for a prompt."""
        if prompt_id in self._traces:
            return list(self._traces[prompt_id])
        return []

    # ── Signal recording ─────────────────────────────────────────────────

    def _ensure_fitness(self, prompt_id: str) -> PromptFitness:
        if prompt_id not in self._fitness:
            self._fitness[prompt_id] = PromptFitness()
        return self._fitness[prompt_id]

    def record_fire(self, prompt_id: str) -> None:
        pf = self._ensure_fitness(prompt_id)
        pf.fired += 1
        pf.last_fired = time.time()
        self._pending_speak_check[prompt_id] = time.time()
        self._log_event({"event": "fire", "prompt": prompt_id})

    def record_spoken(self, prompt_id: str) -> None:
        pf = self._ensure_fitness(prompt_id)
        pf.spoken += 1
        self._log_event({"event": "spoken", "prompt": prompt_id})
        self._save()

    def record_suppressed(self, prompt_id: str) -> None:
        pf = self._ensure_fitness(prompt_id)
        pf.suppressed += 1
        self._pending_speak_check.pop(prompt_id, None)
        self._log_event({"event": "suppressed", "prompt": prompt_id})
        self._save()

    def record_engaged(self, prompt_id: str) -> None:
        pf = self._ensure_fitness(prompt_id)
        pf.engaged += 1
        pf.last_engaged = time.time()
        self._pending_speak_check.pop(prompt_id, None)
        self.mark_trace_engaged(prompt_id)
        self._log_event({
            "event": "engaged", "prompt": prompt_id,
            "fitness": round(self._get_fitness_score(prompt_id), 3),
        })
        self._save()

    def record_ignored(self, prompt_id: str) -> None:
        pf = self._ensure_fitness(prompt_id)
        pf.ignored += 1
        self._pending_speak_check.pop(prompt_id, None)
        self.mark_trace_ignored(prompt_id)
        self._log_event({
            "event": "ignored", "prompt": prompt_id,
            "fitness": round(self._get_fitness_score(prompt_id), 3),
        })
        self._save()

    def record_silent_cycle(self, prompt_id: str) -> None:
        """Cycle produced no output at all."""
        self._pending_speak_check.pop(prompt_id, None)
        self._log_event({"event": "silent", "prompt": prompt_id})

    # ── Fitness queries ──────────────────────────────────────────────────

    def _get_fitness_score(self, prompt_id: str) -> float:
        pf = self._ensure_fitness(prompt_id)
        prompt = self._find_prompt(prompt_id)
        size = len(prompt["text"]) if prompt else 0
        return pf.fitness(prompt_size=size)

    def get_fitness(self, prompt_id: str) -> float:
        return self._get_fitness_score(prompt_id)

    def get_fitness_report(self) -> List[dict]:
        """Ranked fitness report for all prompts."""
        report = []
        for p in self._population:
            pf = self._ensure_fitness(p["id"])
            score = pf.fitness(prompt_size=len(p["text"]))
            report.append({
                "id": p["id"],
                "energy": p["energy"],
                "generation": p.get("generation", 0),
                "mutated": p.get("mutated", False),
                "fitness": round(score, 3),
                "engagement_rate": round(pf.engagement_rate, 3),
                "speak_rate": round(pf.speak_rate, 3),
                "suppression_rate": round(pf.suppression_rate, 3),
                "fired": pf.fired,
                "spoken": pf.spoken,
                "engaged": pf.engaged,
                "suppressed": pf.suppressed,
                "ignored": pf.ignored,
                "size": len(p["text"]),
            })
        report.sort(key=lambda x: x["fitness"], reverse=True)
        return report

    def get_weakest(self, n: int = 5) -> List[dict]:
        """Return the n weakest prompts with enough samples."""
        candidates = []
        for p in self._population:
            pf = self._ensure_fitness(p["id"])
            if pf.fired >= MIN_SAMPLES_FOR_MUTATION:
                candidates.append((p, pf.fitness(prompt_size=len(p["text"]))))
        candidates.sort(key=lambda x: x[1])
        return [c[0] for c in candidates[:n]]

    def get_strongest(self, n: int = 5) -> List[dict]:
        """Return the n strongest prompts."""
        candidates = []
        for p in self._population:
            pf = self._ensure_fitness(p["id"])
            if pf.fired >= MIN_SAMPLES_FOR_MUTATION:
                candidates.append((p, pf.fitness(prompt_size=len(p["text"]))))
        candidates.sort(key=lambda x: x[1], reverse=True)
        return [c[0] for c in candidates[:n]]

    # ── Evolution ────────────────────────────────────────────────────────

    def should_evolve(self) -> bool:
        hours_since = (time.time() - self._last_evolution) / 3600
        return hours_since >= EVOLUTION_INTERVAL_HOURS

    async def evolve(self, llm_caller=None) -> dict:
        """
        Run one evolution cycle with constraint-gated mutations.

        Pipeline (from Nous architecture):
          1. Tournament selection → weakest prompts
          2. Failure analysis + trace collection
          3. LLM-driven mutation with trace context (GEPA pattern)
          4. Constraint gates — reject invalid variants
          5. Population injection for valid variants
        """
        async with self._evolution_lock:
            weakest = self.get_weakest(MUTATION_BATCH_SIZE)
            strongest = self.get_strongest(3)

            if not weakest:
                logger.info("Evolution: not enough data for evolution yet")
                self._last_evolution = time.time()
                self._save()
                return {"mutations": 0, "reason": "insufficient_data"}

            mutations_applied = 0
            mutations_rejected = 0

            for weak_prompt in weakest:
                exemplar = random.choice(strongest) if strongest else None
                pf = self._ensure_fitness(weak_prompt["id"])
                traces = self.get_traces(weak_prompt["id"])

                failure_analysis = self._analyze_failure(weak_prompt, pf)
                trace_context = self._build_trace_context(traces)

                if llm_caller:
                    variant = await self._llm_mutate(
                        weak_prompt, exemplar, failure_analysis,
                        trace_context, llm_caller,
                    )
                else:
                    variant = self._heuristic_mutate(weak_prompt, failure_analysis)

                if not variant:
                    continue

                # ── Constraint gates ──────────────────────────────────
                gate_results = validate_variant(variant, parent=weak_prompt)

                if passes_all_gates(gate_results):
                    self._inject_variant(variant, parent_id=weak_prompt["id"])
                    mutations_applied += 1
                    self._log_event({
                        "event": "mutation_accepted",
                        "variant": variant["id"],
                        "parent": weak_prompt["id"],
                        "gates": [{"name": r.name, "msg": r.message} for r in gate_results],
                    })
                else:
                    mutations_rejected += 1
                    failed_gates = [r for r in gate_results if not r.passed]
                    logger.info(
                        "Evolution: variant %s REJECTED — failed gates: %s",
                        variant.get("id", "?"),
                        ", ".join(f"{r.name}: {r.message}" for r in failed_gates),
                    )
                    self._log_event({
                        "event": "mutation_rejected",
                        "parent": weak_prompt["id"],
                        "failed_gates": [
                            {"name": r.name, "msg": r.message}
                            for r in failed_gates
                        ],
                    })

            self._cull_population()
            self._last_evolution = time.time()
            self._save()

            summary = {
                "mutations": mutations_applied,
                "rejected": mutations_rejected,
                "population_size": len(self._population),
                "weakest_evolved": [w["id"] for w in weakest],
                "exemplars_used": [s["id"] for s in strongest] if strongest else [],
            }

            self._log_event({"event": "evolution", **summary})
            logger.info(
                "Evolution: cycle complete — %d accepted, %d rejected, population=%d",
                mutations_applied, mutations_rejected, len(self._population),
            )
            return summary

    @staticmethod
    def _analyze_failure(prompt: dict, pf: PromptFitness) -> str:
        """Build a plain-text failure analysis for the LLM mutator."""
        issues = []
        if pf.speak_rate < 0.05:
            issues.append(
                f"Almost never produces visible output "
                f"(speak rate {pf.speak_rate:.1%} over {pf.fired} fires). "
                f"The prompt may be too passive or introspective."
            )
        if pf.suppressed > pf.spoken and pf.suppressed > 3:
            issues.append(
                f"Output gets suppressed {pf.suppressed} times vs {pf.spoken} spoken. "
                f"The prompt tends to produce long introspective monologues "
                f"instead of short casual messages."
            )
        if pf.spoken > 0 and pf.engagement_rate < 0.1:
            issues.append(
                f"Low engagement ({pf.engagement_rate:.1%}). "
                f"When this prompt does produce output, nobody responds. "
                f"The messages may not be interesting or directed enough."
            )
        if pf.ignored > pf.engaged and pf.ignored > 3:
            issues.append(
                f"Ignored {pf.ignored} times vs engaged {pf.engaged} times. "
                f"Output doesn't spark conversation."
            )
        if pf.suppression_rate > 0.6 and (pf.spoken + pf.suppressed) > 5:
            issues.append(
                f"High suppression rate ({pf.suppression_rate:.0%}). "
                f"The prompt consistently generates content that's too long "
                f"or too introspective for the chat."
            )

        if not issues:
            issues.append("General low fitness. No specific failure pattern identified.")

        return "\n".join(f"- {i}" for i in issues)

    @staticmethod
    def _build_trace_context(traces: List[CycleTrace]) -> str:
        """
        Build execution trace context for the LLM mutator.
        This is the GEPA pattern — feed actual execution traces into mutations
        so the LLM understands WHY things failed, not just that they failed.
        """
        if not traces:
            return ""

        lines = [
            "\nExecution traces (what this prompt actually produced recently):"
        ]
        for i, trace in enumerate(traces[-3:], 1):
            lines.append(f"\n  Trace {i}:")
            lines.append(trace.summary())

        return "\n".join(lines)

    async def _llm_mutate(
        self,
        weak: dict,
        exemplar: Optional[dict],
        failure_analysis: str,
        trace_context: str,
        llm_caller,
    ) -> Optional[dict]:
        """Generate a mutated prompt variant using the LLM with trace-aware context."""
        exemplar_section = ""
        if exemplar:
            epf = self._ensure_fitness(exemplar["id"])
            exemplar_section = (
                f"\nHere's a high-performing prompt for reference "
                f"(fitness={epf.fitness(prompt_size=len(exemplar['text'])):.2f}, "
                f"engagement={epf.engagement_rate:.1%}):\n"
                f"  Energy: {exemplar['energy']}\n"
                f"  Text: \"{exemplar['text']}\"\n"
            )

        mutation_prompt = (
            f"You are optimizing autonomous wake-cycle prompts for Reina, an AI agent "
            f"that lives on a chat platform. These prompts shape her internal motivation "
            f"when she wakes up on her own — they're never shown to users.\n\n"
            f"A good prompt should lead to behavior that:\n"
            f"- Produces short, casual, natural messages (not monologues)\n"
            f"- Engages people in the room (someone responds)\n"
            f"- Feels authentic — like a person who had a thought, not a bot performing\n"
            f"- Matches its energy level (low=quiet, medium=social, high=active)\n\n"
            f"CONSTRAINTS (your output MUST satisfy all of these):\n"
            f"- Maximum {MAX_PROMPT_SIZE} characters\n"
            f"- Must keep energy level: {weak['energy']}\n"
            f"- Must read as an internal motivation/impulse, not a command\n"
            f"- Shorter prompts are preferred (length penalty applies above "
            f"{int(MAX_PROMPT_SIZE * LENGTH_PENALTY_THRESHOLD)} chars)\n\n"
            f"Here's a prompt that's performing poorly:\n"
            f"  ID: {weak['id']}\n"
            f"  Energy: {weak['energy']}\n"
            f"  Text ({len(weak['text'])} chars): \"{weak['text']}\"\n\n"
            f"Failure analysis:\n{failure_analysis}\n"
            f"{trace_context}\n"
            f"{exemplar_section}\n"
            f"Generate a SINGLE improved variant. Address the specific failures and "
            f"trace patterns above. Keep it under {MAX_PROMPT_SIZE} chars.\n\n"
            f"Respond with ONLY the new prompt text, nothing else. No quotes, no "
            f"explanation, no preamble. Just the prompt text."
        )

        try:
            result = await llm_caller(mutation_prompt)
            if result and len(result.strip()) > 10:
                text = result.strip().strip('"').strip("'")
                return {
                    "id": f"{weak['id']}_v{weak.get('generation', 0) + 1}_{int(time.time()) % 10000}",
                    "energy": weak["energy"],
                    "text": text,
                    "generation": weak.get("generation", 0) + 1,
                    "parent": weak["id"],
                    "mutated": True,
                }
        except Exception as e:
            logger.warning("Evolution: LLM mutation failed — %s", e)

        return None

    @staticmethod
    def _heuristic_mutate(weak: dict, failure_analysis: str) -> Optional[dict]:
        """Simple rule-based mutation when no LLM is available."""
        text = weak["text"]
        mutations = [
            lambda t: t + " If you say something, @mention someone specific.",
            lambda t: t + " Keep it short — one line max.",
            lambda t: t.replace("observe", "check out").replace("notice", "look at"),
            lambda t: t + " React to something specific someone said recently.",
            lambda t: t + " Ask someone a question if the moment is right.",
        ]

        mutator = random.choice(mutations)
        new_text = mutator(text)

        if new_text == text:
            return None

        return {
            "id": f"{weak['id']}_h{int(time.time()) % 10000}",
            "energy": weak["energy"],
            "text": new_text,
            "generation": weak.get("generation", 0) + 1,
            "parent": weak["id"],
            "mutated": True,
        }

    def _inject_variant(self, variant: dict, parent_id: str) -> None:
        """Add a variant to the population, respecting limits."""
        variant_count = sum(
            1 for p in self._population
            if p.get("parent") == parent_id or p["id"] == parent_id
        )

        if variant_count >= MAX_VARIANTS_PER_PROMPT + 1:
            siblings = [
                p for p in self._population
                if p.get("parent") == parent_id and p.get("mutated")
            ]
            if siblings:
                weakest_sibling = min(
                    siblings,
                    key=lambda s: self._get_fitness_score(s["id"]),
                )
                self._population.remove(weakest_sibling)
                self._fitness.pop(weakest_sibling["id"], None)

        self._population.append(variant)
        self._fitness[variant["id"]] = PromptFitness()

        logger.info(
            "Evolution: injected variant %s (parent=%s, gen=%d)",
            variant["id"], parent_id, variant.get("generation", 1),
        )

    def _cull_population(self) -> None:
        """Keep population under MAX_POPULATION_SIZE by removing weakest mutants."""
        if len(self._population) <= MAX_POPULATION_SIZE:
            return

        mutants = [p for p in self._population if p.get("mutated")]
        mutants.sort(key=lambda p: self._get_fitness_score(p["id"]))

        while len(self._population) > MAX_POPULATION_SIZE and mutants:
            worst = mutants.pop(0)
            self._population.remove(worst)
            self._fitness.pop(worst["id"], None)

    # ── Observability / reports ──────────────────────────────────────────

    def get_evolution_report(self) -> str:
        """
        Human-readable report of the evolution state — designed for demos,
        slash commands, and hackathon judges who need to see the engine fast.
        """
        report = self.get_fitness_report()
        total_fires = sum(r["fired"] for r in report)
        total_spoken = sum(r["spoken"] for r in report)
        total_engaged = sum(r["engaged"] for r in report)
        total_suppressed = sum(r["suppressed"] for r in report)
        mutants = [r for r in report if r["mutated"]]
        originals = [r for r in report if not r["mutated"]]

        hours_since_evo = (time.time() - self._last_evolution) / 3600
        next_evo = max(0, EVOLUTION_INTERVAL_HOURS - hours_since_evo)

        lines = [
            "═══ EVOLUTION ENGINE STATUS ═══",
            "",
            f"Population: {len(report)} motives ({len(originals)} original, {len(mutants)} mutated)",
            f"Total cycles: {total_fires}  |  Spoken: {total_spoken}  |  Engaged: {total_engaged}  |  Suppressed: {total_suppressed}",
            f"Next evolution: {next_evo:.1f}h" if self._last_evolution > 0 else "Next evolution: awaiting first cycle",
            "",
        ]

        # Top 5 performers
        qualified = [r for r in report if r["fired"] >= MIN_SAMPLES_FOR_MUTATION]
        if qualified:
            lines.append("── TOP PERFORMERS ──")
            for r in qualified[:5]:
                tag = " [mutant]" if r["mutated"] else ""
                lines.append(
                    f"  {r['id']}{tag}  fitness={r['fitness']:.3f}  "
                    f"eng={r['engagement_rate']:.0%}  "
                    f"fired={r['fired']} spoke={r['spoken']} engaged={r['engaged']}"
                )
                prompt = self._find_prompt(r["id"])
                if prompt:
                    lines.append(f"    \"{prompt['text'][:120]}{'...' if len(prompt['text']) > 120 else ''}\"")
            lines.append("")

            # Bottom 5
            lines.append("── WEAKEST (mutation candidates) ──")
            for r in qualified[-5:]:
                tag = " [mutant]" if r["mutated"] else ""
                lines.append(
                    f"  {r['id']}{tag}  fitness={r['fitness']:.3f}  "
                    f"eng={r['engagement_rate']:.0%}  "
                    f"fired={r['fired']} spoke={r['spoken']} supp={r['suppressed']}"
                )
                prompt = self._find_prompt(r["id"])
                if prompt:
                    lines.append(f"    \"{prompt['text'][:120]}{'...' if len(prompt['text']) > 120 else ''}\"")
            lines.append("")
        else:
            lines.append(f"Not enough data yet — need {MIN_SAMPLES_FOR_MUTATION} fires per motive to rank.")
            lines.append("")

        # Mutation history
        if mutants:
            lines.append("── MUTATIONS ──")
            for m in mutants:
                prompt = self._find_prompt(m["id"])
                gen = prompt.get("generation", "?") if prompt else "?"
                parent = prompt.get("parent", "?") if prompt else "?"
                lines.append(
                    f"  {m['id']}  gen={gen}  parent={parent}  "
                    f"fitness={m['fitness']:.3f}"
                )
                if prompt:
                    lines.append(f"    \"{prompt['text'][:120]}{'...' if len(prompt['text']) > 120 else ''}\"")

                # Show parent text for before/after comparison
                parent_prompt = self._find_prompt(parent) if parent else None
                if parent_prompt:
                    lines.append(f"    ← was: \"{parent_prompt['text'][:120]}{'...' if len(parent_prompt['text']) > 120 else ''}\"")
            lines.append("")

        # Energy distribution
        energy_counts = {"low": 0, "medium": 0, "high": 0}
        for r in report:
            energy_counts[r.get("energy", "medium")] = energy_counts.get(r.get("energy", "medium"), 0) + 1
        lines.append(
            f"Energy distribution: low={energy_counts['low']}  "
            f"medium={energy_counts['medium']}  high={energy_counts['high']}"
        )

        return "\n".join(lines)

    def get_last_cycle_summary(self) -> Optional[str]:
        """
        One-line summary of the most recent execution trace, for injecting
        into the next autonomous wake context (memory-threaded wakes).
        """
        most_recent = None
        most_recent_time = ""

        for pid, trace_deque in self._traces.items():
            if trace_deque:
                last = trace_deque[-1]
                if last.timestamp > most_recent_time:
                    most_recent = last
                    most_recent_time = last.timestamp

        if not most_recent:
            return None

        prompt = self._find_prompt(most_recent.prompt_id)
        prompt_text = prompt["text"][:80] if prompt else most_recent.prompt_id

        parts = [f"Last cycle: \"{prompt_text}\""]
        if most_recent.room:
            parts.append(f"in {most_recent.room}")
        if most_recent.tools_used:
            parts.append(f"used {', '.join(most_recent.tools_used[:4])}")
        if most_recent.was_spoken and most_recent.output_text:
            parts.append(f"said: \"{most_recent.output_text[:100]}\"")
        elif most_recent.was_suppressed:
            parts.append("(output suppressed)")
        else:
            parts.append("(stayed silent)")

        if most_recent.was_engaged:
            parts.append("— someone responded")
        elif most_recent.was_ignored:
            parts.append("— nobody responded")

        return " | ".join(parts)

    # ── Engagement timeout checker ───────────────────────────────────────

    async def check_engagement_timeouts(self) -> None:
        """
        Called periodically to mark prompts as 'ignored' if nobody responded
        within the engagement window after Reina spoke.
        """
        now = time.time()
        expired = []
        for prompt_id, fire_time in list(self._pending_speak_check.items()):
            if now - fire_time > ENGAGEMENT_WINDOW_SECONDS:
                expired.append(prompt_id)

        for prompt_id in expired:
            pf = self._ensure_fitness(prompt_id)
            if pf.spoken > 0 and pf.spoken > (pf.engaged + pf.ignored):
                self.record_ignored(prompt_id)
