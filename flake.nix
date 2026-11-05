{
  description = "FIBRE monitoring for NixOS: the USDT exporter and its services.fibre-exporter module";

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

      # For hosts that run fibre-nix's services.fibre module; the exporter
      # reads its binary path and follows its unit.
      nixosModules.fibre-exporter = ./nix/module.nix;
      nixosModules.default = self.nixosModules.fibre-exporter;

      checks.${system} = {
        inherit (self.packages.${system}) fibre-exporter;
      };
    };
}
