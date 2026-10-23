"""File manifest logic: decide which files to download and which version wins.

This is the "watermark" of a file-based pipeline. Instead of remembering the
last timestamp, we remember every (file name) already landed in bronze, and
each run downloads only names we have never seen.

Rules:
  * Skip alias files (no _v<N>) - they duplicate a versioned file.
  * Download every versioned file not yet in the manifest (bronze keeps all
    versions for audit and replay).
  * In silver, for each (report, period) keep only the HIGHEST version.
"""

from __future__ import annotations

from typing import Iterable

from .filenames import ReportFile, parse_filename


def select_new_files(listing: Iterable[str], already_processed: Iterable[str]) -> list[ReportFile]:
    """Return versioned files from a directory listing that are not yet processed,
    oldest version first so they land in order."""
    seen = set(already_processed)
    new = []
    for name in listing:
        rf = parse_filename(name)
        if rf is None or not rf.is_versioned or name in seen:
            continue
        new.append(rf)
    return sorted(new, key=lambda f: (f.report, f.period or "", f.version))


def latest_versions(files: Iterable[ReportFile]) -> dict[tuple, ReportFile]:
    """Map (report, period) -> the highest-version file."""
    best: dict[tuple, ReportFile] = {}
    for f in files:
        if not f.is_versioned:
            continue
        current = best.get(f.key)
        if current is None or f.version > current.version:
            best[f.key] = f
    return best
