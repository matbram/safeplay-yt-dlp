-- Intelligence Module Schema
-- Adds tables for smarter learning and fix correlation

-- Error Fingerprints: Cluster similar errors together
CREATE TABLE IF NOT EXISTS agent_error_fingerprints (
    fingerprint VARCHAR(16) PRIMARY KEY,
    error_type VARCHAR(100),
    key_patterns TEXT[] DEFAULT '{}',
    first_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    occurrence_count INTEGER DEFAULT 1,
    successful_fixes TEXT[] DEFAULT '{}',
    failed_fixes TEXT[] DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_fingerprints_error_type
ON agent_error_fingerprints(error_type);

CREATE INDEX IF NOT EXISTS idx_fingerprints_last_seen
ON agent_error_fingerprints(last_seen);

-- Fix Correlations: Track which fixes work for which errors
CREATE TABLE IF NOT EXISTS agent_fix_correlations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    error_fingerprint VARCHAR(16) REFERENCES agent_error_fingerprints(fingerprint),
    error_type VARCHAR(100) NOT NULL,
    fix_type VARCHAR(50) NOT NULL,
    success BOOLEAN NOT NULL,
    details JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for correlation lookups
CREATE INDEX IF NOT EXISTS idx_correlations_fingerprint
ON agent_fix_correlations(error_fingerprint);

CREATE INDEX IF NOT EXISTS idx_correlations_error_type
ON agent_fix_correlations(error_type);

CREATE INDEX IF NOT EXISTS idx_correlations_fix_type
ON agent_fix_correlations(fix_type, success);

-- Add last_validated_at column to agent_knowledge for confidence decay
ALTER TABLE agent_knowledge
ADD COLUMN IF NOT EXISTS last_validated_at TIMESTAMP WITH TIME ZONE;

-- Update existing entries to set last_validated_at = created_at
UPDATE agent_knowledge
SET last_validated_at = created_at
WHERE last_validated_at IS NULL;

-- View: Fix Success Rates by Error Type
CREATE OR REPLACE VIEW fix_success_rates AS
SELECT
    error_type,
    fix_type,
    COUNT(*) as total_attempts,
    SUM(CASE WHEN success THEN 1 ELSE 0 END) as successes,
    SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as failures,
    ROUND(
        (SUM(CASE WHEN success THEN 1 ELSE 0 END)::NUMERIC / COUNT(*)::NUMERIC) * 100,
        2
    ) as success_rate
FROM agent_fix_correlations
GROUP BY error_type, fix_type
ORDER BY error_type, success_rate DESC;

-- View: Most Common Errors
CREATE OR REPLACE VIEW common_errors AS
SELECT
    fingerprint,
    error_type,
    key_patterns,
    occurrence_count,
    first_seen,
    last_seen,
    successful_fixes,
    failed_fixes,
    CASE
        WHEN array_length(successful_fixes, 1) > 0 THEN 'has_fix'
        WHEN array_length(failed_fixes, 1) > 0 THEN 'needs_fix'
        ELSE 'unknown'
    END as fix_status
FROM agent_error_fingerprints
ORDER BY occurrence_count DESC;

-- Function: Get recommended fix for an error
CREATE OR REPLACE FUNCTION get_recommended_fix(
    p_fingerprint VARCHAR(16),
    p_error_type VARCHAR(100)
)
RETURNS TABLE (
    fix_type VARCHAR(50),
    confidence NUMERIC,
    attempts INTEGER
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        fc.fix_type,
        ROUND(
            (SUM(CASE WHEN fc.success THEN 1 ELSE 0 END)::NUMERIC / COUNT(*)::NUMERIC),
            3
        ) as confidence,
        COUNT(*)::INTEGER as attempts
    FROM agent_fix_correlations fc
    WHERE fc.error_fingerprint = p_fingerprint
       OR fc.error_type = p_error_type
    GROUP BY fc.fix_type
    HAVING COUNT(*) >= 2
    ORDER BY confidence DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql;

-- Function: Get intelligence summary
CREATE OR REPLACE FUNCTION get_intelligence_summary()
RETURNS TABLE (
    total_fingerprints BIGINT,
    total_correlations BIGINT,
    known_fixes BIGINT,
    avg_success_rate NUMERIC,
    top_error_type VARCHAR(100),
    best_fix_type VARCHAR(50)
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        (SELECT COUNT(*) FROM agent_error_fingerprints)::BIGINT as total_fingerprints,
        (SELECT COUNT(*) FROM agent_fix_correlations)::BIGINT as total_correlations,
        (SELECT COUNT(DISTINCT fix_type) FROM agent_fix_correlations WHERE success)::BIGINT as known_fixes,
        (SELECT ROUND(AVG(CASE WHEN success THEN 100 ELSE 0 END), 2) FROM agent_fix_correlations) as avg_success_rate,
        (SELECT error_type FROM agent_error_fingerprints ORDER BY occurrence_count DESC LIMIT 1) as top_error_type,
        (SELECT fix_type FROM agent_fix_correlations WHERE success GROUP BY fix_type ORDER BY COUNT(*) DESC LIMIT 1) as best_fix_type;
END;
$$ LANGUAGE plpgsql;

-- Grant access to the service role
GRANT ALL ON agent_error_fingerprints TO service_role;
GRANT ALL ON agent_fix_correlations TO service_role;
GRANT SELECT ON fix_success_rates TO service_role;
GRANT SELECT ON common_errors TO service_role;
