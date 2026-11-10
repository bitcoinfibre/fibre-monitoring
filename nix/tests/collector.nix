# The collector against a stand-in exporter: Prometheus scrapes it over TLS
# with basic auth, the alert rules evaluate on what it serves, and Grafana
# serves the dashboards from Prometheus. The stand-in serves fixed metrics,
# since the real exporter needs a FIBRE node to trace.
{ pkgs, module }:
let
  scrapePassword = "test-scrape-password";
  adminPassword = "test-admin-password";
  dashboards = ../../docker/grafana/provisioning/dashboards;

  # Test-only CA and a certificate for the host name "node".
  certs = pkgs.runCommand "fibre-collector-test-certs" { nativeBuildInputs = [ pkgs.openssl ]; } ''
    mkdir $out
    ec() { echo -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes; }
    openssl req -x509 $(ec) -days 3650 -subj "/CN=Test CA" \
      -addext basicConstraints=critical,CA:TRUE -keyout $out/ca.key -out $out/ca.crt
    openssl req $(ec) -subj /CN=node -keyout $out/node.key -out node.csr
    printf 'subjectAltName=DNS:node\n' > ext
    openssl x509 -req -in node.csr -CA $out/ca.crt -CAkey $out/ca.key -CAcreateserial \
      -days 3650 -extfile ext -out $out/node.crt
  '';

  metrics = pkgs.writeText "metrics" ''
    # TYPE fibre_exporter_up gauge
    fibre_exporter_up 1
    # TYPE fibre_exporter_probes_attached gauge
    fibre_exporter_probes_attached 5
    # TYPE fibre_blocks_sent_total counter
    fibre_blocks_sent_total{node="node"} 3
    # TYPE fibre_host_udp_send_buffer_errors_total counter
    fibre_host_udp_send_buffer_errors_total{node="node",ip="4"} 0
    # TYPE fibre_udp_socket_receive_drops_total counter
    fibre_udp_socket_receive_drops_total{node="node",ip="4",port="4242"} 0
  '';

  # Serves the metrics file, re-read per request so the test can change it,
  # to clients with the right password.
  stub = pkgs.writeText "stub-exporter.py" ''
    import base64, http.server, ssl, sys

    cert, key, password, path = sys.argv[1:]
    expected = "Basic " + base64.b64encode(f"prometheus:{password}".encode()).decode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="fibre"')
                self.end_headers()
                return
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("0.0.0.0", 9435), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
  '';

  # Records each notification the Alertmanager posts to it.
  sink = pkgs.writeText "alert-sink.py" ''
    import http.server, sys

    path = sys.argv[1]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            with open(path, "ab") as f:
                f.write(body + b"\n")
            self.send_response(200)
            self.end_headers()

    http.server.HTTPServer(("127.0.0.1", 9099), Handler).serve_forever()
  '';

  # Fires at once, so delivery is tested without waiting out the real
  # rules' `for`; fibre.rules.test.yml tests their timing.
  alwaysFiring = pkgs.writeText "always-firing.rules.yml" ''
    groups:
      - name: test
        rules:
          - alert: FibreTestAlwaysFiring
            expr: vector(1)
            labels:
              severity: test
  '';

  # A secret as a runtime file owned by its service's user, as sops-nix
  # would provide it.
  secret = user: text: {
    inherit text user;
    mode = "0400";
  };
