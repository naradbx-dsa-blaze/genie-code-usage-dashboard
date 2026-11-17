"""Build the Genie Code per-user usage dashboard (.lvdash.json).

Usage:
  python3 build.py <workspace_id>[,<workspace_id>...] [out.lvdash.json] [tasks_catalog.schema]

Pass tasks_catalog.schema (where task_classification/*.sql was deployed) to add the
"Cost by task type" page. That page reads only the classification tables, never prompts.

Workspace IDs are baked into every dataset's WHERE clause. With "share data
permissions" (embedded credentials), viewers can only run these fixed queries,
so they never see other workspaces' rows from the account-wide system tables.
"""
import json
import sys

if len(sys.argv) < 2:
    sys.exit(__doc__)
ws_ids = [w.strip() for w in sys.argv[1].split(",") if w.strip()]
if not all(w.isdigit() for w in ws_ids):
    sys.exit("workspace IDs must be numeric")
out = sys.argv[2] if len(sys.argv) > 2 else "genie_code_usage.lvdash.json"
TASKS = sys.argv[3] if len(sys.argv) > 3 else None
if TASKS and not all(part.replace("_", "").isalnum() for part in TASKS.split(".")) or (TASKS and TASKS.count(".") != 1):
    sys.exit("tasks schema must be catalog.schema")
WS = ", ".join(f"'{w}'" for w in ws_ids)

# ---------- SQL (all fixed; viewers cannot edit) ----------
WS_NAMES = (
    "ws AS (SELECT CAST(workspace_id AS STRING) AS workspace_id, "
    "COALESCE(workspace_name, CAST(workspace_id AS STRING)) AS workspace_name "
    f"FROM system.access.workspaces_latest WHERE CAST(workspace_id AS STRING) IN ({WS}))"
)

COST_BASE = f"""
cost AS (
  SELECT
    u.usage_date,
    CAST(u.workspace_id AS STRING) AS workspace_id,
    u.identity_metadata.run_as AS user_name,
    SUM(CASE WHEN u.sku_name <> 'GENIE_FREE_USAGE' THEN u.usage_quantity ELSE 0 END) AS billed_dbus,
    SUM(CASE WHEN u.sku_name = 'GENIE_FREE_USAGE' THEN u.usage_quantity ELSE 0 END) AS free_dbus,
    SUM(CASE WHEN u.sku_name <> 'GENIE_FREE_USAGE'
             THEN u.usage_quantity * p.pricing.effective_list.default ELSE 0 END) AS est_list_cost_usd
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name
   AND u.cloud = p.cloud
   AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.billing_origin_product = 'GENIE'
    AND u.usage_metadata.genie.surface IN ('GENIE_CODE', 'GENIE_CODE_CLI')
    AND u.usage_date >= DATE'2026-07-01'
    AND CAST(u.workspace_id AS STRING) IN ({WS})
  GROUP BY ALL
)"""

ACT_BASE = f"""
act AS (
  SELECT
    CAST(event_date AS DATE) AS usage_date,
    CAST(workspace_id AS STRING) AS workspace_id,
    initiated_by AS user_name,
    COUNT(*) AS messages
  FROM system.access.assistant_events
  WHERE CAST(event_date AS DATE) >= DATE'2026-07-01'
    AND CAST(workspace_id AS STRING) IN ({WS})
  GROUP BY ALL
)"""

SQL_COST_DAILY = f"""WITH {WS_NAMES},{COST_BASE}
SELECT c.usage_date, COALESCE(ws.workspace_name, c.workspace_id) AS workspace_name, c.user_name,
       c.billed_dbus, c.free_dbus, c.billed_dbus + c.free_dbus AS total_dbus, c.est_list_cost_usd
FROM cost c LEFT JOIN ws USING (workspace_id)"""

SQL_ACT_DAILY = f"""WITH {WS_NAMES},{ACT_BASE}
SELECT a.usage_date, COALESCE(ws.workspace_name, a.workspace_id) AS workspace_name, a.user_name, a.messages
FROM act a LEFT JOIN ws USING (workspace_id)"""

