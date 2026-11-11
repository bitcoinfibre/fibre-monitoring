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
| `block_reconstruction_detail` | udp | Fired when a reconstructed block's missing-transaction and mempool details are available |
| `block_send_start` | udp | Fired when the node starts sending a block to peers |
| `block_race_winner` | udp | Fired on block delivery — records mechanism and peer |
| `block_race_time` | udp | Fired on block delivery — records how long the FIBRE/UDP and compact block paths had each been running |
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
fibre_block_missing_tx_count_bucket{...}
fibre_block_missing_tx_bytes_bucket{...}
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

**Dashboard Panels** (top to bottom; the public dashboard groups its panels under Network, Nodes and Blocks):

| Panel | What it shows |
|---|---|
| **Nodes** | How many monitored nodes the collector can reach, and how many it cannot. A node counts as down once its exporter has not answered for 30 seconds. The down count turns red when it is above zero. |
| **Chain Tip** | The highest block height any node has seen. |
| **Time Since Last Block** | How long ago the chain tip last advanced on any node, by any delivery path. Blocks arrive about every 10 minutes on average; the figure turns orange after an hour and red after two. |
| **Node Status** | One row per monitored node. On the public dashboard: whether its exporter has been reachable in the last 30 seconds and, for the selected time range, how many blocks it reconstructed from FIBRE/UDP chunks (Rebuilt), its mean reconstruction time (Mean time) and the share of the chunks it received that it did not use (Duplicate chunks). The main dashboard's copy follows the node picker and adds what an operator watches for trouble: how many blocks the node's last seen height trails the chain tip (Behind), its FIBRE win rate, and the number of races that rate is taken over (Races). Rebuilt and Mean time come from the per-block records that Recent Blocks lists, so the three agree. The win rate is, of the blocks FIBRE/UDP and compact blocks raced to deliver to the node, the share FIBRE/UDP delivered; a node with no races shows a dash. A block the node heard first over TCP is not a race, so the node that usually hears blocks first has few of them and its rate swings on single blocks. A node leaves a chunk unused almost always because it already had it, from another peer or filled in from its own mempool, so a node fed by several peers normally shows more of them; the few others arrived out of order or failed a check. It is not a measure of packet loss: a lost packet never arrives to be counted, a chunk that replaces it counts as needed, and chunks arriving after the block is complete are not counted at all. A node that is down keeps its row and shows no figures. |
| **Availability** | One strip per node over the selected time range: faint green while the collector could reach its exporter, solid red once it could not for 30 seconds or more, so a restart that misses one scrape does not show. Up is drawn faint and down solid so the two differ in brightness as well as hue, which keeps them apart for red-green colour-blind readers. It follows the exporter: a node can relay blocks while its strip is red. |
| **Reconstruction Time per Block** | Time from the first UDP packet of a block to the block being fully decoded. Each dot is one block on one node, drawn from the block's own record at the moment the exporter first reports it, and hovering over it names the block. The scale is logarithmic, with dashed lines at 25, 50, 100 and 250 ms, the edges of the distribution's bands. Two blocks less than about a minute apart on the same node share a dot at their average, and its block number shows as a dash. Lower is better. The time includes waiting for chunks to arrive, so it rises when the node's mempool is missing more of the block. |
| **Missing Transactions per Block** | The transactions of a block that a node did not already have in its mempool and had to receive as FIBRE chunks. Each dot is placed by the serialized bytes that were missing, and hovering over it also shows how many transactions that was and which block it is. It sits beside the reconstruction time because the missing bytes largely drive it: more bytes mean more chunks to wait for. |
| **Reconstruction Time Distribution** | How many block reconstructions, counted once per node, fell into each time band over the selected time range: under 25 ms, 25 to 50 ms, 50 to 100 ms, 100 to 250 ms, and over 250 ms. The bars count the records listed in Recent Blocks beside it. |
| **Recent Blocks** | Every block reconstructed in the selected time range, newest first, leaving out the blocks whose records were already there when the range began: the node that reconstructed it, the exact time it took, and its time band. The filter on the Band column lists the blocks behind one bar of the distribution; it works for visitors of the shared dashboard, where pickers and clickable links do not. |
| **Block Delivery by Peer** | A table showing which peers delivered blocks and via which mechanism: **FIBRE/UDP** (green) or **Compact** blocks (orange). This reveals whether FIBRE is actually winning the relay race and which peers are most active. *(Main dashboard only — removed from public dashboard to avoid exposing peer IPs.)* |

The public dashboard's two per-block charts colour each node by its place in the list of scraped nodes sorted by name, so a node keeps the same colour in every chart whether or not it is up. A node with no blocks in the selected time range is left out of a chart's legend. Adding or removing a scrape target shifts the colours of the nodes that sort after it.

The block number in a dot's tooltip is `fibre_last_reconstructed_block_height` at the moment the block's record appears. The "Block Arrival" markers show the same heights, but only to a logged-in viewer: Grafana does not draw markers that come from a Prometheus query on a shared dashboard.

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
