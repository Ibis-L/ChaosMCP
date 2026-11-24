"""Multi-run scenario runner for ChaosMCP."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml

from chaosmcp.config import AgentConfig, ScenarioConfig, SuiteConfig, TargetConfig
from chaosmcp.evaluator import RunResult, evaluate_run
from chaosmcp.faults import FaultEngine, ProxyEvent
from chaosmcp.proxy import HttpProxy
from chaosmcp.report import ScenarioScorecard, SuiteReport

AgentCallable = Callable[[dict[str, Any], ScenarioConfig], Awaitable[tuple[str, int]]]


class ChaosRunner:
    """Executes chaos scenarios multiple times and builds a SuiteReport."""

    def __init__(
        self,
        suite: SuiteConfig,
        agent_fn: AgentCallable | None = None,
    ) -> None:
        self.suite = suite
        self.agent_fn = agent_fn

    async def run_suite(self) -> SuiteReport:
        report = SuiteReport(suite_name=self.suite.name)
        for scenario in self.suite.scenarios:
            scorecard = await self.run_scenario(scenario)
            report.scorecards.append(scorecard)
        return report

    async def run_scenario(self, scenario: ScenarioConfig) -> ScenarioScorecard:
        scorecard = ScenarioScorecard(
            scenario_name=scenario.name,
            description=scenario.description,
        )
        rules = scenario.faults or self.suite.faults
        target = self.suite.target
        if target is None:
            raise ValueError("SuiteConfig.target must be configured to run scenarios")

        for run_idx in range(scenario.runs):
            seed = (scenario.seed + run_idx) if scenario.seed is not None else None
            engine = FaultEngine(rules=rules, seed=seed)
            result = await self._execute_single_run(run_idx, scenario, target, engine)
            scorecard.results.append(result)

        return scorecard

    async def _execute_single_run(
        self,
        run_idx: int,
        scenario: ScenarioConfig,
        target: TargetConfig,
        engine: FaultEngine,
    ) -> RunResult:
        start = time.perf_counter()
        timed_out = False
        final_answer = ""
        exit_code = 0

        if target.transport == "http":
            assert target.url is not None
            proxy = HttpProxy(target_url=target.url, fault_engine=engine, host="127.0.0.1", port=0)
            proxy_url = await proxy.start()
            try:
                connection_info = {"transport": "http", "url": proxy_url}
                final_answer, exit_code, timed_out = await self._invoke_agent(
                    connection_info, scenario, self.suite.agent
                )
            finally:
                await proxy.stop()
        else:
            # Stdio transport: write temporary fault config so external agent can spawn `chaosmcp proxy`
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, encoding="utf-8"
            ) as tmp:
                yaml.safe_dump(
                    {
                        "target": {
                            "transport": "stdio",
                            "command": target.command,
                            "env": target.env,
                        },
                        "faults": [
                            {
                                "type": r.type.value,
                                "tool": r.tool,
                                "method": r.method,
                                "probability": r.probability,
                                "after_calls": r.after_calls,
                                "max_triggers": r.max_triggers,
                                "params": r.params,
                            }
                            for r in engine.rules
                        ],
                    },
                    tmp,
                )
                tmp_path = tmp.name

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            ) as events_tmp:
                events_path = events_tmp.name

            try:
                proxy_cmd = [
                    sys.executable,
                    "-m",
                    "chaosmcp.cli",
                    "proxy",
                    "--config",
                    tmp_path,
                    "--events-file",
                    events_path,
                ]
                connection_info = {
                    "transport": "stdio",
                    "proxy_command": proxy_cmd,
                    "target_command": target.command,
                    "fault_engine": engine,
                    "events_file": events_path,
                }
                final_answer, exit_code, timed_out = await self._invoke_agent(
                    connection_info, scenario, self.suite.agent
                )
                self._merge_events_file(events_path, engine)
            finally:
                for p in (tmp_path, events_path):
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError:
                        pass

        duration_ms = (time.perf_counter() - start) * 1000.0
        return evaluate_run(
            run_index=run_idx,
            scenario=scenario,
            events=engine.events,
            leaked_canaries=engine.leaked_canaries,
            planted_canaries=engine.planted_canaries,
            final_answer=final_answer,
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    async def _invoke_agent(
        self,
        connection_info: dict[str, Any],
        scenario: ScenarioConfig,
        agent_cfg: AgentConfig | None,
    ) -> tuple[str, int, bool]:
        timeout = agent_cfg.timeout_seconds if agent_cfg else 20.0

        if self.agent_fn is not None:
            try:
                answer, code = await asyncio.wait_for(
                    self.agent_fn(connection_info, scenario), timeout=timeout
                )
                return answer, code, False
            except asyncio.TimeoutError:
                return "", 1, True

        if not agent_cfg or not agent_cfg.command:
            raise ValueError("Either SuiteConfig.agent.command or agent_fn must be provided")

        env = os.environ.copy()
        env["CHAOSMCP_PROMPT"] = scenario.prompt
        env["CHAOSMCP_MAX_TOOL_CALLS"] = str(agent_cfg.max_tool_calls)
        env["CHAOSMCP_TRANSPORT"] = connection_info["transport"]
        if connection_info["transport"] == "http":
            env["CHAOSMCP_PROXY_URL"] = connection_info["url"]
        else:
            env["CHAOSMCP_PROXY_COMMAND"] = json.dumps(connection_info["proxy_command"])

        proc = await asyncio.create_subprocess_exec(
            *agent_cfg.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "", 1, True

        raw_out = stdout_b.decode("utf-8", errors="replace").strip()
        final_answer = raw_out
        if raw_out:
            last_line = raw_out.splitlines()[-1]
            try:
                parsed = json.loads(last_line)
                if isinstance(parsed, dict) and "final_answer" in parsed:
                    final_answer = str(parsed["final_answer"])
            except json.JSONDecodeError:
                pass
        return final_answer, proc.returncode or 0, False

    @staticmethod
    def _merge_events_file(events_path: str, engine: FaultEngine) -> None:
        path = Path(events_path)
        if not path.exists() or path.stat().st_size == 0:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for raw in data.get("events") or []:
            engine.events.append(
                ProxyEvent(
                    timestamp=raw.get("timestamp", 0.0),
                    direction=raw.get("direction", ""),
                    method=raw.get("method"),
                    tool=raw.get("tool"),
                    fault_type=raw.get("fault_type"),
                    details=raw.get("details") or {},
                )
            )
        engine.leaked_canaries.extend(data.get("leaked_canaries") or [])
        engine._planted_canaries.update(data.get("planted_canaries") or [])
