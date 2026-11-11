# A collector for FIBRE exporters, built on nixpkgs' Prometheus and Grafana
# modules. Prometheus scrapes the exporters, by default over TLS with basic
# auth, and evaluates alerts/fibre.rules.yml. Grafana shows the dashboards
# from docker/grafana, the same ones the Docker setup provisions.
{
  config,
  lib,
  ...
}:
let
  inherit (lib) mkOption types;
  cfg = config.services.fibre-collector;

  # The dashboards only; the folder also holds Docker's provider config.
  dashboards = lib.fileset.toSource {
    root = ../docker/grafana/provisioning/dashboards;
    fileset = lib.fileset.fileFilter (file: file.hasExt "json") ../docker/grafana/provisioning/dashboards;
  };

  # A runtime path, so the secret stays out of the world-readable store.
  secretFile =
    description:
    mkOption {
      type = types.str;
      inherit description;
    };
  inStore = path: path != null && lib.hasPrefix builtins.storeDir path;
in
{
  options.services.fibre-collector = {
    enable = lib.mkEnableOption "the FIBRE collector: Prometheus and Grafana for FIBRE exporters";

    targets = mkOption {
      type = types.attrsOf types.str;
      default = { };
      example = {
        node1 = "10.0.0.1:9435";
      };
      description = ''
        Exporters to scrape, as node name = "host:port". The name becomes
        the `node` label, so an exporter can't report under another node's
        name.
      '';
    };

    scrapeInterval = mkOption {
      type = types.str;
      default = "10s";
      description = "How often to scrape the exporters.";
    };

    username = mkOption {
      type = types.str;
      default = "prometheus";
      description = "Basic auth user, the exporters' FIBRE_METRICS_AUTH_USERNAME.";
    };

    passwordFile = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "/run/secrets/fibre-collector/scrape-password";
      description = ''
        Runtime file holding the exporters' basic auth password, readable by
        the prometheus user. Null scrapes without authentication.
      '';
    };

    tls = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Scrape over HTTPS.";
      };
      caFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "/etc/fibre-collector/ca.crt";
        description = ''
          The CA that signed the exporters' certificates, such as
          generate-certs.sh's ca.crt. Each exporter's certificate must name
          the host or address its target uses.
        '';
      };
      insecureSkipVerify = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Accept any exporter certificate. The traffic is still encrypted,
          but anyone between the collector and an exporter can pose as it.
        '';
      };
    };

    retentionTime = mkOption {
      type = types.str;
      default = "30d";
      description = "How long Prometheus keeps samples.";
    };

    alertmanagers = mkOption {
      type = types.listOf types.str;
      default = lib.optional config.services.prometheus.alertmanager.enable "127.0.0.1:${toString config.services.prometheus.alertmanager.port}";
      defaultText = lib.literalMD "this host's Alertmanager, if `services.prometheus.alertmanager` is enabled";
      example = [ "alerts.example.org:9093" ];
      description = ''
        Alertmanagers that Prometheus sends firing alerts to, as "host:port".
        Where alerts go (email, chat, a webhook) is the Alertmanager's own
        configuration: enable `services.prometheus.alertmanager` on this host
        and set its routes and receivers. For an Alertmanager behind TLS or
        authentication, add an entry to `services.prometheus.alertmanagers`
        instead.
      '';
    };

    grafana = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Run Grafana with the FIBRE dashboards.";
      };
      address = mkOption {
        type = types.str;
        default = "127.0.0.1";
        description = "Address Grafana listens on.";
      };
      port = mkOption {
        type = types.port;
        default = 3000;
        description = "Port Grafana listens on.";
      };
      openFirewall = mkOption {
        type = types.bool;
        default = false;
        description = "Open Grafana's port in the firewall.";
      };
      adminUser = mkOption {
        type = types.str;
        default = "admin";
        description = "Grafana's admin user.";
      };
      adminPasswordFile = secretFile ''
        Runtime file holding the admin password, readable by the grafana
        user. Grafana sets it only when it first starts and creates the
        admin; changing the file later doesn't change the password. To
        rotate it, also run `grafana cli admin reset-admin-password` on the
        host.
      '';
      secretKeyFile = secretFile ''
        Runtime file holding Grafana's secret key, which signs stored
        datasource secrets, readable by the grafana user. Generate it once,
        e.g. with `openssl rand -hex 32`, and keep it: changing it makes
        stored secrets unreadable.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !cfg.tls.enable || cfg.tls.caFile != null || cfg.tls.insecureSkipVerify;
        message = "services.fibre-collector.tls: set caFile to the CA that signed the exporters' certificates, or insecureSkipVerify = true.";
      }
      {
        assertion = !(inStore cfg.passwordFile);
        message = "services.fibre-collector.passwordFile points into the Nix store, which is world-readable.";
      }
      {
        assertion = !cfg.grafana.enable || !(inStore cfg.grafana.adminPasswordFile || inStore cfg.grafana.secretKeyFile);
        message = "services.fibre-collector.grafana: adminPasswordFile and secretKeyFile must be outside the Nix store, which is world-readable.";
      }
    ];

    warnings =
      lib.optional (cfg.passwordFile == null) ''
        services.fibre-collector: passwordFile is unset, so the exporters are
        scraped without authentication.
      ''
      ++ lib.optional (cfg.tls.enable && cfg.tls.insecureSkipVerify) ''
        services.fibre-collector: tls.insecureSkipVerify accepts any exporter
        certificate.
      ''
      ++ lib.optional (cfg.alertmanagers == [ ] && config.services.prometheus.alertmanagers == [ ]) ''
        services.fibre-collector: no Alertmanager, so the alert rules are
        evaluated but nobody is notified. Enable services.prometheus.alertmanager
        with a receiver, or set alertmanagers.
      '';

    services.prometheus = {
      enable = true;
      # Grafana is the way in; Prometheus answers only on this host.
      listenAddress = "127.0.0.1";
      port = 9090;
      inherit (cfg) retentionTime;
      globalConfig = {
        scrape_interval = "15s";
        evaluation_interval = "15s";
      };
      ruleFiles = [ ../alerts/fibre.rules.yml ];
      alertmanagers = lib.optional (cfg.alertmanagers != [ ]) {
        static_configs = [ { targets = cfg.alertmanagers; } ];
      };
      # A local Alertmanager answers only on this host too, and doesn't
      # listen for cluster peers it doesn't have.
      alertmanager = {
        listenAddress = lib.mkDefault "127.0.0.1";
        extraFlags = lib.mkIf (config.services.prometheus.alertmanager.clusterPeers == [ ]) [
          "--cluster.listen-address="
        ];
      };
      scrapeConfigs = [
        {
          job_name = "prometheus";
          static_configs = [ { targets = [ "127.0.0.1:9090" ]; } ];
        }
        (
          {
            job_name = "fibre";
            scrape_interval = cfg.scrapeInterval;
            scheme = if cfg.tls.enable then "https" else "http";
            static_configs = lib.mapAttrsToList (node: address: {
              targets = [ address ];
              labels = { inherit node; };
            }) cfg.targets;
          }
          // lib.optionalAttrs (cfg.passwordFile != null) {
            basic_auth = {
              inherit (cfg) username;
              password_file = cfg.passwordFile;
            };
          }
          // lib.optionalAttrs cfg.tls.enable {
            tls_config =
              lib.optionalAttrs (cfg.tls.caFile != null) { ca_file = cfg.tls.caFile; }
              // lib.optionalAttrs cfg.tls.insecureSkipVerify { insecure_skip_verify = true; };
          }
        )
      ];
    };

    services.grafana = lib.mkIf cfg.grafana.enable {
      enable = true;
      settings = {
        server = {
          http_addr = cfg.grafana.address;
          http_port = cfg.grafana.port;
        };
        security = {
          admin_user = cfg.grafana.adminUser;
          admin_password = "$__file{${cfg.grafana.adminPasswordFile}}";
          secret_key = "$__file{${cfg.grafana.secretKeyFile}}";
        };
        users.allow_sign_up = false;
        # Nix pins Grafana, and nothing should change it behind Nix's back:
        # by default it installs and updates plugins from grafana.com at
        # startup. Nor does it report to or fetch anything from there.
        plugins = {
          preinstall_disabled = true;
          public_key_retrieval_disabled = true;
        };
        analytics = {
          reporting_enabled = false;
          check_for_updates = false;
          check_for_plugin_updates = false;
        };
        news.news_feed_enabled = false;
      };
      provision = {
        enable = true;
        datasources.settings.datasources = [
          {
            name = "Prometheus";
            type = "prometheus";
            # The dashboards refer to their datasource by this uid.
            uid = "prometheus";
            url = "http://127.0.0.1:9090";
            isDefault = true;
            jsonData = {
              httpMethod = "POST";
              # How often the exporters are scraped. Grafana sizes the
              # window of a rate from it, so a per-block chart draws each
              # block once: a wider window can show one block twice.
              timeInterval = cfg.scrapeInterval;
            };
          }
        ];
        dashboards.settings.providers = [
          {
            name = "FIBRE Dashboards";
            options.path = dashboards;
          }
        ];
      };
    };

    networking.firewall.allowedTCPPorts = lib.mkIf (cfg.grafana.enable && cfg.grafana.openFirewall) [
      cfg.grafana.port
    ];
  };
}
