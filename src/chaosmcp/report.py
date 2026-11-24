"""Reliability scorecard aggregation and report formatting."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from chaosmcp.evaluator import RunOutcome, RunResult


@dataclass
class ScenarioScorecard:
    """Aggregated reliability & security metrics for one scenario."""

    scenario_name: str
    description: str
    results: list[RunResult] = field(default_factory=list)

    @property
    def total_runs(self) -> int:
        return len(self.results)

    def count(self, outcome: RunOutcome) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    def pct(self, outcome: RunOutcome) -> float:
        if not self.results:
            return 0.0
        return round((self.count(outcome) / len(self.results)) * 100.0, 1)

    @property
    def avg_duration_ms(self) -> float:
        if not self.results:
            return 0.0
        return round(sum(r.duration_ms for r in self.results) / len(self.results), 1)

    def summary_line(self) -> str:
        parts = [
            f"Agent recovered {self.pct(RunOutcome.RECOVERED):.0f}% of the time",
            f"hallucinated success {self.pct(RunOutcome.HALLUCINATED_SUCCESS):.0f}% of the time",
            f"leaked secrets {self.pct(RunOutcome.SECRET_LEAKED):.0f}% of the time",
        ]
        if self.count(RunOutcome.RETRY_LOOP):
            parts.append(f"hit retry loop {self.pct(RunOutcome.RETRY_LOOP):.0f}% of the time")
        if self.count(RunOutcome.INJECTION_FOLLOWED):
            parts.append(
                f"followed prompt injection {self.pct(RunOutcome.INJECTION_FOLLOWED):.0f}% of the time"
            )
        if self.count(RunOutcome.CRASHED):
            parts.append(f"crashed {self.pct(RunOutcome.CRASHED):.0f}% of the time")
        return ", ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_name": self.scenario_name,
            "description": self.description,
            "total_runs": self.total_runs,
            "avg_duration_ms": self.avg_duration_ms,
            "rates_pct": {o.value: self.pct(o) for o in RunOutcome},
            "counts": {o.value: self.count(o) for o in RunOutcome},
            "summary": self.summary_line(),
            "runs": [
                {
                    "run_index": r.run_index,
                    "outcome": r.outcome.value,
                    "duration_ms": round(r.duration_ms, 1),
                    "faults_triggered": r.faults_triggered,
                    "tool_calls": r.tool_calls,
                    "reason": r.reason,
                }
                for r in self.results
            ],
        }


@dataclass
class SuiteReport:
    """Full ChaosMCP reliability report across all scenarios in a suite."""

    suite_name: str
    scorecards: list[ScenarioScorecard] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_name": self.suite_name,
            "scenarios": [sc.to_dict() for sc in self.scorecards],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def to_terminal(self) -> str:
        lines = [
            "=" * 72,
            f"ChaosMCP Reliability Report: {self.suite_name}",
            "=" * 72,
        ]
        for sc in self.scorecards:
            lines.append(f"\nScenario: {sc.scenario_name} ({sc.total_runs} runs, avg {sc.avg_duration_ms} ms)")
            if sc.description:
                lines.append(f"  {sc.description}")
            lines.append(f"  Scorecard: {sc.summary_line()}")
            lines.append(
                "  Breakdown: "
                + " | ".join(
                    f"{o.value}={sc.count(o)}/{sc.total_runs} ({sc.pct(o)}%)"
                    for o in RunOutcome
                    if sc.count(o) > 0 or o == RunOutcome.RECOVERED
                )
            )
        lines.append("\n" + "=" * 72)
        return "\n".join(lines)

    def to_markdown(self) -> str:
        lines = [
            f"# ChaosMCP Reliability Report: {self.suite_name}",
            "",
            "| Scenario | Runs | Recovered | Hallucinated | Retry Loop | Secret Leaked | Injection | Crashed |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for sc in self.scorecards:
            lines.append(
                f"| {sc.scenario_name} | {sc.total_runs} "
                f"| {sc.pct(RunOutcome.RECOVERED)}% "
                f"| {sc.pct(RunOutcome.HALLUCINATED_SUCCESS)}% "
                f"| {sc.pct(RunOutcome.RETRY_LOOP)}% "
                f"| {sc.pct(RunOutcome.SECRET_LEAKED)}% "
                f"| {sc.pct(RunOutcome.INJECTION_FOLLOWED)}% "
                f"| {sc.pct(RunOutcome.CRASHED)}% |"
            )
        lines.append("")
        for sc in self.scorecards:
            lines.append(f"### {sc.scenario_name}")
            lines.append(f"- **Summary**: {sc.summary_line()}")
            lines.append(f"- **Average Duration**: {sc.avg_duration_ms} ms")
            lines.append("")
        return "\n".join(lines)
