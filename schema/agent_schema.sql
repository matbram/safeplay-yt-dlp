-- SafePlay AI Agent Schema
-- This schema creates the tables needed for the self-healing, self-learning download agent
-- Run this in your Supabase SQL Editor

-- ============================================================================
-- TELEMETRY: Raw download attempt data for pattern analysis
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_telemetry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),

    -- Download identity
    job_id TEXT NOT NULL,
    youtube_id TEXT NOT NULL,

    -- Outcome
    success BOOLEAN NOT NULL,
    error_code TEXT,
    error_message TEXT,
    error_category TEXT,  -- 'permanent', 'retryable', 'unknown'

    -- Timing breakdown
    total_duration_ms INTEGER,
    phase_1_duration_ms INTEGER,      -- Metadata extraction
    phase_2_duration_ms INTEGER,      -- CDN download
    phase_3_duration_ms INTEGER,      -- Supabase upload

    -- Configuration used
    player_client TEXT,
    proxy_country TEXT,
    proxy_session_id TEXT,
    ytdlp_version TEXT,
    config_snapshot JSONB,

    -- Video metadata (when available)
    video_title TEXT,
    video_duration_seconds INTEGER,
    video_category TEXT,
    file_size_bytes BIGINT,
    format_id TEXT,

    -- Retry context
    attempt_number INTEGER DEFAULT 1,
    was_cache_hit BOOLEAN DEFAULT FALSE,

    -- Temporal context for pattern analysis
    hour_of_day INTEGER,
    day_of_week INTEGER,

    -- Full error log for debugging
    verbose_logs TEXT
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_agent_telemetry_created_at ON agent_telemetry(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_telemetry_youtube_id ON agent_telemetry(youtube_id);
CREATE INDEX IF NOT EXISTS idx_agent_telemetry_success ON agent_telemetry(success);
CREATE INDEX IF NOT EXISTS idx_agent_telemetry_error_code ON agent_telemetry(error_code);
CREATE INDEX IF NOT EXISTS idx_agent_telemetry_player_client ON agent_telemetry(player_client);
CREATE INDEX IF NOT EXISTS idx_agent_telemetry_hour ON agent_telemetry(hour_of_day);

-- ============================================================================
-- KNOWLEDGE BASE: The explorer's journal - learnings about YouTube
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_knowledge (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- Classification
    category TEXT NOT NULL CHECK (category IN (
        'youtube_behavior',    -- How YouTube works (CDN, APIs, etc.)
        'error_pattern',       -- Specific error types and their causes
        'success_pattern',     -- What configurations work well
        'workaround',          -- Tricks to bypass issues
        'hypothesis',          -- Unconfirmed theories
        'ytdlp_knowledge',     -- Understanding yt-dlp internals
        'proxy_pattern',       -- Proxy-related learnings
        'temporal_pattern'     -- Time-based observations
    )),
    subcategory TEXT,

    -- Content - the actual learning
    title TEXT NOT NULL,
    observation TEXT NOT NULL,        -- What was observed
    explanation TEXT,                 -- Why it happens (if known)
    solution TEXT,                    -- How to handle it (if applicable)

    -- Supporting evidence
    evidence JSONB DEFAULT '{}',      -- Raw data that supports this learning
    example_errors TEXT[],            -- Example error messages
    example_telemetry_ids UUID[],     -- References to telemetry entries

    -- Confidence tracking - learnings prove themselves over time
    confidence FLOAT DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    times_validated INTEGER DEFAULT 0,
    times_invalidated INTEGER DEFAULT 0,
    last_validated_at TIMESTAMPTZ,

    -- Relationships
    tags TEXT[],
    related_entry_ids UUID[],
    superseded_by UUID REFERENCES agent_knowledge(id),

    -- Code changes associated with this learning
    git_commits TEXT[],
    files_modified TEXT[],

    -- Status
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'deprecated', 'superseded', 'investigating'))
);

-- Indexes for knowledge retrieval
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_category ON agent_knowledge(category);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_status ON agent_knowledge(status);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_confidence ON agent_knowledge(confidence DESC);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_tags ON agent_knowledge USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_updated ON agent_knowledge(updated_at DESC);

-- Full text search on knowledge content
CREATE INDEX IF NOT EXISTS idx_agent_knowledge_search ON agent_knowledge
    USING GIN(to_tsvector('english', coalesce(title, '') || ' ' || coalesce(observation, '') || ' ' || coalesce(explanation, '')));

