"""The simulated source systems. Importing this package registers every system in it
(base.REGISTRY); a new system is a new module here with a SourceSystem subclass."""

from . import entra, servicenow  # noqa: F401  (registers them)
from .base import REGISTRY, SourceSystem

__all__ = ["REGISTRY", "SourceSystem"]
