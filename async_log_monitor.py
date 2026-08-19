#!/usr/bin/env python3
"""Aggregate AsyncFileWriter USDT telemetry into an append-only JSONL log."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, TextIO


__version__ = "0.1.0"

QUEUE_CAPACITY = 127
HISTOGRAM_BUCKETS = 32
PROBES = (
    ("async_file_queued", "trace_async_file_queued"),
    ("async_file_operation_started", "trace_async_file_operation_started"),
    ("async_file_operation_completed", "trace_async_file_operation_completed"),
)


class Stat(IntEnum):
    QUEUED_TOTAL = 0
    STARTED_TOTAL = 1
    COMPLETED_TOTAL = 2
    WRITE_STARTED_TOTAL = 3
    WRITE_COMPLETED_TOTAL = 4
    REOPEN_STARTED_TOTAL = 5
    REOPEN_COMPLETED_TOTAL = 6
    DEPTH_CURRENT = 7
    DEPTH_HIGH_WATER = 8
    DEPTH_OBSERVED = 9
    FULL_EPISODES = 10
    FULL_COMPLETED_NS = 11
    FULL_MAX_NS = 12
    QUEUE_WAIT_COUNT = 13
    QUEUE_WAIT_SUM_NS = 14
    QUEUE_WAIT_MAX_NS = 15
    WRITE_DURATION_COUNT = 16
    WRITE_DURATION_SUM_NS = 17
    WRITE_DURATION_MAX_NS = 18
    REOPEN_DURATION_COUNT = 19
    REOPEN_DURATION_SUM_NS = 20
    REOPEN_DURATION_MAX_NS = 21
    QUEUE_TIMESTAMP_MISSES = 22
    QUEUE_TIMESTAMP_OVERWRITES = 23
    ACTIVE_OPERATION_MISSES = 24
    ACTIVE_OPERATION_OVERLAPS = 25
    ACTIVE_OPERATION_KIND_MISMATCHES = 26


BPF_PROGRAM = r"""
#include <uapi/linux/ptrace.h>

#define QUEUE_CAPACITY 127
#define HISTOGRAM_BUCKETS 32

enum stat_index {
    STAT_QUEUED_TOTAL = 0,
    STAT_STARTED_TOTAL = 1,
    STAT_COMPLETED_TOTAL = 2,
    STAT_WRITE_STARTED_TOTAL = 3,
    STAT_WRITE_COMPLETED_TOTAL = 4,
    STAT_REOPEN_STARTED_TOTAL = 5,
    STAT_REOPEN_COMPLETED_TOTAL = 6,
    STAT_DEPTH_CURRENT = 7,
    STAT_DEPTH_HIGH_WATER = 8,
    STAT_DEPTH_OBSERVED = 9,
    STAT_FULL_EPISODES = 10,
    STAT_FULL_COMPLETED_NS = 11,
    STAT_FULL_MAX_NS = 12,
    STAT_QUEUE_WAIT_COUNT = 13,
    STAT_QUEUE_WAIT_SUM_NS = 14,
    STAT_QUEUE_WAIT_MAX_NS = 15,
    STAT_WRITE_DURATION_COUNT = 16,
    STAT_WRITE_DURATION_SUM_NS = 17,
    STAT_WRITE_DURATION_MAX_NS = 18,
    STAT_REOPEN_DURATION_COUNT = 19,
    STAT_REOPEN_DURATION_SUM_NS = 20,
    STAT_REOPEN_DURATION_MAX_NS = 21,
    STAT_QUEUE_TIMESTAMP_MISSES = 22,
    STAT_QUEUE_TIMESTAMP_OVERWRITES = 23,
    STAT_ACTIVE_OPERATION_MISSES = 24,
    STAT_ACTIVE_OPERATION_OVERLAPS = 25,
    STAT_ACTIVE_OPERATION_KIND_MISMATCHES = 26,
    STAT_COUNT = 27,
};

enum operation_kind {
    OPERATION_WRITE_LINE = 1,
    OPERATION_REOPEN_FILE = 2,
};

struct active_operation_t {
    u64 sequence;
    u64 started_ns;
    u32 kind;
    u32 active;
};

struct queued_timestamp_t {
    u64 sequence;
    u64 queued_ns;
    u32 valid;
    u32 padding;
};

