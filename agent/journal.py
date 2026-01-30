"""
Journal generator - Creates human-readable knowledge documents.

Generates the explorer's research journal from the knowledge base.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import settings


class JournalGenerator:
    """Generates the research journal from knowledge base."""

    def __init__(self, knowledge_manager, pattern_analyzer, telemetry_logger):
        self.knowledge = knowledge_manager
        self.patterns = pattern_analyzer
        self.telemetry = telemetry_logger

    async def generate(self) -> str:
        """Generate the complete research journal."""
        sections = []

        # Header
        sections.append(await self._generate_header())

        # Executive summary
        sections.append(await self._generate_executive_summary())

        # Table of contents
        sections.append(self._generate_toc())

        # Knowledge sections by category
        sections.append(await self._generate_knowledge_sections())

        # Pattern analysis
        sections.append(await self._generate_pattern_analysis())

        # Recent activity
        sections.append(await self._generate_recent_activity())

        # Appendix
        sections.append(await self._generate_appendix())

        return "\n\n".join(sections)

    async def save(self, path: Optional[str] = None) -> str:
        """Generate and save the journal to a file."""
        content = await self.generate()

        output_path = Path(path or settings.JOURNAL_OUTPUT_PATH)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: output_path.write_text(content)
        )

        return str(output_path)

    async def _generate_header(self) -> str:
        """Generate the journal header."""
        now = datetime.now(timezone.utc)
        return f"""# YouTube Download Research Journal

**SafePlay AI Agent - Accumulated Knowledge Base**

*Last updated: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}*

---

This document contains the accumulated learnings of the SafePlay AI Agent
about YouTube's download infrastructure, common issues, and effective solutions.
It is automatically generated and updated as the agent learns from experience.

"""

    async def _generate_executive_summary(self) -> str:
        """Generate the executive summary section."""
        # Get stats
        stats = await self.knowledge.get_statistics()
        health = await self.patterns._compute_overall_health()
        client_ranking = await self.patterns.get_latest_pattern("player_client_ranking")

        best_client = "unknown"
        if client_ranking and client_ranking.get("data", {}).get("rankings"):
            best_client = client_ranking["data"]["rankings"][0]["player_client"]

        return f"""## Executive Summary

| Metric | Value |
|--------|-------|
| **Total Knowledge Entries** | {stats['total_entries']} |
| **High-Confidence Learnings** | {stats['high_confidence_count']} |
| **24h Success Rate** | {health['success_rate_24h']:.1f}% |
| **Downloads (24h)** | {health['total_downloads_24h']} |
| **Fixes Applied (24h)** | {health['fixes_applied_24h']} |
| **Active Alerts** | {health['active_alerts']} |
| **Best Player Client** | `{best_client}` |

"""

    def _generate_toc(self) -> str:
        """Generate table of contents."""
        return """## Table of Contents

