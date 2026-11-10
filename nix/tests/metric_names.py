"""Every exporter metric the dashboards and alert rules name is one the
exporter defines, so renaming a metric can't silently empty a panel or stop
an alert from firing.

usage: metric_names.py <fibre_exporter.py> <dashboard or rules file>...
"""

import re
import sys

source = open(sys.argv[1]).read()
# The series a metric is exposed as: counters gain _total, histograms
# _bucket, _sum and _count, Info metrics _info.
suffix = re.compile(r"_(total|bucket|sum|count|info)$")

missing = {}
for path in sys.argv[2:]:
    with open(path) as f:
        names = set(re.findall(r"\b(?:fibre|bitcoin)_[a-z0-9_]+", f.read()))
    for name in names:
        if name not in source and suffix.sub("", name) not in source:
            missing.setdefault(name, []).append(path.rsplit("/", 1)[-1])

for name, files in sorted(missing.items()):
    print(f"{name}: named in {', '.join(files)} but not defined by the exporter")
sys.exit(1 if missing else 0)