BPF_ARRAY(stats, u64, STAT_COUNT);
BPF_ARRAY(full_since_ns, u64, 1);
BPF_ARRAY(active_operation, struct active_operation_t, 1);
BPF_ARRAY(queued_timestamps, struct queued_timestamp_t, 256);
BPF_ARRAY(queue_wait_histogram, u64, HISTOGRAM_BUCKETS);
BPF_ARRAY(write_duration_histogram, u64, HISTOGRAM_BUCKETS);
BPF_ARRAY(reopen_duration_histogram, u64, HISTOGRAM_BUCKETS);

static __always_inline void stat_increment(u32 index)
{
    u64 *value = stats.lookup(&index);
    if (value != 0) __sync_fetch_and_add(value, 1);
}

static __always_inline void stat_add(u32 index, u64 amount)
{
    u64 *value = stats.lookup(&index);
    if (value != 0) __sync_fetch_and_add(value, amount);
}

static __always_inline void stat_set(u32 index, u64 new_value)
{
    u64 *value = stats.lookup(&index);
    if (value != 0) *value = new_value;
}

/* Each maximum has a single serialized producer in AsyncFileWriter. */
static __always_inline void stat_max(u32 index, u64 candidate)
{
    u64 *value = stats.lookup(&index);
    if (value != 0 && candidate > *value) *value = candidate;
}

/* Bucket 0 is <1 us; bucket N is [2^(N-1), 2^N) us; bucket 31 is open-ended. */
static __always_inline u32 latency_bucket(u64 duration_ns)
{
    u64 duration_us = duration_ns / 1000;
    if (duration_us == 0) return 0;

    u64 logarithm = bpf_log2l(duration_us);
    if (logarithm >= HISTOGRAM_BUCKETS - 1) return HISTOGRAM_BUCKETS - 1;
    return (u32)(logarithm + 1);
}

static __always_inline void record_queue_wait(u64 duration_ns)
{
    stat_increment(STAT_QUEUE_WAIT_COUNT);
    stat_add(STAT_QUEUE_WAIT_SUM_NS, duration_ns);
    stat_max(STAT_QUEUE_WAIT_MAX_NS, duration_ns);

    u32 bucket = latency_bucket(duration_ns);
    u64 *count = queue_wait_histogram.lookup(&bucket);
    if (count != 0) __sync_fetch_and_add(count, 1);
}

static __always_inline void record_operation_duration(u32 kind, u64 duration_ns)
{
    u32 bucket = latency_bucket(duration_ns);

    if (kind == OPERATION_WRITE_LINE) {
        stat_increment(STAT_WRITE_DURATION_COUNT);
        stat_add(STAT_WRITE_DURATION_SUM_NS, duration_ns);
        stat_max(STAT_WRITE_DURATION_MAX_NS, duration_ns);
        u64 *count = write_duration_histogram.lookup(&bucket);
        if (count != 0) __sync_fetch_and_add(count, 1);
    } else if (kind == OPERATION_REOPEN_FILE) {
        stat_increment(STAT_REOPEN_DURATION_COUNT);
        stat_add(STAT_REOPEN_DURATION_SUM_NS, duration_ns);
        stat_max(STAT_REOPEN_DURATION_MAX_NS, duration_ns);
        u64 *count = reopen_duration_histogram.lookup(&bucket);
        if (count != 0) __sync_fetch_and_add(count, 1);
    }
}

int trace_async_file_queued(struct pt_regs *ctx)
{
    u64 sequence = 0;
    u64 depth = 0;
    bpf_usdt_readarg(1, ctx, &sequence);
    bpf_usdt_readarg(2, ctx, &depth);

    u64 now = bpf_ktime_get_ns();
    stat_increment(STAT_QUEUED_TOTAL);
    stat_set(STAT_DEPTH_CURRENT, depth);
    stat_set(STAT_DEPTH_OBSERVED, 1);
    stat_max(STAT_DEPTH_HIGH_WATER, depth);

    u32 timestamp_slot = (u32)(sequence & 255);
    struct queued_timestamp_t *queued = queued_timestamps.lookup(&timestamp_slot);
    if (queued != 0) {
        if (queued->valid && queued->sequence != sequence) {
            stat_increment(STAT_QUEUE_TIMESTAMP_OVERWRITES);
        }
        queued->sequence = sequence;
        queued->queued_ns = now;
        queued->valid = 1;
    }

    if (depth == QUEUE_CAPACITY) {
        u32 zero = 0;
        u64 *full_since = full_since_ns.lookup(&zero);
        if (full_since != 0 && *full_since == 0) {
            *full_since = now;
            stat_increment(STAT_FULL_EPISODES);
        }
    }
    return 0;
}

