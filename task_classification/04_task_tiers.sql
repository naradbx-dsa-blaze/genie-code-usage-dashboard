-- Low / Medium / High tier per task category, derived from data (not preset):
-- cut-offs are the 33rd / 67th percentile of estimated DBUs per task across all tasks;
-- a category's tier is where its median task falls. Total DBUs (free + billed) is used
-- because free-allowance usage has no $ price but is still consumption.
CREATE OR REPLACE VIEW {{TARGET}}.genie_code_task_tiers AS
WITH t AS (
  SELECT * FROM {{TARGET}}.genie_code_task_cost WHERE trace_id IS NOT NULL
),
cut AS (
  SELECT percentile_approx(est_total_dbus, 0.33) AS p33,
         percentile_approx(est_total_dbus, 0.67) AS p67
  FROM t
),
cat AS (
  SELECT
    task_category,
    COUNT(*)                                  AS tasks,
    COUNT(DISTINCT user_name)                 AS users,
    percentile_approx(est_total_dbus, 0.5)    AS median_dbus_per_task,
    AVG(est_total_dbus)                       AS avg_dbus_per_task,
    percentile_approx(model_calls, 0.5)       AS median_model_calls,
    percentile_approx(duration_ms, 0.5) / 1000 AS median_duration_s,
    SUM(est_total_dbus)                       AS total_dbus,
    SUM(est_list_cost_usd)                    AS total_billed_cost_usd
  FROM t
  GROUP BY task_category
)
SELECT
  cat.*,
  CASE WHEN median_dbus_per_task <= cut.p33 THEN 'Low'
       WHEN median_dbus_per_task <= cut.p67 THEN 'Medium'
       ELSE 'High' END AS cost_tier,
  cut.p33 AS low_max_dbus,
  cut.p67 AS medium_max_dbus
FROM cat CROSS JOIN cut;
