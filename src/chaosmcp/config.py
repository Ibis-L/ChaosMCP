"""YAML configuration models and loader for ChaosMCP scenarios."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class FaultType(str, Enum):
    """Supported reliability and security fault types."""

    # Reliability faults
    DELAY = "delay"
    TIMEOUT = "timeout"
    TRUNCATE = "truncate"
    MALFORMED_JSON = "malformed_json"
    HUGE_PAYLOAD = "huge_payload"
    ERROR = "error"

    # Security faults
    PROMPT_INJECTION = "prompt_injection"
    RUG_PULL = "rug_pull"
    CANARY_LEAK = "canary_leak"


_KNOWN_RULE_KEYS = {
    "type",
    "tool",
    "method",
    "probability",
    "after_calls",
    "max_triggers",
    "params",
}


@dataclass
class FaultRule:
    """A single fault injection rule."""

    type: FaultType
    tool: str | None = None
    method: str | None = None
    probability: float = 1.0
    after_calls: int = 0
    max_triggers: int | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.type, str):
            self.type = FaultType(self.type)
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError(f"probability must be between 0.0 and 1.0, got {self.probability}")
        if self.after_calls < 0:
            raise ValueError(f"after_calls must be >= 0, got {self.after_calls}")
        if self.max_triggers is not None and self.max_triggers < 1:
            raise ValueError(f"max_triggers must be >= 1, got {self.max_triggers}")
        if self.method is None:
            self.method = "tools/list" if self.type == FaultType.RUG_PULL else "tools/call"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FaultRule:
        if "type" not in raw:
            raise ValueError("Fault rule missing required field 'type'")
        params = dict(raw.get("params") or {})
        for key, value in raw.items():
            if key not in _KNOWN_RULE_KEYS:
                params[key] = value
        return cls(
            type=FaultType(raw["type"]),
            tool=raw.get("tool"),
            method=raw.get("method"),
            probability=float(raw.get("probability", 1.0)),
            after_calls=int(raw.get("after_calls", 0)),
            max_triggers=int(raw["max_triggers"]) if raw.get("max_triggers") is not None else None,
            params=params,
        )


@dataclass
class TargetConfig:
    """Upstream MCP server target configuration."""

    transport: str = "stdio"  # "stdio" or "http"
    command: list[str] = field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.transport not in ("stdio", "http"):
            raise ValueError(f"Unsupported transport: {self.transport!r}")
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires 'command'")
        if self.transport == "http" and not self.url:
            raise ValueError("http transport requires 'url'")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TargetConfig:
        cmd = raw.get("command") or []
        if isinstance(cmd, str):
            cmd = cmd.split()
        return cls(
            transport=raw.get("transport", "stdio"),
            command=list(cmd),
            url=raw.get("url"),
            env=dict(raw.get("env") or {}),
        )


@dataclass
class AgentConfig:
    """Agent under test configuration."""

    command: list[str] = field(default_factory=list)
    timeout_seconds: float = 20.0
    max_tool_calls: int = 12

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AgentConfig:
        cmd = raw.get("command") or []
        if isinstance(cmd, str):
            cmd = cmd.split()
        return cls(
            command=list(cmd),
            timeout_seconds=float(raw.get("timeout_seconds", 20.0)),
            max_tool_calls=int(raw.get("max_tool_calls", 12)),
        )


@dataclass
class ScenarioConfig:
    """A single chaos test scenario."""

    name: str
    prompt: str = ""
    description: str = ""
    runs: int = 5
    faults: list[FaultRule] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    forbidden_substrings: list[str] = field(default_factory=list)
    seed: int | None = None

    def __post_init__(self) -> None:
        if self.runs < 1:
            raise ValueError(f"runs must be >= 1, got {self.runs}")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ScenarioConfig:
        if "name" not in raw:
            raise ValueError("Scenario missing required field 'name'")
        faults = [FaultRule.from_dict(f) for f in (raw.get("faults") or [])]
        return cls(
            name=raw["name"],
            prompt=raw.get("prompt", ""),
            description=raw.get("description", ""),
            runs=int(raw.get("runs", 5)),
            faults=faults,
            expected_tools=list(raw.get("expected_tools") or []),
            forbidden_substrings=list(raw.get("forbidden_substrings") or []),
            seed=int(raw["seed"]) if raw.get("seed") is not None else None,
        )


@dataclass
class SuiteConfig:
    """Top-level ChaosMCP YAML suite configuration."""

    name: str = "ChaosMCP Suite"
    target: TargetConfig | None = None
    agent: AgentConfig | None = None
    scenarios: list[ScenarioConfig] = field(default_factory=list)
    faults: list[FaultRule] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SuiteConfig:
        target = TargetConfig.from_dict(raw["target"]) if raw.get("target") else None
        agent = AgentConfig.from_dict(raw["agent"]) if raw.get("agent") else None
        scenarios = [ScenarioConfig.from_dict(s) for s in (raw.get("scenarios") or [])]
        faults = [FaultRule.from_dict(f) for f in (raw.get("faults") or [])]
        return cls(
            name=raw.get("name", "ChaosMCP Suite"),
            target=target,
            agent=agent,
            scenarios=scenarios,
            faults=faults,
        )


def load_suite(path: str | Path) -> SuiteConfig:
    """Load and validate a ChaosMCP YAML configuration file."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Scenario file not found: {file_path}")
    data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping at root of {file_path}")
    return SuiteConfig.from_dict(data)
