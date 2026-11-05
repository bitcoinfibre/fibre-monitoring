# The exporter on a FIBRE node run by fibre-nix's services.fibre module. It
# runs as root, because attaching BPF programs needs it, and restarts with
# bitcoind to follow its PID.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkOption types;
  cfg = config.services.fibre-exporter;
  fibre = config.services.fibre;

  # USDT attaches by binary path and PID; both change on every deploy and
  # restart, so they are read when the exporter starts.
  runExporter = pkgs.writeShellScript "fibre-exporter-start" ''
    set -eu
    pid=$(${pkgs.systemd}/bin/systemctl show --property MainPID --value fibre.service)
    if [ "$pid" = 0 ]; then
      echo "fibre.service has no main process" >&2
      exit 1
    fi
    exec ${lib.getExe cfg.package} \
      --bitcoind ${fibre.package}/bin/bitcoind \
      --pid "$pid" \
      --node-name ${lib.escapeShellArg cfg.nodeName} \
      --port ${toString cfg.port} \
      --health-port ${toString cfg.healthPort}
  '';
in
{
  options.services.fibre-exporter = {
    enable = lib.mkEnableOption "the FIBRE USDT metrics exporter";

    package = mkOption {
      type = types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalMD "the exporter from this repository";
      description = "The fibre-monitoring exporter build.";
    };

    nodeName = mkOption {
      type = types.str;
      default = config.networking.hostName;
      defaultText = lib.literalExpression "config.networking.hostName";
      description = "Node label on the metrics.";
    };

    port = mkOption {
      type = types.port;
      default = 9435;
      description = "Prometheus metrics port.";
    };

    healthPort = mkOption {
      type = types.port;
      default = 9436;
      description = "Health check port.";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open the metrics port in the firewall.";
    };

    environmentFile = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "/run/secrets/fibre-exporter.env";
      description = ''
        Runtime file setting FIBRE_METRICS_AUTH_USERNAME,
        FIBRE_METRICS_AUTH_PASSWORD, FIBRE_TLS_CERT and FIBRE_TLS_KEY.
        Without it the metrics are served unauthenticated over plain HTTP.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        # The full path, so a host without fibre-nix's module fails here
        # instead of on the missing option.
        assertion = config.services.fibre.enable or false;
        message = "services.fibre-exporter traces the node of services.fibre; import fibre-nix's module and enable it.";
      }
      {
        assertion = cfg.environmentFile == null || !lib.hasPrefix builtins.storeDir cfg.environmentFile;
        message = "services.fibre-exporter.environmentFile points into the Nix store, which is world-readable.";
      }
    ];

    warnings = lib.optional (cfg.openFirewall && cfg.environmentFile == null) ''
      services.fibre-exporter: the metrics port is open without an environmentFile,
      so metrics are served unauthenticated over plain HTTP.
    '';

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

    # bcc compiles its BPF programs against the running kernel.
    programs.bcc.enable = true;

    systemd.services.fibre-exporter = {
      description = "FIBRE USDT metrics exporter";
      # Restart together with bitcoind: started by it, stopped with it,
      # ordered after its READY.
      wantedBy = [ "fibre.service" ];
      bindsTo = [ "fibre.service" ];
      after = [ "fibre.service" ];
      # bcc gets kernel headers by loading kheaders and unpacking
      # /sys/kernel/kheaders.tar.xz.
      path = [
        pkgs.kmod
        pkgs.gnutar
        pkgs.xz
      ];
      serviceConfig = {
        ExecStart = runExporter;
        Restart = "on-failure";
        RestartSec = 10;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
      }
      // lib.optionalAttrs (cfg.environmentFile != null) {
        EnvironmentFile = cfg.environmentFile;
      };
    };
  };
}