-- ============================================================================
-- COMPUTED PATTERNS: Aggregated statistics refreshed periodically
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_patterns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    computed_at TIMESTAMPTZ DEFAULT NOW(),
    valid_until TIMESTAMPTZ,

    -- Pattern identification
    pattern_type TEXT NOT NULL CHECK (pattern_type IN (
        'player_client_ranking',     -- Success rates by player client
        'country_ranking',           -- Success rates by proxy country
        'hourly_success_rate',       -- Success by hour of day
        'daily_success_rate',        -- Success by day of week
        'error_frequency',           -- Most common errors
        'video_type_success',        -- Success by video category
        'duration_success',          -- Success by video duration
        'overall_health'             -- System-wide health metrics
    )),

    -- The computed data
    data JSONB NOT NULL,

    -- Computation metadata
    sample_size INTEGER,
    time_window_hours INTEGER,

    UNIQUE(pattern_type, computed_at)
);

CREATE INDEX IF NOT EXISTS idx_agent_patterns_type ON agent_patterns(pattern_type);
CREATE INDEX IF NOT EXISTS idx_agent_patterns_computed ON agent_patterns(computed_at DESC);

-- ============================================================================
-- AGENT ACTIONS: Log of everything the agent does (meta-learning)
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),

    -- What triggered this action
    trigger_type TEXT NOT NULL CHECK (trigger_type IN (
        'download_failure',          -- Reacting to a failed download
        'pattern_detection',         -- Detected concerning pattern
        'scheduled_maintenance',     -- Routine maintenance task
        'health_check',              -- Health check triggered action
        'manual_request'             -- Human requested action
    )),
    trigger_job_id TEXT,
    trigger_error_code TEXT,
    trigger_error_message TEXT,

    -- What action was taken
    action_type TEXT NOT NULL CHECK (action_type IN (
        'config_change',             -- Changed runtime configuration
        'code_fix',                  -- Modified code files
        'ytdlp_update',              -- Updated yt-dlp
        'service_restart',           -- Restarted the downloader service
        'retry_download',            -- Triggered a retry
        'escalation',                -- Escalated to human
        'knowledge_update',          -- Updated knowledge base
        'pattern_recompute',         -- Recomputed patterns
        'no_action'                  -- Analyzed but no action needed
    )),
    action_description TEXT NOT NULL,

    -- Details of what was changed
    config_changes JSONB,
    files_modified TEXT[],
    git_commit TEXT,
    git_branch TEXT,

    -- Outcome tracking
    outcome TEXT CHECK (outcome IN ('success', 'failure', 'partial', 'pending', 'unknown')),
    outcome_details TEXT,
    retry_succeeded BOOLEAN,

    -- Learning linkage
    knowledge_entry_id UUID REFERENCES agent_knowledge(id),
    used_knowledge_ids UUID[],        -- Knowledge entries consulted for this decision

    -- LLM interaction details (for debugging/improvement)
    llm_provider TEXT,                -- 'claude' or 'gemini'
    llm_model TEXT,
    llm_prompt_tokens INTEGER,
    llm_completion_tokens INTEGER,
    llm_reasoning TEXT                -- Summary of LLM's reasoning
);

CREATE INDEX IF NOT EXISTS idx_agent_actions_created ON agent_actions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_actions_type ON agent_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_agent_actions_outcome ON agent_actions(outcome);
CREATE INDEX IF NOT EXISTS idx_agent_actions_trigger_job ON agent_actions(trigger_job_id);

-- ============================================================================
-- AGENT ALERTS: Notifications sent to admins
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),

    -- Alert classification
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
    alert_type TEXT NOT NULL CHECK (alert_type IN (
        'download_failure',          -- Download failed after all retries
        'fix_applied',               -- Agent applied a fix
        'fix_failed',                -- Agent's fix didn't work
        'escalation',                -- Agent needs human help
        'pattern_anomaly',           -- Unusual pattern detected
        'health_degraded',           -- Overall health declining
        'ytdlp_update_available',    -- New yt-dlp version
        'ytdlp_updated',             -- yt-dlp was updated
        'system_maintenance'         -- Maintenance performed
    )),

    -- Content
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    details JSONB DEFAULT '{}',

    -- Related entities
    job_id TEXT,
    youtube_id TEXT,
    action_id UUID REFERENCES agent_actions(id),

    -- Status tracking
    acknowledged BOOLEAN DEFAULT FALSE,
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by TEXT,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,

    -- Notification delivery tracking
    email_sent BOOLEAN DEFAULT FALSE,
    email_sent_at TIMESTAMPTZ,
    website_notified BOOLEAN DEFAULT FALSE,
    website_notified_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_agent_alerts_created ON agent_alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_alerts_severity ON agent_alerts(severity);
