"""Platform expansion primitives for safe job application workflows."""

from .base import JobPlatformAdapter, PlatformRegistry, SafetyGate, UnsupportedAction
from .models import (
    ApprovalGate,
    DiscoveryQuery,
    OutreachDraft,
    PlatformCapabilities,
    ProgressEdge,
    ProgressNode,
    ProgressSnapshot,
    ResumeArtifact,
    VacancyObservation,
)
from .registry import default_registry

__all__ = [
    "ApprovalGate",
    "DiscoveryQuery",
    "JobPlatformAdapter",
    "OutreachDraft",
    "PlatformCapabilities",
    "PlatformRegistry",
    "ProgressEdge",
    "ProgressNode",
    "ProgressSnapshot",
    "ResumeArtifact",
    "SafetyGate",
    "UnsupportedAction",
    "VacancyObservation",
    "default_registry",
]
