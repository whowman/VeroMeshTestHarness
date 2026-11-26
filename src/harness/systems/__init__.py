"""The simulated source systems. Importing this package registers every system in it
(base.REGISTRY); a new system is a new module here with a SourceSystem subclass."""

from . import (  # noqa: F401  (registers them)
    databricks,
    dbt,
    emr,
    entra,
    erp,
    governance,
    halo,
    leanix,
    servicenow,
    sharepoint,
    stardog,
    workday,
)
from .base import REGISTRY, SourceSystem

__all__ = ["REGISTRY", "SourceSystem"]
