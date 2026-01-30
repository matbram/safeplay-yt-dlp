"""
Pattern computation and analysis.

Analyzes telemetry data to discover patterns and compute statistics.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import defaultdict


class PatternAnalyzer:
    """Computes and stores patterns from telemetry data."""

    def __init__(self, supabase_client):
        self.supabase = supabase_client

    async def compute_all_patterns(self) -> dict:
        """Compute all pattern types and store them."""
        patterns = {}

        # Compute each pattern type
        patterns["player_client_ranking"] = await self._compute_player_client_ranking()
        patterns["country_ranking"] = await self._compute_country_ranking()
        patterns["hourly_success_rate"] = await self._compute_hourly_pattern()
        patterns["daily_success_rate"] = await self._compute_daily_pattern()
        patterns["error_frequency"] = await self._compute_error_frequency()
        patterns["overall_health"] = await self._compute_overall_health()

        # Store all patterns
        for pattern_type, data in patterns.items():
            await self._store_pattern(pattern_type, data)

        return patterns

    async def _store_pattern(
        self,
        pattern_type: str,
        data: dict,
        valid_hours: int = 6
    ) -> None:
        """Store a computed pattern."""
        record = {
            "pattern_type": pattern_type,
            "data": data,
            "sample_size": data.get("sample_size", 0),
            "time_window_hours": data.get("time_window_hours", 168),
            "valid_until": (datetime.now(timezone.utc) + timedelta(hours=valid_hours)).isoformat(),
        }

        self.supabase.table("agent_patterns").insert(record).execute()

    async def get_latest_pattern(self, pattern_type: str) -> Optional[dict]:
        """Get the most recent pattern of a given type."""
        response = self.supabase.table("agent_patterns") \
            .select("*") \
            .eq("pattern_type", pattern_type) \
            .order("computed_at", desc=True) \
            .limit(1) \
            .execute()

        if response.data:
            return response.data[0]
        return None

    async def get_all_latest_patterns(self) -> dict[str, dict]:
        """Get the latest of each pattern type."""
        pattern_types = [
            "player_client_ranking",
            "country_ranking",
            "hourly_success_rate",
            "daily_success_rate",
            "error_frequency",
            "overall_health",
        ]

        patterns = {}
        for pt in pattern_types:
            pattern = await self.get_latest_pattern(pt)
            if pattern:
                patterns[pt] = pattern["data"]

        return patterns

    async def _fetch_telemetry(self, hours: int = 168) -> list[dict]:
        """Fetch telemetry data for analysis."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        response = self.supabase.table("agent_telemetry") \
            .select("*") \
            .gte("created_at", cutoff) \
            .execute()
        return response.data or []

    async def _compute_player_client_ranking(self, hours: int = 168) -> dict:
        """Compute success rates by player client."""
        data = await self._fetch_telemetry(hours)

        if not data:
            return {"rankings": [], "sample_size": 0, "time_window_hours": hours}

        # Group by player client
        by_client: dict[str, dict] = defaultdict(lambda: {"total": 0, "success": 0, "durations": []})

        for row in data:
            client = row.get("player_client") or "unknown"
            by_client[client]["total"] += 1
            if row.get("success"):
                by_client[client]["success"] += 1
                if row.get("total_duration_ms"):
                    by_client[client]["durations"].append(row["total_duration_ms"])

        # Compute rankings
        rankings = []
        for client, stats in by_client.items():
            success_rate = (stats["success"] / stats["total"]) * 100 if stats["total"] > 0 else 0
            avg_duration = sum(stats["durations"]) / len(stats["durations"]) if stats["durations"] else None

            rankings.append({
                "player_client": client,
                "total_attempts": stats["total"],
                "successes": stats["success"],
                "success_rate": round(success_rate, 2),
                "avg_duration_ms": round(avg_duration) if avg_duration else None,
            })

        # Sort by success rate
        rankings.sort(key=lambda x: x["success_rate"], reverse=True)

        return {
            "rankings": rankings,
            "sample_size": len(data),
            "time_window_hours": hours,
            "best_client": rankings[0]["player_client"] if rankings else None,
        }

    async def _compute_country_ranking(self, hours: int = 168) -> dict:
        """Compute success rates by proxy country."""
        data = await self._fetch_telemetry(hours)

        if not data:
            return {"rankings": [], "sample_size": 0, "time_window_hours": hours}

        # Group by country
        by_country: dict[str, dict] = defaultdict(lambda: {"total": 0, "success": 0})

        for row in data:
            country = row.get("proxy_country") or "unknown"
            by_country[country]["total"] += 1
            if row.get("success"):
                by_country[country]["success"] += 1

        # Compute rankings
        rankings = []
        for country, stats in by_country.items():
            success_rate = (stats["success"] / stats["total"]) * 100 if stats["total"] > 0 else 0
            rankings.append({
                "country": country,
                "total_attempts": stats["total"],
                "successes": stats["success"],
                "success_rate": round(success_rate, 2),
            })

        rankings.sort(key=lambda x: x["success_rate"], reverse=True)

        return {
            "rankings": rankings,
            "sample_size": len(data),
            "time_window_hours": hours,
            "best_country": rankings[0]["country"] if rankings else None,
        }

    async def _compute_hourly_pattern(self, hours: int = 168) -> dict:
        """Compute success rates by hour of day."""
        data = await self._fetch_telemetry(hours)

        if not data:
            return {"hourly": [], "sample_size": 0, "time_window_hours": hours}

        # Group by hour
        by_hour: dict[int, dict] = {h: {"total": 0, "success": 0} for h in range(24)}

        for row in data:
            hour = row.get("hour_of_day")
            if hour is not None:
                by_hour[hour]["total"] += 1
                if row.get("success"):
                    by_hour[hour]["success"] += 1

        # Compute hourly stats
        hourly = []
        for hour in range(24):
            stats = by_hour[hour]
            success_rate = (stats["success"] / stats["total"]) * 100 if stats["total"] > 0 else 100
            hourly.append({
                "hour": hour,
                "total_attempts": stats["total"],
                "successes": stats["success"],
                "success_rate": round(success_rate, 2),
            })

        # Find best and worst hours
        active_hours = [h for h in hourly if h["total_attempts"] > 0]
        best_hour = max(active_hours, key=lambda x: x["success_rate"]) if active_hours else None
        worst_hour = min(active_hours, key=lambda x: x["success_rate"]) if active_hours else None

        return {
            "hourly": hourly,
            "sample_size": len(data),
            "time_window_hours": hours,
            "best_hour": best_hour["hour"] if best_hour else None,
            "worst_hour": worst_hour["hour"] if worst_hour else None,
            "best_hour_rate": best_hour["success_rate"] if best_hour else None,
            "worst_hour_rate": worst_hour["success_rate"] if worst_hour else None,
        }

    async def _compute_daily_pattern(self, hours: int = 168) -> dict:
        """Compute success rates by day of week."""
        data = await self._fetch_telemetry(hours)

        if not data:
            return {"daily": [], "sample_size": 0, "time_window_hours": hours}

        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        # Group by day
        by_day: dict[int, dict] = {d: {"total": 0, "success": 0} for d in range(7)}

        for row in data:
            day = row.get("day_of_week")
            if day is not None:
                by_day[day]["total"] += 1
                if row.get("success"):
                    by_day[day]["success"] += 1

        # Compute daily stats
        daily = []
        for day in range(7):
            stats = by_day[day]
            success_rate = (stats["success"] / stats["total"]) * 100 if stats["total"] > 0 else 100
            daily.append({
                "day": day,
                "day_name": day_names[day],
                "total_attempts": stats["total"],
                "successes": stats["success"],
                "success_rate": round(success_rate, 2),
            })

        return {
            "daily": daily,
            "sample_size": len(data),
            "time_window_hours": hours,
        }

    async def _compute_error_frequency(self, hours: int = 168) -> dict:
        """Compute frequency of error codes."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        response = self.supabase.table("agent_telemetry") \
            .select("error_code, error_message") \
            .eq("success", False) \
            .gte("created_at", cutoff) \
            .execute()

        data = response.data or []

        if not data:
            return {"errors": [], "sample_size": 0, "time_window_hours": hours}

        # Count by error code
        by_code: dict[str, dict] = defaultdict(lambda: {"count": 0, "examples": []})

        for row in data:
            code = row.get("error_code") or "unknown"
            by_code[code]["count"] += 1
            if len(by_code[code]["examples"]) < 3:
                msg = row.get("error_message", "")
                if msg and msg not in by_code[code]["examples"]:
                    by_code[code]["examples"].append(msg[:200])

        # Build error list
        errors = []
        total_errors = len(data)
        for code, info in by_code.items():
            errors.append({
                "error_code": code,
                "count": info["count"],
                "percentage": round((info["count"] / total_errors) * 100, 2),
                "examples": info["examples"],
            })

        errors.sort(key=lambda x: x["count"], reverse=True)

        return {
            "errors": errors[:20],  # Top 20
            "total_errors": total_errors,
            "sample_size": total_errors,
            "time_window_hours": hours,
            "most_common": errors[0]["error_code"] if errors else None,
        }

    async def _compute_overall_health(self, hours: int = 24) -> dict:
        """Compute overall system health metrics."""
        # Use the database function
        response = self.supabase.rpc("get_agent_health_summary").execute()

        if response.data:
            health = response.data
        else:
            health = {
                "success_rate_24h": 100.0,
                "total_downloads_24h": 0,
                "successful_downloads_24h": 0,
                "active_alerts": 0,
                "fixes_applied_24h": 0,
            }

        # Add trend data
        # Compare to previous 24 hours
        now = datetime.now(timezone.utc)
        cutoff_48h = (now - timedelta(hours=48)).isoformat()
        cutoff_24h = (now - timedelta(hours=24)).isoformat()
        prev_response = self.supabase.table("agent_telemetry") \
            .select("success") \
            .gte("created_at", cutoff_48h) \
            .lt("created_at", cutoff_24h) \
            .execute()

        prev_data = prev_response.data or []
        if prev_data:
            prev_total = len(prev_data)
            prev_success = sum(1 for r in prev_data if r.get("success"))
            prev_rate = (prev_success / prev_total) * 100 if prev_total > 0 else 100
            health["previous_24h_rate"] = round(prev_rate, 2)
            health["rate_change"] = round(health["success_rate_24h"] - prev_rate, 2)
        else:
            health["previous_24h_rate"] = None
            health["rate_change"] = None

        health["time_window_hours"] = hours
        health["sample_size"] = health.get("total_downloads_24h", 0)

        return health

    async def detect_anomalies(self) -> list[dict]:
        """Detect anomalies in recent patterns."""
        anomalies = []

        # Get current health
        health = await self._compute_overall_health()

        # Check for significant rate drops
        if health.get("rate_change") is not None and health["rate_change"] < -10:
            anomalies.append({
                "type": "success_rate_drop",
                "severity": "warning" if health["rate_change"] > -20 else "critical",
                "message": f"Success rate dropped by {abs(health['rate_change']):.1f}%",
                "data": health,
            })

        # Check for low success rate
        if health["success_rate_24h"] < 80:
            anomalies.append({
                "type": "low_success_rate",
                "severity": "warning" if health["success_rate_24h"] > 60 else "critical",
                "message": f"Success rate is {health['success_rate_24h']:.1f}%",
                "data": health,
            })

        # Check for error spikes
        errors = await self._compute_error_frequency(hours=1)
        total_errors = errors.get("sample_size", 0)
        if total_errors > 10:
            anomalies.append({
                "type": "error_spike",
                "severity": "warning",
                "message": f"{total_errors} errors in the last hour",
                "data": errors,
            })

        return anomalies

    async def get_recommended_config(self) -> dict:
        """Get recommended configuration based on patterns."""
        patterns = await self.get_all_latest_patterns()

        recommendations = {}

        # Recommend best player client
        client_ranking = patterns.get("player_client_ranking", {})
        if client_ranking.get("best_client"):
            recommendations["player_client"] = client_ranking["best_client"]
            recommendations["player_client_confidence"] = client_ranking["rankings"][0]["success_rate"] if client_ranking.get("rankings") else 0

        # Recommend best country
        country_ranking = patterns.get("country_ranking", {})
        if country_ranking.get("best_country"):
            recommendations["proxy_country"] = country_ranking["best_country"]

        # Identify hours to avoid
        hourly = patterns.get("hourly_success_rate", {})
        if hourly.get("worst_hour_rate") and hourly["worst_hour_rate"] < 70:
            recommendations["avoid_hours"] = [hourly["worst_hour"]]
            recommendations["avoid_hours_reason"] = f"Only {hourly['worst_hour_rate']:.1f}% success rate"

        return recommendations