SQL_USER_MONTH = f"""WITH {WS_NAMES},{COST_BASE},{ACT_BASE},
cm AS (SELECT date_format(usage_date, 'yyyy-MM') AS month, workspace_id, user_name,
              SUM(billed_dbus) billed_dbus, SUM(free_dbus) free_dbus, SUM(est_list_cost_usd) est_list_cost_usd
       FROM cost GROUP BY ALL),
am AS (SELECT date_format(usage_date, 'yyyy-MM') AS month, workspace_id, user_name,
              SUM(messages) messages, COUNT(DISTINCT usage_date) active_days
       FROM act GROUP BY ALL)
SELECT COALESCE(cm.month, am.month) AS month,
       COALESCE(ws.workspace_name, COALESCE(cm.workspace_id, am.workspace_id)) AS workspace_name,
       COALESCE(cm.user_name, am.user_name) AS user_name,
       COALESCE(am.messages, 0) AS messages,
       COALESCE(am.active_days, 0) AS active_days,
       COALESCE(cm.free_dbus, 0) AS free_dbus,
       COALESCE(cm.billed_dbus, 0) AS billed_dbus,
       COALESCE(cm.free_dbus, 0) + COALESCE(cm.billed_dbus, 0) AS total_dbus,
       COALESCE(cm.est_list_cost_usd, 0) AS est_list_cost_usd
FROM cm FULL OUTER JOIN am
  ON cm.month = am.month AND cm.workspace_id = am.workspace_id AND cm.user_name = am.user_name
LEFT JOIN ws ON ws.workspace_id = COALESCE(cm.workspace_id, am.workspace_id)
ORDER BY month DESC, est_list_cost_usd DESC, total_dbus DESC, messages DESC"""

datasets = [
    {"name": "ds_cost_daily", "displayName": "Genie Code cost (daily, per user)", "queryLines": [SQL_COST_DAILY]},
    {"name": "ds_activity_daily", "displayName": "Genie Code messages (daily, per user)", "queryLines": [SQL_ACT_DAILY]},
    {"name": "ds_user_month", "displayName": "Genie Code per user per month", "queryLines": [SQL_USER_MONTH]},
]

# ---------- widget helpers ----------
USD = {"type": "number-currency", "currencyCode": "USD", "decimalPlaces": {"type": "max", "places": 2}}
NUM = {"type": "number", "decimalPlaces": {"type": "max", "places": 2}}
INT = {"type": "number", "decimalPlaces": {"type": "max", "places": 0}}


