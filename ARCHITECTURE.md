# Architecture

This document explains the architecture of the FIBRE Monitoring system, its components, and how they integrate to provide real-time observability of Bitcoin block propagation.

## Overview

The FIBRE Monitoring system is a metrics pipeline that captures FIBRE/UDP block relay performance data from a Bitcoin node and visualizes it through Grafana dashboards.

- **FIBRE/UDP**: Fast Internet Bitcoin Relay Engine - a UDP-based protocol for low-latency block propagation

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              HOST MACHINE                                   │
│                                                                             │
│  ┌─────────────────────┐                                                    │
│  │      bitcoind       │                                                    │
│  │  (USDT tracepoints) │                                                    │
│  └──────────┬──────────┘                                                    │
│             │ eBPF hooks                                                    │
│             ▼                                                               │
│  ┌─────────────────────┐         ┌─────────────────────────────────────┐    │
│  │  fibre_exporter.py  │◄────────│     Prometheus scrapes /metrics     │    │
│  │     (port 9435)     │         │            every 10s                │    │
│  └─────────────────────┘         └─────────────────────────────────────┘    │
│                                                     │                       │
└─────────────────────────────────────────────────────┼───────────────────────┘
                                                      │
              ┌───────────────────────────────────────┼─────────────────────┐
              │              DOCKER NETWORK            │                     │
              │                                       │                     │
              │                                       ▼                     │
              │                                ┌──────────────┐             │
              │                                │  Prometheus  │             │
              │                                │ (port 9090)  │             │
              │                                └──────┬───────┘             │
              │                                       │                     │
              │                                ┌──────┴───────┐             │
              │                                │    Grafana   │             │
              │                                │ (port 3000)  │             │
              │                                └──────────────┘             │
              │                                                             │
              └─────────────────────────────────────────────────────────────┘
```

## Components

### 1. bitcoind (Bitcoin Node)

The Bitcoin daemon with FIBRE patches and USDT (Userland Statically Defined Tracing) tracepoints compiled in.

**Role**: Source of all block relay events

**USDT Tracepoints exposed**:
| Tracepoint | Provider | Description |
|------------|----------|-------------|
| `block_reconstructed` | udp | Fired when a block is successfully reconstructed via FIBRE/UDP |
| `block_send_start` | udp | Fired when the node starts sending a block to peers |
| `block_race_winner` | udp | Fired on block delivery — records mechanism and peer |
| `block_connected` | validation | Fired when any block is connected to the chain |

**Requirements**:
- Compiled with `--enable-usdt` configure flag
- Running on Linux kernel 4.4+ (for eBPF support)

---

### 2. FIBRE Exporter (`fibre_exporter.py`)

A Python application that captures tracepoint events using eBPF and exposes them as Prometheus metrics.

**Role**: Bridge between bitcoind tracepoints and Prometheus

**How it works**:

```
┌─────────────────────────────────────────────────────────────────┐
│                      fibre_exporter.py                          │
│                                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐  │
│  │  BCC/eBPF   │───►│   Event     │───►│ Prometheus Metrics  │  │
│  │   Probes    │    │  Handler    │    │  (Counter, Gauge,   │  │
│  └─────────────┘    └─────────────┘    │   Histogram)        │  │
│        ▲                               └──────────┬──────────┘  │
│        │                                          │             │
│        │ USDT                                     ▼             │
│        │ attach                          ┌───────────────┐      │
│  ┌─────┴─────┐                           │ HTTP Server   │      │
│  │ bitcoind  │                           │ /metrics      │      │
│  │  process  │                           │ (port 9435)   │      │
│  └───────────┘                           └───────────────┘      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Technology Stack**:

| Component | Purpose |
|-----------|---------|
| **BCC (BPF Compiler Collection)** | Python bindings for eBPF, used to attach to USDT tracepoints |
| **eBPF** | Linux kernel technology for safe, efficient tracing without kernel modules |
| **prometheus_client** | Python library to expose metrics in Prometheus format |

**Endpoints**:
- `GET /metrics` (port 9435) - Prometheus metrics endpoint (supports basic auth)
- `GET /health` (port 9436) - Health check endpoint

