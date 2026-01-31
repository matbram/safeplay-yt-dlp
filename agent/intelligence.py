"""
Intelligence module - Makes the agent smarter over time.

This module adds:
1. Fix Success Correlation - Track which fixes work for which errors
2. Confidence Decay - Age knowledge over time
3. Error Fingerprinting - Cluster similar errors
4. Smart Retry - Try different configs before escalating
5. Learning Reinforcement - Boost confidence when fixes work
"""

import hashlib
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Any
from dataclasses import dataclass, field

from .config import settings


@dataclass
class ErrorFingerprint:
    """Fingerprint for clustering similar errors."""

    fingerprint: str
    error_type: str
    key_patterns: list[str]
    first_seen: datetime
    last_seen: datetime
    occurrence_count: int = 1
    successful_fixes: list[str] = field(default_factory=list)
    failed_fixes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "error_type": self.error_type,
            "key_patterns": self.key_patterns,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "occurrence_count": self.occurrence_count,
            "successful_fixes": self.successful_fixes,
            "failed_fixes": self.failed_fixes,
        }


class ErrorFingerprintManager:
    """Manages error fingerprinting and clustering."""

    # Patterns to extract from error messages for fingerprinting
    FINGERPRINT_PATTERNS = [
        r"HTTP Error (\d{3})",
        r"(Sign in to confirm|age-restricted|private video|unavailable)",
        r"(bot detection|captcha|rate limit)",
        r"(extraction failed|unable to extract)",
        r"client ([a-z_]+)",
        r"(nsig|sig|cipher) extraction failed",
        r"(connection|timeout|network)",
    ]

    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self._cache: dict[str, ErrorFingerprint] = {}

    def generate_fingerprint(self, error_message: str, error_code: Optional[str] = None) -> str:
        """
        Generate a fingerprint for an error by extracting key patterns.

        This allows us to cluster similar errors even if the exact message differs.
        """
        message_lower = error_message.lower()
        patterns_found = []

        # Extract patterns
        for pattern in self.FINGERPRINT_PATTERNS:
            match = re.search(pattern, message_lower, re.IGNORECASE)
            if match:
                patterns_found.append(match.group(0).lower())

        # Add error code if present
        if error_code:
            patterns_found.append(f"code:{error_code.lower()}")

        # Sort for consistency
        patterns_found.sort()

        # Create fingerprint hash
        fingerprint_str = "|".join(patterns_found) if patterns_found else message_lower[:100]
        return hashlib.sha256(fingerprint_str.encode()).hexdigest()[:16]

    async def record_error(
        self,
        error_message: str,
        error_code: Optional[str] = None,
        error_type: Optional[str] = None,
    ) -> ErrorFingerprint:
        """Record an error occurrence and return its fingerprint."""
        fingerprint = self.generate_fingerprint(error_message, error_code)
        now = datetime.now(timezone.utc)

        # Check cache first
        if fingerprint in self._cache:
            fp = self._cache[fingerprint]
            fp.last_seen = now
            fp.occurrence_count += 1
            return fp

        # Check database
        try:
            response = self.supabase.table("agent_error_fingerprints") \
                .select("*") \
                .eq("fingerprint", fingerprint) \
                .single() \
                .execute()

            if response.data:
                # Update existing
                fp = ErrorFingerprint(
                    fingerprint=fingerprint,
                    error_type=response.data.get("error_type", error_type or "unknown"),
                    key_patterns=response.data.get("key_patterns", []),
                    first_seen=datetime.fromisoformat(response.data["first_seen"].replace("Z", "+00:00")),
                    last_seen=now,
                    occurrence_count=response.data.get("occurrence_count", 0) + 1,
                    successful_fixes=response.data.get("successful_fixes", []),
                    failed_fixes=response.data.get("failed_fixes", []),
                )

                # Update in database
                self.supabase.table("agent_error_fingerprints") \
                    .update({
                        "last_seen": now.isoformat(),
                        "occurrence_count": fp.occurrence_count,
                    }) \
                    .eq("fingerprint", fingerprint) \
                    .execute()

                self._cache[fingerprint] = fp
                return fp
        except Exception:
            pass  # Table might not exist yet

        # Create new fingerprint
        key_patterns = []
        for pattern in self.FINGERPRINT_PATTERNS:
            match = re.search(pattern, error_message.lower(), re.IGNORECASE)
            if match:
                key_patterns.append(match.group(0).lower())

        fp = ErrorFingerprint(
            fingerprint=fingerprint,
            error_type=error_type or "unknown",
            key_patterns=key_patterns,
            first_seen=now,
            last_seen=now,
        )

        # Try to store in database
        try:
            self.supabase.table("agent_error_fingerprints") \
                .insert(fp.to_dict()) \
                .execute()
        except Exception as e:
            print(f"Could not store error fingerprint: {e}")

        self._cache[fingerprint] = fp
        return fp

    async def record_fix_result(
        self,
        fingerprint: str,
        fix_type: str,
        success: bool,
    ) -> None:
        """Record whether a fix worked for this error type."""
        if fingerprint not in self._cache:
            return

        fp = self._cache[fingerprint]

        if success:
            if fix_type not in fp.successful_fixes:
                fp.successful_fixes.append(fix_type)
        else:
            if fix_type not in fp.failed_fixes:
                fp.failed_fixes.append(fix_type)

        # Update database
        try:
            self.supabase.table("agent_error_fingerprints") \
                .update({
                    "successful_fixes": fp.successful_fixes,
                    "failed_fixes": fp.failed_fixes,
                }) \
                .eq("fingerprint", fingerprint) \
                .execute()
        except Exception:
            pass

    async def get_recommended_fix(self, fingerprint: str) -> Optional[str]:
        """Get the most successful fix type for this error fingerprint."""
        if fingerprint in self._cache:
            fp = self._cache[fingerprint]
            if fp.successful_fixes:
                # Return most common successful fix
                return fp.successful_fixes[0]
        return None


