# Secrets for the Docker stack

docker-compose.yml reads these files, and refuses to start without them. Git
ignores everything here except this README.

| File | Holds | Read by |
|------|-------|---------|
| `exporter-ca.crt` | The CA that signed the exporters' certificates (`certs/ca.crt` from `generate-certs.sh`) | Prometheus |
| `exporter-password` | The exporters' `metrics_auth_password`, for user `prometheus` | Prometheus, and the exporters on this machine |
| `grafana-secret-key` | Grafana's secret key, which encrypts the secrets Grafana stores | Grafana |

From the repository root:

```bash
cp certs/ca.crt docker/secrets/exporter-ca.crt
openssl rand -hex 24 | tr -d '\n' > docker/secrets/exporter-password
openssl rand -hex 32 > docker/secrets/grafana-secret-key
chmod 644 docker/secrets/exporter-ca.crt docker/secrets/exporter-password docker/secrets/grafana-secret-key
```

Docker Compose mounts them into the containers as they are, and Prometheus
and Grafana run as their own users there, so the files must be readable by
others on this machine. Keep the stack on a machine only you use.

Generate the Grafana key once and keep it: changing it makes the secrets
Grafana has stored unreadable. A stack started before this file existed used
Grafana's built-in default key.
