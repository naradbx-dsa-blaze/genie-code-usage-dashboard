"""Fill placeholders in the task-classification SQL.
Usage: python3 render.py <trace_table> <target_catalog.schema> <ws_id>[,<ws_id>...] <out_dir>
"""
import os, sys
if len(sys.argv) != 5:
    sys.exit(__doc__)
trace, target, ws, out = sys.argv[1:]
ids = [w.strip() for w in ws.split(",") if w.strip()]
if not all(i.isdigit() for i in ids):
    sys.exit("workspace IDs must be numeric")
here = os.path.dirname(os.path.abspath(__file__))
os.makedirs(out, exist_ok=True)
for f in sorted(os.listdir(here)):
    if f.endswith(".sql"):
        s = open(os.path.join(here, f)).read()
        s = s.replace("{{TRACE_TABLE}}", trace).replace("{{TARGET}}", target)
        s = s.replace("{{WORKSPACE_IDS}}", ", ".join(f"'{i}'" for i in ids))
        open(os.path.join(out, f), "w").write(s)
        print("wrote", os.path.join(out, f))