class ConfidenceManager:
    """Manages confidence decay and reinforcement for knowledge entries."""

    # How much confidence decays per day without validation
    DECAY_RATE_PER_DAY = 0.02

    # Minimum confidence before knowledge is considered stale
    MIN_CONFIDENCE = 0.1

    # How much to boost confidence when a fix works
    VALIDATION_BOOST = 0.1

    # How much to decrease confidence when a fix fails
    INVALIDATION_PENALTY = 0.15

    def __init__(self, supabase_client):
        self.supabase = supabase_client

    async def apply_decay(self) -> int:
        """
        Apply confidence decay to all knowledge entries.

        Returns number of entries updated.
        """
        now = datetime.now(timezone.utc)
        updated_count = 0

        try:
            # Get all active knowledge entries
            response = self.supabase.table("agent_knowledge") \
                .select("id, confidence, last_validated_at, created_at") \
                .eq("status", "active") \
                .gt("confidence", self.MIN_CONFIDENCE) \
                .execute()

            for entry in response.data or []:
                # Calculate days since last validation (or creation)
                last_validated = entry.get("last_validated_at") or entry.get("created_at")
                if not last_validated:
                    continue

                last_validated_dt = datetime.fromisoformat(
                    last_validated.replace("Z", "+00:00")
                )
                days_since = (now - last_validated_dt).days

                if days_since > 0:
                    # Calculate decay
                    current_confidence = entry.get("confidence", 0.5)
                    decay = self.DECAY_RATE_PER_DAY * days_since
                    new_confidence = max(self.MIN_CONFIDENCE, current_confidence - decay)

                    if new_confidence != current_confidence:
                        # Update in database
                        self.supabase.table("agent_knowledge") \
                            .update({"confidence": new_confidence}) \
                            .eq("id", entry["id"]) \
                            .execute()
                        updated_count += 1

            return updated_count
        except Exception as e:
            print(f"Confidence decay error: {e}")
            return 0

    async def reinforce(self, knowledge_id: str, success: bool) -> float:
        """
        Reinforce a knowledge entry based on fix success.

        Returns new confidence value.
        """
        try:
            # Get current confidence
            response = self.supabase.table("agent_knowledge") \
                .select("confidence") \
                .eq("id", knowledge_id) \
                .single() \
                .execute()

            if not response.data:
                return 0.5

            current = response.data.get("confidence", 0.5)

            if success:
                new_confidence = min(1.0, current + self.VALIDATION_BOOST)
            else:
                new_confidence = max(self.MIN_CONFIDENCE, current - self.INVALIDATION_PENALTY)

            # Update
            now = datetime.now(timezone.utc)
            self.supabase.table("agent_knowledge") \
                .update({
                    "confidence": new_confidence,
                    "last_validated_at": now.isoformat(),
                    "times_validated": response.data.get("times_validated", 0) + 1,
                }) \
                .eq("id", knowledge_id) \
                .execute()

            return new_confidence
        except Exception as e:
            print(f"Confidence reinforce error: {e}")
            return 0.5


