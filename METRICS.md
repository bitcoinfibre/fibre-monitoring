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
| `block_race_time` | `udp` | `fibrerace.cpp` | How long each of the two paths had been running when the block connected, or that it had not started |
| `block_connected` | `validation` | `validation.cpp` | Block connected to the chain (standard Bitcoin Core probe) |

Unused probes: `udp:block_header_chunk` (internal timing breakdown), `udp:block_coinbase` (coinbase script extraction).

## Labels

All metrics use at least the `node` label, set by `--node-name` at exporter startup. This identifies which bitcoind instance the metric came from in multi-node setups.

| Label | Metrics | Values | Description |
|---|---|---|---|
| `node` | All FIBRE and block metrics | User-configured name | Identifies the bitcoind instance |
| `mechanism` | `fibre_block_deliveries_total` | `fibre_udp`, `bip152_cmpct`, `other` | How the block was delivered |
| `peer` | `fibre_block_deliveries_total` | IP:port string | Which peer delivered the block |
| `outcome` | `fibre_block_races_total` | `fibre_won`, `fibre_lost`, `no_race` | How the block's race between FIBRE/UDP and compact blocks ended |
| `height` | `fibre_recent_block_reconstruction_seconds`, `fibre_block_reconstructed_event` | Block height | Which block the series is about |
| `event_type` | `fibre_exporter_events_processed_total` | `block_reconstructed`, `block_reconstruction_detail`, `block_send_start`, `block_delivery`, `block_race_time`, `block_connected` | Type of BPF event |
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

Time from receiving the first UDP packet of a block to the block being fully decoded, before validation. It includes waiting for chunks to arrive, so it depends heavily on how much of the block the node's mempool was missing: a node with most transactions already in its mempool needs few chunks and finishes sooner, while one missing most of the block waits for many more. A long time can therefore mean a poorly synced mempool as much as a slow or lossy link.

### `fibre_block_chunks_used`

- **Type:** Histogram
- **Labels:** `node`
- **Buckets:** 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000
- **USDT probe:** `udp:block_reconstructed` (argument 3: `chunks_used`)

Number of chunks the node took from the network to reconstruct each block: the header chunks plus the block chunks its mempool could not supply. A block is split into chunks for UDP relay, and FEC encoding means any sufficient subset reconstructs it. The value follows `fibre_block_missing_tx_bytes`, at roughly one chunk per 1,152 missing bytes.

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

Running total of all chunks used across all block reconstructions. Compare with `fibre_chunks_received_total` to find the share of received chunks that were not needed.

### `fibre_chunks_received_total`

- **Type:** Counter
- **Labels:** `node`
- **USDT probe:** `udp:block_reconstructed` (argument 4: `chunks_recvd`)

Running total of all chunks received while a block was still being collected. A received chunk goes unused when the node already had it, from another peer or filled in from its own mempool, or when it arrived out of order; `1 - fibre_chunks_used_total / fibre_chunks_received_total` is that unused share, which the public dashboard shows per node as Duplicate chunks. It is not a measure of packet loss: a lost packet never arrives to be counted, an FEC chunk that replaces it counts as used, and chunks arriving after the block is complete are not counted.

### `fibre_blocks_sent_total`

- **Type:** Counter
- **Labels:** `node`
- **USDT probe:** `udp:block_send_start`

Incremented each time the node begins relaying a block via UDP to its FIBRE peers. This happens once for every newly accepted block, however the block arrived, including blocks connected while catching up after a restart, so it counts blocks relayed rather than FIBRE performance.

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

This metric says which peers a node's blocks come from. It is a poor win rate: the node that first hears a block from outside the FIBRE network receives it as a compact block before any FIBRE peer has it to send, and that counts here as a compact block delivery. Use `fibre_block_races_total` for the win rate.

### `fibre_block_races_total`

- **Type:** Counter
- **Labels:** `node`, `outcome`
- **USDT probes:** `udp:block_race_winner` and `udp:block_race_time`

Counts each block that connected through FIBRE/UDP or a compact block, by how the race between the two ended. The exporter pairs the block's winner with its timing, which says whether each path had started by the time the block connected, and `classify_race()` in `fibre_exporter.py` turns the pair into an outcome:

| `outcome` value | Meaning |
|---|---|
| `fibre_won` | FIBRE/UDP delivered the block, whether or not a compact block had arrived by then |
| `fibre_lost` | A compact block delivered the block although a FIBRE header for it had already been decoded, so FIBRE was in the race |
| `no_race` | A compact block delivered the block and no FIBRE header had been decoded by then |

`no_race` is the node that heard the block first, before any FIBRE peer had it to send, which says nothing about FIBRE's speed. It also covers a block whose FIBRE copy was still on its way: bitcoind drops UDP packets for a block it already has without recording them, so one node cannot tell the two apart. A loss has no margin either, because bitcoind drops the unfinished reconstruction once the block is accepted another way.

All three series exist at 0 from the exporter's start. The win rate is `fibre_won / (fibre_won + fibre_lost)`.

### `fibre_recent_block_reconstruction_seconds`

- **Type:** Gauge
- **Labels:** `node`, `height`
- **USDT probe:** `udp:block_reconstructed` (argument 6: `duration_us`, argument 7: `height`)

The exact reconstruction time of each of the node's 16 most recently reconstructed blocks, one series per block. Older blocks lose their series so the exporter's series count stays bounded, while Prometheus keeps their history. A block's series lasts for as long as it is among the node's last 16, about two and a half hours, so the series seen in a time window also include up to 16 blocks per node from before it: to list only the blocks reconstructed inside the window, leave out the series that were already there when it began (see the example queries). Each block adds one series to Prometheus per node, about 144 a day.