CREATE INDEX IF NOT EXISTS idx_agent_alerts_acknowledged ON agent_alerts(acknowledged);
CREATE INDEX IF NOT EXISTS idx_agent_alerts_type ON agent_alerts(alert_type);

-- ============================================================================
-- AGENT CONFIG: Persistent agent configuration
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_config (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    updated_by TEXT DEFAULT 'system'
);

-- Insert default configuration
INSERT INTO agent_config (key, value) VALUES
    ('llm_provider', '"gemini"'),
    ('llm_model_analysis', '"gemini-1.5-flash"'),
    ('llm_model_synthesis', '"gemini-1.5-pro"'),
    ('max_fix_attempts_per_hour', '10'),
    ('max_llm_calls_per_hour', '50'),
    ('auto_push_enabled', 'true'),
    ('email_alerts_enabled', 'true'),
    ('website_alerts_enabled', 'true'),
    ('telemetry_retention_days', '30'),
    ('pattern_recompute_interval_hours', '6'),
    ('health_check_interval_minutes', '5')
ON CONFLICT (key) DO NOTHING;

-- ============================================================================
-- HELPER FUNCTIONS
-- ============================================================================

-- Function to update the updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply trigger to knowledge table
DROP TRIGGER IF EXISTS update_agent_knowledge_updated_at ON agent_knowledge;
CREATE TRIGGER update_agent_knowledge_updated_at
    BEFORE UPDATE ON agent_knowledge
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Function to search knowledge base
CREATE OR REPLACE FUNCTION search_agent_knowledge(
    search_query TEXT,
    category_filter TEXT DEFAULT NULL,
    min_confidence FLOAT DEFAULT 0.0,
    max_results INTEGER DEFAULT 10
)
RETURNS TABLE (
    id UUID,
    category TEXT,
    title TEXT,
    observation TEXT,
    solution TEXT,
    confidence FLOAT,
    relevance FLOAT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        k.id,
        k.category,
        k.title,
        k.observation,
        k.solution,
        k.confidence,
        ts_rank(
            to_tsvector('english', coalesce(k.title, '') || ' ' || coalesce(k.observation, '') || ' ' || coalesce(k.explanation, '')),
            plainto_tsquery('english', search_query)
        ) as relevance
    FROM agent_knowledge k
    WHERE
        k.status = 'active'
        AND k.confidence >= min_confidence
        AND (category_filter IS NULL OR k.category = category_filter)
        AND to_tsvector('english', coalesce(k.title, '') || ' ' || coalesce(k.observation, '') || ' ' || coalesce(k.explanation, ''))
            @@ plainto_tsquery('english', search_query)
    ORDER BY relevance DESC, k.confidence DESC
    LIMIT max_results;
END;
$$ LANGUAGE plpgsql;

-- Function to get current system health
CREATE OR REPLACE FUNCTION get_agent_health_summary()
RETURNS JSONB AS $$
DECLARE
    result JSONB;
    total_24h INTEGER;
    success_24h INTEGER;
    success_rate FLOAT;
    active_alerts INTEGER;
    recent_fixes INTEGER;
BEGIN
    -- Get 24h stats
    SELECT
        COUNT(*),
        COUNT(*) FILTER (WHERE success = true)
    INTO total_24h, success_24h
    FROM agent_telemetry
    WHERE created_at > NOW() - INTERVAL '24 hours';

    success_rate := CASE WHEN total_24h > 0 THEN (success_24h::FLOAT / total_24h) * 100 ELSE 100 END;

    -- Get active alerts
    SELECT COUNT(*) INTO active_alerts
    FROM agent_alerts
    WHERE acknowledged = false AND severity IN ('error', 'critical');

    -- Get recent fixes
    SELECT COUNT(*) INTO recent_fixes
    FROM agent_actions
    WHERE created_at > NOW() - INTERVAL '24 hours'
    AND action_type IN ('config_change', 'code_fix', 'ytdlp_update');

    result := jsonb_build_object(
        'success_rate_24h', round(success_rate::numeric, 2),
        'total_downloads_24h', total_24h,
        'successful_downloads_24h', success_24h,
        'active_alerts', active_alerts,
        'fixes_applied_24h', recent_fixes,
        'computed_at', NOW()
    );

    RETURN result;
END;
$$ LANGUAGE plpgsql;

-- Function to validate a knowledge entry (increment validation count)
CREATE OR REPLACE FUNCTION validate_knowledge(
    knowledge_id UUID,
    is_valid BOOLEAN
)
RETURNS VOID AS $$
BEGIN
    IF is_valid THEN
        UPDATE agent_knowledge
        SET
            times_validated = times_validated + 1,
            last_validated_at = NOW(),
            confidence = LEAST(confidence + 0.05, 1.0)
        WHERE id = knowledge_id;
    ELSE
        UPDATE agent_knowledge
        SET
            times_invalidated = times_invalidated + 1,
            confidence = GREATEST(confidence - 0.1, 0.0)
        WHERE id = knowledge_id;
    END IF;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- VIEWS FOR COMMON QUERIES
-- ============================================================================

-- Recent failures with context
CREATE OR REPLACE VIEW recent_failures AS
SELECT
    t.id,
    t.created_at,
    t.job_id,
    t.youtube_id,
    t.error_code,
    t.error_message,
    t.player_client,
    t.proxy_country,
    t.attempt_number,
    t.ytdlp_version
FROM agent_telemetry t
WHERE t.success = false
ORDER BY t.created_at DESC
LIMIT 100;

-- Player client performance
CREATE OR REPLACE VIEW player_client_performance AS
SELECT
    player_client,
    COUNT(*) as total_attempts,
    COUNT(*) FILTER (WHERE success) as successes,
    ROUND((COUNT(*) FILTER (WHERE success)::NUMERIC / NULLIF(COUNT(*), 0)) * 100, 2) as success_rate,
    AVG(total_duration_ms) FILTER (WHERE success) as avg_duration_ms
FROM agent_telemetry
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY player_client
ORDER BY success_rate DESC;

-- Hourly success pattern
CREATE OR REPLACE VIEW hourly_success_pattern AS
SELECT
    hour_of_day,
    COUNT(*) as total_attempts,
    COUNT(*) FILTER (WHERE success) as successes,
    ROUND((COUNT(*) FILTER (WHERE success)::NUMERIC / NULLIF(COUNT(*), 0)) * 100, 2) as success_rate
FROM agent_telemetry
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY hour_of_day
ORDER BY hour_of_day;

-- Active high-confidence knowledge
CREATE OR REPLACE VIEW active_knowledge AS
SELECT
    id,
    category,
    title,
    observation,
    solution,
    confidence,
    times_validated,
    updated_at
FROM agent_knowledge
WHERE status = 'active' AND confidence >= 0.5
ORDER BY confidence DESC, updated_at DESC;

-- ============================================================================
-- ROW LEVEL SECURITY (Optional - enable if needed)
-- ============================================================================

-- For now, we'll use service role access only (RLS disabled)
-- Enable these if you need more granular access control

-- ALTER TABLE agent_telemetry ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE agent_knowledge ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE agent_patterns ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE agent_actions ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE agent_alerts ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- CLEANUP FUNCTION (call periodically to manage storage)
-- ============================================================================

CREATE OR REPLACE FUNCTION cleanup_old_telemetry(retention_days INTEGER DEFAULT 30)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM agent_telemetry
    WHERE created_at < NOW() - (retention_days || ' days')::INTERVAL;

    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- GRANT PERMISSIONS (adjust as needed for your setup)
-- ============================================================================

-- These use the service role, so should have full access
-- If using anon or authenticated roles, add grants here

COMMENT ON TABLE agent_telemetry IS 'Raw download attempt data for pattern analysis and debugging';
COMMENT ON TABLE agent_knowledge IS 'The agents learning journal - accumulated knowledge about YouTube downloading';
COMMENT ON TABLE agent_patterns IS 'Computed statistics and patterns, refreshed periodically';
COMMENT ON TABLE agent_actions IS 'Log of all agent actions for meta-learning and auditing';
COMMENT ON TABLE agent_alerts IS 'Notifications sent to admins about agent activity';
COMMENT ON TABLE agent_config IS 'Persistent agent configuration';
