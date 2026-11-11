# The USDT exporter: bcc probes on a FIBRE bitcoind, served as Prometheus
# metrics.
{
  lib,
  stdenvNoCC,
  makeWrapper,
  python3,
}:
let
  python = python3.withPackages (ps: [
    ps.bcc
    ps.prometheus-client
  ]);
  script = ../fibre_exporter.py;
in
stdenvNoCC.mkDerivation {
  pname = "fibre-exporter";
  version = lib.head (builtins.match ''.*__version__ = "([^"]+)".*'' (builtins.readFile script));

  # Only the exporter itself, so changes to the dashboards, Docker files or
  # docs leave its store path, and the running service, alone.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = script;
  };

  nativeBuildInputs = [ makeWrapper ];
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    install -Dm644 fibre_exporter.py "$out/libexec/fibre_exporter.py"
    makeWrapper ${python.interpreter} "$out/bin/fibre-exporter" \
      --add-flags "$out/libexec/fibre_exporter.py" \
      --set PYTHONUNBUFFERED 1
    runHook postInstall
  '';

  # Starting it imports everything it needs, so a missing module or Python
  # dependency fails here rather than on a node. --version stops before it
  # touches BPF or bitcoind.
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    $out/bin/fibre-exporter --version
    # A scrape reads the metrics in the order FibreMetrics declares them, and
    # a block's series must be read before the height that names it.
    ${python.interpreter} - "$out/libexec" <<'EOF'
    import sys
    sys.path.insert(0, sys.argv[1])
    from prometheus_client import REGISTRY, generate_latest
    import fibre_exporter
    fibre_exporter.FibreMetrics()
    order = [line.split()[2] for line in generate_latest(REGISTRY).decode().splitlines() if line.startswith("# TYPE ")]
    block, height = "fibre_recent_block_reconstruction_seconds", "fibre_last_reconstructed_block_height"
    assert order.index(block) < order.index(height), f"{height} is scraped before {block}"
    EOF
    runHook postInstallCheck
  '';

  meta = {
    description = "Prometheus exporter for FIBRE's USDT probes";
    homepage = "https://github.com/bitcoinfibre/fibre-monitoring";
    mainProgram = "fibre-exporter";
    platforms = lib.platforms.linux;
  };
}
