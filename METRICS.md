# Metrics Reference

This document describes all Prometheus metrics exposed by the FIBRE exporter, the USDT tracepoints they originate from, and how to query them.

## How Metrics Are Collected

The exporter attaches to USDT (Userland Statically Defined Tracing) probes compiled into the bitcoind binary using eBPF. When bitcoind hits a tracepoint, the eBPF program captures the event data in kernel space and forwards it to the exporter, which updates Prometheus counters, gauges, and histograms.

```
bitcoind tracepoint → eBPF program → perf buffer → Python callback → Prometheus metric
```

Five of the eight available USDT probes are used:

| USDT Probe | Provider | Source File | Description |
|---|---|---|---|
| `block_reconstructed` | `udp` | `udprelay.cpp` | Block fully reconstructed from FEC chunks |
| `block_reconstruction_detail` | `udp` | `udprelay.cpp` | Per-block missing transaction and mempool reconstruction details |
| `block_send_start` | `udp` | `udprelay.cpp` | Block relay started via UDP |
| `block_race_winner` | `udp` | `fibrerace.cpp` | Which mechanism (FIBRE/UDP vs compact block) delivered the block first |
| `block_connected` | `validation` | `validation.cpp` | Block connected to the chain (standard Bitcoin Core probe) |

Unused probes: `udp:block_header_chunk` (internal timing breakdown), `udp:block_coinbase` (coinbase script extraction), `udp:block_race_time` (detailed path timing comparison).

## Labels

All metrics use at least the `node` label, set by `--node-name` at exporter startup. This identifies which bitcoind instance the metric came from in multi-node setups.

| Label | Metrics | Values | Description |
|---|---|---|---|
| `node` | All FIBRE and block metrics | User-configured name | Identifies the bitcoind instance |
| `mechanism` | `fibre_block_deliveries_total` | `fibre_udp`, `bip152_cmpct`, `other` | How the block was delivered |
| `peer` | `fibre_block_deliveries_total` | IP:port string | Which peer delivered the block |
| `event_type` | `fibre_exporter_events_processed_total` | `block_reconstructed`, `block_reconstruction_detail`, `block_send_start`, `block_delivery`, `block_connected` | Type of BPF event |
| `error_type` | `fibre_exporter_errors_total` | `event_processing` | Category of error |

## FIBRE Block Relay Metrics

These metrics come from the FIBRE-specific UDP probes and only fire when blocks are received or sent via the FIBRE/UDP relay protocol.

### `fibre_blocks_reconstructed_total`

- **Type:** Counter
- **Labels:** `node`
- **USDT probe:** `udp:block_reconstructed`

Incremented each time a block is fully reconstructed from FEC-encoded UDP chunks. This is the primary indicator that FIBRE relay is working — if this counter is not increasing, blocks are arriving via other paths (compact blocks, full block download).

### `fibre_block_reconstruction_duration_seconds`

- **Type:** Histogram
- **Labels:** `node`
- **Buckets:** 1ms, 5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms, 1s, 2.5s, 5s, 10s
- **USDT probe:** `udp:block_reconstructed` (argument 6: `duration_us`)

Time from receiving the first header packet to completing block reconstruction. This measures the end-to-end FIBRE relay latency. Typical values on a healthy FIBRE connection are under 100ms. Values over 1s suggest network issues or high chunk loss requiring extra FEC recovery.

### `fibre_block_chunks_used`

- **Type:** Histogram
- **Labels:** `node`
- **Buckets:** 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000
- **USDT probe:** `udp:block_reconstructed` (argument 3: `chunks_used`)

Number of FEC chunks needed to reconstruct each block. A block is split into chunks for UDP relay; the FEC encoding means you only need a subset to reconstruct. Lower values relative to `chunks_received` indicate good network conditions.

### `fibre_block_missing_tx_count`

- **Type:** Histogram
- **Labels:** `node`
- **Buckets:** 0, 1, 2, 5, 10, 20, 50, 100, 250, 500, 1000, 2000, 5000, 10000
- **USDT probe:** `udp:block_reconstruction_detail` (argument 3: `missing_tx_count`)

Number of non-prefilled block transactions that were not locally available at reconstruction start and therefore had to be recovered from the FIBRE payload rather than the local mempool. Higher values generally indicate less overlap between the node's mempool and the relayed block contents.

### `fibre_block_missing_tx_bytes`

- **Type:** Histogram
- **Labels:** `node`
- **Buckets:** 100B, 500B, 1KB, 5KB, 10KB, 50KB, 100KB, 250KB, 500KB, 1MB, 2MB, 4MB
- **USDT probe:** `udp:block_reconstruction_detail` (argument 4: `missing_tx_bytes`)

Total serialized size of the transactions that were not locally available at reconstruction start. This is often more explanatory than `fibre_block_missing_tx_count`, because a small number of large missing transactions may contribute more to reconstruction work than many tiny ones.

### `fibre_block_mempool_tx_count`

- **Type:** Histogram
- **Labels:** `node`
- **Buckets:** 0, 1, 10, 50, 100, 250, 500, 1000, 2000, 3000, 5000, 10000
- **USDT probe:** `udp:block_reconstruction_detail` (argument 5: `mempool_tx_count`)

Number of transactions satisfied from the local mempool during FIBRE reconstruction. Higher values indicate better local transaction availability and less dependence on the relayed transaction payload.

### `fibre_block_all_tx_from_mempool_total`

- **Type:** Counter
- **Labels:** `node`
- **USDT probe:** `udp:block_reconstruction_detail` (argument 7: `all_tx_from_mempool`)

