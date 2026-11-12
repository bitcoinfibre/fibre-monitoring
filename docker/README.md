# FIBRE Monitoring Docker Setup

Prometheus and Grafana in Docker Compose, set up like the Nix collector
(`nix/collector.nix`): the same Grafana and Prometheus versions, alert rules
(`../alerts/`) and dashboards (`../dashboards/`). Prometheus scrapes the
exporters over HTTPS with a password, and Prometheus and Grafana answer only
on this machine unless you say otherwise. `nix flake check` fails if the two
setups drift apart.

## Quick Start

Run these from the repository root unless a step says otherwise.

### 1. Make the certificates and the secrets

Make a certificate for each exporter, for the address Prometheus scrapes it
at. Docker reaches an exporter on this machine as `host.docker.internal`:

```bash
./generate-certs.sh host.docker.internal 192.168.1.10
```

Then give Prometheus the CA and a new password for the exporters, and
Grafana its key:

```bash
cp certs/ca.crt docker/secrets/exporter-ca.crt
openssl rand -hex 24 | tr -d '\n' > docker/secrets/exporter-password
openssl rand -hex 32 > docker/secrets/grafana-secret-key
chmod 644 docker/secrets/exporter-ca.crt docker/secrets/exporter-password docker/secrets/grafana-secret-key
```

`secrets/README.md` explains each file. Git ignores them, and `certs/` with
its keys.

### 2. Run the exporters with TLS and the password

Start each exporter with its certificate and the password. The password goes
in the environment, which `sudo -E` passes on: as a command-line argument,
anyone on the machine could read it in `ps`.

```bash
export FIBRE_METRICS_AUTH_USERNAME=prometheus
export FIBRE_METRICS_AUTH_PASSWORD="$(cat docker/secrets/exporter-password)"
sudo -E .venv/bin/python3 fibre_exporter.py \
  --bitcoind /path/to/bitcoind \
  --pid $(pgrep bitcoind) \
  --node-name mynode \
  --port 9435 \
  --tls-cert certs/host.docker.internal/node.crt \
  --tls-key certs/host.docker.internal/node.key
```

A second bitcoind on this machine needs an exporter of its own, with that
bitcoind's `--pid` (`pgrep bitcoind` then prints both) and its own ports,
such as `--port 9437 --health-port 9438`: the defaults, 9435 and 9436, are
the first exporter's. An exporter on another machine takes that node's
certificate and key, copied there, and the same password.

### 3. List the nodes

```bash
cp docker/prometheus/targets/nodes.yml.example docker/prometheus/targets/nodes.yml
```

Edit `nodes.yml`: one entry per exporter, at the address its certificate
holds, with the `node` label the dashboards show. Git ignores it, so no
node's address ends up in the repository.

### 4. Set the Grafana admin password

```bash
cd docker
cp .env.example .env
```

Edit `.env` and set `GRAFANA_ADMIN_PASSWORD`.

### 5. Start Prometheus and Grafana

```bash
docker compose up -d
```

### 6. Open the dashboards

- **Grafana**: http://localhost:3000, as `admin` (or your configured user)
  with your password. Both dashboards are pre-loaded.
- **Prometheus**: http://localhost:9090

Both answer only on this machine. To open Grafana to the network, set
`GRAFANA_ADDRESS=0.0.0.0` in `.env`; Prometheus stays local.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GRAFANA_ADMIN_PASSWORD` | Yes | - | Grafana admin password, set when Grafana first starts |
| `GRAFANA_ADMIN_USER` | No | admin | Grafana admin username |
| `GRAFANA_ADDRESS` | No | 127.0.0.1 | Address Grafana listens on, on this machine |

## Services

| Service | Port | Description |
|---------|------|-------------|
| Prometheus | 127.0.0.1:9090 | Metrics storage, queries and alert rules |
| Grafana | 127.0.0.1:3000 | Dashboards |
| FIBRE Exporter | 9435 | Runs on each node (not in Docker), over HTTPS |

## File Structure

```
docker/
├── docker-compose.yml          # Container orchestration
├── .env.example                # Environment variables template
├── prometheus/
│   ├── prometheus.yml          # Prometheus scrape and rule config
│   └── targets/
│       └── nodes.yml.example   # The nodes to scrape: copy to nodes.yml
├── secrets/
│   └── README.md               # The CA, passwords and key to put here
└── grafana/
    └── provisioning/
        ├── datasources/
        │   └── prometheus.yml  # Prometheus datasource
        └── dashboards/
            └── dashboard.yml   # Dashboard provisioner config
