"""Create or update one Databricks SQL alert per user that emails that user when their
month-to-date Genie Code DBUs reach a percentage (default 50%) of a per-user monthly limit.

Each alert:
  - runs a query scoped to one user and the given workspace IDs
  - fires when pct_used >= --alert-at-pct
  - emails only that user, once per crossing (no repeats until it resets next month)
  - never blocks usage

Run it on a schedule (for example daily, as a Lakeflow Job Python task) so new users get an alert.
It never deletes alerts.

Examples:
  # Dry run for two users
  python3 sync_user_alerts.py --profile my-ws --warehouse-id abc123 --workspace-ids 1234567890 \\
      --users alice@example.com,bob@example.com --dry-run

  # All users with Genie Code usage this month
  python3 sync_user_alerts.py --profile my-ws --warehouse-id abc123 --workspace-ids 1234567890 \\
      --users ALL_ACTIVE --per-user-limit-dbus 150
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

NAME_PREFIX = "Genie Code usage alert - "
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# Month start in UTC, matching how billing dates usage.
MONTH_START_UTC = "trunc(to_date(convert_timezone('UTC', current_timestamp())), 'MM')"


def genie_code_filter(ws_list):
    return (
        "billing_origin_product = 'GENIE' "
        "AND usage_metadata.genie.surface IN ('GENIE_CODE', 'GENIE_CODE_CLI') "
        f"AND CAST(workspace_id AS STRING) IN ({ws_list}) "
        f"AND usage_date >= {MONTH_START_UTC}"
    )


def user_query(email, ws_list, limit_dbus):
    # Returns only the percentage. DBU figures are intentionally not exposed in the alert.
    return f"""SELECT
  ROUND(100 * COALESCE(SUM(usage_quantity), 0) / {limit_dbus}, 1) AS pct_used
FROM system.billing.usage
WHERE {genie_code_filter(ws_list)}
  AND identity_metadata.run_as = '{email}'"""


def alert_body(email, args, ws_list, parent_path):
    body = {
        "display_name": NAME_PREFIX + email,
        "query_text": user_query(email, ws_list, args.per_user_limit_dbus),
        "warehouse_id": args.warehouse_id,
        "evaluation": {
            "source": {"name": "pct_used", "display": "Percent of monthly limit used"},
            "comparison_operator": "GREATER_THAN_OR_EQUAL",
            "threshold": {"value": {"double_value": float(args.alert_at_pct)}},
            # The query always returns one row; treat an empty result as OK, not ERROR.
            "empty_result_state": "OK",
            "notification": {
                "subscriptions": [{"user_email": email}],
                # 0 = notify once when it triggers; no repeats until it returns to OK (next month).
                "retrigger_seconds": 0,
                "notify_on_ok": False,
            },
        },
        "schedule": {
            "quartz_cron_schedule": args.cron,
            "timezone_id": "UTC",
            "pause_status": "UNPAUSED",
        },
        "custom_summary": "Genie Code usage: {{QUERY_RESULT_VALUE}}% of your monthly limit",
        "custom_description": (
            "<p>Hi,</p>"
            "<p>Your Genie Code usage this month has reached <b>{{QUERY_RESULT_VALUE}}%</b> "
            "of your monthly limit.</p>"
            "<p>This is a notification only. Your access is not blocked. "
            "Usage data can lag by a few hours, and limits reset on the 1st of each month (UTC).</p>"
        ),
        "custom_template_format": "HTML",
    }
    if parent_path:
        body["parent_path"] = parent_path
    if args.run_as_sp:
        body["run_as"] = {"service_principal_name": args.run_as_sp}
    return body


class Api:
    """REST calls via the Databricks SDK (inside Databricks) or the Databricks CLI (local, --profile)."""

    def __init__(self, profile=None, cli=None):
        self.profile = profile
        if profile:
            self.cli = cli or os.environ.get("DATABRICKS_CLI") or shutil.which("databricks")
            if not self.cli:
                sys.exit("Databricks CLI not found; set --cli-path")
        else:
            from databricks.sdk import WorkspaceClient
            self.w = WorkspaceClient()

    def do(self, method, path, body=None):
        if not self.profile:
            return self.w.api_client.do(method, path, body=body)
        cmd = [self.cli, "api", method.lower(), path, "-p", self.profile]
        if body is not None:
            cmd += ["--json", json.dumps(body)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"{method} {path} failed: {(r.stderr or r.stdout).strip()[-800:]}")
        return json.loads(r.stdout) if r.stdout.strip() else {}


def run_sql(w, warehouse_id, sql):
    r = w.do("POST", "/api/2.0/sql/statements", body={
        "warehouse_id": warehouse_id, "statement": sql, "wait_timeout": "50s"})
    while r["status"]["state"] in ("PENDING", "RUNNING"):
        time.sleep(3)
        r = w.do("GET", f"/api/2.0/sql/statements/{r['statement_id']}")
    if r["status"]["state"] != "SUCCEEDED":
        sys.exit(f"SQL failed: {r['status'].get('error', {}).get('message')}")
    return r.get("result", {}).get("data_array") or []


def active_users(w, args, ws_list):
    rows = run_sql(w, args.warehouse_id, f"""SELECT DISTINCT identity_metadata.run_as
