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

  meta = {
    description = "Prometheus exporter for FIBRE's USDT probes";
    homepage = "https://github.com/bitcoinfibre/fibre-monitoring";
    mainProgram = "fibre-exporter";
    platforms = lib.platforms.linux;
  };
}
