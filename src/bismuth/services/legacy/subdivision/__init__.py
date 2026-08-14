"""Structured local-growth harness retained at its historical module path.

The hybrid ingest path uses this implementation through ``services.subdivision``. The
package name records its origin; it no longer means the code is outside production.
"""

from bismuth.services.legacy.subdivision.models import Divided
from bismuth.services.legacy.subdivision.service import LibraryMaintenanceService

SubdivisionService = LibraryMaintenanceService

__all__ = ["Divided", "LibraryMaintenanceService", "SubdivisionService"]
