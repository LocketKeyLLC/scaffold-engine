CREATE TABLE IF NOT EXISTS dedup_log (
    id SERIAL PRIMARY KEY,
    new_content_hash VARCHAR(64) NOT NULL,
    existing_entry_id VARCHAR(255) NOT NULL,
    similarity_score FLOAT NOT NULL,
    action_taken VARCHAR(20) NOT NULL DEFAULT 'rejected',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_dedup_log_created_at ON dedup_log (created_at DESC);
CREATE INDEX idx_dedup_log_existing_entry ON dedup_log (existing_entry_id);