**Metrics exposed** (see [METRICS.md](METRICS.md) for full reference):
```
# FIBRE relay (udp provider probes)
fibre_blocks_reconstructed_total{node="mynode"} 42
fibre_block_reconstruction_duration_seconds_bucket{...}
fibre_block_deliveries_total{node="mynode",mechanism="fibre_udp",peer="1.2.3.4:8333"} 35

# Block connection (validation provider probe — fires for ALL blocks)
bitcoin_blocks_connected_total{node="mynode"} 100
bitcoin_block_connection_duration_seconds_bucket{...}
bitcoin_block_tx_count_bucket{...}

# Current state
fibre_last_block_height{node="mynode"} 876543
```

---

### 3. Prometheus

A time-series database that scrapes and stores metrics from the exporter.

**Role**: Metrics collection and storage

**How it works**:
```
┌────────────────────────────────────────────────────┐
│                    Prometheus                      │
│                                                    │
│  ┌────────────┐    ┌────────────┐    ┌──────────┐  │
│  │  Scraper   │───►│   TSDB     │◄───│  PromQL  │  │
│  │ (pull)     │    │ (storage)  │    │ (query)  │  │
│  └─────┬──────┘    └────────────┘    └────┬─────┘  │
│        │                                  │        │
│        │ HTTP GET /metrics                │        │
│        │ every 10s                        │        │
│        ▼                                  ▼        │
│  ┌───────────────┐                 ┌───────────┐   │
│  │ fibre_exporter│                 │  Grafana  │   │
│  │ :9435         │                 │  queries  │   │
│  └───────────────┘                 └───────────┘   │
│                                                    │
└────────────────────────────────────────────────────┘
```

**Key Concepts**:
- **Pull-based**: Prometheus actively fetches metrics from targets (vs push-based systems)
- **Scrape interval**: Configured to 10 seconds for FIBRE metrics
- **Retention**: 30 days of historical data (configurable)
- **PromQL**: Query language used by Grafana to retrieve and aggregate metrics

**Configuration** (`prometheus.yml`):
```yaml
scrape_configs:
  - job_name: 'fibre'
    scrape_interval: 10s
    basic_auth:
      username: 'prometheus'
      password: 'secret'
    static_configs:
      - targets: ['host.docker.internal:9435']
        labels:
          node: 'mynode'
```

---

### 4. Grafana

A visualization platform for metrics dashboards.

**Role**: Dashboards, alerting, and data exploration

**Data Sources**:
| Source | Type | Purpose |
|--------|------|---------|
| Prometheus | Metrics | FIBRE performance metrics, block connection stats |

**Dashboard Panels** (top to bottom):

| Panel | What it shows |
|---|---|
| **Blocks Reconstructed** | How many blocks were assembled from FIBRE/UDP FEC chunks in the selected time range. If this number is zero, blocks are arriving via compact blocks or full download instead of FIBRE. |
| **Blocks Sent** | How many blocks this node relayed out to its FIBRE peers. Only relevant if the node acts as a relay sender. |
| **Block Height** | The latest block height seen by the node. A quick liveness check — if it stops increasing, the node may be stalled or disconnected. |
| **Block Reconstruction Time** | A time-series graph showing how long it takes to reconstruct each block from UDP chunks (p50, p95, p99 percentiles). Lower is better. Values under 100ms indicate a healthy FIBRE connection; spikes above 1s suggest packet loss or network issues. |
| **Chunks per Block** | FIBRE splits each block into small UDP packets called chunks and adds extra chunks using Forward Error Correction (FEC), so the block can be reconstructed even if some packets are lost in transit. This panel plots two lines over time: **Received/block** (all chunks that arrived per block, including redundant FEC ones) and **Used/block** (only the chunks that were actually needed per block). When the two lines are close together, the network is clean. If "received" is noticeably higher than "used", extra FEC chunks are arriving because some packets were lost and needed replacement. |
| **Chunk Efficiency** | This is the ratio of the two Chunk Throughput lines expressed as a single number: `used / received`. It answers a simple question: out of all the chunks that arrived, what fraction was actually needed? A value of **1.0 (100%)** means every chunk that arrived was used — zero waste, zero packet loss. A value of **0.5 (50%)** means only half the chunks were useful and the other half were FEC replacements for lost packets. A sustained drop points to network-level packet loss between the FIBRE peers. |
| **Block Delivery by Peer** | A table showing which peers delivered blocks and via which mechanism: **FIBRE/UDP** (green) or **Compact** blocks (orange). This reveals whether FIBRE is actually winning the relay race and which peers are most active. *(Main dashboard only — removed from public dashboard to avoid exposing peer IPs.)* |

