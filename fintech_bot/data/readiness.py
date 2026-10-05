"""Typed cache-readiness states and safe public errors for market data."""

from dataclasses import dataclass
from enum import Enum


class ReadinessState(str, Enum):
    READY = "READY"
    MISSING = "MISSING"
    FETCHING = "FETCHING"
    STALE = "STALE"
    INVALID = "INVALID"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ComponentHealth:
    state: ReadinessState
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.state is ReadinessState.READY


class DataLayerError(ValueError):
    """Base error whose text is safe to expose without local filesystem details."""


class DataNotReady(DataLayerError):
    pass


class DataUnavailable(DataLayerError):
    pass


class InvalidData(DataLayerError):
    pass


class StaleData(DataLayerError):
    pass


def public_data_error(error: Exception) -> str:
    """Return a stable user-facing error and never expose an OS path."""
    if isinstance(error, DataLayerError):
        return str(error)
    if error.__class__.__name__ == "DataValidationError":
        return str(error)
    if isinstance(error, (FileNotFoundError, OSError)):
        return "Dữ liệu local chưa sẵn sàng."
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return "Dữ liệu nhận được chưa hợp lệ."
    return "Dữ liệu chưa sẵn sàng."