### `fibre_last_reconstructed_block_height`

- **Type:** Gauge
- **Labels:** `node`
- **USDT probe:** `udp:block_reconstructed` (argument 7: `height`)

The height of the block the node most recently reconstructed from FIBRE/UDP chunks. The series does not exist until the node has reconstructed a block. Unlike `fibre_last_block_height` it does not move for a block that arrived another way, so at the moment a new `fibre_recent_block_reconstruction_seconds` series appears it is that block's height as a number. The public dashboard uses it to name the block behind each dot of its per-block charts.

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

## UDP Loss Metrics

FIBRE sends blocks as UDP datagrams, which have no flow control: when a sender writes faster than its link carries, or a receiver reads slower than packets arrive, the kernel drops packets and counts why. These metrics are read from the kernel on every scrape, not from USDT probes, so they work without BPF.

### `fibre_host_udp_send_buffer_errors_total`

- **Type:** Counter
- **Labels:** `node`, `ip` (`4` or `6`)
- **Source:** `SndbufErrors` in `/proc/net/snmp` (`Udp6SndbufErrors` in `/proc/net/snmp6`)

Sends refused because a socket's send buffer was full; FIBRE drops the chunk. bitcoind 32.x logs each with `-debug=udpnet` (`UDP: send failed: Resource temporarily unavailable (11)`); 31.x doesn't log them, so this counter is their only record. A few during a block come from the sender's writer thread waking late and catching up in a burst; a steady rate means the configured UDP bandwidth (`-udpport=<port>,<group>,<bw>`) is above what the link carries.

### `fibre_host_udp_receive_buffer_errors_total`

- **Type:** Counter
- **Labels:** `node`, `ip`
- **Source:** `RcvbufErrors` in `/proc/net/snmp` (`Udp6RcvbufErrors`)

Arriving datagrams dropped because a socket's receive buffer was full: the reader fell behind. The sender is not told.

### `fibre_host_udp_in_errors_total`, `fibre_host_udp_in_datagrams_total`, `fibre_host_udp_out_datagrams_total`

- **Type:** Counter
- **Labels:** `node`, `ip`
- **Source:** `InErrors`, `InDatagrams`, `OutDatagrams`

All receive errors (receive buffer overflows included), and datagrams delivered and sent. Comparing one node's sent with its peer's delivered shows loss on the path between them.

The host counters cover every UDP socket on the machine, DNS and NTP included. On a dedicated FIBRE host, FIBRE is nearly all of it; the socket metrics below are bitcoind's alone.

### `fibre_udp_socket_receive_drops_total`

- **Type:** Counter
- **Labels:** `node`, `ip`, `port`
- **Source:** the `drops` column of `/proc/net/udp` and `/proc/net/udp6`, for the sockets bitcoind holds

Datagrams dropped on arrival at one of bitcoind's UDP sockets. It restarts from zero with bitcoind, since the sockets are new.

### `fibre_udp_socket_send_queue_bytes`, `fibre_udp_socket_receive_queue_bytes`

- **Type:** Gauge
- **Labels:** `node`, `ip`, `port`
- **Source:** the `tx_queue` and `rx_queue` columns of `/proc/net/udp` and `/proc/net/udp6`

Bytes waiting in the socket's send or receive buffer at the moment of the scrape. Normally near zero; a buffer that stays near full shows where packets are queuing.

## Exporter Self-Monitoring Metrics

These are internal metrics about the exporter process itself, not derived from USDT probes.

| Metric | Type | Description |
|---|---|---|
| `fibre_exporter_up` | Gauge | `1` if the exporter is running, `0` on shutdown |
| `fibre_exporter_start_time_seconds` | Gauge | Unix timestamp when the exporter started |
| `fibre_exporter_events_processed_total` | Counter | Events processed, by `event_type` label |
| `fibre_exporter_errors_total` | Counter | Errors encountered, by `error_type` label |
| `fibre_exporter_probes_attached` | Gauge | Number of USDT probes successfully attached (out of 6) |
| `fibre_exporter_info` | Info | Exporter version, node name, and bitcoind path |

## Example PromQL Queries

**FIBRE relay win rate** — of the blocks FIBRE and compact blocks raced for, the fraction FIBRE delivered:
```promql
sum(increase(fibre_block_races_total{outcome="fibre_won"}[1h]))
/
sum(increase(fibre_block_races_total{outcome=~"fibre_won|fibre_lost"}[1h]))
```

**Blocks reconstructed in the last six hours, with each one's time** — every series seen in the window, less those already there when it began:
```promql
max by (node, height) (last_over_time(fibre_recent_block_reconstruction_seconds[6h]))
unless
max by (node, height) (fibre_recent_block_reconstruction_seconds offset 6h)
```

**p95 reconstruction time over the last hour:**
```promql
histogram_quantile(0.95, sum(rate(fibre_block_reconstruction_duration_seconds_bucket[1h])) by (le, node))
```

**Share of received chunks that were not needed, over the last hour:**
```promql
1 - increase(fibre_chunks_used_total[1h]) / increase(fibre_chunks_received_total[1h])
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

**UDP sends refused per second, by node** — non-zero means the configured UDP bandwidth is too high for the link:
```promql
sum by (node) (rate(fibre_host_udp_send_buffer_errors_total[5m]))
```

**Datagrams dropped at bitcoind's sockets per second, by node:**
```promql
sum by (node) (rate(fibre_udp_socket_receive_drops_total[5m]))
```