FROM system.billing.usage
WHERE {genie_code_filter(ws_list)} AND identity_metadata.run_as LIKE '%@%'
ORDER BY 1""")
    return [r[0] for r in rows]


def existing_alerts(w):
    found, token = {}, None
    while True:
        q = "/api/2.0/alerts?page_size=100" + (f"&page_token={token}" if token else "")
        r = w.do("GET", q)
        for a in r.get("alerts", []):
            if a.get("display_name", "").startswith(NAME_PREFIX) and a.get("lifecycle_state") != "DELETED":
                found[a["display_name"]] = a
        token = r.get("next_page_token")
        if not token:
            return found


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", help="~/.databrickscfg profile (omit when running inside Databricks)")
    p.add_argument("--warehouse-id", required=True)
    p.add_argument("--workspace-ids", required=True, help="comma-separated workspace IDs to count usage from")
    p.add_argument("--users", required=True,
                   help="comma-separated emails, or ALL_ACTIVE for every user with Genie Code usage this month")
    p.add_argument("--per-user-limit-dbus", type=float, default=150.0,
                   help="monthly DBU limit per user (default 150, the free allowance)")
    p.add_argument("--alert-at-pct", type=float, default=50.0, help="alert when usage reaches this %% (default 50)")
    p.add_argument("--cron", default="0 0 */4 * * ?",
                   help="Quartz cron, UTC (default every 4 hours; billing refreshes every few hours)")
    p.add_argument("--folder", help="workspace folder for the alerts (default: your home folder)")
    p.add_argument("--run-as-sp", help="service principal application ID the alerts run as (optional)")
    p.add_argument("--cli-path", help="path to the Databricks CLI (local runs with --profile)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    ws_ids = [x.strip() for x in args.workspace_ids.split(",") if x.strip()]
    if not ws_ids or not all(x.isdigit() for x in ws_ids):
        sys.exit("--workspace-ids must be numeric")
    if args.per_user_limit_dbus <= 0 or not 0 < args.alert_at_pct <= 100:
        sys.exit("--per-user-limit-dbus must be > 0 and --alert-at-pct between 0 and 100")
    ws_list = ", ".join(f"'{x}'" for x in ws_ids)

    w = Api(args.profile, args.cli_path)

    users = active_users(w, args, ws_list) if args.users == "ALL_ACTIVE" else \
        [u.strip() for u in args.users.split(",") if u.strip()]
    bad = [u for u in users if not EMAIL_RE.match(u)]
    if bad:
        sys.exit(f"invalid emails: {bad}")

    folder = args.folder
    if folder and not args.dry_run:
        w.do("POST", "/api/2.0/workspace/mkdirs", body={"path": folder})

    existing = {} if args.dry_run else existing_alerts(w)
    print(f"{len(users)} user(s); limit {args.per_user_limit_dbus} DBUs/month; alert at {args.alert_at_pct}%")
    for email in users:
        body = alert_body(email, args, ws_list, folder)
        name = body["display_name"]
        if args.dry_run:
            print(f"[dry-run] would create/update: {name}")
            continue
        if name in existing:
            upd = {k: v for k, v in body.items() if k not in ("parent_path",)}
            mask = ",".join(upd.keys())
            w.do("PATCH", f"/api/2.0/alerts/{existing[name]['id']}?update_mask={mask}", body=upd)
            print(f"updated: {name} ({existing[name]['id']})")
        else:
            r = w.do("POST", "/api/2.0/alerts", body=body)
            print(f"created: {name} ({r['id']})")


if __name__ == "__main__":
    main()
