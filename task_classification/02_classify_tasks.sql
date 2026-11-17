-- Incremental: classifies only new turns. Schedule hourly (SQL file task in a job).
-- Stores the category and metrics only, NOT the prompt, so this table is safe for dashboards.
-- Edit the label list to match the customer's task categories.
CREATE TABLE IF NOT EXISTS {{TARGET}}.genie_code_task_class (
  trace_id       STRING,
  `date`         DATE,
  `time`         TIMESTAMP,
  start_time     TIMESTAMP,
  workspace_id   STRING,
  user_name      STRING,
  task_category  STRING,
  model_calls    BIGINT,
  tool_calls     BIGINT,
  input_tokens   BIGINT,
  output_tokens  BIGINT,
  duration_ms    DOUBLE,
  run_outcome    STRING,
  tools          ARRAY<STRING>,
  classified_at  TIMESTAMP
);

INSERT INTO {{TARGET}}.genie_code_task_class
SELECT
  t.trace_id, t.`date`, t.`time`, t.start_time, t.workspace_id, t.user_name,
  ai_classify(
    concat('Tools used: ', array_join(t.tools, ', '),
           '\nUser request: ', substring(COALESCE(t.prompt, ''), 1, 4000)),
    array('Build dashboard', 'Analyze or explore data', 'Write or edit code',
          'Debug or fix errors', 'Build data pipeline', 'Build ML model or agent',
          'Explain or answer a question', 'Other')
  ) AS task_category,
  t.model_calls, t.tool_calls, t.input_tokens, t.output_tokens, t.duration_ms,
  t.run_outcome, t.tools, current_timestamp()
FROM {{TARGET}}.genie_code_turns t
WHERE t.`date` >= date_sub(current_date(), 35)
  -- wait 2h so all child spans of a turn have landed before measuring it
  AND t.`time` < timestampadd(HOUR, -2, current_timestamp())
  AND NOT EXISTS (SELECT 1 FROM {{TARGET}}.genie_code_task_class c WHERE c.trace_id = t.trace_id);
