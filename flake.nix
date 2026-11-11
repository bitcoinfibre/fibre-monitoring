{
  description = "FIBRE monitoring for NixOS: the USDT exporter, and a Prometheus and Grafana collector for it";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      packages.${system} = rec {
        fibre-exporter = pkgs.callPackage ./nix/package.nix { };
        default = fibre-exporter;
      };

      # For FIBRE nodes: the exporter traces the node's bitcoind and follows
      # its systemd unit.
      nixosModules.fibre-exporter = ./nix/module.nix;
      nixosModules.default = self.nixosModules.fibre-exporter;

      # For the monitoring host: scrapes the exporters, evaluates the alert
      # rules and serves the dashboards.
      nixosModules.fibre-collector = ./nix/collector.nix;

      checks.${system} = {
        inherit (self.packages.${system}) fibre-exporter;

        metric-names = pkgs.runCommand "fibre-metric-names" { nativeBuildInputs = [ pkgs.python3 ]; } ''
          python3 ${./nix/tests/metric_names.py} ${./fibre_exporter.py} \
            ${./alerts}/*.yml ${./docker/grafana/provisioning/dashboards}/*.json
          touch $out
        '';

        collector-rules = pkgs.runCommand "fibre-collector-rules" { nativeBuildInputs = [ pkgs.prometheus.cli ]; } ''
          cd ${./alerts}
          promtool check rules fibre.rules.yml
          promtool test rules fibre.rules.test.yml
          touch $out
        '';

        dashboard-queries =
          pkgs.runCommand "fibre-dashboard-queries"
            {
              nativeBuildInputs = [
                pkgs.python3
                pkgs.prometheus.cli
              ];
            }
            ''
              python3 ${./nix/tests/dashboard_queries.py} ${./docker/grafana/provisioning/dashboards} \
                ${./nix/tests/dashboard-queries.test.yml} dashboard-queries.test.yml
              promtool test rules dashboard-queries.test.yml
              touch $out
            '';

        collector-vm = import ./nix/tests/collector.nix {
          inherit pkgs;
          module = self.nixosModules.fibre-collector;
        };
      };
    };
}
