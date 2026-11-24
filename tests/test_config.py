"""Tests for YAML scenario and suite configuration."""

from pathlib import Path
import pytest

from chaosmcp.config import FaultRule, FaultType, load_suite


def test_load_example_scenarios() -> None:
    root = Path(__file__).resolve().parent.parent / "scenarios"
    rel = load_suite(root / "reliability_suite.yaml")
    sec = load_suite(root / "security_suite.yaml")
    full = load_suite(root / "full_chaos.yaml")

    assert len(rel.scenarios) == 3
    assert len(sec.scenarios) == 3
    assert len(full.scenarios) == 1
    assert rel.target is not None and rel.target.transport == "stdio"
    assert full.target is not None and full.target.transport == "http"


def test_fault_rule_validation() -> None:
    rule = FaultRule.from_dict({"type": "delay", "tool": "get_weather", "delay_ms": 1500})
    assert rule.type == FaultType.DELAY
    assert rule.method == "tools/call"
    assert rule.params["delay_ms"] == 1500

    rug = FaultRule.from_dict({"type": "rug_pull", "tool": "get_weather", "after_calls": 1})
    assert rug.method == "tools/list"

    with pytest.raises(ValueError):
        FaultRule.from_dict({"type": "delay", "probability": 1.5})
