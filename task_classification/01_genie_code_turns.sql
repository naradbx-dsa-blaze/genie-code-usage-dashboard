-- One row per Genie Code turn (trace), with effort metrics from child spans.
-- CONTAINS PROMPTS: grant SELECT on this view only to admins. Dashboards never read it.
CREATE OR REPLACE VIEW {{TARGET}}.genie_code_turns AS
WITH spans AS (
  SELECT trace_id, `date`, `time`, name, parent_span_id, attributes, status,
         start_time_unix_nano, end_time_unix_nano
  FROM {{TRACE_TABLE}}
  WHERE service_name = 'genie-code-agent'
),
roots AS (
  SELECT
    trace_id,
    `date`,
    `time`,
    timestamp_micros(CAST(start_time_unix_nano / 1000 AS BIGINT)) AS start_time,
    attributes:['enduser.id']::string                         AS user_name,
    attributes:workspace_id::string                           AS workspace_id,
    attributes:['gen_ai.conversation.id']::string             AS conversation_id,
    attributes:['gen_ai.input.messages']::string              AS prompt,
    attributes:['databricks.genie_code.run.outcome']::string  AS run_outcome,
    status.code                                               AS status_code,
    (end_time_unix_nano - start_time_unix_nano) / 1e6         AS duration_ms
  FROM spans
  WHERE (parent_span_id IS NULL OR parent_span_id = '')
    AND attributes:workspace_id::string IN ({{WORKSPACE_IDS}})
),
children AS (
  SELECT
    trace_id,
    COUNT_IF(name = 'chat')                  AS model_calls,
    COUNT_IF(name LIKE 'tool.execute:%')     AS tool_calls,
    SUM(CASE WHEN name = 'chat' THEN attributes:['gen_ai.usage.input_tokens']::bigint END)  AS input_tokens,
    SUM(CASE WHEN name = 'chat' THEN attributes:['gen_ai.usage.output_tokens']::bigint END) AS output_tokens,
    array_sort(array_distinct(collect_list(
      CASE WHEN name LIKE 'tool.execute:%'
           THEN COALESCE(attributes:['gen_ai.tool.name']::string, substring(name, 14)) END
    ))) AS tools
  FROM spans
  WHERE parent_span_id IS NOT NULL AND parent_span_id <> ''
  GROUP BY trace_id
)
SELECT
  r.*,
  COALESCE(c.model_calls, 0)  AS model_calls,
  COALESCE(c.tool_calls, 0)   AS tool_calls,
  c.input_tokens,
  c.output_tokens,
  COALESCE(c.tools, array())  AS tools
FROM roots r
LEFT JOIN children c USING (trace_id);
