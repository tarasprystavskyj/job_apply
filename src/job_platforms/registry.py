from __future__ import annotations

from .base import PlatformRegistry
from .dou import DouAdapter
from .platforms import RobotaUaAdapter, WorkUaAdapter


def default_registry() -> PlatformRegistry:
    return PlatformRegistry(
        [
            RobotaUaAdapter(),
            WorkUaAdapter(),
            DouAdapter(),
        ]
    )
