"""
SafePlay AI Agent - Main entry point.

Self-healing, self-learning YouTube download agent.
"""

import asyncio
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from supabase import create_client

from .config import settings, runtime_config, agent_state
from .llm import llm_client
from .monitor import LogMonitor, LogEntry
from .telemetry import TelemetryLogger
from .knowledge import KnowledgeManager, KnowledgeEntry
from .fixer import Fixer
from .alerting import AlertManager, Alert
from .patterns import PatternAnalyzer
from .journal import JournalGenerator


class SafePlayAgent:
    """The main AI agent that monitors and heals the downloader."""

    def __init__(self):
        # Initialize Supabase client
        self.supabase = create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY
        )

        # Initialize components
        self.telemetry = TelemetryLogger(self.supabase)
        self.knowledge = KnowledgeManager(self.supabase)
        self.patterns = PatternAnalyzer(self.supabase)
        self.alerts = AlertManager(self.supabase)
        self.journal = JournalGenerator(self.knowledge, self.patterns, self.telemetry)

        # Monitor and fixer need to be initialized after we have the callback
        self.monitor: Optional[LogMonitor] = None
        self.fixer: Optional[Fixer] = None

        # Tracking
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._failure_queue: asyncio.Queue = asyncio.Queue()
        self._current_fix_job: Optional[str] = None

    async def _startup_diagnostics(self) -> None:
        """Run startup diagnostics to verify everything is working."""
        print("=" * 50)
        print("STARTUP DIAGNOSTICS")
        print("=" * 50)

        # Check Supabase connectivity
        print("\n[1/4] Checking Supabase connection...")
        try:
            # Try to query the agent_telemetry table
            response = self.supabase.table("agent_telemetry") \
                .select("id") \
                .limit(1) \
                .execute()
            print("  ✓ Supabase connected")
            print(f"  ✓ agent_telemetry table exists")

            # Count existing records
            count_response = self.supabase.table("agent_telemetry") \
                .select("id", count="exact") \
                .execute()
            record_count = count_response.count if hasattr(count_response, 'count') else len(count_response.data or [])
            print(f"  ✓ Found {record_count} telemetry records")
        except Exception as e:
            print(f"  ✗ Supabase error: {e}")
            print("  NOTE: Make sure the SQL schema has been applied to Supabase!")

        # Check agent_knowledge table
        print("\n[2/4] Checking knowledge base...")
        try:
            response = self.supabase.table("agent_knowledge") \
                .select("id") \
                .limit(1) \
                .execute()
            print("  ✓ agent_knowledge table exists")
        except Exception as e:
            print(f"  ✗ Knowledge table error: {e}")

        # Check downloader API connectivity
        print("\n[3/4] Checking downloader API connection...")
        print(f"  Downloader URL: {settings.DOWNLOADER_URL}")
        print(f"  API Key configured: {'Yes' if settings.DOWNLOADER_API_KEY else 'NO - MISSING!'}")

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                # Try the health endpoint first (no auth required usually)
                async with session.get(f"{settings.DOWNLOADER_URL}/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        print(f"  ✓ Downloader is reachable (health check OK)")
                    else:
                        print(f"  ? Health check returned status {resp.status}")

                # Try authenticated endpoint
                headers = {"X-API-Key": settings.DOWNLOADER_API_KEY}
                async with session.get(f"{settings.DOWNLOADER_URL}/api/admin/logs?limit=1", headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        print(f"  ✓ API authentication successful")
                    elif resp.status == 401:
                        print(f"  ✗ API authentication FAILED - check DOWNLOADER_API_KEY")
                    else:
                        print(f"  ? API returned status {resp.status}")
        except Exception as e:
            print(f"  ✗ Could not connect to downloader: {e}")

        # Check LLM provider
        print("\n[4/4] Checking LLM provider...")
        print(f"  Provider: {settings.LLM_PROVIDER}")
        available = llm_client.get_available_providers()
        if available:
            print(f"  ✓ Available providers: {', '.join(available)}")
        else:
            print("  ✗ NO LLM PROVIDERS CONFIGURED!")
            print("  Set ANTHROPIC_API_KEY or GOOGLE_API_KEY in .env")

        print("\n" + "=" * 50)
        print("IMPORTANT: The DOWNLOADER service must be restarted")
        print("to enable telemetry hooks. Run:")
        print(f"  sudo systemctl restart {settings.DOWNLOADER_SERVICE_NAME}")
        print("=" * 50)
        print("")

    async def start(self) -> None:
        """Start the agent."""
        print("SafePlay AI Agent starting...")
        print("")

        # Run startup diagnostics
        await self._startup_diagnostics()

        # Load runtime config from database
        await runtime_config.load(self.supabase)

        # Initialize monitor with failure callback
        self.monitor = LogMonitor(
            downloader_url=settings.DOWNLOADER_URL,
            api_key=settings.DOWNLOADER_API_KEY,
            on_failure=self._on_download_failure,
        )

        # Initialize fixer
        self.fixer = Fixer(self.monitor, self.supabase)

        self._running = True

        # Start background tasks
        self._tasks = [
            asyncio.create_task(self._failure_processor()),
            asyncio.create_task(self._periodic_health_check()),
            asyncio.create_task(self._periodic_pattern_update()),
            asyncio.create_task(self._periodic_journal_update()),
            asyncio.create_task(self._hourly_rate_limit_reset()),
            asyncio.create_task(self._proactive_optimization()),
        ]

        # Start telemetry flushing
        await self.telemetry.start_periodic_flush()

        # Start log monitoring
        await self.monitor.start()

        print(f"Agent started. Monitoring {settings.DOWNLOADER_URL}")
        print(f"LLM Provider: {settings.LLM_PROVIDER}")
        print(f"Git Branch: {settings.GIT_BRANCH}")

        # Send startup alert
        await self.alerts.send(Alert(
            severity="info",
            alert_type="system_maintenance",
            title="Agent Started",
            message="SafePlay AI Agent has started monitoring the downloader.",
            details={
                "llm_provider": settings.LLM_PROVIDER,
                "git_branch": settings.GIT_BRANCH,
            }
        ))

    async def stop(self) -> None:
        """Stop the agent gracefully."""
        print("Agent stopping...")
        self._running = False

        # Stop monitor
        if self.monitor:
            await self.monitor.stop()

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Stop telemetry
        await self.telemetry.stop()

        # Final journal update
        try:
            await self.journal.save()
            print("Final journal saved")
        except Exception as e:
            print(f"Failed to save final journal: {e}")

        print("Agent stopped")

    async def _on_download_failure(self, entry: LogEntry) -> None:
        """Called when a download failure is detected."""
        await self._failure_queue.put(entry)

    async def _failure_processor(self) -> None:
        """Process download failures from the queue."""
        while self._running:
            try:
                entry = await asyncio.wait_for(
                    self._failure_queue.get(),
                    timeout=5.0
                )
                await self._handle_failure(entry)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error processing failure: {e}")

    async def _handle_failure(self, entry: LogEntry) -> None:
        """Handle a single download failure."""
        job_id = entry.job_id
        youtube_id = entry.youtube_id or "unknown"

        print(f"Handling failure for job {job_id} (video: {youtube_id})")

        # Check if we're already working on this job
        if self._current_fix_job == job_id:
            return
        self._current_fix_job = job_id

        try:
            # Gather context
            logs = await self.monitor.get_logs_for_job(job_id)
            log_text = "\n".join(f"[{l.level}] {l.message}" for l in logs[-50:])

            system_state = await self.monitor.get_system_state()
            current_config = await self.monitor.get_current_config()

            # Get relevant knowledge
            knowledge_context = await self.knowledge.build_context_string(
                error_code=entry.error_code,
                error_message=entry.message,
            )

            # Analyze with LLM
            if not agent_state.can_call_llm():
                print("LLM rate limit reached, skipping analysis")
                await self.alerts.escalation(
                    reason="LLM rate limit reached, cannot analyze failure",
                    job_id=job_id,
                    youtube_id=youtube_id,
                )
                return

            analysis = await llm_client.analyze_error(
                error_logs=log_text,
                system_state=system_state,
                knowledge_context=knowledge_context,
                current_config=current_config,
            )

            diagnosis = analysis.get("diagnosis", {})
            fix_info = analysis.get("fix", {})
            learning = analysis.get("learning", {})

            print(f"Diagnosis: {diagnosis.get('error_type')} (confidence: {diagnosis.get('confidence', 0):.0%})")
            print(f"Recommended fix: {fix_info.get('type')}")

            # Record learning if applicable
            knowledge_entry_id = None
            if learning.get("should_document"):
                knowledge_entry_id = await self.knowledge.add_from_analysis(analysis)
                if knowledge_entry_id:
                    print(f"Added knowledge entry: {knowledge_entry_id}")

            # Apply fix if not escalation
            if fix_info.get("type") == "escalate":
                await self.alerts.escalation(
                    reason=fix_info.get("description", "Unknown reason"),
                    job_id=job_id,
                    youtube_id=youtube_id,
                    context={
                        "diagnosis": diagnosis,
                        "logs": log_text[-1000:],
                    }
                )
                await self.fixer.log_action(
                    trigger_type="download_failure",
                    action_type="escalation",
                    description=fix_info.get("description", "Escalated to human"),
                    result=type("FixResult", (), {
                        "success": False,
                        "fix_type": "escalate",
                        "details": {},
                        "git_commit": None,
                        "error": "Requires human intervention"
                    })(),
                    trigger_job_id=job_id,
                    trigger_error_code=entry.error_code,
                    trigger_error_message=entry.message,
                    llm_metadata=analysis.get("_llm_metadata"),
                    knowledge_entry_id=knowledge_entry_id,
                )
                return

            # Apply the fix
            result = await self.fixer.apply_fix(analysis)

            # Log the action
            await self.fixer.log_action(
                trigger_type="download_failure",
                action_type=result.fix_type,
                description=result.description,
                result=result,
                trigger_job_id=job_id,
                trigger_error_code=entry.error_code,
                trigger_error_message=entry.message,
                llm_metadata=analysis.get("_llm_metadata"),
                knowledge_entry_id=knowledge_entry_id,
            )

            if result.success:
                print(f"Fix applied successfully: {result.description}")

                # Send alert
                await self.alerts.fix_applied(
                    fix_type=result.fix_type,
                    description=result.description,
                    job_id=job_id,
                    git_commit=result.git_commit,
                )

                # Restart service if needed
                if result.requires_restart:
                    print("Restarting downloader service...")
                    restart_success = await self.fixer.restart_service()
                    if restart_success:
                        print("Service restarted successfully")
                    else:
                        print("Failed to restart service")
                        await self.alerts.escalation(
                            reason="Failed to restart service after applying fix",
                            job_id=job_id,
                        )

                # Validate knowledge if fix worked
                if knowledge_entry_id:
                    await self.knowledge.validate(knowledge_entry_id, True)

            else:
                print(f"Fix failed: {result.error}")
                await self.alerts.fix_failed(
                    fix_type=result.fix_type,
                    error=result.error or "Unknown error",
                    job_id=job_id,
                )

                # Invalidate knowledge if fix didn't work
                if knowledge_entry_id:
                    await self.knowledge.validate(knowledge_entry_id, False)

        except Exception as e:
            print(f"Error handling failure: {e}")
            await self.alerts.escalation(
                reason=f"Agent error while handling failure: {str(e)}",
                job_id=job_id,
                youtube_id=youtube_id,
            )
        finally:
            self._current_fix_job = None

    async def _periodic_health_check(self) -> None:
        """Periodically check system health."""
        while self._running:
            try:
                await asyncio.sleep(settings.HEALTH_CHECK_INTERVAL_SECONDS)

                # Check for anomalies
                anomalies = await self.patterns.detect_anomalies()

                for anomaly in anomalies:
                    if anomaly["severity"] == "critical":
                        await self.alerts.send(Alert(
                            severity="critical",
                            alert_type="pattern_anomaly",
                            title=anomaly["message"],
                            message=f"Critical anomaly detected: {anomaly['type']}",
                            details=anomaly["data"],
                        ))
                    elif anomaly["severity"] == "warning":
                        await self.alerts.send(Alert(
                            severity="warning",
                            alert_type="health_degraded",
                            title=anomaly["message"],
                            message=f"Health warning: {anomaly['type']}",
                            details=anomaly["data"],
                        ))

                # Check for yt-dlp updates
                version_info = await self.fixer.check_ytdlp_version()
                if version_info.get("update_available"):
                    await self.alerts.send(Alert(
                        severity="info",
                        alert_type="ytdlp_update_available",
                        title=f"yt-dlp update available: {version_info['latest']}",
                        message=f"Current: {version_info['current']}, Latest: {version_info['latest']}",
                        details=version_info,
                    ))

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Health check error: {e}")

    async def _periodic_pattern_update(self) -> None:
        """Periodically update computed patterns."""
        while self._running:
            try:
                # Wait for the configured interval
                await asyncio.sleep(settings.PATTERN_RECOMPUTE_INTERVAL_HOURS * 3600)

                print("Recomputing patterns...")
                patterns = await self.patterns.compute_all_patterns()
                print(f"Patterns updated: {list(patterns.keys())}")

                # Run knowledge synthesis
                if agent_state.can_call_llm():
                    telemetry_data = await self.telemetry.get_recent_failures(limit=100)
                    existing_knowledge = await self.knowledge.get_active_knowledge(limit=50)

                    synthesis = await llm_client.synthesize_knowledge(
                        telemetry_data=telemetry_data,
                        existing_knowledge=existing_knowledge,
                        patterns=patterns,
                    )

                    # Process insights
                    for insight in synthesis.get("insights", []):
                        if insight.get("confidence", 0) > 0.5:
                            entry = KnowledgeEntry(
                                category=insight.get("category", "hypothesis"),
                                title=insight.get("title", "Untitled"),
                                observation=insight.get("observation", ""),
                                explanation=insight.get("explanation"),
                                confidence=insight.get("confidence", 0.5),
                                tags=["synthesized", "auto-generated"],
                            )
                            await self.knowledge.add(entry)

                    # Process knowledge updates
                    for update in synthesis.get("knowledge_updates", []):
                        if update.get("knowledge_id") and update.get("action") == "validate":
                            await self.knowledge.validate(update["knowledge_id"], True)
                        elif update.get("knowledge_id") and update.get("action") == "invalidate":
                            await self.knowledge.validate(update["knowledge_id"], False)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Pattern update error: {e}")

    async def _periodic_journal_update(self) -> None:
        """Periodically update the research journal."""
        while self._running:
            try:
                # Update every 6 hours
                await asyncio.sleep(6 * 3600)

                print("Updating research journal...")
                path = await self.journal.save()
                print(f"Journal saved to {path}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Journal update error: {e}")

    async def _hourly_rate_limit_reset(self) -> None:
        """Reset rate limit counters every hour."""
        while self._running:
            try:
                await asyncio.sleep(3600)
                agent_state.reset_hourly_counters()
                print(f"Rate limit counters reset. Fix attempts: 0/{runtime_config.max_fix_attempts_per_hour}")
            except asyncio.CancelledError:
                break

    async def _proactive_optimization(self) -> None:
        """
        Periodically analyze ALL telemetry (successes + failures) to find
        optimizations. This is the continuous learning loop.
        """
        # Wait a bit before first run to let data accumulate
        await asyncio.sleep(300)  # 5 minutes

        while self._running:
            try:
                print("[Optimization] Running proactive optimization analysis...")

                # Get all recent telemetry
                telemetry = await self.telemetry.get_all_recent_telemetry(
                    limit=500,
                    hours=24
                )

                successes = telemetry["successes"]
                failures = telemetry["failures"]
                total = telemetry["total"]

                print(f"[Optimization] Analyzing {total} downloads: {len(successes)} successes, {len(failures)} failures")

                if total < 10:
                    print("[Optimization] Not enough data for meaningful analysis, skipping")
                    await asyncio.sleep(1800)  # Try again in 30 minutes
                    continue

                # Get patterns and current config
                patterns = await self.patterns.get_cached_patterns()
                current_config = await self.monitor.get_current_config()

                # Check if we can call LLM
                if not agent_state.can_call_llm():
                    print("[Optimization] LLM rate limit reached, skipping")
                    await asyncio.sleep(1800)
                    continue

                # Analyze for optimizations
                analysis = await llm_client.analyze_for_optimization(
                    success_data=successes,
                    failure_data=failures,
                    patterns=patterns,
                    current_config=current_config,
                )

                # Log analysis results
                data_quality = analysis.get("analysis", {}).get("data_quality", "unknown")
                key_findings = analysis.get("analysis", {}).get("key_findings", [])
                recommendations = analysis.get("recommendations", [])
                learnings = analysis.get("learnings", [])

                print(f"[Optimization] Data quality: {data_quality}")
                if key_findings:
                    print(f"[Optimization] Key findings: {', '.join(key_findings[:3])}")

                # Store learnings from successes
                for learning in learnings:
                    if learning.get("confidence", 0) > 0.6:
                        entry = KnowledgeEntry(
                            category=learning.get("category", "success_pattern"),
                            title=learning.get("title", "Untitled"),
                            observation=learning.get("observation", ""),
                            confidence=learning.get("confidence", 0.6),
                            tags=["auto-optimization", "success-based"],
                        )
                        entry_id = await self.knowledge.add(entry)
                        print(f"[Optimization] Added learning: {learning.get('title')}")

                # Apply high-confidence recommendations if flagged
                if analysis.get("should_apply_changes") and recommendations:
                    for rec in recommendations:
                        if rec.get("priority") == "high" and rec.get("confidence", 0) > 0.7:
                            config_changes = rec.get("config_changes")
                            if config_changes:
                                print(f"[Optimization] Applying: {rec.get('description')}")
                                success = await self.monitor.update_config(config_changes)
                                if success:
                                    print(f"[Optimization] Config updated successfully")
                                    await self.alerts.send(Alert(
                                        severity="info",
                                        alert_type="proactive_optimization",
                                        title="Proactive Optimization Applied",
                                        message=rec.get("description", "Configuration optimized"),
                                        details={
                                            "rationale": rec.get("rationale"),
                                            "expected_improvement": rec.get("expected_improvement"),
                                            "config_changes": config_changes,
                                        }
                                    ))

                                    # Log the action
                                    await self.fixer.log_action(
                                        trigger_type="proactive_optimization",
                                        action_type="config_change",
                                        description=rec.get("description", "Proactive config optimization"),
                                        result=type("FixResult", (), {
                                            "success": True,
                                            "fix_type": "config_change",
                                            "details": config_changes,
                                            "git_commit": None,
                                            "error": None
                                        })(),
                                        llm_metadata=analysis.get("_llm_metadata"),
                                    )
                                else:
                                    print(f"[Optimization] Failed to apply config change")
                        elif rec.get("type") != "no_change":
                            # Log recommendation but don't auto-apply
                            print(f"[Optimization] Recommendation (not auto-applied): {rec.get('description')}")
                else:
                    print("[Optimization] No changes recommended at this time")

                # Wait before next optimization cycle (30 minutes default)
                await asyncio.sleep(1800)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Optimization] Error: {e}")
                await asyncio.sleep(600)  # Retry in 10 minutes on error


async def main():
    """Main entry point."""
    agent = SafePlayAgent()

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def signal_handler():
        print("\nReceived shutdown signal...")
        asyncio.create_task(agent.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await agent.start()

        # Keep running
        while agent._running:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\nKeyboard interrupt received")
    finally:
        await agent.stop()


def run():
    """Entry point for the agent."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
