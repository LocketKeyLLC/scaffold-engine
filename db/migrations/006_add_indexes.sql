-- Migration 006: Add indexes on dag_nodes(domain) and performance_logs(job_id)
CREATE INDEX IF NOT EXISTS idx_dag_nodes_domain ON dag_nodes(domain);
CREATE INDEX IF NOT EXISTS idx_performance_logs_job_id ON performance_logs(job_id);
