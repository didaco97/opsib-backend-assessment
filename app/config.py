"""Runtime configuration.

All scenario logic uses the frozen evaluation time, never the machine clock.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")

MAPPING_VERSION = "mapping/v1"
RULE_VERSION = "receipt-shortfall/v1"
ROUTING_POLICY_VERSION = "routing/v1"
TENANT_ID = "opsib-pilot"


@dataclass(frozen=True)
class Settings:
    database_url: str
    fixtures_dir: str
    eval_time: datetime          # frozen evaluation time (timezone-aware)
    availability_freshness_minutes: int = 60
    task_due_hours: int = 24
    shortfall_tolerance: float = 0.0

    @staticmethod
    def from_env() -> "Settings":
        raw_eval = os.environ.get("EVAL_TIME", "2026-10-16T15:00:00+05:30")
        eval_time = datetime.fromisoformat(raw_eval)
        if eval_time.tzinfo is None:
            raise ValueError("EVAL_TIME must carry an explicit UTC offset")
        return Settings(
            database_url=os.environ.get("DATABASE_URL", "sqlite:///./opsib.db"),
            fixtures_dir=os.environ.get("FIXTURES_DIR", "fixtures"),
            eval_time=eval_time,
            availability_freshness_minutes=int(os.environ.get("AVAILABILITY_FRESHNESS_MINUTES", "60")),
            task_due_hours=int(os.environ.get("TASK_DUE_HOURS", "24")),
            shortfall_tolerance=float(os.environ.get("SHORTFALL_TOLERANCE", "0")),
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings() -> None:
    """Test hook: force re-read of environment."""
    global _settings
    _settings = None