```

The dashboards themselves are in `../dashboards/`, and the alert rules in
`../alerts/`, both shared with the Nix collector:

```
dashboards/
├── fibre-dashboard.json        # Main dashboard (with node filter)
└── fibre-dashboard-public.json # Public-shareable dashboard (all nodes)
```

## Public Dashboard

`fibre-dashboard-public.json` is a variant of the main dashboard designed for Grafana's **Share externally** feature. It removes template variables and the built-in annotations datasource, which are unsupported for public sharing. It always displays all configured nodes.

To share it publicly: open the "FIBRE Block Relay" dashboard in Grafana (the main dashboard is "FIBRE Block Relay (Operators)"), click **Share > Share externally**, and enable the public link.

## Alerts

Prometheus evaluates the rules in `../alerts/fibre.rules.yml` and shows them
at http://localhost:9090/alerts; Grafana lists them too. Nobody is notified:
to send them somewhere, run an Alertmanager and add it under `alerting:` in
`prometheus/prometheus.yml`.

## Managing Containers

```bash
# Start
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down

# Stop and remove volumes (reset data)
docker compose down -v

# Restart
docker compose restart
```

## Adding More Nodes

Make a certificate for the node's address (`./generate-certs.sh <address>`),
start its exporter with it and the password, and add it to
`prometheus/targets/nodes.yml`:

```yaml
- targets: ['192.168.1.11:9435']
  labels:
    node: 'remote_node_2'
```

Prometheus picks the change up by itself, within five minutes at most.

## Upgrading From Grafana 12

Earlier versions of this setup ran Grafana 12.3 and Prometheus 3.8, with the
nodes in `prometheus.yml` and no secrets. To upgrade a running stack:

1. Back up Grafana's data. Grafana 13 migrates its database when it first
   starts, and Grafana 12 can't read it afterwards:

   ```bash
   docker compose stop grafana
   docker run --rm -v docker_grafana_data:/data -v "$PWD":/backup alpine \
     tar czf /backup/grafana-data-backup.tgz -C /data .
   ```

   (`docker_grafana_data` is the volume's name when the stack runs from this
   folder; `docker volume ls` shows it.)
2. Move your nodes from the old `prometheus.yml` into
   `prometheus/targets/nodes.yml`, and do steps 1 and 2 of the Quick Start.
   The stack used Grafana's built-in secret key until now; secrets Grafana
   stored with it become unreadable under the new key. The provisioned
   datasource holds none.
3. `docker compose up -d`.

## Troubleshooting

### Docker stack fails to start

```
GRAFANA_ADMIN_PASSWORD must be set
```

Ensure you've created the `.env` file:
```bash
cp .env.example .env
# Edit .env and set GRAFANA_ADMIN_PASSWORD
```

A missing file in `secrets/` stops it too:

```
bind source path does not exist: .../docker/secrets/grafana-secret-key
```

`secrets/README.md` says how to make each one.

### Prometheus can't scrape an exporter

Open http://localhost:9090/targets: each node shows its last error.

- `x509: certificate is valid for ..., not ...`: the address in
  `nodes.yml` isn't one the exporter's certificate holds. Use that address,
  or make a certificate for this one.
- `x509: certificate signed by unknown authority`: `secrets/exporter-ca.crt`
  isn't the CA that signed the exporter's certificate.
- `401 Unauthorized`: `secrets/exporter-password` doesn't match the
  exporter's password.
- `connection refused`: the exporter isn't running, or doesn't listen on an
  address Docker can reach.

### View container logs

```bash
docker compose logs prometheus
docker compose logs grafana
```

### Reset everything

```bash
docker compose down -v
docker compose up -d
```
