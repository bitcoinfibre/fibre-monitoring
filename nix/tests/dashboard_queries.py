"""Fill a promtool test with the dashboards' own queries, so the test runs
what Grafana runs and can't drift from it.

A test names a query as @[<dashboard file> | <panel title> | <refId>] in an
expression. Each is replaced with that panel's query, in parentheses so the
test can build on it, and Grafana's interval variables set as Grafana sets
them when the time
range is short enough for the panel's minimum interval to apply: the
panel's own interval, or else the datasource's, the 10s scrape interval.

usage: dashboard_queries.py <dashboards dir> <test file> <output file>
"""

import json
import re
import sys
from pathlib import Path

SCRAPE_INTERVAL = 10
UNITS = {"s": 1, "m": 60, "h": 3600}


def seconds(interval):
    number, unit = re.fullmatch(r"(\d+)([smh])", interval).groups()
    return int(number) * UNITS[unit]


def panels(items):
    for panel in items:
        yield panel
        yield from panels(panel.get("panels", []))


def fill(expr, interval):
    # Grafana's $__rate_interval: at least four scrapes, and at least one
    # scrape more than the step.
    rate_interval = max(interval + SCRAPE_INTERVAL, 4 * SCRAPE_INTERVAL)
    # Longest name first, since $__interval is a prefix of $__interval_ms.
    for name, value in [
        ("$__interval_ms", str(interval * 1000)),
        ("$__interval", f"{interval}s"),
        ("$__rate_interval", f"{rate_interval}s"),
    ]:
        expr = expr.replace(name, value)
    return expr


def query(dashboards, ref):
    file, title, ref_id = (part.strip() for part in ref.split("|"))
    dashboard = json.loads((dashboards / file).read_text())
    for panel in panels(dashboard["panels"]):
        if panel.get("title") != title:
            continue
        for target in panel.get("targets", []):
            if target.get("refId") == ref_id:
                interval = target.get("interval") or panel.get("interval")
                expr = fill(target["expr"], seconds(interval) if interval else SCRAPE_INTERVAL)
                if "$" in expr:
                    sys.exit(f"{ref}: a variable is left unfilled in {expr}")
                return expr
    sys.exit(f"{ref}: no such query")


dashboards, test, out = (Path(arg) for arg in sys.argv[1:4])


def fill_line(line):
    # Only expressions: the test's comments may show the syntax.
    if not line.lstrip().startswith("- expr:"):
        return line
    return re.sub(
        r"@\[([^\]]+)\]",
        # The tests quote expressions in single quotes, where YAML writes ' as ''.
        lambda m: "(" + query(dashboards, m.group(1)).replace("'", "''") + ")",
        line,
    )


out.write_text("".join(fill_line(line) for line in test.read_text().splitlines(keepends=True)))
