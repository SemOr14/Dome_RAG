"""Shared read-only telemetry analysis, also usable from llm_agent without installing the agent."""
import sys
from pathlib import Path

try:
    from kupol.dome.monitoring import NumericMonitor as NumericMonitor, assess_impacts as assess_impacts, leaves as leaves
except ModuleNotFoundError as exc:
    if exc.name != 'kupol':
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'agent/src'))
    from kupol.dome.monitoring import NumericMonitor as NumericMonitor, assess_impacts as assess_impacts, leaves as leaves
