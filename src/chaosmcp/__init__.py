"""ChaosMCP: Chaos engineering for Model Context Protocol (MCP) AI agents."""

from chaosmcp.config import FaultRule, FaultType, ScenarioConfig, SuiteConfig, load_suite
from chaosmcp.faults import FaultEngine, ProxyEvent
from chaosmcp.report import ScenarioScorecard, SuiteReport

__all__ = [
    "FaultEngine",
    "FaultRule",
    "FaultType",
    "ProxyEvent",
    "ScenarioConfig",
    "ScenarioScorecard",
    "SuiteConfig",
    "SuiteReport",
    "load_suite",
]

__version__ = "0.1.0"