int trace_async_file_operation_started(struct pt_regs *ctx)
{
    u64 sequence = 0;
    u32 kind = 0;
    bpf_usdt_readarg(1, ctx, &sequence);
    bpf_usdt_readarg(2, ctx, &kind);

    u64 now = bpf_ktime_get_ns();
    stat_increment(STAT_STARTED_TOTAL);
    if (kind == OPERATION_WRITE_LINE) {
        stat_increment(STAT_WRITE_STARTED_TOTAL);
    } else if (kind == OPERATION_REOPEN_FILE) {
        stat_increment(STAT_REOPEN_STARTED_TOTAL);
    }

    u32 timestamp_slot = (u32)(sequence & 255);
    struct queued_timestamp_t *queued = queued_timestamps.lookup(&timestamp_slot);
    if (queued != 0 && queued->valid && queued->sequence == sequence) {
        if (now >= queued->queued_ns) record_queue_wait(now - queued->queued_ns);
        queued->valid = 0;
    } else {
        stat_increment(STAT_QUEUE_TIMESTAMP_MISSES);
        if (queued != 0) queued->valid = 0;
    }

    u32 zero = 0;
    struct active_operation_t *active = active_operation.lookup(&zero);
    if (active != 0) {
        if (active->active) stat_increment(STAT_ACTIVE_OPERATION_OVERLAPS);
        active->sequence = sequence;
        active->started_ns = now;
        active->kind = kind;
        active->active = 1;
    }
    return 0;
}