@dataclass
class RetryConfig:
    """Configuration for a retry attempt."""
    player_client: str
    use_proxy: bool
    description: str
    priority: int = 0


class SmartRetryManager:
    """
    Manages intelligent retry strategies based on past success patterns.

    Instead of immediately escalating, try different configurations that
    have worked for similar errors in the past.
    """

    # Standard player clients to try
    PLAYER_CLIENTS = [
        RetryConfig("ios", False, "iOS client (often bypasses restrictions)", priority=1),
        RetryConfig("android", False, "Android client", priority=2),
        RetryConfig("mweb", False, "Mobile web client", priority=3),
        RetryConfig("tv_embedded", False, "TV embedded client", priority=4),
        RetryConfig("web", True, "Web client with proxy", priority=5),
    ]

    def __init__(self, supabase_client, monitor):
        self.supabase = supabase_client
        self.monitor = monitor
        self._retry_history: dict[str, list[str]] = {}  # job_id -> attempted configs

    async def get_retry_configs(
        self,
        job_id: str,
        error_fingerprint: Optional[str] = None,
    ) -> list[RetryConfig]:
        """
        Get prioritized retry configurations for a failed download.

        Uses past success data to prioritize configs most likely to work.
        """
        # Get what we've already tried for this job
        tried = self._retry_history.get(job_id, [])

        # Get success patterns from telemetry
        success_rates = await self._get_client_success_rates()

        # Build prioritized list
        configs = []
        for config in self.PLAYER_CLIENTS:
            if config.player_client in tried:
                continue  # Already tried this one

            # Adjust priority based on success rate
            success_rate = success_rates.get(config.player_client, 0.5)
            adjusted_priority = config.priority - (success_rate * 5)  # Lower is better

            configs.append((adjusted_priority, config))

        # Sort by adjusted priority
        configs.sort(key=lambda x: x[0])

        return [c[1] for c in configs]

    async def _get_client_success_rates(self) -> dict[str, float]:
        """Get success rates by player client from recent telemetry."""
        try:
            # Get recent telemetry grouped by client
            response = self.supabase.table("agent_telemetry") \
                .select("player_client, success") \
                .gte("created_at", (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()) \
                .execute()

            # Calculate success rates
            client_stats: dict[str, dict[str, int]] = {}
            for record in response.data or []:
                client = record.get("player_client", "unknown")
                if client not in client_stats:
                    client_stats[client] = {"success": 0, "total": 0}

                client_stats[client]["total"] += 1
                if record.get("success"):
                    client_stats[client]["success"] += 1

            # Convert to rates
            rates = {}
            for client, stats in client_stats.items():
                if stats["total"] > 0:
                    rates[client] = stats["success"] / stats["total"]

            return rates
        except Exception:
            return {}

    async def attempt_retry(
        self,
        job_id: str,
        youtube_id: str,
        config: RetryConfig,
    ) -> bool:
        """
        Attempt to retry a download with a specific configuration.

        Returns True if retry was successful.
        """
        # Record attempt
        if job_id not in self._retry_history:
            self._retry_history[job_id] = []
        self._retry_history[job_id].append(config.player_client)

        # Update downloader config temporarily
        config_update = {
            "preferred_player_client": config.player_client,
        }

        if config.use_proxy:
            config_update["use_proxy"] = True

        try:
            # Apply config
            await self.monitor.update_config(config_update)

            # Note: The actual retry would need to be triggered through your
            # download queue system. This is a placeholder for the integration point.
            print(f"[SmartRetry] Would retry {youtube_id} with {config.player_client}")

            return False  # Placeholder - actual implementation depends on download queue
        except Exception as e:
            print(f"[SmartRetry] Error: {e}")
            return False

    def clear_retry_history(self, job_id: str) -> None:
        """Clear retry history for a job (call after success or giving up)."""
        if job_id in self._retry_history:
            del self._retry_history[job_id]


class FixCorrelationTracker:
    """
    Tracks which fixes work for which error types.

    Builds a model over time of fix -> error_type -> success_rate
    """

    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self._correlation_cache: dict[str, dict] = {}

    async def record_fix_attempt(
        self,
        error_fingerprint: str,
        error_type: str,
        fix_type: str,
        success: bool,
        details: Optional[dict] = None,
    ) -> None:
        """Record a fix attempt and its outcome."""
        try:
            self.supabase.table("agent_fix_correlations").insert({
                "error_fingerprint": error_fingerprint,
                "error_type": error_type,
                "fix_type": fix_type,
                "success": success,
                "details": details or {},
            }).execute()
        except Exception as e:
            print(f"Could not record fix correlation: {e}")

    async def get_best_fix(
        self,
        error_fingerprint: str,
        error_type: str,
    ) -> Optional[dict]:
        """
        Get the best fix for an error based on historical success.

        Returns dict with fix_type and confidence, or None if no data.
        """
        try:
            # Get all correlations for this error
            response = self.supabase.table("agent_fix_correlations") \
                .select("fix_type, success") \
                .eq("error_fingerprint", error_fingerprint) \
                .execute()

            if not response.data:
                # Try by error type instead
                response = self.supabase.table("agent_fix_correlations") \
                    .select("fix_type, success") \
                    .eq("error_type", error_type) \
                    .execute()

            if not response.data:
                return None

            # Calculate success rates by fix type
            fix_stats: dict[str, dict] = {}
            for record in response.data:
                fix_type = record["fix_type"]
                if fix_type not in fix_stats:
                    fix_stats[fix_type] = {"success": 0, "total": 0}

                fix_stats[fix_type]["total"] += 1
                if record["success"]:
                    fix_stats[fix_type]["success"] += 1

            # Find best fix
            best_fix = None
            best_rate = 0
            for fix_type, stats in fix_stats.items():
                if stats["total"] >= 2:  # Require at least 2 attempts
                    rate = stats["success"] / stats["total"]
                    if rate > best_rate:
                        best_rate = rate
                        best_fix = fix_type

            if best_fix:
                return {
                    "fix_type": best_fix,
                    "confidence": best_rate,
                    "attempts": fix_stats[best_fix]["total"],
                }

            return None
        except Exception as e:
            print(f"Error getting best fix: {e}")
            return None

    async def get_fix_statistics(self) -> dict:
        """Get overall fix success statistics."""
        try:
            response = self.supabase.table("agent_fix_correlations") \
                .select("fix_type, success") \
                .execute()

            stats: dict[str, dict] = {}
            for record in response.data or []:
                fix_type = record["fix_type"]
                if fix_type not in stats:
                    stats[fix_type] = {"success": 0, "failure": 0}

                if record["success"]:
                    stats[fix_type]["success"] += 1
                else:
                    stats[fix_type]["failure"] += 1

            # Add success rates
            for fix_type, data in stats.items():
                total = data["success"] + data["failure"]
                data["total"] = total
                data["success_rate"] = data["success"] / total if total > 0 else 0

            return stats
        except Exception:
            return {}


class IntelligenceEngine:
    """
    Main intelligence engine that coordinates all learning components.

    This is the "brain" that makes the agent smarter over time.
    """

    def __init__(self, supabase_client, monitor):
        self.fingerprints = ErrorFingerprintManager(supabase_client)
        self.confidence = ConfidenceManager(supabase_client)
        self.retry = SmartRetryManager(supabase_client, monitor)
        self.correlations = FixCorrelationTracker(supabase_client)

    async def process_failure(
        self,
        error_message: str,
        error_code: Optional[str] = None,
        error_type: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> dict:
        """
        Process a failure through the intelligence engine.

        Returns enriched context for better LLM analysis.
        """
        # Generate and record fingerprint
        fingerprint = await self.fingerprints.record_error(
            error_message=error_message,
            error_code=error_code,
            error_type=error_type,
        )

        # Check if we have a known fix for this error
        best_fix = await self.correlations.get_best_fix(
            error_fingerprint=fingerprint.fingerprint,
            error_type=error_type or "unknown",
        )

        # Get retry recommendations
        retry_configs = []
        if job_id:
            retry_configs = await self.retry.get_retry_configs(
                job_id=job_id,
                error_fingerprint=fingerprint.fingerprint,
            )

        return {
            "fingerprint": fingerprint.fingerprint,
            "error_type": fingerprint.error_type,
            "occurrence_count": fingerprint.occurrence_count,
            "first_seen": fingerprint.first_seen.isoformat(),
            "key_patterns": fingerprint.key_patterns,
            "historical_fixes": {
                "successful": fingerprint.successful_fixes,
                "failed": fingerprint.failed_fixes,
            },
            "recommended_fix": best_fix,
            "retry_configs": [
                {"client": c.player_client, "description": c.description}
                for c in retry_configs[:3]
            ],
        }

    async def record_fix_outcome(
        self,
        error_fingerprint: str,
        error_type: str,
        fix_type: str,
        success: bool,
        knowledge_id: Optional[str] = None,
    ) -> None:
        """
        Record the outcome of a fix attempt.

        This is how the agent learns what works.
        """
        # Record in fingerprint manager
        await self.fingerprints.record_fix_result(
            fingerprint=error_fingerprint,
            fix_type=fix_type,
            success=success,
        )

        # Record in correlation tracker
        await self.correlations.record_fix_attempt(
            error_fingerprint=error_fingerprint,
            error_type=error_type,
            fix_type=fix_type,
            success=success,
        )

        # Reinforce knowledge if applicable
        if knowledge_id:
            new_confidence = await self.confidence.reinforce(knowledge_id, success)
            print(f"[Intelligence] Knowledge {knowledge_id[:8]} confidence: {new_confidence:.2f}")

    async def periodic_maintenance(self) -> dict:
        """
        Run periodic intelligence maintenance tasks.

        Should be called periodically (e.g., every hour).
        """
        # Apply confidence decay
        decayed_count = await self.confidence.apply_decay()

        # Get fix statistics
        fix_stats = await self.correlations.get_fix_statistics()

        return {
            "decayed_entries": decayed_count,
            "fix_statistics": fix_stats,
        }

    def build_llm_context(self, intelligence_data: dict) -> str:
        """
        Build additional context for LLM from intelligence data.

        This context helps the LLM make better decisions.
        """
        lines = ["## Intelligence Context\n"]

        # Error fingerprint info
        if intelligence_data.get("occurrence_count", 0) > 1:
            lines.append(f"**This error has occurred {intelligence_data['occurrence_count']} times before.**")
            lines.append(f"Key patterns: {', '.join(intelligence_data.get('key_patterns', []))}")

        # Historical fix info
        historical = intelligence_data.get("historical_fixes", {})
        if historical.get("successful"):
            lines.append(f"\n**Fixes that worked for this error:** {', '.join(historical['successful'])}")
        if historical.get("failed"):
            lines.append(f"**Fixes that failed:** {', '.join(historical['failed'])}")

        # Recommended fix
        if intelligence_data.get("recommended_fix"):
            rec = intelligence_data["recommended_fix"]
            lines.append(f"\n**Recommended fix based on history:** {rec['fix_type']} "
                        f"(confidence: {rec['confidence']:.0%}, {rec['attempts']} attempts)")

        # Retry suggestions
        if intelligence_data.get("retry_configs"):
            lines.append("\n**Suggested retry configs (by success rate):**")
            for cfg in intelligence_data["retry_configs"]:
                lines.append(f"- {cfg['client']}: {cfg['description']}")

        return "\n".join(lines)
