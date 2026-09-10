"""
Event Log - Append-only JSONL event logging for paper positions.

Provides thread-safe, durable event logging with rotation and query capabilities.
"""

from __future__ import annotations

import json
import threading
import gzip
import shutil
import typing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Iterator, TYPE_CHECKING, IO
from dataclasses import asdict

from paper_position_manager import PositionEvent

if TYPE_CHECKING:
    from paper_position_manager import PaperPosition


class EventLogger:
    """
    Thread-safe append-only JSONL event logger with rotation.

    Features:
    - Atomic writes (write to temp, then rename)
    - Automatic rotation by size/date
    - Compression of rotated logs
    - Query by mint, event type, time range
    """

    def __init__(
        self,
        log_dir: Path,
        max_file_size_mb: int = 100,
        max_files: int = 30,
        compress_rotated: bool = True,
    ):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_file_size = max_file_size_mb * 1024 * 1024
        self.max_files = max_files
        self.compress_rotated = compress_rotated

        self._current_file: Optional[Path] = None
        self._current_size = 0
        self._lock = threading.Lock()
        self._file_handle: Optional[IO] = None

        self._open_new_file()

    def _open_new_file(self) -> None:
        """Open a new log file with timestamp."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._current_file = self.log_dir / f"events_{timestamp}.jsonl"
        self._file_handle = open(self._current_file, "a", buffering=1)  # Line buffered
        self._current_size = self._current_file.stat().st_size if self._current_file.exists() else 0
        self._rotate_if_needed()

    def _rotate_if_needed(self) -> None:
        """Rotate log file if it exceeds max size."""
        if self._current_size >= self.max_file_size:
            self._rotate()

    def _rotate(self) -> None:
        """Rotate current log file."""
        if self._file_handle:
            self._file_handle.close()
            self._file_handle = None

        if self.compress_rotated and self._current_file and self._current_file.exists():
            gz_path = self._current_file.with_suffix(".jsonl.gz")
            with open(self._current_file, "rb") as f_in:
                with gzip.open(gz_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            self._current_file.unlink()
            self._current_file = gz_path

        # Clean up old files
        self._cleanup_old_files()

        self._open_new_file()

    def _cleanup_old_files(self) -> None:
        """Remove oldest files if we exceed max_files."""
        files = sorted(self.log_dir.glob("events_*.jsonl*"), key=lambda p: p.stat().st_mtime)
        while len(files) > self.max_files:
            oldest = files.pop(0)
            oldest.unlink()

    def log(self, event: PositionEvent) -> None:
        """Log a single event (thread-safe)."""
        with self._lock:
            if self._file_handle is None:
                self._open_new_file()

            line = event.to_jsonl() + "\n"
            self._file_handle.write(line)
            self._current_size += len(line.encode("utf-8"))

            # Check rotation after write
            if self._current_size >= self.max_file_size:
                self._rotate()

    def log_batch(self, events: list[PositionEvent]) -> None:
        """Log multiple events efficiently."""
        with self._lock:
            if self._file_handle is None:
                self._open_new_file()

            for event in events:
                line = event.to_jsonl() + "\n"
                self._file_handle.write(line)
                self._current_size += len(line.encode("utf-8"))

            if self._current_size >= self.max_file_size:
                self._rotate()

    def log_position(self, position: "PaperPosition") -> None:
        """Log all events from a position."""
        self.log_batch(position.events)

    def close(self) -> None:
        """Close the logger."""
        with self._lock:
            if self._file_handle:
                self._file_handle.close()
                self._file_handle = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class EventQuery:
    """Query interface for event logs."""

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)

    def _iter_files(self) -> Iterator[Path]:
        """Iterate over log files (newest first)."""
        for f in sorted(self.log_dir.glob("events_*.jsonl*"), key=lambda p: p.stat().st_mtime, reverse=True):
            yield f

    def _open_file(self, path: Path):
        """Open a log file (handles both .jsonl and .jsonl.gz)."""
        if path.suffix == ".gz":
            return gzip.open(path, "rt", encoding="utf-8")
        return open(path, "r", encoding="utf-8")

    def query(
        self,
        mint: Optional[str] = None,
        event_type: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> Iterator[PositionEvent]:
        """
        Query events with filters.
        Returns events in chronological order (oldest first).
        """
        count = 0
        # We need to read all matching files and sort by timestamp
        all_events = []

        for log_file in self._iter_files():
            try:
                with self._open_file(log_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # Apply filters
                        if mint and data.get("mint") != mint:
                            continue
                        if event_type and data.get("event") != event_type:
                            continue
                        if start_time:
                            event_ts = datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
                            if event_ts < start_time:
                                continue
                        if end_time:
                            event_ts = datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
                            if event_ts > end_time:
                                continue

                        all_events.append(data)
            except Exception:
                continue

        # Sort by timestamp
        all_events.sort(key=lambda e: e["ts"])

        for data in all_events:
            if limit and count >= limit:
                break
            yield PositionEvent(**data)
            count += 1

    def get_position_timeline(self, mint: str) -> list[PositionEvent]:
        """Get complete timeline for a position."""
        return list(self.query(mint=mint))

    def get_stats(self, mint: Optional[str] = None) -> dict:
        """Get summary statistics."""
        stats = {
            "total_events": 0,
            "events_by_type": {},
            "mints": set(),
            "time_range": None,
        }

        first_ts = None
        last_ts = None

        for event in self.query(mint=mint):
            stats["total_events"] += 1
            etype = event.event
            stats["events_by_type"][etype] = stats["events_by_type"].get(etype, 0) + 1
            stats["mints"].add(event.mint)

            ts = datetime.fromisoformat(event.ts.replace("Z", "+00:00"))
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

        stats["mints"] = list(stats["mints"])
        if first_ts and last_ts:
            stats["time_range"] = {
                "start": first_ts.isoformat(),
                "end": last_ts.isoformat(),
            }

        return stats


def replay_position_from_logs(log_dir: Path, mint: str) -> list[PositionEvent]:
    """Convenience function to replay a position's events."""
    query = EventQuery(log_dir)
    return query.get_position_timeline(mint)