"""The Docker stack runs Prometheus and Grafana as the Nix collector does, so
a change to one that the other lacks fails the check instead of drifting.

The Nix side comes from evaluating the collector module, so a setting added
there is checked without touching this script. Some differences are by
design and left out: where the nodes are listed (a gitignored targets file in
Docker, `targets` in Nix), the datasource's URL (Prometheus' address on each),
and where Grafana's admin password comes from (.env in Docker, a file in Nix).

usage: docker_matches_nix.py <collector.json> <repository root>
"""

import json
import re
import sys
from pathlib import Path

import yaml

nix = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
compose = yaml.safe_load((root / "docker/docker-compose.yml").read_text())
prometheus = yaml.safe_load((root / "docker/prometheus/prometheus.yml").read_text())
datasources = yaml.safe_load((root / "docker/grafana/provisioning/datasources/prometheus.yml").read_text())
providers = yaml.safe_load((root / "docker/grafana/provisioning/dashboards/dashboard.yml").read_text())

failures = []


def check(what, docker, expected):
    if docker != expected:
        failures.append(f"{what}: Docker has {docker!r}, Nix has {expected!r}")


def no_nulls(value):
    if isinstance(value, dict):
        return {k: no_nulls(v) for k, v in value.items() if v is not None}
    return value


def merged(definitions):
    out = {}
    for definition in definitions:
        for section, keys in definition.items():
            out.setdefault(section, {}).update(keys)
    return out


def service(name):
    return compose["services"][name]


def environment(name):
    return dict(entry.split("=", 1) for entry in service(name).get("environment", []))


def published(name, container_port):
    """The host address and port a service publishes container_port on, with
    any ${VAR:-default} replaced by its default."""
    for entry in service(name).get("ports", []):
        entry = re.sub(r"\$\{\w+:-([^}]*)\}", r"\1", str(entry))
        address, host_port, port = entry.rsplit(":", 2)
        if int(port) == container_port:
            return address, int(host_port)
    return None


def mounts(name):
    """Container path -> repository path for the service's bind mounts."""
    out = {}
    for entry in service(name).get("volumes", []):
        source, target = entry.split(":")[:2]
        if source.startswith("."):
            out[target] = (root / "docker" / source).resolve()
    return out


def host_path(name, container_path):
    for target, source in mounts(name).items():
        if container_path == target or container_path.startswith(target + "/"):
            return source / container_path[len(target):].lstrip("/")
    return None


def flag(name, prefix):
    for argument in service(name).get("command", []):
        if argument.startswith(prefix):
            return argument[len(prefix):]
    return None


def image_version(name):
    return service(name)["image"].rsplit(":", 1)[1].removeprefix("v")


# Versions: the images are the packages Nix pins.
check("Grafana image", image_version("grafana"), nix["grafana"]["version"])
check("Prometheus image", image_version("prometheus"), nix["prometheus"]["version"])

# Grafana's settings. Where it listens and its secrets differ in form, not
# in substance; every other setting the collector makes is an environment
# variable here.
settings = merged(nix["grafana"]["settings"])
env = environment("grafana")
server = settings.pop("server", {})
check("Grafana's published address and port", published("grafana", server["http_port"]), (server["http_addr"], server["http_port"]))
security = settings.pop("security", {})
admin_user = re.sub(r"\$\{\w+:-([^}]*)\}", r"\1", env.get("GF_SECURITY_ADMIN_USER", ""))
check("Grafana admin user", admin_user, security["admin_user"])
if not str(security.get("secret_key", "")).startswith("$__file{"):
    failures.append("Nix: Grafana's secret key no longer comes from a file")
if "GF_SECURITY_SECRET_KEY__FILE" not in env:
    failures.append("Docker: Grafana's secret key isn't read from a file (GF_SECURITY_SECRET_KEY__FILE)")
for section, keys in settings.items():
    for key, value in keys.items():
        variable = f"GF_{section.upper()}_{key.upper()}"
        expected = str(value).lower() if isinstance(value, bool) else str(value)
        check(f"Grafana {section}.{key} ({variable})", env.get(variable), expected)

# The datasource the dashboards query.
datasource = no_nulls(nix["grafana"]["datasource"])
docker_datasource = next(d for d in datasources["datasources"] if d.get("uid") == datasource["uid"])
for key in sorted(set(datasource) - {"url"}):
    check(f"datasource {key}", docker_datasource.get(key), datasource[key])

# The dashboards: Grafana's provider reads the folder compose mounts from
# ../dashboards, the folder the collector serves.
provider_path = providers["providers"][0]["options"]["path"]
check("dashboard folder", host_path("grafana", provider_path), (root / "dashboards").resolve())

# Prometheus' global settings, retention and address.
for key, value in no_nulls(nix["prometheus"]["globalConfig"]).items():
    check(f"Prometheus global {key}", prometheus.get("global", {}).get(key), value)
check("retention", flag("prometheus", "--storage.tsdb.retention.time="), nix["prometheus"]["retentionTime"])
check("Prometheus' published address and port", published("prometheus", 9090), (nix["prometheus"]["listenAddress"], 9090))

# The alert rules: the same files, from ../alerts.
rule_files = prometheus.get("rule_files", [])
check("alert rule files", sorted(Path(f).name for f in rule_files), sorted(nix["prometheus"]["ruleFiles"]))
for rule_file in rule_files:
    source = host_path("prometheus", rule_file)
    if source is None or source.parent != (root / "alerts").resolve() or not source.is_file():
        failures.append(f"Docker: rule file {rule_file} isn't mounted from alerts/")

# The FIBRE scrape: interval, HTTPS checked against a CA, and a password
# read from a file.
job = no_nulls(nix["prometheus"]["fibreJob"])
docker_job = next(j for j in prometheus["scrape_configs"] if j["job_name"] == "fibre")
for key in ("scrape_interval", "scheme"):
    check(f"fibre job {key}", docker_job.get(key), job.get(key))
check("fibre job basic_auth username", docker_job.get("basic_auth", {}).get("username"), job["basic_auth"]["username"])
if "password_file" not in docker_job.get("basic_auth", {}) or "password" in docker_job.get("basic_auth", {}):
    failures.append("Docker: the fibre job's password isn't read from a file")
if "ca_file" not in docker_job.get("tls_config", {}) or docker_job.get("tls_config", {}).get("insecure_skip_verify"):
    failures.append("Docker: the fibre job doesn't check exporters' certificates against a CA")
check("datasource timeInterval vs the fibre job's scrape_interval", docker_datasource["jsonData"].get("timeInterval"), docker_job.get("scrape_interval"))

for failure in failures:
    print(failure)
if failures:
    sys.exit(1)
print("The Docker stack matches the Nix collector.")
