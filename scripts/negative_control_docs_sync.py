"""Negative control: do the new assertions actually catch the pre-change files?

Reads the committed (HEAD) copies out of git and applies exactly the
predicates added in tests/test_units_doc.py and project-hub tests/test_app.py.
Run from /home/ev/pi-cicd.
"""
import subprocess


def old(path):
    return subprocess.run(["git", "show", f"HEAD:{path}"],
                          capture_output=True, text=True).stdout


RETIRED = {"cs2-dashboard", "cs2-tracker", "mark-site"}

rows = {l.strip().strip("|").split("|")[0].strip()
        for l in old("docs/units.md").splitlines() if l.startswith("|")}
print("pre-change index rows hitting retired:", sorted(RETIRED & rows))

text = old("templates/service-probe.conf.example")
probed = {}
for line in text.splitlines():
    if line.startswith(("PROBE_HTTP=", "PROBE_DNS=")):
        for item in line.split("=", 1)[1].split(","):
            probed[item.split("=", 1)[0].strip()] = item
print("pre-change probe targets hitting retired:",
      {u: [v for k, v in probed.items() if u in k or u in v] for u in RETIRED})

print("pre-change layers.md claims cs2-tracker healthy gate:",
      "cs2-tracker's JSON `healthy` gate" in old("docs/layers.md"))
