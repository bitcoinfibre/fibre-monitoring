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
            ${./alerts}/*.yml ${./dashboards}/*.json
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
              python3 ${./nix/tests/dashboard_queries.py} ${./dashboards} \
                ${./nix/tests/dashboard-queries.test.yml} dashboard-queries.test.yml
              promtool test rules dashboard-queries.test.yml
              touch $out
            '';

        # The Docker stack against the collector module as Nix evaluates it.
        docker-matches-nix =
          let
            lib = nixpkgs.lib;
            evaluated = lib.nixosSystem {
              inherit system;
              modules = [
                self.nixosModules.fibre-collector
                {
                  services.fibre-collector = {
                    enable = true;
                    tls.caFile = "/ca.crt";
                    passwordFile = "/scrape-password";
                    grafana = {
                      adminPasswordFile = "/admin-password";
                      secretKeyFile = "/secret-key";
                    };
                  };
                  system.stateVersion = "26.05";
                }
              ];
            };
            inherit (evaluated.config.services) grafana prometheus;
            collector = pkgs.writeText "fibre-collector.json" (
              builtins.toJSON {
                grafana = {
                  inherit (grafana.package) version;
                  # Only what the collector module sets, not nixpkgs' defaults.
                  settings = map (d: d.value) (
                    builtins.filter (
                      d: lib.hasSuffix "/nix/collector.nix" d.file
                    ) evaluated.options.services.grafana.settings.definitionsWithLocations
                  );
                  datasource = builtins.head grafana.provision.datasources.settings.datasources;
                };
                prometheus = {
                  inherit (prometheus.package) version;
                  inherit (prometheus) globalConfig retentionTime listenAddress;
                  ruleFiles = map (file: baseNameOf (toString file)) prometheus.ruleFiles;
                  fibreJob = removeAttrs (lib.findFirst (
                    job: job.job_name == "fibre"
                  ) null prometheus.scrapeConfigs) [ "static_configs" ];
                };
              }
            );
            repository = lib.fileset.toSource {
              root = ./.;
              fileset = lib.fileset.unions [
                ./docker
                ./alerts
                ./dashboards
              ];
            };
          in
          pkgs.runCommand "fibre-docker-matches-nix"
            {
              nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pyyaml ])) ];
            }
            ''
              python3 ${./nix/tests/docker_matches_nix.py} ${collector} ${repository}
              touch $out
            '';

        collector-vm = import ./nix/tests/collector.nix {
          inherit pkgs;
          module = self.nixosModules.fibre-collector;
        };
      };
    };
}