def pos(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


def text(name, line, p):
    return {"widget": {"name": name, "multilineTextboxSpec": {"lines": [line]}}, "position": p}


def counter(name, ds, fname, expr, title, fmt, p):
    return {
        "widget": {
            "name": name,
            "queries": [{"name": "main_query", "query": {
                "datasetName": ds, "fields": [{"name": fname, "expression": expr}], "disaggregated": False}}],
            "spec": {"version": 2, "widgetType": "counter",
                     "encodings": {"value": {"fieldName": fname, "displayName": title, "format": fmt}},
                     "frame": {"showTitle": True, "title": title}},
        },
        "position": p,
    }


def daily_chart(name, wtype, ds, yfields, title, p):
    fields = [{"name": "daily(usage_date)", "expression": 'DATE_TRUNC("DAY", `usage_date`)'}]
    fields += [{"name": f, "expression": e} for f, e, _ in yfields]
    y = {"scale": {"type": "quantitative"},
         "fields": [{"fieldName": f, "displayName": d} for f, _, d in yfields]}
    return {
        "widget": {
            "name": name,
            "queries": [{"name": "main_query", "query": {"datasetName": ds, "fields": fields, "disaggregated": False}}],
            "spec": {"version": 3, "widgetType": wtype,
                     "encodings": {"x": {"fieldName": "daily(usage_date)", "scale": {"type": "temporal"},
                                         "displayName": "Date (UTC)"}, "y": y},
                     "frame": {"showTitle": True, "title": title}},
        },
        "position": p,
    }


def filt(name, wtype, title, bindings, p):
    """bindings: list of (dataset, field)."""
    queries, enc = [], []
    for ds, field in bindings:
        qn = f"{ds}_{field}"
        queries.append({"name": qn, "query": {"datasetName": ds,
                        "fields": [{"name": field, "expression": f"`{field}`"}], "disaggregated": False}})
        enc.append({"fieldName": field, "displayName": title, "queryName": qn})
    return {
        "widget": {"name": name, "queries": queries,
                   "spec": {"version": 2, "widgetType": wtype, "encodings": {"fields": enc},
                            "frame": {"showTitle": True, "title": title}}},
        "position": p,
    }


# ---------- page: overview ----------
C, A, M = "ds_cost_daily", "ds_activity_daily", "ds_user_month"
overview = [
    text("title", "## Genie Code usage by user", pos(0, 0, 12, 1)),
    text("subtitle",
         "Genie Code only (notebooks, SQL editor, pipelines, dashboards, CLI). **Cost** = `system.billing.usage` "
         "(refreshes every few hours; est. at list price, not your contract rate). **Messages** = "
         "`system.access.assistant_events` (~1 hour delay). Messages and cost are separate measures. Times are UTC.",
         pos(0, 1, 12, 2)),
    counter("kpi_cost", C, "sum(est_list_cost_usd)", "SUM(`est_list_cost_usd`)",
            "Est. billed cost (list $)", USD, pos(0, 3, 3, 3)),
    counter("kpi_billed_dbus", C, "sum(billed_dbus)", "SUM(`billed_dbus`)", "Billed DBUs", NUM, pos(3, 3, 3, 3)),
    counter("kpi_free_dbus", C, "sum(free_dbus)", "SUM(`free_dbus`)", "Free-allowance DBUs", NUM, pos(6, 3, 3, 3)),
    counter("kpi_users", A, "countdistinct(user_name)", "COUNT(DISTINCT `user_name`)",
            "Active users", INT, pos(9, 3, 3, 3)),
    daily_chart("trend_dbus", "bar", C,
                [("sum(billed_dbus)", "SUM(`billed_dbus`)", "Billed DBUs"),
                 ("sum(free_dbus)", "SUM(`free_dbus`)", "Free DBUs")],
                "Daily Genie Code DBUs", pos(0, 6, 6, 5)),
    daily_chart("trend_messages", "line", A,
                [("sum(messages)", "SUM(`messages`)", "Messages")],
                "Daily Genie Code messages", pos(6, 6, 6, 5)),
    text("hdr_users", "### Per-user breakdown (by month)", pos(0, 11, 12, 1)),
    {
        "widget": {
            "name": "user_table",
            "queries": [{"name": "main_query", "query": {"datasetName": M, "fields": [
                {"name": f, "expression": f"`{f}`"} for f in
                ["month", "workspace_name", "user_name", "messages", "active_days",
                 "free_dbus", "billed_dbus", "total_dbus", "est_list_cost_usd"]],
                "disaggregated": True}}],
            "spec": {"version": 2, "widgetType": "table", "encodings": {"columns": [
                {"fieldName": "month", "displayName": "Month"},
                {"fieldName": "workspace_name", "displayName": "Workspace"},
                {"fieldName": "user_name", "displayName": "User"},
                {"fieldName": "messages", "displayName": "Messages"},
                {"fieldName": "active_days", "displayName": "Active days"},
                {"fieldName": "free_dbus", "displayName": "Free DBUs"},
                {"fieldName": "billed_dbus", "displayName": "Billed DBUs"},
                {"fieldName": "total_dbus", "displayName": "Total DBUs"},
                {"fieldName": "est_list_cost_usd", "displayName": "Est. billed cost (list $)"},
            ]}, "frame": {"showTitle": True, "title": "Genie Code usage per user"}},
        },
        "position": pos(0, 12, 12, 8),
    },
]

filters = [
    filt("f_date", "filter-date-range-picker", "Date range (cost & message charts)",
         [(C, "usage_date"), (A, "usage_date")], pos(0, 0, 4, 2)),
    filt("f_month", "filter-multi-select", "Month (per-user table)", [(M, "month")], pos(0, 2, 4, 2)),
    filt("f_workspace", "filter-multi-select", "Workspace",
         [(C, "workspace_name"), (A, "workspace_name"), (M, "workspace_name")], pos(0, 4, 4, 2)),
    filt("f_user", "filter-multi-select", "User",
         [(C, "user_name"), (A, "user_name"), (M, "user_name")], pos(0, 6, 4, 2)),
]


# ---------- optional page: cost by task type ----------
pages = [
    {"name": "overview", "displayName": "Genie Code usage", "pageType": "PAGE_TYPE_CANVAS",
     "layoutVersion": "GRID_V1", "layout": overview},
]
if TASKS:
    T, TT, TU = "ds_task_cost", "ds_task_tiers", "ds_task_user"
    datasets += [
        {"name": T, "displayName": "Genie Code est. cost per task", "queryLines": [f"""WITH {WS_NAMES}
SELECT c.`date` AS usage_date, COALESCE(ws.workspace_name, c.workspace_id) AS workspace_name, c.user_name,
       c.task_category, c.trace_id, c.est_total_dbus, c.est_billed_dbus, c.est_free_dbus, c.est_list_cost_usd,
       CASE WHEN c.trace_id IS NOT NULL THEN 1 ELSE 0 END AS is_task
FROM {TASKS}.genie_code_task_cost c LEFT JOIN ws USING (workspace_id)
WHERE c.workspace_id IN ({WS})"""]},
        {"name": TT, "displayName": "Genie Code task cost tiers", "queryLines": [f"""SELECT cost_tier, task_category, tasks, users,
       median_dbus_per_task, avg_dbus_per_task, median_model_calls, median_duration_s, total_dbus, total_billed_cost_usd,
       low_max_dbus, medium_max_dbus
FROM {TASKS}.genie_code_task_tiers
ORDER BY median_dbus_per_task DESC"""]},
        {"name": TU, "displayName": "Genie Code tasks per user and category", "queryLines": [f"""WITH {WS_NAMES}
SELECT date_format(c.`date`, 'yyyy-MM') AS month, COALESCE(ws.workspace_name, c.workspace_id) AS workspace_name,
       c.user_name, c.task_category, COUNT(c.trace_id) AS tasks, SUM(c.est_total_dbus) AS est_total_dbus,
       SUM(c.est_list_cost_usd) AS est_list_cost_usd
FROM {TASKS}.genie_code_task_cost c LEFT JOIN ws USING (workspace_id)
WHERE c.workspace_id IN ({WS})
GROUP BY ALL
ORDER BY month DESC, est_total_dbus DESC"""]},
    ]

    def cat_bar(name, fname, expr, title, disp, fmt, p):
        return {"widget": {
            "name": name,
            "queries": [{"name": "main_query", "query": {"datasetName": T, "fields": [
                {"name": "task_category", "expression": "`task_category`"},
                {"name": fname, "expression": expr}], "disaggregated": False}}],
            "spec": {"version": 3, "widgetType": "bar", "encodings": {
                "x": {"scale": {"type": "quantitative"}, "fields": [{"fieldName": fname, "displayName": disp, "format": fmt}]},
                "y": {"fieldName": "task_category", "scale": {"type": "categorical"},
                      "displayName": "Task type"}},
                "frame": {"showTitle": True, "title": title}}},
            "position": p}

    def table(name, ds, cols, title, p):
        return {"widget": {
            "name": name,
            "queries": [{"name": "main_query", "query": {"datasetName": ds, "fields": [
                {"name": f, "expression": f"`{f}`"} for f, _ in cols], "disaggregated": True}}],
            "spec": {"version": 2, "widgetType": "table", "encodings": {"columns": [
                {"fieldName": f, "displayName": d} for f, d in cols]}, "frame": {"showTitle": True, "title": title}}},
            "position": p}

    tasks_layout = [
        text("t_title", "## Genie Code cost by task type", pos(0, 0, 12, 1)),
        text("t_sub",
             "**Estimates.** Tasks come from Genie Code traces (Beta), classified with `ai_classify`. Billing has no task ID, "
             "so each user's hourly DBUs are split across their tasks in that hour by tokens (or model calls). "
             "Tiers are data-driven: Low/Medium/High = where a category's median task falls vs. the 33rd/67th percentile "
             "of all tasks. *Unattributed* = billed hours with no traced task (e.g. before tracing was on).",
             pos(0, 1, 12, 2)),
        counter("t_kpi_tasks", T, "sum(is_task)", "SUM(`is_task`)", "Classified tasks", INT, pos(0, 3, 4, 3)),
        counter("t_kpi_dbus", T, "sum(est_total_dbus)", "SUM(`est_total_dbus`)", "Total DBUs (incl. unattributed)", NUM, pos(4, 3, 4, 3)),
        counter("t_kpi_cost", T, "sum(est_list_cost_usd)", "SUM(`est_list_cost_usd`)", "Est. billed cost (list $)", USD, pos(8, 3, 4, 3)),
        table("t_tiers", TT, [
            ("cost_tier", "Cost tier"), ("task_category", "Task type"), ("tasks", "Tasks"), ("users", "Users"),
            ("median_dbus_per_task", "Median DBUs / task"), ("avg_dbus_per_task", "Avg DBUs / task"),
            ("median_model_calls", "Median model calls"), ("median_duration_s", "Median duration (s)"),
            ("low_max_dbus", "Low tier max DBUs"), ("medium_max_dbus", "Medium tier max DBUs")],
            "Cost tier by task type (all classified tasks)", pos(0, 6, 12, 5)),
        cat_bar("t_bar_dbus", "sum(est_total_dbus)", "SUM(`est_total_dbus`)", "Est. DBUs by task type",
                "Est. DBUs", NUM, pos(0, 11, 6, 5)),
        cat_bar("t_bar_tasks", "sum(is_task)", "SUM(`is_task`)", "Tasks by task type",
                "Tasks", INT, pos(6, 11, 6, 5)),
        table("t_user", TU, [
            ("month", "Month"), ("workspace_name", "Workspace"), ("user_name", "User"),
            ("task_category", "Task type"), ("tasks", "Tasks"), ("est_total_dbus", "Est. DBUs"),
            ("est_list_cost_usd", "Est. billed cost (list $)")],
            "Per user and task type (by month)", pos(0, 16, 12, 7)),
    ]
    pages.append({"name": "tasks", "displayName": "Cost by task type", "pageType": "PAGE_TYPE_CANVAS",
                  "layoutVersion": "GRID_V1", "layout": tasks_layout})
    # extend global filters to the task datasets
    for fw in filters:
        w = fw["widget"]
        name = w["name"]
        extra = {"f_date": [(T, "usage_date")], "f_month": [(TU, "month")],
                 "f_workspace": [(T, "workspace_name"), (TU, "workspace_name")],
                 "f_user": [(T, "user_name"), (TU, "user_name")]}[name]
        for ds, field in extra:
            qn = f"{ds}_{field}"
            w["queries"].append({"name": qn, "query": {"datasetName": ds,
                                 "fields": [{"name": field, "expression": f"`{field}`"}], "disaggregated": False}})
            w["spec"]["encodings"]["fields"].append(
                {"fieldName": field, "displayName": w["spec"]["frame"]["title"], "queryName": qn})

pages.append({"name": "filters", "displayName": "Filters", "pageType": "PAGE_TYPE_GLOBAL_FILTERS",
              "layoutVersion": "GRID_V1", "layout": filters})

dashboard = {"datasets": datasets, "pages": pages}

with open(out, "w") as f:
    json.dump(dashboard, f, indent=2)
print(f"wrote {out} for workspaces {ws_ids}")