int trace_async_file_operation_completed(struct pt_regs *ctx)
{
    u64 sequence = 0;
    u32 kind = 0;
    u64 depth = 0;
    bpf_usdt_readarg(1, ctx, &sequence);
    bpf_usdt_readarg(2, ctx, &kind);
    bpf_usdt_readarg(3, ctx, &depth);

    u64 now = bpf_ktime_get_ns();
    stat_increment(STAT_COMPLETED_TOTAL);
    if (kind == OPERATION_WRITE_LINE) {
        stat_increment(STAT_WRITE_COMPLETED_TOTAL);
    } else if (kind == OPERATION_REOPEN_FILE) {
        stat_increment(STAT_REOPEN_COMPLETED_TOTAL);
    }
    stat_set(STAT_DEPTH_CURRENT, depth);
    stat_set(STAT_DEPTH_OBSERVED, 1);

    if (depth < QUEUE_CAPACITY) {
        u32 zero = 0;
        u64 *full_since = full_since_ns.lookup(&zero);
        if (full_since != 0 && *full_since != 0) {
            u64 started = *full_since;
            *full_since = 0;
            if (now >= started) {
                u64 duration = now - started;
                stat_add(STAT_FULL_COMPLETED_NS, duration);
                stat_max(STAT_FULL_MAX_NS, duration);
            }
        }
    }

    u32 zero = 0;
    struct active_operation_t *active = active_operation.lookup(&zero);
    if (active == 0 || !active->active || active->sequence != sequence) {
        stat_increment(STAT_ACTIVE_OPERATION_MISSES);
    } else {
        if (active->kind != kind) {
            stat_increment(STAT_ACTIVE_OPERATION_KIND_MISMATCHES);
        }
        if (now >= active->started_ns) {
            record_operation_duration(kind, now - active->started_ns);
        }
        active->active = 0;
    }
    return 0;
}
"""


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with microsecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def operation_kind_name(kind: int) -> str:
    return {1: "write", 2: "reopen"}.get(kind, f"unknown_{kind}")


def histogram_bucket_metadata() -> list[dict[str, int | None]]:
    buckets: list[dict[str, int | None]] = [
        {"index": 0, "lower_us": None, "upper_us": 1},
    ]
    for index in range(1, HISTOGRAM_BUCKETS):
        lower = 1 << (index - 1)
        upper = None if index == HISTOGRAM_BUCKETS - 1 else 1 << index
        buckets.append({"index": index, "lower_us": lower, "upper_us": upper})
    return buckets


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("async_log_monitor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


class JsonlWriter:
    """Append line-buffered JSON records while holding an exclusive file lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: TextIO | None = None

    def __enter__(self) -> JsonlWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_APPEND | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        created = False
        try:
            fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o644)
            created = True
        except FileExistsError:
            fd = os.open(self.path, flags)
        try:
            if created:
                os.fchmod(fd, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.file = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
        except Exception:
            os.close(fd)
            raise
        return self

    def write(self, record: dict[str, Any]) -> None:
        if self.file is None:
            raise RuntimeError("JSONL writer is not open")
        self.file.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        self.file.flush()

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None


@dataclass(frozen=True)
class MapSnapshot:
    captured_ns: int
    stats: tuple[int, ...]
    histograms: dict[str, tuple[int, ...]]
    full_since_ns: int
    active_operation: dict[str, Any] | None
    queued_timestamp_count: int
    oldest_queued_age_ns: int | None


class AsyncLogMonitor:
    @staticmethod
    def _empty_snapshot(captured_ns: int) -> MapSnapshot:
        return MapSnapshot(
            captured_ns=captured_ns,
            stats=tuple(0 for _ in Stat),
            histograms={
                "queue_wait": tuple(0 for _ in range(HISTOGRAM_BUCKETS)),
                "write": tuple(0 for _ in range(HISTOGRAM_BUCKETS)),
                "reopen": tuple(0 for _ in range(HISTOGRAM_BUCKETS)),
            },
            full_since_ns=0,
            active_operation=None,
            queued_timestamp_count=0,
            oldest_queued_age_ns=None,
        )

    def __init__(
        self,
        bitcoind_path: Path,
        pid: int,
        node_name: str,
        interval_seconds: float,
        writer: JsonlWriter,
        logger: logging.Logger,
    ) -> None:
        self.bitcoind_path = bitcoind_path
        self.pid = pid
        self.node_name = node_name
        self.interval_seconds = interval_seconds
        self.writer = writer
        self.logger = logger
        self.session_id = str(uuid.uuid4())
        self.pid_start_ticks = self._read_pid_start_ticks()
        self.bpf: Any | None = None
        self._usdt: Any | None = None
        self._stop_event = threading.Event()
        self._stop_reason = "requested"
        self._session_started_ns = time.monotonic_ns()
        self._previous_snapshot = self._empty_snapshot(self._session_started_ns)
        self._previous_full_effective_ns = 0

    def _read_pid_start_ticks(self) -> int:
        text = Path(f"/proc/{self.pid}/stat").read_text(encoding="utf-8")
        closing_paren = text.rfind(")")
        if closing_paren < 0:
            raise RuntimeError(f"Could not parse /proc/{self.pid}/stat")
        fields_after_comm = text[closing_paren + 2 :].split()
        return int(fields_after_comm[19])

    def _target_is_same_process(self) -> bool:
        try:
            proc_exe = Path(os.path.realpath(f"/proc/{self.pid}/exe"))
            return (
                proc_exe == self.bitcoind_path
                and self._read_pid_start_ticks() == self.pid_start_ticks
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def request_stop(self, signum: int, _frame: Any) -> None:
        try:
            self._stop_reason = f"signal_{signal.Signals(signum).name.lower()}"
        except ValueError:
            self._stop_reason = f"signal_{signum}"
        self._stop_event.set()

    def attach(self) -> None:
        from bcc import BPF, USDT

        self._usdt = USDT(path=str(self.bitcoind_path), pid=self.pid)
        try:
            for probe_name, function_name in PROBES:
                self._usdt.enable_probe(probe=probe_name, fn_name=function_name)
            self.bpf = BPF(text=BPF_PROGRAM, usdt_contexts=[self._usdt])
        except Exception:
            self._usdt = None
            raise

    def detach(self) -> None:
        if self.bpf is not None:
            try:
                self.bpf.cleanup()
            finally:
                self.bpf = None
        self._usdt = None

    def _array_value(self, table_name: str, index: int) -> int:
        table = self.bpf[table_name]
        return int(table[table.Key(index)].value)

    def _read_histogram(self, table_name: str) -> tuple[int, ...]:
        return tuple(
            self._array_value(table_name, index)
            for index in range(HISTOGRAM_BUCKETS)
        )

    def _read_snapshot(self) -> MapSnapshot:
        captured_ns = time.monotonic_ns()
        stats = tuple(self._array_value("stats", stat.value) for stat in Stat)
        histograms = {
            "queue_wait": self._read_histogram("queue_wait_histogram"),
            "write": self._read_histogram("write_duration_histogram"),
            "reopen": self._read_histogram("reopen_duration_histogram"),
        }

        full_since_ns = self._array_value("full_since_ns", 0)
        active_leaf = self.bpf["active_operation"]
        active = active_leaf[active_leaf.Key(0)]
        active_operation: dict[str, Any] | None = None
        if int(active.active):
            started_ns = int(active.started_ns)
            active_operation = {
                "sequence": int(active.sequence),
                "kind": operation_kind_name(int(active.kind)),
                "started_ns": started_ns,
                "age_ns": max(0, captured_ns - started_ns),
            }

        queued_timestamps = [
            int(value.queued_ns)
            for _, value in self.bpf["queued_timestamps"].items()
            if int(value.valid)
        ]
        oldest_queued_age_ns = None
        if queued_timestamps:
            oldest_queued_age_ns = max(0, captured_ns - min(queued_timestamps))

        return MapSnapshot(
            captured_ns=captured_ns,
            stats=stats,
            histograms=histograms,
            full_since_ns=full_since_ns,
            active_operation=active_operation,
            queued_timestamp_count=len(queued_timestamps),
            oldest_queued_age_ns=oldest_queued_age_ns,
        )

    @staticmethod
    def _stat(snapshot: MapSnapshot, stat: Stat) -> int:
        return snapshot.stats[stat.value]

    def _counter_delta(self, current: MapSnapshot, stat: Stat) -> int:
        return max(0, self._stat(current, stat) - self._stat(self._previous_snapshot, stat))

    def _latency_payload(
        self,
        current: MapSnapshot,
        histogram_name: str,
        count_stat: Stat,
        sum_stat: Stat,
        max_stat: Stat,
    ) -> dict[str, Any]:
        count = self._stat(current, count_stat)
        total_ns = self._stat(current, sum_stat)
        interval_count = self._counter_delta(current, count_stat)
        interval_total_ns = self._counter_delta(current, sum_stat)
        histogram = current.histograms[histogram_name]
        previous_histogram = self._previous_snapshot.histograms[histogram_name]
        interval_histogram = [
            max(0, value - previous)
            for value, previous in zip(histogram, previous_histogram)
        ]
        return {
            "cumulative": {
                "count": count,
                "sum_ns": total_ns,
                "max_ns": self._stat(current, max_stat),
                "mean_ns": total_ns // count if count else None,
                "histogram": list(histogram),
            },
            "interval": {
                "count": interval_count,
                "sum_ns": interval_total_ns,
                "mean_ns": interval_total_ns // interval_count if interval_count else None,
                "histogram": interval_histogram,
            },
        }

    def _build_sample(self, current: MapSnapshot, reason: str) -> dict[str, Any]:
        depth_known = bool(self._stat(current, Stat.DEPTH_OBSERVED))
        full_active = current.full_since_ns != 0
        full_active_age_ns = (
            max(0, current.captured_ns - current.full_since_ns)
            if full_active
            else 0
        )
        full_effective_ns = self._stat(current, Stat.FULL_COMPLETED_NS) + full_active_age_ns
        # A full->not-full transition can occur between two map reads. Defer that
        # tiny interval to the next snapshot instead of emitting a negative delta.
        full_effective_ns = max(full_effective_ns, self._previous_full_effective_ns)

        events = {}
        for name, stat in (
            ("queued", Stat.QUEUED_TOTAL),
            ("started", Stat.STARTED_TOTAL),
            ("completed", Stat.COMPLETED_TOTAL),
            ("write_started", Stat.WRITE_STARTED_TOTAL),
            ("write_completed", Stat.WRITE_COMPLETED_TOTAL),
            ("reopen_started", Stat.REOPEN_STARTED_TOTAL),
            ("reopen_completed", Stat.REOPEN_COMPLETED_TOTAL),
        ):
            events[name] = {
                "total": self._stat(current, stat),
                "interval": self._counter_delta(current, stat),
            }

        diagnostics = {}
        for name, stat in (
            ("queue_timestamp_misses", Stat.QUEUE_TIMESTAMP_MISSES),
            ("queue_timestamp_overwrites", Stat.QUEUE_TIMESTAMP_OVERWRITES),
            ("active_operation_misses", Stat.ACTIVE_OPERATION_MISSES),
            ("active_operation_overlaps", Stat.ACTIVE_OPERATION_OVERLAPS),
            ("active_operation_kind_mismatches", Stat.ACTIVE_OPERATION_KIND_MISMATCHES),
        ):
            diagnostics[name] = {
                "total": self._stat(current, stat),
                "interval": self._counter_delta(current, stat),
            }

        record = {
            "schema_version": 1,
            "record_type": "sample",
            "sample_reason": reason,
            "timestamp_utc": utc_now_iso(),
            "monotonic_ns": current.captured_ns,
            "session_id": self.session_id,
            "node_name": self.node_name,
            "pid": self.pid,
            "session_elapsed_ns": current.captured_ns - self._session_started_ns,
            "sample_interval_ns": current.captured_ns - self._previous_snapshot.captured_ns,
            "events": events,
            "queue": {
                "capacity": QUEUE_CAPACITY,
                "depth_known": depth_known,
                "current_depth": self._stat(current, Stat.DEPTH_CURRENT) if depth_known else None,
                "high_water": self._stat(current, Stat.DEPTH_HIGH_WATER) if depth_known else None,
                "unstarted_timestamp_count": current.queued_timestamp_count,
                "oldest_unstarted_age_ns": current.oldest_queued_age_ns,
            },
            "full_capacity": {
                "active": full_active,
                "active_age_ns": full_active_age_ns if full_active else None,
                "episodes_total": self._stat(current, Stat.FULL_EPISODES),
                "episodes_interval": self._counter_delta(current, Stat.FULL_EPISODES),
                "completed_duration_ns": self._stat(current, Stat.FULL_COMPLETED_NS),
                "effective_duration_ns": full_effective_ns,
                "effective_duration_interval_ns": max(
                    0, full_effective_ns - self._previous_full_effective_ns
                ),
                "max_completed_episode_ns": self._stat(current, Stat.FULL_MAX_NS),
            },
            "queue_wait_latency": self._latency_payload(
                current,
                "queue_wait",
                Stat.QUEUE_WAIT_COUNT,
                Stat.QUEUE_WAIT_SUM_NS,
                Stat.QUEUE_WAIT_MAX_NS,
            ),
            "operation_latency": {
                "write": self._latency_payload(
                    current,
                    "write",
                    Stat.WRITE_DURATION_COUNT,
                    Stat.WRITE_DURATION_SUM_NS,
                    Stat.WRITE_DURATION_MAX_NS,
                ),
                "reopen": self._latency_payload(
                    current,
                    "reopen",
                    Stat.REOPEN_DURATION_COUNT,
                    Stat.REOPEN_DURATION_SUM_NS,
                    Stat.REOPEN_DURATION_MAX_NS,
                ),
            },
            "worker": {"active_operation": current.active_operation},
            "diagnostics": diagnostics,
        }

        self._previous_snapshot = current
        self._previous_full_effective_ns = full_effective_ns
        return record

    def _write_sample(self, reason: str) -> dict[str, Any]:
        record = self._build_sample(self._read_snapshot(), reason)
        self.writer.write(record)

        queue = record["queue"]
        full = record["full_capacity"]
        write_latency = record["operation_latency"]["write"]["cumulative"]
        self.logger.info(
            "sample node=%s queued=%d depth=%s high_water=%s full_interval_ms=%.3f "
            "write_max_ms=%.3f oldest_unstarted_ms=%s",
            self.node_name,
            record["events"]["queued"]["interval"],
            queue["current_depth"],
            queue["high_water"],
            full["effective_duration_interval_ns"] / 1_000_000,
            write_latency["max_ns"] / 1_000_000,
            (
                f"{queue['oldest_unstarted_age_ns'] / 1_000_000:.3f}"
                if queue["oldest_unstarted_age_ns"] is not None
                else None
            ),
        )
        return record

    def run(self) -> None:
        self.attach()
        self._session_started_ns = time.monotonic_ns()
        self._previous_snapshot = self._empty_snapshot(self._session_started_ns)
        self._previous_full_effective_ns = 0
        reason = "requested"
        try:
            self.logger.info(
                "Attached %d logging probes to pid=%d node=%s; output is line-buffered JSONL",
                len(PROBES),
                self.pid,
                self.node_name,
            )
            self.writer.write(
                {
                    "schema_version": 1,
                    "record_type": "session_start",
                    "timestamp_utc": utc_now_iso(),
                    "monotonic_ns": self._session_started_ns,
                    "session_id": self.session_id,
                    "monitor_version": __version__,
                    "node_name": self.node_name,
                    "pid": self.pid,
                    "pid_start_ticks": self.pid_start_ticks,
                    "bitcoind_path": str(self.bitcoind_path),
                    "sample_interval_seconds": self.interval_seconds,
                    "queue_capacity": QUEUE_CAPACITY,
                    "probes": [f"logging:{name}" for name, _ in PROBES],
                    "operation_kinds": {"1": "write", "2": "reopen"},
                    "latency_histogram": {
                        "unit": "microseconds",
                        "buckets": histogram_bucket_metadata(),
                    },
                    "notes": [
                        "Metrics begin at probe attachment, not at node startup.",
                        "Write operation latency spans the pre-fwrite start probe through slot release after queue-lock reacquisition.",
                        "Full-capacity duration measures time at depth 127; it does not imply that a producer attempted to enqueue.",
                    ],
                }
            )
            self._write_sample("initial")
            while not self._stop_event.wait(self.interval_seconds):
                if not self._target_is_same_process():
                    reason = "target_exited_or_changed"
                    break
                self._write_sample("periodic")
            else:
                reason = self._stop_reason

            self._write_sample("final")
        except Exception:
            reason = "monitor_error"
            raise
        finally:
            try:
                self.writer.write(
                    {
                        "schema_version": 1,
                        "record_type": "session_end",
                        "timestamp_utc": utc_now_iso(),
                        "monotonic_ns": time.monotonic_ns(),
                        "session_id": self.session_id,
                        "node_name": self.node_name,
                        "pid": self.pid,
                        "reason": reason,
                    }
                )
            finally:
                self.detach()
            self.logger.info("Stopped monitor session=%s reason=%s", self.session_id, reason)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aggregate AsyncFileWriter USDT telemetry into a flushed JSONL file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Run one monitor per node, using a distinct output file:

  sudo .venv/bin/python3 async_log_monitor.py \\
    --bitcoind /path/to/bitcoind --pid 12345 --node-name node1 \\
    --output runtime/async-log-node1.jsonl

The monitor requires root for eBPF/USDT attachment. The bitcoind process itself
should continue to run as its normal unprivileged user.
""",
    )
    parser.add_argument("--bitcoind", "-b", required=True, type=Path, help="Path to the instrumented bitcoind binary")
    parser.add_argument("--pid", "-p", required=True, type=int, help="PID of the exact bitcoind instance to monitor")
    parser.add_argument("--node-name", "-n", default="localhost", help="Stable node label stored in every record")
    parser.add_argument("--output", "-o", required=True, type=Path, help="Append-only JSONL output path")
    parser.add_argument("--interval", type=float, default=30.0, help="Snapshot interval in seconds (default: 30)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Path:
    if os.geteuid() != 0:
        parser.error("eBPF/USDT attachment requires root; run this command through sudo")
    if args.pid <= 0:
        parser.error("--pid must be positive")
    if args.interval < 1:
        parser.error("--interval must be at least 1 second")
    if not args.node_name.strip():
        parser.error("--node-name must not be empty")

    try:
        bitcoind_path = args.bitcoind.expanduser().resolve(strict=True)
    except OSError as error:
        parser.error(f"invalid --bitcoind path: {error}")
    if not bitcoind_path.is_file():
        parser.error("--bitcoind must name a regular file")

    try:
        proc_exe = Path(f"/proc/{args.pid}/exe").resolve(strict=True)
    except OSError as error:
        parser.error(f"cannot inspect PID {args.pid}: {error}")
    if proc_exe != bitcoind_path:
        parser.error(
            f"PID {args.pid} runs {proc_exe}, which does not match --bitcoind {bitcoind_path}"
        )
    return bitcoind_path


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    bitcoind_path = validate_args(args, parser)
    output_path = args.output.expanduser().absolute()
    logger = setup_logging()

    try:
        with JsonlWriter(output_path) as writer:
            monitor = AsyncLogMonitor(
                bitcoind_path=bitcoind_path,
                pid=args.pid,
                node_name=args.node_name.strip(),
                interval_seconds=args.interval,
                writer=writer,
                logger=logger,
            )
            signal.signal(signal.SIGINT, monitor.request_stop)
            signal.signal(signal.SIGTERM, monitor.request_stop)
            monitor.run()
    except BlockingIOError:
        logger.error("Output file is already locked by another monitor: %s", output_path)
        raise SystemExit(1)
    except Exception as error:
        logger.error("Monitor failed: %s", error)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
