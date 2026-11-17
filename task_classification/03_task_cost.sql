-- Estimated cost per task. Billing has no task ID, so each user's hourly Genie Code DBUs
-- are split across that user's tasks in that hour, weighted by tokens (or model calls if
-- tokens are absent for any task in that user-hour). Tasks are placed by start time. Billed hours with no traced task appear as 'Unattributed (no trace)'.
CREATE OR REPLACE VIEW {{TARGET}}.genie_code_task_cost AS
WITH tasks0 AS (
  SELECT *,
    date_trunc('HOUR', start_time) AS hour,
    COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) AS tokens
  FROM {{TARGET}}.genie_code_task_class
),
-- one weighting basis per user-hour: tokens only if every task in that hour has them
basis AS (
  SELECT hour, workspace_id, user_name, BOOL_AND(tokens > 0) AS use_tokens
  FROM tasks0 GROUP BY ALL
),
tasks AS (
  SELECT t.*,
    CASE WHEN b.use_tokens THEN t.tokens ELSE GREATEST(t.model_calls, 1) END AS weight
  FROM tasks0 t JOIN basis b USING (hour, workspace_id, user_name)
),
hour_weight AS (
  SELECT hour, workspace_id, user_name, SUM(weight) AS total_weight
  FROM tasks GROUP BY ALL
),
billing AS (
  SELECT
    u.usage_start_time AS hour,
    MIN(u.usage_date) AS usage_date,
    CAST(u.workspace_id AS STRING) AS workspace_id,
    u.identity_metadata.run_as AS user_name,
    SUM(CASE WHEN u.sku_name <> 'GENIE_FREE_USAGE' THEN u.usage_quantity ELSE 0 END) AS billed_dbus,
    SUM(CASE WHEN u.sku_name =  'GENIE_FREE_USAGE' THEN u.usage_quantity ELSE 0 END) AS free_dbus,
    SUM(CASE WHEN u.sku_name <> 'GENIE_FREE_USAGE'
             THEN u.usage_quantity * p.pricing.effective_list.default ELSE 0 END) AS est_list_cost_usd
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.billing_origin_product = 'GENIE'
    AND u.usage_metadata.genie.surface IN ('GENIE_CODE', 'GENIE_CODE_CLI')
    AND CAST(u.workspace_id AS STRING) IN ({{WORKSPACE_IDS}})
    AND u.usage_start_time >= (SELECT MIN(hour) FROM tasks)
  GROUP BY ALL
)
SELECT
  t.trace_id, t.`date`, t.workspace_id, t.user_name, t.task_category,
  t.model_calls, t.tool_calls, t.duration_ms,
  t.weight / w.total_weight                                         AS share_of_hour,
  COALESCE(b.billed_dbus, 0) * t.weight / w.total_weight            AS est_billed_dbus,
  COALESCE(b.free_dbus, 0) * t.weight / w.total_weight              AS est_free_dbus,
  (COALESCE(b.billed_dbus, 0) + COALESCE(b.free_dbus, 0)) * t.weight / w.total_weight AS est_total_dbus,
  COALESCE(b.est_list_cost_usd, 0) * t.weight / w.total_weight      AS est_list_cost_usd
FROM tasks t
JOIN hour_weight w USING (hour, workspace_id, user_name)
LEFT JOIN billing b USING (hour, workspace_id, user_name)
UNION ALL
SELECT
  NULL, b.usage_date, b.workspace_id, b.user_name, 'Unattributed (no trace)',
  NULL, NULL, NULL, NULL,
  b.billed_dbus, b.free_dbus, b.billed_dbus + b.free_dbus, b.est_list_cost_usd
FROM billing b
LEFT ANTI JOIN hour_weight w USING (hour, workspace_id, user_name);