---

## Data Flow

### Metrics Flow (Real-time Performance Data)

```
1. Block Event Occurs
   bitcoind receives/sends a block
         │
         ▼
2. USDT Tracepoint Fires
   Kernel triggers the tracepoint with event data
         │
         ▼
3. eBPF Program Captures Event
   BPF program in kernel space copies data to perf buffer
         │
         ▼
4. Exporter Processes Event
   Python callback updates Prometheus metrics (counters, histograms)
         │
         ▼
5. Prometheus Scrapes Metrics
   HTTP GET /metrics every 10 seconds
         │
         ▼
6. Grafana Queries Prometheus
   PromQL queries aggregate and display data
         │
         ▼
7. Dashboard Renders Visualization
   Graphs, gauges, and tables update in real-time
```

---

## Network Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Docker Network                              │
│                       (fibre-network)                               │
│                                                                     │
│            ┌───────────┐               ┌───────────┐                │
│            │ prometheus│               │  grafana  │                │
│            │   :9090   │               │   :3000   │                │
│            └─────┬─────┘               └─────┬─────┘                │
│                  │                           │                      │
│                  └───────────────────────────┘                      │
│                              │                                      │
└──────────────────────────────┼──────────────────────────────────────┘
                               │
                    host.docker.internal
                               │
┌──────────────────────────────┼──────────────────────────────────────┐
│                         HOST MACHINE                                │
│                               │                                     │
│     ┌─────────────────────────┘                                     │
│     │                                                               │
│     ▼                                                               │
│ ┌─────────┐           ┌──────────────┐                              │
│ │bitcoind │◄──────────│fibre_exporter│                              │
│ │         │   eBPF    │    :9435     │                              │
│ └─────────┘           └──────────────┘                              │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Port Summary**:
| Port | Service | Access |
|------|---------|--------|
| 3000 | Grafana | External (browser) |
| 9090 | Prometheus | External (optional) |
| 9435 | FIBRE Exporter | Host + Docker |
| 9436 | Exporter Health | Host only |

---

## Security Considerations

### Metrics Endpoint Authentication

The exporter supports HTTP Basic Authentication to prevent unauthorized access:

```
                    ┌─────────────────────┐
                    │     Prometheus      │
                    │                     │
                    │  Authorization:     │
                    │  Basic base64(u:p)  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   fibre_exporter    │
                    │                     │
                    │  ┌───────────────┐  │
                    │  │ Auth Check    │  │
                    │  │ (hmac.compare │  │
                    │  │  _digest)     │  │
                    │  └───────┬───────┘  │
                    │          │          │
                    │    ┌─────┴─────┐    │
                    │    ▼           ▼    │
                    │  200 OK     401     │
                    │  metrics   Unauth   │
                    └─────────────────────┘
```

### eBPF Security

- Requires root privileges (CAP_SYS_ADMIN or CAP_BPF)
- eBPF programs are verified by the kernel before execution
- No kernel module installation required

---

## Scaling Considerations

### Multi-Node Monitoring

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│   Node A    │  │   Node B    │  │   Node C    │
│  bitcoind   │  │  bitcoind   │  │  bitcoind   │
│  exporter   │  │  exporter   │  │  exporter   │
│   :9435     │  │   :9435     │  │   :9435     │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │
       └────────────────┼────────────────┘
                        │
                        ▼
              ┌─────────────────┐
              │   Prometheus    │
              │                 │
              │  scrape_configs:│
              │  - node_a:9435  │
              │  - node_b:9435  │
              │  - node_c:9435  │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │     Grafana     │
              │                 │
              │  Dashboard with │
              │  node selector  │
              └─────────────────┘
```

Each node runs its own exporter, and a central Prometheus instance scrapes all of them. The `node` label differentiates metrics in queries.

---

## Technology Summary

| Technology | Version | Purpose |
|------------|---------|---------|
| Python | 3.8+ | Exporter runtime |
| BCC | Latest | eBPF Python bindings |
| eBPF | Kernel 4.4+ | Efficient kernel tracing |
| Prometheus | v3.x | Metrics storage |
| Grafana | v12.x | Visualization |
| Docker | 20.10+ | Container orchestration |
