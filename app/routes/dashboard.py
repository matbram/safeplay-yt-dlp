"""
Dashboard API routes for the agent monitoring frontend.

Provides endpoints for real-time agent status, telemetry data,
pattern analysis, and system health metrics.
"""

from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone, timedelta
from typing import Optional
import asyncio

from app.config import settings

# Try to import supabase
try:
    from supabase import create_client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Cached Supabase client
_supabase_client = None


def get_supabase():
    """Get or create Supabase client."""
    global _supabase_client
    if not SUPABASE_AVAILABLE:
        return None
    if _supabase_client is None:
        try:
            _supabase_client = create_client(
                settings.SUPABASE_URL,
                settings.SUPABASE_SERVICE_KEY
            )
        except Exception:
            return None
    return _supabase_client


@router.get("/status")
async def get_dashboard_status():
    """Get overall system status for the dashboard."""
    supabase = get_supabase()

    # Check connections
    supabase_connected = False
    if supabase:
        try:
            supabase.table("agent_telemetry").select("id").limit(1).execute()
            supabase_connected = True
        except Exception:
            pass

    # Check agent status
    agent_status = await get_agent_status_internal(supabase)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "connections": {
            "supabase": supabase_connected,
            "downloader": True,  # If this endpoint responds, downloader is up
            "agent": agent_status.get("online", False),
        },
        "agent": agent_status,
        "service": {
            "name": "SafePlay YT-DLP",
            "version": "1.0.0",
            "uptime": "running",
        }
    }


async def get_agent_status_internal(supabase) -> dict:
    """Check if the AI agent is online based on recent activity."""
    if not supabase:
        return {"online": False, "reason": "supabase_unavailable"}

    try:
        now = datetime.now(timezone.utc)
        # Agent is considered online if there's activity in the last 10 minutes
        cutoff = (now - timedelta(minutes=10)).isoformat()

        # Check for recent agent actions
        actions_response = supabase.table("agent_actions") \
            .select("created_at") \
            .gte("created_at", cutoff) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if actions_response.data:
            return {
                "online": True,
                "last_activity": actions_response.data[0]["created_at"],
                "reason": "recent_action"
            }

        # Check for recent alerts (agent sends alerts when it starts)
        alerts_response = supabase.table("agent_alerts") \
            .select("created_at") \
            .gte("created_at", cutoff) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if alerts_response.data:
            return {
                "online": True,
                "last_activity": alerts_response.data[0]["created_at"],
                "reason": "recent_alert"
            }

        # Check for recent patterns (agent computes patterns periodically)
        patterns_response = supabase.table("agent_patterns") \
            .select("computed_at") \
            .gte("computed_at", cutoff) \
            .order("computed_at", desc=True) \
            .limit(1) \
            .execute()

        if patterns_response.data:
            return {
                "online": True,
                "last_activity": patterns_response.data[0]["computed_at"],
                "reason": "recent_pattern"
            }

        # No recent activity - check for any activity in last hour
        cutoff_1h = (now - timedelta(hours=1)).isoformat()
        any_response = supabase.table("agent_actions") \
            .select("created_at") \
            .gte("created_at", cutoff_1h) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if any_response.data:
            return {
                "online": False,
                "last_activity": any_response.data[0]["created_at"],
                "reason": "idle"
            }

        return {"online": False, "reason": "no_recent_activity"}

    except Exception as e:
        return {"online": False, "reason": f"error: {str(e)}"}


@router.get("/agent-status")
async def get_agent_status():
    """Check if the AI agent is online."""
    supabase = get_supabase()
    return await get_agent_status_internal(supabase)


@router.get("/telemetry/summary")
async def get_telemetry_summary():
    """Get telemetry summary for the last 24 hours."""
    supabase = get_supabase()
    if not supabase:
        return {"error": "Supabase not available", "data": None}

    try:
        now = datetime.now(timezone.utc)
        cutoff_24h = (now - timedelta(hours=24)).isoformat()
        cutoff_1h = (now - timedelta(hours=1)).isoformat()

        # Get 24h data
        response = supabase.table("agent_telemetry") \
            .select("success, total_duration_ms, player_client, created_at") \
            .gte("created_at", cutoff_24h) \
            .order("created_at", desc=True) \
            .execute()

        data = response.data or []

        total = len(data)
        successes = sum(1 for d in data if d.get("success"))
        failures = total - successes
        success_rate = (successes / total * 100) if total > 0 else 100

        # Average duration for successful downloads
        durations = [d["total_duration_ms"] for d in data if d.get("success") and d.get("total_duration_ms")]
        avg_duration = sum(durations) / len(durations) if durations else 0

        # Last hour stats
        last_hour = [d for d in data if d.get("created_at", "") >= cutoff_1h]
        last_hour_total = len(last_hour)
        last_hour_success = sum(1 for d in last_hour if d.get("success"))

        # Player client breakdown
        client_stats = {}
        for d in data:
            client = d.get("player_client") or "unknown"
            if client not in client_stats:
                client_stats[client] = {"total": 0, "success": 0}
            client_stats[client]["total"] += 1
            if d.get("success"):
                client_stats[client]["success"] += 1

        return {
            "period": "24h",
            "total_downloads": total,
            "successful": successes,
            "failed": failures,
            "success_rate": round(success_rate, 1),
            "avg_duration_ms": round(avg_duration),
            "last_hour": {
                "total": last_hour_total,
                "successful": last_hour_success,
            },
            "by_client": [
                {
                    "client": k,
                    "total": v["total"],
                    "success": v["success"],
                    "rate": round(v["success"] / v["total"] * 100, 1) if v["total"] > 0 else 0
                }
                for k, v in sorted(client_stats.items(), key=lambda x: -x[1]["total"])
            ],
        }
    except Exception as e:
        return {"error": str(e), "data": None}


