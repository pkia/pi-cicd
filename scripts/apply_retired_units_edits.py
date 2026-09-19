#!/usr/bin/env python3
"""One-shot edits for the retired-list change (radar implementer 2026-09-19).

Every replacement asserts it matched exactly once, so a drift in the source
fails loudly instead of half-applying.

Note to self (the bug this file was written twice for): a replacement held in
a single-quoted literal may not contain a single quote — write joins as
"x".join(...) so the literals stay valid.
"""
from pathlib import Path

REPO = Path("/home/ev/pi-cicd")


def edit(rel, old, new, count=1):
    p = REPO / rel
    text = p.read_text(encoding="utf-8")
    assert text.count(old) == count, f"{rel}: {text.count(old)} matches for {old[:60]!r}"
    p.write_text(text.replace(old, new), encoding="utf-8")
    print(f"edited {rel}: +{len(new.splitlines()) - len(old.splitlines())} lines")


# ---- pi-doctor: read the shared list, the owner's decision comes first
edit("pi-doctor",
     "from datetime import datetime, timedelta\n\nimport ntfy_lib",
     "from datetime import datetime, timedelta\n\nimport ntfy_lib\nimport retired_units")

edit("pi-doctor",
     '    rc, out = run(["systemctl", "is-enabled", svc])\n'
     '    return out.strip() in ("disabled", "masked")\n',
     '    if retired_units.is_retired(svc):\n'
     '        # The shared list (./retired-units) is the owner\'s decision;\n'
     '        # `systemctl disable` is only its derived signal, and it is how\n'
     '        # this knowledge ended up in three places in the first place.\n'
     '        return True\n'
     '    rc, out = run(["systemctl", "is-enabled", svc])\n'
     '    return out.strip() in ("disabled", "masked")\n')

# ---- service-probe: a retired unit in the probe config is named at once
edit("service-probe",
     "from pathlib import Path\n\nimport ntfy_lib",
     "from pathlib import Path\n\nimport ntfy_lib\nimport retired_units")

edit("service-probe",
     '              + ", ".join(long_dead))\n',
     '              + ", ".join(long_dead))\n'
     '\n'
     '    # A probe row named after a retired unit is a config leftover the\n'
     '    # moment it is written, not 1000 sweeps later. Name it every sweep\n'
     '    # so the next reader of the journal drops the row. Publishes\n'
     '    # nothing, exactly like the long-dead list above.\n'
     '    retired_rows = {}\n'
     '    for name, url in cfg["http"]:\n'
     '        hit = retired_units.hits(f"{name} {url}")\n'
     '        if hit:\n'
     '            retired_rows[name] = ",".join(hit)\n'
     '    for name, server, domain in cfg["dns"]:\n'
     '        hit = retired_units.hits(f"{name} {server} {domain}")\n'
     '        if hit:\n'
     '            retired_rows[name] = ",".join(hit)\n'
     '    if retired_rows:\n'
     '        print("! probe(s) target a retired unit — drop from "\n'
     '              "PROBE_HTTP/PROBE_DNS: "\n'
     '              + ", ".join(f"{n} ({h})"\n'
     '                          for n, h in sorted(retired_rows.items())))\n')

edit("service-probe",
     '        print(f"  {key}: {status}{lat}{fails}{err}{stale}")\n',
     '        if not stale:\n'
     '            hit = retired_units.hits(key)\n'
     '            if hit:\n'
     '                stale = (" (retired unit " + "/".join(hit) + "; drop "\n'
     '                         "from PROBE_HTTP/PROBE_DNS)")\n'
     '        print(f"  {key}: {status}{lat}{fails}{err}{stale}")\n')

# ---- the unit-index test reads the one file instead of its own copy
edit("tests/test_units_doc.py",
     "from pathlib import Path\n\nREPO = ",
     "from pathlib import Path\n\nimport retired_units\n\nREPO = ")

edit("tests/test_units_doc.py",
     "# Owner-retired 2026-09-15 (units + deploy timers disabled, code kept).\n"
     "# They must not come back as live index rows or as probe targets.\n"
     'RETIRED_UNITS = {"cs2-dashboard", "cs2-tracker", "mark-site"}\n'
     "# Probe rows use shorter names than the units do (`cs2-dash`, funnel-side\n"
     "# `cs2trk`), so the probe check matches on every spelling that has been\n"
     "# used rather than only the unit name.\n"
     'RETIRED_PROBE_TOKENS = RETIRED_UNITS | {"cs2-dash", "cs2trk"}\n',
     "# Owner-retired 2026-09-15 (units + deploy timers disabled, code kept),\n"
     "# names kept in ./retired-units — the one list pi-doctor, service-probe\n"
     "# and this file read through `retired_units.py`, so a retirement is one\n"
     "# edit and probe rows keep matching on every spelling the list carries\n"
     "# (`cs2-dash`, funnel-side `cs2trk`).\n"
     "RETIRED = retired_units.load()\n"
     "RETIRED_UNITS = set(RETIRED.units)\n"
     "RETIRED_PROBE_TOKENS = set(RETIRED.tokens)\n")

# ---- docs: the layer page points at the one list
edit("docs/units.md",
     "  never starts it. cs2-tracker, cs2-dashboard and mark-site were retired\n"
     "  on 2026-09-15 (units **and** their deploy timers disabled, code kept),\n"
     "  and the daily audit used to revive all three every morning.",
     "  never starts it. cs2-tracker, cs2-dashboard and mark-site were retired\n"
     "  on 2026-09-15 (units **and** their deploy timers disabled, code kept),\n"
     "  and the daily audit used to revive all three every morning. The names\n"
     "  live in one file, [`retired-units`](../retired-units), read through\n"
     "  `retired_units.py` by the doctor, the prober and the index tests —\n"
     "  retiring a unit is one edit, and a name that is retired and still\n"
     "  listed as live fails CI instead of disagreeing quietly.")

print("all edits applied")
