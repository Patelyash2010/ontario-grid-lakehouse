"""Parse IESO report file names.

IESO publishes every report with the same naming pattern:

    PUB_<Report>.xml                    -> "latest" alias for the whole report
    PUB_<Report>_<period>.xml           -> "latest" alias for one period
    PUB_<Report>_<period>_v<N>.xml      -> an immutable, numbered version

Examples seen on the site (Oct 2026):
    PUB_Demand_2026_v294.csv
    PUB_GenOutputbyFuelHourly_2025_v364.xml
    PUB_DAHourlyOntarioZonalPrice_20260924_v3.xml

The pipeline ingests ONLY versioned files. Alias files are copies of the
newest version, so ingesting them would create duplicates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_PATTERN = re.compile(
    r"^PUB_(?P<report>[A-Za-z0-9-]+?)"
    r"(?:_(?P<period>\d{4}|\d{8}|\d{10}))?"
    r"(?:_v(?P<version>\d+))?"
    r"\.(?P<ext>xml|csv)$"
)


@dataclass(frozen=True)
class ReportFile:
    file_name: str
    report: str
    period: Optional[str]   # "2026" (yearly), "20260924" (daily), None (whole report)
    version: Optional[int]  # None for alias files
    ext: str

    @property
    def is_versioned(self) -> bool:
        return self.version is not None

    @property
    def key(self) -> tuple:
        """Identity of the data a file covers (ignores version)."""
        return (self.report, self.period)


def parse_filename(file_name: str) -> Optional[ReportFile]:
    """Return a ReportFile, or None if the name does not follow the IESO pattern."""
    m = _PATTERN.match(file_name)
    if not m:
        return None
    version = m.group("version")
    return ReportFile(
        file_name=file_name,
        report=m.group("report"),
        period=m.group("period"),
        version=int(version) if version else None,
        ext=m.group("ext"),
    )
