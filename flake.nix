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

        collector-rules = pkgs.runCommand "fibre-collector-rules" { nativeBuildInputs = [ pkgs.prometheus.cli ]; } ''
          cd ${./alerts}
          promtool check rules fibre.rules.yml
          promtool test rules fibre.rules.test.yml
          touch $out
        '';

        collector-vm = import ./nix/tests/collector.nix {
          inherit pkgs;
          module = self.nixosModules.fibre-collector;
        };
      };
    };
}