in
pkgs.testers.runNixOSTest {
  name = "fibre-collector";

  nodes = {
    node = {
      environment.systemPackages = [ pkgs.curl ];
      networking.firewall.allowedTCPPorts = [ 9435 ];
      systemd.services.stub-exporter = {
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          RuntimeDirectory = "stub-exporter";
          ExecStartPre = "${pkgs.coreutils}/bin/install -m 644 ${metrics} /run/stub-exporter/metrics";
          ExecStart = "${pkgs.python3.interpreter} ${stub} ${certs}/node.crt ${certs}/node.key ${scrapePassword} /run/stub-exporter/metrics";
        };
      };
    };

    collector =
      { nodes, ... }:
      {
        imports = [ module ];
        virtualisation.memorySize = 2048;
        environment.systemPackages = [
          pkgs.curl
          pkgs.jq
        ];
        environment.etc = {
          "fibre-test/scrape-password" = secret "prometheus" scrapePassword;
          "fibre-test/grafana-admin" = secret "grafana" adminPassword;
          "fibre-test/grafana-secret-key" = secret "grafana" "test-secret-key";
        };
        services.fibre-collector = {
          enable = true;
          targets = {
            node = "node:9435";
            # The same exporter by address, which its certificate doesn't name.
            by-address = "${nodes.node.networking.primaryIPAddress}:9435";
          };
          passwordFile = "/etc/fibre-test/scrape-password";
          tls.caFile = "${certs}/ca.crt";
          grafana = {
            adminPasswordFile = "/etc/fibre-test/grafana-admin";
            secretKeyFile = "/etc/fibre-test/grafana-secret-key";
          };
        };

        # Delivery through a local Alertmanager to a webhook, which any
        # channel (email, chat) stands in for.
        services.prometheus.alertmanager = {
          enable = true;
          configuration = {
            route = {
              receiver = "sink";
              group_wait = "1s";
            };
            receivers = [
              {
                name = "sink";
                webhook_configs = [ { url = "http://127.0.0.1:9099/"; } ];
              }
            ];
          };
        };
        services.prometheus.ruleFiles = [ alwaysFiring ];
        systemd.services.alert-sink = {
          wantedBy = [ "multi-user.target" ];
          serviceConfig = {
            RuntimeDirectory = "alert-sink";
            ExecStart = "${pkgs.python3.interpreter} ${sink} /run/alert-sink/received";
          };
        };
      };
  };

  testScript = ''
    import json
    import re
    import shlex

    prom = "http://127.0.0.1:9090/api/v1"
    grafana = "http://admin:${adminPassword}@127.0.0.1:3000/api"

    def uid_of(*items):
        """The datasource uid the first of these names, as Grafana resolves
        a query's datasource: its own, else its panel's."""
        for item in items:
            datasource = item.get("datasource")
            if isinstance(datasource, dict) and datasource.get("uid"):
                return datasource["uid"]
        return "prometheus"

    def dashboard_queries(path):
        """Each panel and annotation query in a dashboard: a name, the query
        and the datasource uid it asks for."""
        with open(path) as f:
            dashboard = json.load(f)

        def walk(panels):
            for panel in panels:
                yield panel
                yield from walk(panel.get("panels", []))

        for panel in walk(dashboard.get("panels", [])):
            for target in panel.get("targets", []):
                if target.get("expr"):
                    yield f"{panel.get('title')} ({target.get('refId')})", target["expr"], uid_of(target, panel)
        for annotation in dashboard.get("annotations", {}).get("list", []):
            if annotation.get("expr"):
                yield f"annotation {annotation.get('name')}", annotation["expr"], uid_of(annotation)

    def grafana_query(expr, uid="prometheus"):
        """Run a query as a dashboard panel does, through the datasource it
        names, which fills in Grafana's own variables such as $__range."""
        body = json.dumps({
            "from": "now-1h",
            "to": "now",
            "queries": [{
                "refId": "A",
                "datasource": {"type": "prometheus", "uid": uid},
                "expr": expr,
                "instant": True,
                "range": False,
                "intervalMs": 15000,
                "maxDataPoints": 100,
            }],
        })
        out = collector.succeed(
            f"curl -s -X POST -H 'Content-Type: application/json' {grafana}/ds/query -d {shlex.quote(body)}"
        )
        response = json.loads(out)
        # A datasource Grafana doesn't have fails the whole request, as
        # {"message": "Data source not found"}, with no per-query result.
        return response.get("results", {}).get("A", {"error": response.get("message", response)})

    def alert_active(name):
        return (
            f"curl -sf {prom}/alerts | jq -e '.data.alerts[]"
            f" | select(.labels.alertname == \"{name}\" and .labels.node == \"node\")'"
        )

    start_all()
    node.wait_for_open_port(9435)
    collector.wait_for_unit("prometheus.service")
    collector.wait_for_unit("grafana.service")
    collector.wait_for_open_port(9090)
    collector.wait_for_open_port(3000)

    with subtest("Prometheus scrapes the exporter over verified TLS with basic auth"):
        collector.wait_until_succeeds(
            f"curl -sf '{prom}/targets?state=active' | jq -e '.data.activeTargets[]"
            " | select(.labels.job == \"fibre\" and .labels.node == \"node\" and .health == \"up\")'"
        )
        result = json.loads(collector.succeed(
            f"curl -sf {prom}/query --data-urlencode 'query=fibre_blocks_sent_total{{job=\"fibre\"}}'"
        ))["data"]["result"]
        assert len(result) == 1, result
        assert result[0]["metric"]["node"] == "node", result
        assert result[0]["value"][1] == "3", result

    with subtest("An exporter whose certificate doesn't name the target is refused"):
        collector.wait_until_succeeds(
            f"curl -sf '{prom}/targets?state=active' | jq -e '.data.activeTargets[]"
            " | select(.labels.node == \"by-address\" and .health == \"down\")"
            " | select(.lastError | test(\"x509|certificate\"))'"
        )

    with subtest("The alert rules are loaded and evaluate on scraped data"):
        rules = json.loads(collector.succeed(f"curl -sf {prom}/rules"))
        names = {r["name"] for g in rules["data"]["groups"] for r in g["rules"]}
        assert {"FibreExporterDown", "FibreProbesDetached"} <= names, names
        collector.fail(alert_active("FibreProbesDetached"))
        node.succeed(
            "sed -i 's/^fibre_exporter_probes_attached 5$/fibre_exporter_probes_attached 3/'"
            " /run/stub-exporter/metrics"
        )
        collector.wait_until_succeeds(alert_active("FibreProbesDetached"))

    with subtest("Grafana reaches Prometheus and serves both dashboards"):
        collector.succeed(f"curl -sf {grafana}/datasources/uid/prometheus/health | jq -e '.status == \"OK\"'")
        for uid in ["fibre-relay", "fibre-relay-public"]:
            collector.succeed(f"curl -sf {grafana}/dashboards/uid/{uid} | jq -e '.dashboard.uid == \"{uid}\"'")
        collector.fail("curl -sf http://admin:wrong@127.0.0.1:3000/api/search")
        # Nothing tried to install plugins or fetch signing keys from grafana.com.
        collector.fail("journalctl -u grafana.service | grep -E 'plugin.backgroundinstaller|key_retriever'")

    with subtest("Every dashboard query runs without error"):
        # The check can fail: a broken query is reported.
        assert "error" in grafana_query("sum("), "a broken query was not reported"
        for name in ["fibre-dashboard.json", "fibre-dashboard-public.json"]:
            queries = list(dashboard_queries(f"${dashboards}/{name}"))
            assert queries, f"{name}: no queries found"
            for title, expr, uid in queries:
                # The node picker, set to all nodes.
                expr = re.sub(r"\$\{?node\}?", ".*", expr)
                result = grafana_query(expr, uid)
                assert "error" not in result and result.get("status") == 200, f"{name}: {title}: {result}"
        # What fills the private dashboard's node picker: every scraped node,
        # whether or not it has reconstructed a block.
        picker = json.loads(collector.succeed(
            f"curl -sfG {prom}/label/node/values --data-urlencode 'match[]=up{{job=\"fibre\"}}'"
        ))
        assert sorted(picker["data"]) == ["by-address", "node"], picker

    with subtest("Firing alerts reach the Alertmanager's receiver"):
        collector.wait_for_unit("alertmanager.service")
        collector.wait_until_succeeds("grep -q FibreTestAlwaysFiring /run/alert-sink/received")

    with subtest("A dead exporter raises FibreExporterDown"):
        node.succeed("systemctl stop stub-exporter")
        collector.wait_until_succeeds(alert_active("FibreExporterDown"))

    with subtest("Prometheus, Grafana and Alertmanager answer only on the collector itself"):
        for port in [9090, 3000, 9093]:
            collector.succeed(f"ss -ltnH 'sport = :{port}' | grep -q '127.0.0.1:{port}'")
        collector.fail("ss -lnH 'sport = :9094' | grep -q .")
        node.fail("curl -sf --max-time 5 http://collector:9090/-/healthy")
        node.fail("curl -sf --max-time 5 http://collector:3000/api/health")
        node.fail("curl -sf --max-time 5 http://collector:9093/-/healthy")
  '';
}