@router.get("/telemetry/recent")
async def get_recent_telemetry(limit: int = 20):
    """Get recent telemetry records."""
    supabase = get_supabase()
    if not supabase:
        return {"error": "Supabase not available", "records": []}

    try:
        response = supabase.table("agent_telemetry") \
            .select("*") \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()

        return {"records": response.data or []}
    except Exception as e:
        return {"error": str(e), "records": []}


@router.get("/telemetry/hourly")
async def get_hourly_stats():
    """Get hourly download statistics for the last 24 hours."""
    supabase = get_supabase()
    if not supabase:
        return {"error": "Supabase not available", "hours": []}

    try:
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=24)).isoformat()

        response = supabase.table("agent_telemetry") \
            .select("success, hour_of_day, created_at") \
            .gte("created_at", cutoff) \
            .execute()

        data = response.data or []

        # Group by hour
        hours = {h: {"total": 0, "success": 0} for h in range(24)}
        for d in data:
            hour = d.get("hour_of_day")
            if hour is not None:
                hours[hour]["total"] += 1
                if d.get("success"):
                    hours[hour]["success"] += 1

        return {
            "hours": [
                {
                    "hour": h,
                    "total": v["total"],
                    "success": v["success"],
                    "rate": round(v["success"] / v["total"] * 100, 1) if v["total"] > 0 else 100
                }
                for h, v in sorted(hours.items())
            ]
        }
    except Exception as e:
        return {"error": str(e), "hours": []}


@router.get("/patterns")
async def get_patterns():
    """Get computed patterns from the agent."""
    supabase = get_supabase()
    if not supabase:
        return {"error": "Supabase not available", "patterns": {}}

    try:
        # Get latest patterns
        pattern_types = [
            "player_client_ranking",
            "country_ranking",
            "hourly_success_rate",
            "error_frequency",
            "overall_health"
        ]

        patterns = {}
        for pt in pattern_types:
            response = supabase.table("agent_patterns") \
                .select("data, computed_at") \
                .eq("pattern_type", pt) \
                .order("computed_at", desc=True) \
                .limit(1) \
                .execute()

            if response.data:
                patterns[pt] = {
                    "data": response.data[0]["data"],
                    "computed_at": response.data[0]["computed_at"]
                }

        return {"patterns": patterns}
    except Exception as e:
        return {"error": str(e), "patterns": {}}


@router.get("/knowledge")
async def get_knowledge(limit: int = 20, min_confidence: float = 0.0):
    """Get knowledge base entries."""
    supabase = get_supabase()
    if not supabase:
        return {"error": "Supabase not available", "entries": []}

    try:
        response = supabase.table("agent_knowledge") \
            .select("*") \
            .gte("confidence", min_confidence) \
            .neq("status", "deprecated") \
            .order("confidence", desc=True) \
            .limit(limit) \
            .execute()

        return {"entries": response.data or []}
    except Exception as e:
        return {"error": str(e), "entries": []}


@router.get("/actions")
async def get_recent_actions(limit: int = 20):
    """Get recent agent actions."""
    supabase = get_supabase()
    if not supabase:
        return {"error": "Supabase not available", "actions": []}

    try:
        response = supabase.table("agent_actions") \
            .select("*") \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()

        return {"actions": response.data or []}
    except Exception as e:
        return {"error": str(e), "actions": []}


@router.get("/health")
async def get_system_health():
    """Get comprehensive system health metrics."""
    supabase = get_supabase()
    if not supabase:
        return {
            "status": "degraded",
            "message": "Supabase not available",
            "metrics": {}
        }

    try:
        now = datetime.now(timezone.utc)
        cutoff_24h = (now - timedelta(hours=24)).isoformat()
        cutoff_1h = (now - timedelta(hours=1)).isoformat()

        # 24h metrics
        response_24h = supabase.table("agent_telemetry") \
            .select("success") \
            .gte("created_at", cutoff_24h) \
            .execute()

        data_24h = response_24h.data or []
        total_24h = len(data_24h)
        success_24h = sum(1 for d in data_24h if d.get("success"))
        rate_24h = (success_24h / total_24h * 100) if total_24h > 0 else 100

        # 1h metrics
        response_1h = supabase.table("agent_telemetry") \
            .select("success") \
            .gte("created_at", cutoff_1h) \
            .execute()

        data_1h = response_1h.data or []
        total_1h = len(data_1h)
        success_1h = sum(1 for d in data_1h if d.get("success"))
        rate_1h = (success_1h / total_1h * 100) if total_1h > 0 else 100

        # Determine status
        if rate_24h >= 90:
            status = "healthy"
            status_color = "green"
        elif rate_24h >= 70:
            status = "degraded"
            status_color = "yellow"
        else:
            status = "critical"
            status_color = "red"

        return {
            "status": status,
            "status_color": status_color,
            "metrics": {
                "success_rate_24h": round(rate_24h, 1),
                "success_rate_1h": round(rate_1h, 1),
                "total_downloads_24h": total_24h,
                "successful_downloads_24h": success_24h,
                "failed_downloads_24h": total_24h - success_24h,
                "total_downloads_1h": total_1h,
            },
            "timestamp": now.isoformat()
        }
    except Exception as e:
        return {
            "status": "unknown",
            "message": str(e),
            "metrics": {}
        }
