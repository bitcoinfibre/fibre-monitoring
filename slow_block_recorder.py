#!/usr/bin/env python3
"""
Record FIBRE block reconstruction events into SQLite and alert on slow blocks.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib import error as urlerror
from urllib import request as urlrequest


__version__ = "0.1.0"


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_env_bool(value: str) -> bool:
    """Parse a truthy or falsy environment variable value."""
    return value.strip().lower() in {"1", "true", "yes", "on"}


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Configure structured logging."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger = logging.getLogger("slow_block_recorder")
    logger.setLevel(log_level)
    logger.handlers.clear()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def find_bitcoind_pid(bitcoind_path: str) -> Optional[int]:
    """Find the PID of a running bitcoind process matching the binary path."""
    binary_name = os.path.basename(bitcoind_path)
    resolved_target = os.path.realpath(bitcoind_path)

    candidate_pids: set[int] = set()
    for cmd in (
        ["pgrep", "-x", binary_name],
        ["pidof", binary_name],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                for tok in result.stdout.strip().split():
                    candidate_pids.add(int(tok))
        except Exception:
            pass

    if not candidate_pids:
        return None

    matched: list[int] = []
    for pid in sorted(candidate_pids):
        try:
            proc_exe = os.path.realpath(f"/proc/{pid}/exe")
            if proc_exe == resolved_target:
                matched.append(pid)
        except OSError:
            continue

    if len(matched) == 1:
        return matched[0]
    return None


def decode_c_string(raw: Any) -> str:
    """Decode a null-terminated C string from a BPF event field."""
    return bytes(raw).decode("utf-8", errors="ignore").rstrip("\x00")


@dataclass
class RecorderConfig:
    """Configuration for the slow block recorder."""

    bitcoind_path: str
    pid: Optional[int] = None
    node_name: str = "localhost"
    db_path: str = "slow-blocks.sqlite"
    slow_threshold_ms: float = 50.0
    very_slow_threshold_ms: float = 100.0
    pending_timeout_sec: int = 60
    housekeeping_interval_sec: int = 5
    alert_enabled: bool = True
    alert_webhook_url: Optional[str] = None
    verbose: bool = False
    log_level: str = "INFO"
    log_file: Optional[str] = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> RecorderConfig:
        return cls(
            bitcoind_path=args.bitcoind or "",
            pid=args.pid,
            node_name=args.node_name,
            db_path=args.db,
            slow_threshold_ms=args.slow_threshold_ms,
            very_slow_threshold_ms=args.very_slow_threshold_ms,
            pending_timeout_sec=args.pending_timeout_sec,
            housekeeping_interval_sec=args.housekeeping_interval_sec,
            alert_enabled=not args.disable_alerts,
            alert_webhook_url=args.alert_webhook_url,
            verbose=args.verbose,
            log_level=args.log_level,
            log_file=getattr(args, "log_file", None),
        )

    @classmethod
    def from_yaml(cls, path: Path) -> RecorderConfig:
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls(
            bitcoind_path=data.get("bitcoind_path", data.get("bitcoind", "")),
            pid=data.get("pid"),
            node_name=data.get("node_name", "localhost"),
            db_path=data.get("db_path", "slow-blocks.sqlite"),
            slow_threshold_ms=float(data.get("slow_threshold_ms", 50.0)),
            very_slow_threshold_ms=float(data.get("very_slow_threshold_ms", 100.0)),
            pending_timeout_sec=int(data.get("pending_timeout_sec", 60)),
            housekeeping_interval_sec=int(data.get("housekeeping_interval_sec", 5)),
            alert_enabled=bool(data.get("alert_enabled", True)),
            alert_webhook_url=data.get("alert_webhook_url"),
            verbose=bool(data.get("verbose", False)),
            log_level=data.get("log_level", "INFO"),
            log_file=data.get("log_file"),
        )

    @classmethod
    def from_env(cls, base_config: Optional[RecorderConfig] = None) -> RecorderConfig:
        if base_config is None:
            base_config = cls(bitcoind_path="")

        return cls(
            bitcoind_path=os.environ.get("FIBRE_RECORDER_BITCOIND_PATH", base_config.bitcoind_path),
            pid=int(os.environ["FIBRE_RECORDER_PID"]) if "FIBRE_RECORDER_PID" in os.environ else base_config.pid,
            node_name=os.environ.get("FIBRE_RECORDER_NODE_NAME", base_config.node_name),
            db_path=os.environ.get("FIBRE_RECORDER_DB_PATH", base_config.db_path),
            slow_threshold_ms=float(os.environ.get("FIBRE_RECORDER_SLOW_THRESHOLD_MS", base_config.slow_threshold_ms)),
            very_slow_threshold_ms=float(os.environ.get("FIBRE_RECORDER_VERY_SLOW_THRESHOLD_MS", base_config.very_slow_threshold_ms)),
            pending_timeout_sec=int(os.environ.get("FIBRE_RECORDER_PENDING_TIMEOUT_SEC", base_config.pending_timeout_sec)),
            housekeeping_interval_sec=int(os.environ.get("FIBRE_RECORDER_HOUSEKEEPING_INTERVAL_SEC", base_config.housekeeping_interval_sec)),
            alert_enabled=parse_env_bool(os.environ["FIBRE_RECORDER_ALERT_ENABLED"]) if "FIBRE_RECORDER_ALERT_ENABLED" in os.environ else base_config.alert_enabled,
            alert_webhook_url=os.environ.get("FIBRE_RECORDER_ALERT_WEBHOOK_URL", base_config.alert_webhook_url),
            verbose=parse_env_bool(os.environ["FIBRE_RECORDER_VERBOSE"]) if "FIBRE_RECORDER_VERBOSE" in os.environ else base_config.verbose,
            log_level=os.environ.get("FIBRE_RECORDER_LOG_LEVEL", base_config.log_level),
            log_file=os.environ.get("FIBRE_RECORDER_LOG_FILE", base_config.log_file),
        )


@dataclass
class PendingBlockRecord:
    """Correlated block record waiting to be persisted."""

    block_hash: str
    node_name: str
    first_seen_monotonic: float
    last_updated_monotonic: float
    seen_at_utc: str = field(default_factory=utc_now_iso)
    height: Optional[int] = None
    src_peer: Optional[str] = None
    reconstruction_ms: Optional[float] = None
    chunks_used: Optional[int] = None
    chunks_recvd: Optional[int] = None
    chunk_peer_count: Optional[int] = None
    missing_tx_count: Optional[int] = None
    missing_tx_bytes: Optional[int] = None
    mempool_tx_count: Optional[int] = None
    total_tx_count: Optional[int] = None
    all_tx_from_mempool: Optional[bool] = None
    has_reconstructed: bool = False
    has_detail: bool = False
    partial_flushed: bool = False
    alert_sent: bool = False
    notes: set[str] = field(default_factory=set)

    def add_note(self, note: str) -> None:
        self.notes.add(note)


class SQLiteStore:
    """SQLite persistence for reconstructed block records and alert history."""

    def __init__(self, db_path: str, logger: logging.Logger) -> None:
        self.logger = logger
        self.db_path = db_path
        db_file = Path(db_path)
        if db_file.parent != Path("."):
            db_file.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS block_reconstruction_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_name TEXT NOT NULL,
                seen_at_utc TEXT NOT NULL,
                block_hash TEXT NOT NULL,
                height INTEGER,
                src_peer TEXT,
                reconstruction_ms REAL NOT NULL,
                chunks_used INTEGER,
                chunks_recvd INTEGER,
                chunk_peer_count INTEGER,
                missing_tx_count INTEGER,
                missing_tx_bytes INTEGER,
                mempool_tx_count INTEGER,
                total_tx_count INTEGER,
                all_tx_from_mempool INTEGER,
                chunk_efficiency REAL,
                mempool_hit_ratio REAL,
                missing_tx_ratio REAL,
                severity TEXT,
                alert_sent INTEGER NOT NULL DEFAULT 0,
                alert_sent_at_utc TEXT,
                notes TEXT,
                UNIQUE(node_name, block_hash)
            );

            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_name TEXT NOT NULL,
                block_hash TEXT NOT NULL,
                severity TEXT NOT NULL,
                channel TEXT NOT NULL,
                sent_at_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_block_recon_seen_at
                ON block_reconstruction_events(seen_at_utc);
            CREATE INDEX IF NOT EXISTS idx_block_recon_severity
                ON block_reconstruction_events(severity, seen_at_utc);
            CREATE INDEX IF NOT EXISTS idx_block_recon_height
                ON block_reconstruction_events(height);
            CREATE INDEX IF NOT EXISTS idx_block_recon_node_seen
                ON block_reconstruction_events(node_name, seen_at_utc);
            """
        )
        self.conn.commit()

    def upsert_block_event(self, record: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO block_reconstruction_events (
                node_name, seen_at_utc, block_hash, height, src_peer,
                reconstruction_ms, chunks_used, chunks_recvd, chunk_peer_count,
                missing_tx_count, missing_tx_bytes, mempool_tx_count, total_tx_count,
                all_tx_from_mempool, chunk_efficiency, mempool_hit_ratio,
                missing_tx_ratio, severity, alert_sent, alert_sent_at_utc, notes
            ) VALUES (
                :node_name, :seen_at_utc, :block_hash, :height, :src_peer,
                :reconstruction_ms, :chunks_used, :chunks_recvd, :chunk_peer_count,
                :missing_tx_count, :missing_tx_bytes, :mempool_tx_count, :total_tx_count,
                :all_tx_from_mempool, :chunk_efficiency, :mempool_hit_ratio,
                :missing_tx_ratio, :severity, :alert_sent, :alert_sent_at_utc, :notes
            )
            ON CONFLICT(node_name, block_hash) DO UPDATE SET
                seen_at_utc = excluded.seen_at_utc,
                height = excluded.height,
                src_peer = excluded.src_peer,
                reconstruction_ms = excluded.reconstruction_ms,
                chunks_used = excluded.chunks_used,
                chunks_recvd = excluded.chunks_recvd,
                chunk_peer_count = excluded.chunk_peer_count,
                missing_tx_count = excluded.missing_tx_count,
                missing_tx_bytes = excluded.missing_tx_bytes,
                mempool_tx_count = excluded.mempool_tx_count,
                total_tx_count = excluded.total_tx_count,
                all_tx_from_mempool = excluded.all_tx_from_mempool,
                chunk_efficiency = excluded.chunk_efficiency,
                mempool_hit_ratio = excluded.mempool_hit_ratio,
                missing_tx_ratio = excluded.missing_tx_ratio,
                severity = excluded.severity,
                alert_sent = CASE
                    WHEN block_reconstruction_events.alert_sent = 1 THEN 1
                    ELSE excluded.alert_sent
                END,
                alert_sent_at_utc = COALESCE(
                    block_reconstruction_events.alert_sent_at_utc,
                    excluded.alert_sent_at_utc
                ),
                notes = excluded.notes
            """,
            record,
        )
        self.conn.commit()

    def update_alert_status(
        self,
        node_name: str,
        block_hash: str,
        alert_sent: bool,
        alert_sent_at_utc: Optional[str],
    ) -> None:
        self.conn.execute(
            """
            UPDATE block_reconstruction_events
            SET alert_sent = ?, alert_sent_at_utc = ?
            WHERE node_name = ? AND block_hash = ?
            """,
            (int(alert_sent), alert_sent_at_utc, node_name, block_hash),
        )
        self.conn.commit()

    def was_alert_sent(self, node_name: str, block_hash: str) -> bool:
        row = self.conn.execute(
            """
            SELECT alert_sent
            FROM block_reconstruction_events
            WHERE node_name = ? AND block_hash = ?
            """,
            (node_name, block_hash),
        ).fetchone()
        return bool(row and row["alert_sent"])

    def insert_alert_history(
        self,
        node_name: str,
        block_hash: str,
        severity: str,
        channel: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO alert_history (
                node_name, block_hash, severity, channel, sent_at_utc, status, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (node_name, block_hash, severity, channel, utc_now_iso(), status, error_message),
        )
        self.conn.commit()

    def query_events(
        self,
        limit: int = 20,
        only_slow: bool = False,
        block_hash: Optional[str] = None,
        since_hours: Optional[float] = None,
    ) -> list[sqlite3.Row]:
        sql = [
            """
            SELECT seen_at_utc, node_name, height, block_hash, reconstruction_ms,
                   severity, src_peer, chunks_used, chunks_recvd, chunk_peer_count,
                   missing_tx_count, missing_tx_bytes, mempool_tx_count, total_tx_count,
                   all_tx_from_mempool, chunk_efficiency, mempool_hit_ratio,
                   missing_tx_ratio, alert_sent, alert_sent_at_utc, notes
            FROM block_reconstruction_events
            WHERE 1=1
            """
        ]
        params: list[Any] = []

        if block_hash:
            sql.append("AND block_hash = ?")
            params.append(block_hash)
        if only_slow:
            sql.append("AND severity IS NOT NULL")
        if since_hours is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).replace(microsecond=0)
            sql.append("AND seen_at_utc >= ?")
            params.append(cutoff.isoformat().replace("+00:00", "Z"))

        sql.append("ORDER BY seen_at_utc DESC")
        sql.append("LIMIT ?")
        params.append(limit)

        return self.conn.execute("\n".join(sql), params).fetchall()

    def close(self) -> None:
        self.conn.close()


BPF_PROGRAM = """
#include <uapi/linux/ptrace.h>

BPF_PERF_OUTPUT(events);

enum event_type {
    EVENT_BLOCK_RECONSTRUCTED = 1,
    EVENT_BLOCK_RECONSTRUCTION_DETAIL = 2,
};

struct event_t {
    u32 type;
    s64 duration_us;
    u32 chunks_used;
    u32 chunks_recvd;
    u32 chunk_peer_count;
    s32 height;
    u32 missing_tx_count;
    u64 missing_tx_bytes;
    u32 mempool_tx_count;
    u32 total_tx_count;
    u32 all_tx_from_mempool;
    char block_hash[80];
    char src_peer[96];
};

int trace_block_reconstructed(struct pt_regs *ctx) {
    struct event_t event = {};
    event.type = EVENT_BLOCK_RECONSTRUCTED;

    u64 hash_ptr;
    bpf_usdt_readarg(1, ctx, &hash_ptr);
    bpf_probe_read_user_str(&event.block_hash, sizeof(event.block_hash), (void *)hash_ptr);

    u64 peer_ptr;
    bpf_usdt_readarg(2, ctx, &peer_ptr);
    bpf_probe_read_user_str(&event.src_peer, sizeof(event.src_peer), (void *)peer_ptr);

    bpf_usdt_readarg(3, ctx, &event.chunks_used);
    bpf_usdt_readarg(4, ctx, &event.chunks_recvd);
    bpf_usdt_readarg(5, ctx, &event.chunk_peer_count);
    bpf_usdt_readarg(6, ctx, &event.duration_us);
    bpf_usdt_readarg(7, ctx, &event.height);

    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

int trace_block_reconstruction_detail(struct pt_regs *ctx) {
    struct event_t event = {};
    event.type = EVENT_BLOCK_RECONSTRUCTION_DETAIL;

    u64 hash_ptr;
    bpf_usdt_readarg(1, ctx, &hash_ptr);
    bpf_probe_read_user_str(&event.block_hash, sizeof(event.block_hash), (void *)hash_ptr);

    bpf_usdt_readarg(2, ctx, &event.height);
    bpf_usdt_readarg(3, ctx, &event.missing_tx_count);
    bpf_usdt_readarg(4, ctx, &event.missing_tx_bytes);
    bpf_usdt_readarg(5, ctx, &event.mempool_tx_count);
    bpf_usdt_readarg(6, ctx, &event.total_tx_count);
    bpf_usdt_readarg(7, ctx, &event.all_tx_from_mempool);

    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}
"""


class SlowBlockRecorder:
    """Record reconstructed blocks into SQLite and alert on slow thresholds."""

    def __init__(self, config: RecorderConfig) -> None:
        self.config = config
        self.logger = setup_logging(config.log_level, config.log_file)
        self.store = SQLiteStore(config.db_path, self.logger)
        self.bpf: Optional[Any] = None
        self._running = False
        self._pid_explicit = config.pid is not None
        self._attached_pid: Optional[int] = None
        self._last_pid_check = 0.0
        self._last_housekeeping = 0.0
        self._pending: dict[str, PendingBlockRecord] = {}

    def _pid_matches_bitcoind(self, pid: int) -> bool:
        try:
            proc_exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return False
        return os.path.realpath(self.config.bitcoind_path) == os.path.realpath(proc_exe)

    def _verify_binary_path(self, pid: int) -> None:
        try:
            proc_exe = os.readlink(f"/proc/{pid}/exe")
            if os.path.realpath(self.config.bitcoind_path) != os.path.realpath(proc_exe):
                self.logger.warning(
                    "Binary path mismatch! --bitcoind=%s but /proc/%d/exe -> %s",
                    self.config.bitcoind_path,
                    pid,
                    proc_exe,
                )
            else:
                self.logger.info("Binary path verified: matches /proc/%d/exe", pid)
        except OSError as e:
            self.logger.warning("Could not verify binary path: %s", e)

    def _resolve_target_pid(self) -> Optional[int]:
        if self._pid_explicit:
            return self.config.pid if self.config.pid and self._pid_matches_bitcoind(self.config.pid) else None
        return find_bitcoind_pid(self.config.bitcoind_path)

    def _detach_probes(self) -> None:
        if self.bpf is not None:
            try:
                self.bpf.cleanup()
            except Exception as e:
                self.logger.warning("Failed to clean up BPF resources: %s", e)
            finally:
                self.bpf = None

        if hasattr(self, "_usdt"):
            try:
                del self._usdt
            except Exception as e:
                self.logger.warning("Failed to release USDT context: %s", e)

    def _attach_probes(self) -> int:
        from bcc import BPF, USDT

        self._usdt = USDT(path=self.config.bitcoind_path, pid=self.config.pid)
        usdt = self._usdt

        probes = [
            ("block_reconstructed", "trace_block_reconstructed", "udp"),
            ("block_reconstruction_detail", "trace_block_reconstruction_detail", "udp"),
        ]

        attached_count = 0
        for probe_name, fn_name, provider in probes:
            try:
                usdt.enable_probe(probe=probe_name, fn_name=fn_name)
                self.logger.info("Attached probe: %s:%s", provider, probe_name)
                attached_count += 1
            except Exception as e:
                self.logger.warning("Failed to attach probe %s:%s: %s", provider, probe_name, e)

        if attached_count == 0:
            return 0

        self.bpf = BPF(text=BPF_PROGRAM, usdt_contexts=[usdt])
        self.bpf["events"].open_perf_buffer(self._handle_event)
        return attached_count

    def _reattach_probes(self, pid: int) -> bool:
        old_pid = self._attached_pid
        if old_pid == pid and self.bpf is not None:
            return True

        self._detach_probes()
        self.config.pid = pid
        self._verify_binary_path(pid)

        attached_count = self._attach_probes()
        if attached_count == 0:
            self._attached_pid = None
            return False

        self._attached_pid = pid
        if old_pid is None:
            self.logger.info("Attached %d/%d probes successfully", attached_count, 2)
        else:
            self.logger.info(
                "Reattached %d/%d probes successfully after PID change %s -> %s",
                attached_count,
                2,
                old_pid,
                pid,
            )
        return True

    def _check_pid_change(self) -> None:
        now = time.monotonic()
        if now - self._last_pid_check < 5:
            return
        self._last_pid_check = now

        target_pid = self._resolve_target_pid()
        if target_pid == self._attached_pid:
            return

        if self._pid_explicit:
            if target_pid is None and self._attached_pid is not None:
                self.logger.warning(
                    "Configured bitcoind PID %s is no longer valid; automatic reattach is disabled when --pid/FIBRE_RECORDER_PID is set",
                    self._attached_pid,
                )
                self._detach_probes()
                self._attached_pid = None
            return

        if target_pid is None:
            if self._attached_pid is not None:
                self.logger.warning(
                    "Lost attached bitcoind PID %s; waiting for a new matching process",
                    self._attached_pid,
                )
                self._detach_probes()
                self._attached_pid = None
            return

        self.logger.info("Detected bitcoind PID change: %s -> %s", self._attached_pid, target_pid)
        if not self._reattach_probes(target_pid):
            self.logger.error("Failed to attach probes to replacement bitcoind PID %s", target_pid)

    def _get_pending(self, block_hash: str) -> PendingBlockRecord:
        now = time.monotonic()
        pending = self._pending.get(block_hash)
        if pending is None:
            pending = PendingBlockRecord(
                block_hash=block_hash,
                node_name=self.config.node_name,
                first_seen_monotonic=now,
                last_updated_monotonic=now,
            )
            self._pending[block_hash] = pending
        else:
            pending.last_updated_monotonic = now
        return pending

    def _handle_event(self, cpu: int, data: Any, size: int) -> None:
        try:
            event = self.bpf["events"].event(data)
            if event.type == 1:
                self._handle_block_reconstructed(event)
            elif event.type == 2:
                self._handle_block_reconstruction_detail(event)
        except Exception as e:
            self.logger.error("Error processing event: %s", e)

    def _handle_block_reconstructed(self, event: Any) -> None:
        block_hash = decode_c_string(event.block_hash)
        if not block_hash:
            self.logger.warning("Received block_reconstructed event with empty block hash")
            return

        pending = self._get_pending(block_hash)
        pending.has_reconstructed = True
        pending.src_peer = decode_c_string(event.src_peer) or pending.src_peer
        pending.reconstruction_ms = event.duration_us / 1000.0
        pending.chunks_used = int(event.chunks_used)
        pending.chunks_recvd = int(event.chunks_recvd)
        pending.chunk_peer_count = int(event.chunk_peer_count)
        if event.height > 0:
            pending.height = int(event.height)

        if self.config.verbose:
            self.logger.info(
                "Reconstructed block=%s height=%s duration_ms=%.3f src_peer=%s",
                block_hash,
                pending.height,
                pending.reconstruction_ms,
                pending.src_peer or "n/a",
            )

        self._maybe_finalize(block_hash)

    def _handle_block_reconstruction_detail(self, event: Any) -> None:
        block_hash = decode_c_string(event.block_hash)
        if not block_hash:
            self.logger.warning("Received block_reconstruction_detail event with empty block hash")
            return

        pending = self._get_pending(block_hash)
        pending.has_detail = True
        if event.height > 0:
            height = int(event.height)
            if pending.height is not None and pending.height != height:
                pending.add_note("height_mismatch_between_probes")
            pending.height = height
        pending.missing_tx_count = int(event.missing_tx_count)
        pending.missing_tx_bytes = int(event.missing_tx_bytes)
        pending.mempool_tx_count = int(event.mempool_tx_count)
        pending.total_tx_count = int(event.total_tx_count)
        pending.all_tx_from_mempool = bool(event.all_tx_from_mempool)

        if self.config.verbose:
            self.logger.info(
                "Reconstruction detail block=%s missing_tx_count=%s missing_tx_bytes=%s all_tx_from_mempool=%s",
                block_hash,
                pending.missing_tx_count,
                pending.missing_tx_bytes,
                pending.all_tx_from_mempool,
            )

        self._maybe_finalize(block_hash)

    def _classify_severity(self, reconstruction_ms: Optional[float]) -> Optional[str]:
        if reconstruction_ms is None:
            return None
        if reconstruction_ms >= self.config.very_slow_threshold_ms:
            return "very_slow"
        if reconstruction_ms >= self.config.slow_threshold_ms:
            return "slow"
        return None

    def _build_record_payload(self, pending: PendingBlockRecord) -> dict[str, Any]:
        chunk_efficiency = None
        if pending.chunks_recvd:
            chunk_efficiency = pending.chunks_used / pending.chunks_recvd

        mempool_hit_ratio = None
        if pending.total_tx_count:
            mempool_hit_ratio = pending.mempool_tx_count / pending.total_tx_count if pending.mempool_tx_count is not None else None

        missing_tx_ratio = None
        if pending.total_tx_count:
            missing_tx_ratio = pending.missing_tx_count / pending.total_tx_count if pending.missing_tx_count is not None else None

        return {
            "node_name": pending.node_name,
            "seen_at_utc": pending.seen_at_utc,
            "block_hash": pending.block_hash,
            "height": pending.height,
            "src_peer": pending.src_peer,
            "reconstruction_ms": pending.reconstruction_ms,
            "chunks_used": pending.chunks_used,
            "chunks_recvd": pending.chunks_recvd,
            "chunk_peer_count": pending.chunk_peer_count,
            "missing_tx_count": pending.missing_tx_count,
            "missing_tx_bytes": pending.missing_tx_bytes,
            "mempool_tx_count": pending.mempool_tx_count,
            "total_tx_count": pending.total_tx_count,
            "all_tx_from_mempool": None if pending.all_tx_from_mempool is None else int(pending.all_tx_from_mempool),
            "chunk_efficiency": chunk_efficiency,
            "mempool_hit_ratio": mempool_hit_ratio,
            "missing_tx_ratio": missing_tx_ratio,
            "severity": self._classify_severity(pending.reconstruction_ms),
            "alert_sent": int(pending.alert_sent),
            "alert_sent_at_utc": None,
            "notes": ",".join(sorted(pending.notes)) if pending.notes else None,
        }

    def _send_webhook_alert(self, payload: dict[str, Any]) -> tuple[bool, Optional[str]]:
        if not self.config.alert_webhook_url:
            return True, None

        request = urlrequest.Request(
            self.config.alert_webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                if 200 <= response.status < 300:
                    return True, None
                return False, f"unexpected_status_{response.status}"
        except urlerror.URLError as e:
            return False, str(e)

    def _maybe_alert(self, record: dict[str, Any], pending: PendingBlockRecord) -> Optional[str]:
        severity = record["severity"]
        if severity is None or not self.config.alert_enabled:
            return None

        if pending.alert_sent or self.store.was_alert_sent(record["node_name"], record["block_hash"]):
            pending.alert_sent = True
            return "already_sent"

        payload = {
            "event_type": "slow_block_reconstruction",
            "severity": severity,
            "seen_at_utc": record["seen_at_utc"],
            "node_name": record["node_name"],
            "block_hash": record["block_hash"],
            "height": record["height"],
            "src_peer": record["src_peer"],
            "reconstruction_ms": record["reconstruction_ms"],
            "chunks_used": record["chunks_used"],
            "chunks_recvd": record["chunks_recvd"],
            "chunk_peer_count": record["chunk_peer_count"],
            "missing_tx_count": record["missing_tx_count"],
            "missing_tx_bytes": record["missing_tx_bytes"],
            "mempool_tx_count": record["mempool_tx_count"],
            "total_tx_count": record["total_tx_count"],
            "all_tx_from_mempool": bool(record["all_tx_from_mempool"]) if record["all_tx_from_mempool"] is not None else None,
            "chunk_efficiency": record["chunk_efficiency"],
            "mempool_hit_ratio": record["mempool_hit_ratio"],
            "missing_tx_ratio": record["missing_tx_ratio"],
            "notes": record["notes"],
        }

        self.logger.warning("slow_block_alert %s", json.dumps(payload, sort_keys=True))
        self.store.insert_alert_history(record["node_name"], record["block_hash"], severity, "local_log", "success")

        webhook_ok, webhook_error = self._send_webhook_alert(payload)
        if self.config.alert_webhook_url:
            self.store.insert_alert_history(
                record["node_name"],
                record["block_hash"],
                severity,
                "webhook",
                "success" if webhook_ok else "failure",
                webhook_error,
            )
            if not webhook_ok:
                self.logger.error(
                    "Webhook alert failed for block=%s severity=%s: %s",
                    record["block_hash"],
                    severity,
                    webhook_error,
                )

        pending.alert_sent = True
        return "sent_now"

    def _persist_record(self, pending: PendingBlockRecord) -> None:
        if pending.reconstruction_ms is None:
            return

        record = self._build_record_payload(pending)
        self.store.upsert_block_event(record)

        alert_state = self._maybe_alert(record, pending)
        if alert_state == "sent_now":
            alert_time = utc_now_iso()
            self.store.update_alert_status(record["node_name"], record["block_hash"], True, alert_time)

    def _maybe_finalize(self, block_hash: str) -> None:
        pending = self._pending.get(block_hash)
        if pending is None:
            return

        if pending.has_reconstructed and pending.has_detail:
            self._persist_record(pending)
            del self._pending[block_hash]

    def _flush_timeouts(self) -> None:
        now = time.monotonic()
        timeout = self.config.pending_timeout_sec
        drop_age = max(timeout * 10, timeout + 300)
        expired: list[str] = []

        for block_hash, pending in list(self._pending.items()):
            age = now - pending.first_seen_monotonic
            if pending.has_reconstructed and not pending.has_detail and age >= timeout and not pending.partial_flushed:
                pending.add_note("partial_record_timeout")
                self._persist_record(pending)
                pending.partial_flushed = True
                self.logger.warning(
                    "Persisted partial record after timeout block=%s age_sec=%.1f",
                    block_hash,
                    age,
                )

            if not pending.has_reconstructed and age >= timeout:
                self.logger.warning(
                    "Dropping pending detail-only record block=%s after timeout age_sec=%.1f",
                    block_hash,
                    age,
                )
                expired.append(block_hash)
                continue

            if age >= drop_age:
                expired.append(block_hash)

        for block_hash in expired:
            self._pending.pop(block_hash, None)

    def run(self) -> None:
        self.logger.info("FIBRE Slow Block Recorder v%s", __version__)
        self.logger.info(
            "Configuration: bitcoind=%s node=%s db=%s slow_threshold_ms=%.1f very_slow_threshold_ms=%.1f",
            self.config.bitcoind_path,
            self.config.node_name,
            self.config.db_path,
            self.config.slow_threshold_ms,
            self.config.very_slow_threshold_ms,
        )

        if self.config.pid is None:
            self.logger.info("PID not specified, attempting auto-detection...")
            detected_pid = self._resolve_target_pid()
            if detected_pid:
                self.config.pid = detected_pid
                self.logger.info("Auto-detected bitcoind PID: %s", detected_pid)
            else:
                self.logger.error("Could not auto-detect bitcoind PID")
                self.logger.error(
                    "Possible causes: no bitcoind running, or multiple instances of the same binary (use --pid to select one)"
                )
                sys.exit(1)

        if not self._reattach_probes(self.config.pid):
            self.logger.error("No USDT probes could be attached")
            self.logger.error("Possible causes:")
            self.logger.error("  - bitcoind was not compiled with USDT tracepoint support")
            self.logger.error("  - The specified PID is not a bitcoind process")
            self.logger.error("  - Insufficient permissions (try running with sudo)")
            sys.exit(1)

        self.logger.info("Waiting for FIBRE reconstruction events...")
        self._running = True
        try:
            while self._running:
                self._check_pid_change()
                now = time.monotonic()
                if now - self._last_housekeeping >= self.config.housekeeping_interval_sec:
                    self._flush_timeouts()
                    self._last_housekeeping = now
                if self.bpf is None:
                    time.sleep(1)
                    continue
                self.bpf.perf_buffer_poll(timeout=1000)
        except KeyboardInterrupt:
            pass
        finally:
            self._flush_timeouts()
            self._detach_probes()
            self.store.close()
            self.logger.info("Shutting down...")


def print_records(rows: list[sqlite3.Row]) -> None:
    if not rows:
        print("No matching records found.")
        return

    for idx, row in enumerate(rows, start=1):
        print(
            f"[{idx}] {row['seen_at_utc']} node={row['node_name']} "
            f"severity={row['severity'] or 'normal'} height={row['height'] or 'n/a'} "
            f"reconstruction_ms={row['reconstruction_ms']:.3f}"
        )
        print(f"    block_hash={row['block_hash']}")
        print(f"    src_peer={row['src_peer'] or 'n/a'}")
        print(
            f"    chunks_used={row['chunks_used'] if row['chunks_used'] is not None else 'n/a'} "
            f"chunks_recvd={row['chunks_recvd'] if row['chunks_recvd'] is not None else 'n/a'} "
            f"chunk_peer_count={row['chunk_peer_count'] if row['chunk_peer_count'] is not None else 'n/a'}"
        )
        print(
            f"    missing_tx_count={row['missing_tx_count'] if row['missing_tx_count'] is not None else 'n/a'} "
            f"missing_tx_bytes={row['missing_tx_bytes'] if row['missing_tx_bytes'] is not None else 'n/a'}"
        )
        print(
            f"    mempool_tx_count={row['mempool_tx_count'] if row['mempool_tx_count'] is not None else 'n/a'} "
            f"total_tx_count={row['total_tx_count'] if row['total_tx_count'] is not None else 'n/a'} "
            f"all_tx_from_mempool={bool(row['all_tx_from_mempool']) if row['all_tx_from_mempool'] is not None else 'n/a'}"
        )
        print(
            f"    chunk_efficiency={row['chunk_efficiency'] if row['chunk_efficiency'] is not None else 'n/a'} "
            f"mempool_hit_ratio={row['mempool_hit_ratio'] if row['mempool_hit_ratio'] is not None else 'n/a'} "
            f"missing_tx_ratio={row['missing_tx_ratio'] if row['missing_tx_ratio'] is not None else 'n/a'}"
        )
        print(
            f"    alert_sent={bool(row['alert_sent'])} "
            f"alert_sent_at_utc={row['alert_sent_at_utc'] or 'n/a'} "
            f"notes={row['notes'] or 'n/a'}"
        )


def run_query_mode(config: RecorderConfig, args: argparse.Namespace) -> None:
    logger = setup_logging(config.log_level, config.log_file)
    store = SQLiteStore(config.db_path, logger)
    try:
        limit = args.show_last if args.show_last is not None else 20
        rows = store.query_events(
            limit=limit,
            only_slow=args.show_slow,
            block_hash=args.show_block,
            since_hours=args.since_hours,
        )
        print_records(rows)
    finally:
        store.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FIBRE slow block recorder with SQLite persistence and threshold alerts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables:
  FIBRE_RECORDER_BITCOIND_PATH           Path to bitcoind binary
  FIBRE_RECORDER_PID                     PID of running bitcoind
  FIBRE_RECORDER_NODE_NAME               Node name label
  FIBRE_RECORDER_DB_PATH                 SQLite database path
  FIBRE_RECORDER_SLOW_THRESHOLD_MS       Slow threshold in milliseconds
  FIBRE_RECORDER_VERY_SLOW_THRESHOLD_MS  Very slow threshold in milliseconds
  FIBRE_RECORDER_PENDING_TIMEOUT_SEC     Timeout for partial records
  FIBRE_RECORDER_HOUSEKEEPING_INTERVAL_SEC  Housekeeping interval in seconds
  FIBRE_RECORDER_ALERT_ENABLED           Enable alerting (true/false)
  FIBRE_RECORDER_ALERT_WEBHOOK_URL       Webhook URL for alert delivery
  FIBRE_RECORDER_VERBOSE                 Enable verbose logging (true/false)
  FIBRE_RECORDER_LOG_LEVEL               Log level (DEBUG, INFO, WARNING, ERROR)
  FIBRE_RECORDER_LOG_FILE                Log file path

Examples:
  %(prog)s --bitcoind /usr/local/bin/bitcoind --pid 12345 --node-name node2 --db /var/lib/fibre/node2.sqlite
  %(prog)s --db ./slow-blocks.sqlite --show-last 20 --show-slow
  %(prog)s --config /etc/fibre-monitoring/slow-block-recorder.yaml --show-block 000000...
""",
    )

    parser.add_argument("--bitcoind", "-b", help="Path to bitcoind binary")
    parser.add_argument("--pid", "-p", type=int, help="PID of running bitcoind (optional, auto-detected)")
    parser.add_argument("--node-name", "-n", default="localhost", help="Node name label (default: localhost)")
    parser.add_argument("--db", default="slow-blocks.sqlite", help="SQLite database path (default: slow-blocks.sqlite)")
    parser.add_argument("--config", "-c", type=Path, help="Path to YAML config file")
    parser.add_argument("--slow-threshold-ms", type=float, default=50.0, help="Slow threshold in milliseconds (default: 50)")
    parser.add_argument("--very-slow-threshold-ms", type=float, default=100.0, help="Very slow threshold in milliseconds (default: 100)")
    parser.add_argument("--pending-timeout-sec", type=int, default=60, help="Timeout before persisting partial records (default: 60)")
    parser.add_argument("--housekeeping-interval-sec", type=int, default=5, help="Housekeeping interval in seconds (default: 5)")
    parser.add_argument("--alert-webhook-url", help="Webhook URL for alert delivery")
    parser.add_argument("--disable-alerts", action="store_true", help="Disable alerting")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument("--log-file", help="Path to log file (logs to both stdout and file)")

    parser.add_argument("--show-last", type=int, help="Show the most recent N records from SQLite")
    parser.add_argument("--show-block", help="Show a specific block hash from SQLite")
    parser.add_argument("--show-slow", action="store_true", help="Only show rows with a non-null severity")
    parser.add_argument("--since-hours", type=float, help="Only show rows newer than the last N hours")

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.config and args.config.exists():
        config = RecorderConfig.from_yaml(args.config)
        config = RecorderConfig.from_env(config)
    else:
        config = RecorderConfig.from_args(args)
        config = RecorderConfig.from_env(config)

    if args.bitcoind:
        config.bitcoind_path = args.bitcoind
    if args.pid:
        config.pid = args.pid
    if args.node_name != "localhost":
        config.node_name = args.node_name
    if args.db != "slow-blocks.sqlite":
        config.db_path = args.db
    if args.slow_threshold_ms != 50.0:
        config.slow_threshold_ms = args.slow_threshold_ms
    if args.very_slow_threshold_ms != 100.0:
        config.very_slow_threshold_ms = args.very_slow_threshold_ms
    if args.pending_timeout_sec != 60:
        config.pending_timeout_sec = args.pending_timeout_sec
    if args.housekeeping_interval_sec != 5:
        config.housekeeping_interval_sec = args.housekeeping_interval_sec
    if args.alert_webhook_url:
        config.alert_webhook_url = args.alert_webhook_url
    if args.disable_alerts:
        config.alert_enabled = False
    if args.verbose:
        config.verbose = True
    if args.log_level != "INFO":
        config.log_level = args.log_level
    if args.log_file:
        config.log_file = args.log_file

    query_mode = bool(args.show_last is not None or args.show_block or args.show_slow)
    if query_mode:
        run_query_mode(config, args)
        return

    if not config.bitcoind_path:
        print("Error: bitcoind path is required", file=sys.stderr)
        print(
            "Provide via --bitcoind, config file, or FIBRE_RECORDER_BITCOIND_PATH env var",
            file=sys.stderr,
        )
        sys.exit(1)

    if config.very_slow_threshold_ms < config.slow_threshold_ms:
        print("Error: very slow threshold must be >= slow threshold", file=sys.stderr)
        sys.exit(1)

    recorder = SlowBlockRecorder(config)
    recorder.run()


if __name__ == "__main__":
    main()
