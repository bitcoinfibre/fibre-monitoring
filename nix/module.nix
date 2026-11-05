# The exporter beside a FIBRE node's bitcoind. It runs as root, because
# attaching BPF programs needs it, and restarts with bitcoind to follow its
# PID.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkOption types;
  cfg = config.services.fibre-exporter;

  # USDT attaches by binary path and PID; both change on every deploy and
  # restart, so they are read when the exporter starts.
  runExporter = pkgs.writeShellScript "fibre-exporter-start" ''
    set -eu
    pid=$(${pkgs.systemd}/bin/systemctl show --property MainPID --value ${lib.escapeShellArg cfg.unit})
    if [ "$pid" = 0 ]; then
      echo "${cfg.unit} has no main process" >&2
      exit 1
    fi
    exec ${lib.getExe cfg.package} \
      --bitcoind ${lib.escapeShellArg cfg.bitcoind} \
      --pid "$pid" \
      --node-name ${lib.escapeShellArg cfg.nodeName} \
      --port ${toString cfg.port} \
      --health-port ${toString cfg.healthPort}
  '';
in
{
  options.services.fibre-exporter = {
    enable = lib.mkEnableOption "the FIBRE USDT metrics exporter";

    bitcoind = mkOption {
      type = types.str;
      example = lib.literalExpression ''"''${config.services.bitcoind.main.package}/bin/bitcoind"'';
      description = ''
        The node's bitcoind: a FIBRE build with USDT probes, the very file
        the node's unit runs, since the probes attach to it.
      '';
    };

    unit = mkOption {
      type = types.str;
      example = "bitcoind-main.service";
      description = ''
        The node's systemd unit. The exporter starts and stops with it and
        traces its main process.
      '';
    };

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
        assertion = lib.hasPrefix "/" cfg.bitcoind;
        message = "services.fibre-exporter.bitcoind must be the absolute path of the node's bitcoind.";
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
      wantedBy = [ cfg.unit ];
      bindsTo = [ cfg.unit ];
      after = [ cfg.unit ];
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