Incremented when all non-prefilled transactions in a FIBRE-reconstructed block were already available from the local mempool. This is a strong signal that the node's mempool was already well aligned with the block being relayed.

### `fibre_chunks_used_total`

- **Type:** Counter
- **Labels:** `node`
- **USDT probe:** `udp:block_reconstructed` (argument 3: `chunks_used`)

Running total of all FEC chunks used across all block reconstructions. Compare with `fibre_chunks_received_total` to calculate overall chunk efficiency.

### `fibre_chunks_received_total`

- **Type:** Counter
- **Labels:** `node`
- **USDT probe:** `udp:block_reconstructed` (argument 4: `chunks_recvd`)

Running total of all FEC chunks received. The ratio `fibre_chunks_used_total / fibre_chunks_received_total` gives the chunk efficiency — values near 1.0 mean almost no redundant chunks, while lower values indicate more FEC overhead being used to compensate for packet loss.

### `fibre_blocks_sent_total`

- **Type:** Counter
- **Labels:** `node`
- **USDT probe:** `udp:block_send_start`

Incremented each time the node begins relaying a block via UDP to its FIBRE peers. Only relevant for nodes that act as FIBRE relay senders.

### `fibre_block_deliveries_total`

- **Type:** Counter
- **Labels:** `node`, `mechanism`, `peer`
- **USDT probe:** `udp:block_race_winner`

Records which peer delivered each block and via which mechanism. The `mechanism` label is derived from the race winner string reported by the probe:

| `mechanism` value | Meaning |
|---|---|
| `fibre_udp` | Block arrived first via FIBRE/UDP relay |
| `bip152_cmpct` | Block arrived first via BIP 152 compact blocks |
| `other` | Unknown or other delivery mechanism |

This metric is key for understanding whether FIBRE is actually winning the relay race against compact blocks.

### `fibre_last_block_height`

- **Type:** Gauge
- **Labels:** `node`
- **USDT probes:** `udp:block_race_winner` and `validation:block_connected`

The height of the most recently processed block. Updated by both the delivery and connection events. Useful for verifying the node is synced and processing new blocks.

## Block Connection Metrics

These come from the standard Bitcoin Core `validation:block_connected` probe and fire for **every** block connected to the chain, regardless of how it was delivered (FIBRE, compact blocks, full block download, IBD). They provide baseline block processing data even when FIBRE-specific probes are not firing.

### `bitcoin_blocks_connected_total`

- **Type:** Counter
- **Labels:** `node`
- **USDT probe:** `validation:block_connected`

Total blocks connected to the chain. Unlike `fibre_blocks_reconstructed_total` (which only counts FIBRE-delivered blocks), this counter increments for every block.

### `bitcoin_block_connection_duration_seconds`

- **Type:** Histogram
- **Labels:** `node`
- **Buckets:** 1ms, 5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms, 1s, 2.5s, 5s, 10s
- **USDT probe:** `validation:block_connected` (argument 6: `connection_time_ns`)

Time to connect a block to the chain (UTXO set updates, script validation). This measures the validation cost, not network latency. Large blocks with many transactions take longer. Note: the probe reports nanoseconds; the exporter converts to seconds.

### `bitcoin_block_tx_count`

- **Type:** Histogram
- **Labels:** `node`
- **Buckets:** 1, 10, 50, 100, 250, 500, 1000, 2000, 3000, 5000, 10000
- **USDT probe:** `validation:block_connected` (argument 3: `tx_count`)

Number of transactions in each connected block. Correlates with `bitcoin_block_connection_duration_seconds` — blocks with more transactions take longer to validate.

## Exporter Self-Monitoring Metrics

These are internal metrics about the exporter process itself, not derived from USDT probes.

| Metric | Type | Description |
|---|---|---|
| `fibre_exporter_up` | Gauge | `1` if the exporter is running, `0` on shutdown |
| `fibre_exporter_start_time_seconds` | Gauge | Unix timestamp when the exporter started |
| `fibre_exporter_events_processed_total` | Counter | Events processed, by `event_type` label |
| `fibre_exporter_errors_total` | Counter | Errors encountered, by `error_type` label |
| `fibre_exporter_probes_attached` | Gauge | Number of USDT probes successfully attached (out of 5) |
| `fibre_exporter_info` | Info | Exporter version, node name, and bitcoind path |

## Example PromQL Queries

**FIBRE relay win rate** — fraction of blocks delivered by FIBRE vs compact blocks:
```promql
sum(rate(fibre_block_deliveries_total{mechanism="fibre_udp"}[1h]))
/
sum(rate(fibre_block_deliveries_total[1h]))
```

**p95 reconstruction time over the last hour:**
```promql
histogram_quantile(0.95, sum(rate(fibre_block_reconstruction_duration_seconds_bucket[1h])) by (le, node))
```

**Chunk efficiency:**
```promql
fibre_chunks_used_total / fibre_chunks_received_total
```

**Average missing transactions per reconstructed block:**
```promql
rate(fibre_block_missing_tx_count_sum[1h]) / rate(fibre_block_missing_tx_count_count[1h])
```

**Average missing transaction bytes per reconstructed block:**
```promql
rate(fibre_block_missing_tx_bytes_sum[1h]) / rate(fibre_block_missing_tx_bytes_count[1h])
```

**Average transactions per block:**
```promql
rate(bitcoin_block_tx_count_sum[1h]) / rate(bitcoin_block_tx_count_count[1h])
```

**Blocks per hour by delivery mechanism:**
```promql
sum by (mechanism) (increase(fibre_block_deliveries_total[1h]))
```