1. [Executive Summary](#executive-summary)
2. [YouTube Behavior](#youtube-behavior)
3. [Error Patterns](#error-patterns)
4. [Success Patterns](#success-patterns)
5. [Workarounds](#workarounds)
6. [yt-dlp Knowledge](#yt-dlp-knowledge)
7. [Proxy Patterns](#proxy-patterns)
8. [Temporal Patterns](#temporal-patterns)
9. [Current Hypotheses](#current-hypotheses)
10. [Pattern Analysis](#pattern-analysis)
11. [Recent Activity](#recent-activity)
12. [Appendix](#appendix)

"""

    async def _generate_knowledge_sections(self) -> str:
        """Generate sections for each knowledge category."""
        sections = []

        category_info = {
            "youtube_behavior": {
                "title": "YouTube Behavior",
                "description": "Observations about how YouTube's infrastructure works.",
            },
            "error_pattern": {
                "title": "Error Patterns",
                "description": "Common errors encountered and their root causes.",
            },
            "success_pattern": {
                "title": "Success Patterns",
                "description": "Configurations and approaches that work reliably.",
            },
            "workaround": {
                "title": "Workarounds",
                "description": "Tricks and techniques to bypass issues.",
            },
            "ytdlp_knowledge": {
                "title": "yt-dlp Knowledge",
                "description": "Understanding of yt-dlp internals and configuration.",
            },
            "proxy_pattern": {
                "title": "Proxy Patterns",
                "description": "Learnings about proxy usage and optimization.",
            },
            "temporal_pattern": {
                "title": "Temporal Patterns",
                "description": "Time-based patterns in YouTube's behavior.",
            },
            "hypothesis": {
                "title": "Current Hypotheses",
                "description": "Unconfirmed theories under investigation.",
            },
        }

        for category, info in category_info.items():
            entries = await self.knowledge.get_by_category(category, min_confidence=0.0)

            section = f"## {info['title']}\n\n"
            section += f"*{info['description']}*\n\n"

            if not entries:
                section += "*No entries yet.*\n"
            else:
                for entry in entries:
                    section += self._format_knowledge_entry(entry)

            sections.append(section)

        return "\n".join(sections)

    def _format_knowledge_entry(self, entry: dict) -> str:
        """Format a single knowledge entry."""
        confidence_pct = int(entry.get("confidence", 0) * 100)
        confidence_bar = "█" * (confidence_pct // 10) + "░" * (10 - confidence_pct // 10)

        status_badge = ""
        if entry.get("status") == "deprecated":
            status_badge = " ⚠️ *DEPRECATED*"
        elif entry.get("status") == "investigating":
            status_badge = " 🔍 *INVESTIGATING*"

        lines = [
            f"### {entry['title']}{status_badge}",
            "",
            f"**Confidence:** {confidence_bar} {confidence_pct}%",
            f"**Validated:** {entry.get('times_validated', 0)} times",
            "",
        ]

        if entry.get("observation"):
            lines.append(f"**Observation:** {entry['observation']}")
            lines.append("")

        if entry.get("explanation"):
            lines.append(f"**Explanation:** {entry['explanation']}")
            lines.append("")

        if entry.get("solution"):
            lines.append(f"**Solution:** {entry['solution']}")
            lines.append("")

        if entry.get("example_errors"):
            lines.append("**Example Errors:**")
            for err in entry["example_errors"][:3]:
                lines.append(f"- `{err[:100]}{'...' if len(err) > 100 else ''}`")
            lines.append("")

        if entry.get("git_commits"):
            lines.append(f"**Related Commits:** {', '.join(entry['git_commits'][:3])}")
            lines.append("")

        if entry.get("tags"):
            lines.append(f"**Tags:** {', '.join(f'`{t}`' for t in entry['tags'])}")
            lines.append("")

        lines.append(f"*Last updated: {entry.get('updated_at', 'Unknown')}*")
        lines.append("")
        lines.append("---")
        lines.append("")

        return "\n".join(lines)

    async def _generate_pattern_analysis(self) -> str:
        """Generate the pattern analysis section."""
        patterns = await self.patterns.get_all_latest_patterns()

        lines = ["## Pattern Analysis", ""]

        # Player client performance
        if "player_client_ranking" in patterns:
            pc = patterns["player_client_ranking"]
            lines.append("### Player Client Performance")
            lines.append("")
            lines.append("| Client | Success Rate | Attempts | Avg Duration |")
            lines.append("|--------|-------------|----------|--------------|")
            for r in pc.get("rankings", [])[:5]:
                duration = f"{r['avg_duration_ms']}ms" if r.get("avg_duration_ms") else "N/A"
                lines.append(f"| `{r['player_client']}` | {r['success_rate']}% | {r['total_attempts']} | {duration} |")
            lines.append("")

        # Hourly patterns
        if "hourly_success_rate" in patterns:
            hp = patterns["hourly_success_rate"]
            lines.append("### Hourly Success Pattern")
            lines.append("")
            if hp.get("best_hour") is not None:
                lines.append(f"- **Best Hour:** {hp['best_hour']}:00 UTC ({hp['best_hour_rate']}% success)")
            if hp.get("worst_hour") is not None:
                lines.append(f"- **Worst Hour:** {hp['worst_hour']}:00 UTC ({hp['worst_hour_rate']}% success)")
            lines.append("")

        # Error distribution
        if "error_frequency" in patterns:
            ef = patterns["error_frequency"]
            lines.append("### Most Common Errors")
            lines.append("")
            lines.append("| Error Code | Count | % of Errors |")
            lines.append("|------------|-------|-------------|")
            for e in ef.get("errors", [])[:10]:
                lines.append(f"| `{e['error_code']}` | {e['count']} | {e['percentage']}% |")
            lines.append("")

        return "\n".join(lines)

    async def _generate_recent_activity(self) -> str:
        """Generate the recent activity section."""
        # Get recent actions
        response = self.telemetry.supabase.table("agent_actions") \
            .select("*") \
            .order("created_at", desc=True) \
            .limit(10) \
            .execute()

        actions = response.data or []

        lines = ["## Recent Activity", ""]

        if not actions:
            lines.append("*No recent agent activity.*")
        else:
            lines.append("| Time | Action | Description | Outcome |")
            lines.append("|------|--------|-------------|---------|")
            for action in actions:
                time = action.get("created_at", "")[:16].replace("T", " ")
                action_type = action.get("action_type", "unknown")
                desc = action.get("action_description", "")[:50]
                outcome = action.get("outcome", "unknown")
                outcome_emoji = "✅" if outcome == "success" else "❌" if outcome == "failure" else "⏳"
                lines.append(f"| {time} | `{action_type}` | {desc} | {outcome_emoji} |")

        lines.append("")
        return "\n".join(lines)

    async def _generate_appendix(self) -> str:
        """Generate the appendix section."""
        return """## Appendix

### How This Document Is Generated

This document is automatically generated by the SafePlay AI Agent from its
accumulated knowledge base. The agent learns from:

1. **Every download attempt** - Success and failure patterns
2. **Error analysis** - Understanding why downloads fail
3. **Fix outcomes** - What solutions work
4. **Pattern recognition** - Discovering trends over time

### Confidence Scores

Knowledge entries have confidence scores from 0% to 100%:

- **0-30%**: Hypothesis, limited evidence
- **30-60%**: Observed pattern, some validation
- **60-80%**: Well-established, multiple validations
- **80-100%**: Highly reliable, extensively validated

### Contributing

This knowledge base evolves automatically. The agent:
- Validates existing knowledge when solutions work
- Invalidates knowledge when solutions fail
- Creates new entries when novel patterns emerge
- Deprecates outdated information

### Data Retention

- Detailed telemetry: 30 days
- Knowledge entries: Permanent (unless deprecated)
- Pattern computations: Refreshed every 6 hours

---

*Generated by SafePlay AI Agent v1.0.0*
"""

    async def generate_daily_summary(self) -> str:
        """Generate a shorter daily summary for alerts/emails."""
        health = await self.patterns._compute_overall_health()
        errors = await self.patterns._compute_error_frequency(hours=24)

        summary = f"""# Daily Summary - {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

## Health Metrics
- Success Rate: {health['success_rate_24h']:.1f}%
- Total Downloads: {health['total_downloads_24h']}
- Failed Downloads: {health['total_downloads_24h'] - health['successful_downloads_24h']}

## Top Errors
"""
        for e in errors.get("errors", [])[:5]:
            summary += f"- `{e['error_code']}`: {e['count']} occurrences\n"

        summary += f"""
## Agent Activity
- Fixes Applied: {health['fixes_applied_24h']}
- Active Alerts: {health['active_alerts']}
"""

        if health.get("rate_change"):
            direction = "📈" if health["rate_change"] > 0 else "📉"
            summary += f"\n## Trend\n{direction} Success rate changed by {health['rate_change']:+.1f}% compared to previous 24h\n"

        return summary
