#!/usr/bin/env python3
# Ticket: 0085_traceability_workflow_consolidation
"""
Agent Supervisor Daemon

Watches the workflow engine work queue and spawns Claude Code sessions
for available phases. Reads agent_registry from config to determine spawn
commands and max_concurrent limits per agent type.

Usage (via CLI):
    workflow-engine supervisor --poll-interval 30
    workflow-engine supervisor --dry-run
"""

import asyncio
import json
import logging
import signal
import uuid
from dataclasses import dataclass, field
from typing import Any

from workflow_engine.engine.claim import ClaimSuccess, claim_next
from workflow_engine.engine.config import load_workflow_config
from workflow_engine.engine.models import Phase, PhaseStatus
from workflow_engine.engine.schema import create_db
from workflow_engine.engine import scheduler as sched

logger = logging.getLogger("workflow_engine.supervisor")


# ---------------------------------------------------------------------------
# RunningAgent tracker
# ---------------------------------------------------------------------------


@dataclass
class RunningAgent:
    """Tracks a spawned agent subprocess and its associated async tasks."""
    agent_id: str
    agent_type: str
    phase_id: int
    ticket_id: str
    phase_name: str
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_prompt(
    db_path: str,
    phase: Phase,
    agent_type: str,
    agent_id: str,
) -> str:
    """Assemble the structured prompt piped to the agent via stdin."""
    conn = create_db(db_path)
    try:
        # Gather prior completed phases for this ticket
        prior_phases = conn.execute(
            """
            SELECT phase_name, result_summary, artifacts
            FROM phases
            WHERE ticket_id = ? AND status = 'completed'
            ORDER BY phase_order ASC, id ASC
            """,
            (phase.ticket_id,),
        ).fetchall()

        prior_section = ""
        if prior_phases:
            lines = []
            for pp in prior_phases:
                lines.append(f"### {pp['phase_name']}")
                if pp["result_summary"]:
                    lines.append(f"**Result:** {pp['result_summary']}")
                if pp["artifacts"]:
                    try:
                        arts = json.loads(pp["artifacts"])
                        lines.append(f"**Artifacts:** {', '.join(arts)}")
                    except (json.JSONDecodeError, TypeError):
                        pass
                lines.append("")
            prior_section = "\n".join(lines)
        else:
            prior_section = "(No prior phases completed yet.)"

        return f"""\
You are a {agent_type} agent executing a workflow phase.

## Your Assignment
- **Ticket**: {phase.ticket_id}
- **Phase**: {phase.phase_name}
- **Agent ID**: {agent_id}
- **Phase ID**: {phase.id}

## Prior Phase Results
{prior_section}

## Instructions
1. The phase is already claimed and started — do NOT call claim_phase or start_phase.
2. Perform the work for the "{phase.phase_name}" phase.
3. When done, call `complete_phase` with agent_id="{agent_id}", phase_id={phase.id}, result_summary, and artifacts.
4. If you cannot complete, call `fail_phase` with agent_id="{agent_id}", phase_id={phase.id}, and error_details.
5. Call `heartbeat` periodically during long operations.
"""
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """
    Main supervisor loop. Polls the work queue and spawns Claude Code
    sessions for available phases, respecting max_concurrent limits.
    """

    def __init__(
        self,
        db_path: str,
        config: Any,
        poll_interval: float = 30.0,
        dry_run: bool = False,
    ):
        self.db_path = db_path
        self.config = config
        self.poll_interval = poll_interval
        self.dry_run = dry_run
        self._stop_event = asyncio.Event()
        self._running: dict[str, list[RunningAgent]] = {}

    async def run(self) -> None:
        """Main loop with signal handling."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop_event.set)

        logger.info(
            "Supervisor started (poll_interval=%.0fs, dry_run=%s, agent_types=%d)",
            self.poll_interval,
            self.dry_run,
            len(self.config.agent_registry),
        )

        try:
            while not self._stop_event.is_set():
                try:
                    await self._poll_once()
                except Exception:
                    logger.exception("Error during poll cycle")

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.poll_interval,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._drain()

    async def _poll_once(self) -> None:
        """Iterate all agent types, reap finished tasks, spawn new workers."""
        registry = self.config.agent_registry
        if not registry:
            logger.debug("No agent types in registry, nothing to poll.")
            return

        for agent_type, agent_config in registry.items():
            # Reap finished tasks
            self._reap_finished(agent_type)

            max_concurrent = agent_config.get("max_concurrent", 1)
            running = self._running.get(agent_type, [])
            slots_free = max_concurrent - len(running)

            if slots_free <= 0:
                continue

            for _ in range(slots_free):
                spawned = await self._try_spawn(agent_type, agent_config)
                if not spawned:
                    break  # No more available work for this type

    def _reap_finished(self, agent_type: str) -> None:
        """Remove completed tasks from the running list."""
        running = self._running.get(agent_type, [])
        still_running = []
        for ra in running:
            if ra.task is not None and ra.task.done():
                # Cancel heartbeat
                if ra.heartbeat_task and not ra.heartbeat_task.done():
                    ra.heartbeat_task.cancel()
                # Collect any exception to avoid "exception was never retrieved"
                try:
                    ra.task.result()
                except Exception:
                    logger.exception(
                        "Agent task for %s (phase %d) raised an exception",
                        ra.agent_id, ra.phase_id,
                    )
            else:
                still_running.append(ra)
        self._running[agent_type] = still_running

    async def _try_spawn(
        self,
        agent_type: str,
        agent_config: dict,
    ) -> bool:
        """
        Register agent, claim phase, start phase, build prompt, launch subprocess.
        Returns True if a worker was spawned, False if no work available.
        """
        agent_id = f"supervisor-{agent_type}-{uuid.uuid4().hex[:8]}"

        conn = create_db(self.db_path)
        try:
            # Register agent
            sched.register_agent(conn, agent_id, agent_type, metadata={"source": "supervisor"})

            # Claim next available phase
            result = claim_next(conn, agent_id, agent_type)
            if not isinstance(result, ClaimSuccess):
                # No work available — terminate the agent registration
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE agents SET status = 'terminated' WHERE id = ?",
                    (agent_id,),
                )
                conn.execute("COMMIT")
                return False

            phase = result.phase

            # Start the phase (claimed → running)
            sched.start_phase(conn, agent_id, phase.id)

        finally:
            conn.close()

        logger.info(
            "Claimed phase %d (%s) for ticket %s — agent %s",
            phase.id, phase.phase_name, phase.ticket_id, agent_id,
        )

        if self.dry_run:
            logger.info(
                "[DRY RUN] Would spawn: %s %s | stdin prompt for phase %d",
                agent_config.get("command", "claude"),
                " ".join(agent_config.get("args", [])),
                phase.id,
            )
            # Release the phase since we won't actually run it
            conn = create_db(self.db_path)
            try:
                from workflow_engine.engine.claim import release_phase
                release_phase(conn, agent_id, phase.id, actor="supervisor-dry-run")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE agents SET status = 'terminated' WHERE id = ?",
                    (agent_id,),
                )
                conn.execute("COMMIT")
            finally:
                conn.close()
            return True

        # Build prompt
        prompt = _build_prompt(self.db_path, phase, agent_type, agent_id)

        # Create RunningAgent and launch
        ra = RunningAgent(
            agent_id=agent_id,
            agent_type=agent_type,
            phase_id=phase.id,
            ticket_id=phase.ticket_id,
            phase_name=phase.phase_name,
        )

        ra.task = asyncio.create_task(
            self._run_agent_process(ra, agent_config, prompt)
        )
        ra.heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(agent_id)
        )

        self._running.setdefault(agent_type, []).append(ra)
        return True

    async def _run_agent_process(
        self,
        ra: RunningAgent,
        agent_config: dict,
        prompt: str,
    ) -> None:
        """Spawn Claude Code subprocess, pipe prompt via stdin, handle completion."""
        command = agent_config.get("command", "claude")
        args = agent_config.get("args", [])
        cmd = [command, "--print"] + args

        logger.info(
            "Spawning: %s (agent=%s, phase=%d, ticket=%s)",
            " ".join(cmd), ra.agent_id, ra.phase_id, ra.ticket_id,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            ra.process = proc

            stdout, stderr = await proc.communicate(input=prompt.encode("utf-8"))

            exit_code = proc.returncode
            logger.info(
                "Agent %s exited with code %d (phase %d, ticket %s)",
                ra.agent_id, exit_code, ra.phase_id, ra.ticket_id,
            )

            if stdout:
                logger.debug("Agent %s stdout: %s", ra.agent_id, stdout.decode("utf-8", errors="replace")[:2000])
            if stderr:
                logger.warning("Agent %s stderr: %s", ra.agent_id, stderr.decode("utf-8", errors="replace")[:2000])

        except Exception as exc:
            logger.exception("Failed to spawn/run agent %s", ra.agent_id)
            exit_code = -1
            stdout = b""
            stderr = str(exc).encode("utf-8")

        # Check if agent already self-reported completion via MCP
        await self._handle_process_exit(ra, exit_code, stdout, stderr)

    async def _handle_process_exit(
        self,
        ra: RunningAgent,
        exit_code: int,
        stdout: bytes,
        stderr: bytes,
    ) -> None:
        """After process exits, check phase status and handle if agent didn't self-report."""
        conn = create_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT status FROM phases WHERE id = ?",
                (ra.phase_id,),
            ).fetchone()

            if row is None:
                logger.error("Phase %d not found after agent exit", ra.phase_id)
                return

            current_status = row["status"]

            if current_status in PhaseStatus.TERMINAL:
                logger.info(
                    "Phase %d already in terminal state '%s' — agent self-reported",
                    ra.phase_id, current_status,
                )
            elif exit_code == 0:
                # Agent exited cleanly but didn't call complete_phase — do it for them
                logger.warning(
                    "Agent %s exited 0 but phase %d still '%s' — marking completed by supervisor",
                    ra.agent_id, ra.phase_id, current_status,
                )
                sched.complete_phase(
                    conn, ra.agent_id, ra.phase_id,
                    result_summary="Completed by supervisor (agent did not self-report)",
                )
            else:
                # Non-zero exit — fail the phase
                error_msg = stderr.decode("utf-8", errors="replace")[:1000] if stderr else f"Exit code {exit_code}"
                logger.warning(
                    "Agent %s exited %d — failing phase %d",
                    ra.agent_id, exit_code, ra.phase_id,
                )
                sched.fail_phase(
                    conn, ra.agent_id, ra.phase_id,
                    error_details=f"Agent exited with code {exit_code}: {error_msg}",
                )

            # Resolve downstream availability after completion/failure
            transitions = sched.resolve_availability(conn, ticket_id=ra.ticket_id)
            if transitions:
                logger.info(
                    "Resolved %d downstream phase transition(s) for ticket %s",
                    len(transitions), ra.ticket_id,
                )

            # Terminate the agent registration
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE agents SET status = 'terminated', current_phase_id = NULL WHERE id = ?",
                (ra.agent_id,),
            )
            conn.execute("COMMIT")

        finally:
            conn.close()

    async def _heartbeat_loop(self, agent_id: str) -> None:
        """Send heartbeats every 5 minutes while agent is running."""
        try:
            while True:
                await asyncio.sleep(300)  # 5 minutes
                conn = create_db(self.db_path)
                try:
                    alive = sched.heartbeat(conn, agent_id)
                    if not alive:
                        logger.warning("Heartbeat failed for agent %s — agent may be stale", agent_id)
                        return
                    logger.debug("Heartbeat sent for agent %s", agent_id)
                finally:
                    conn.close()
        except asyncio.CancelledError:
            pass

    async def _drain(self) -> None:
        """Wait for all running agent tasks to complete before shutdown."""
        all_tasks = []
        for agent_type, agents in self._running.items():
            for ra in agents:
                if ra.task and not ra.task.done():
                    all_tasks.append(ra.task)
                if ra.heartbeat_task and not ra.heartbeat_task.done():
                    ra.heartbeat_task.cancel()

        if all_tasks:
            logger.info("Draining %d running agent(s)...", len(all_tasks))
            await asyncio.gather(*all_tasks, return_exceptions=True)

        logger.info("Supervisor stopped.")
