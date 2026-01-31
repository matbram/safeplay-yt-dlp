"""
Knowledge management system - The explorer's journal.

Manages the agent's accumulated knowledge about YouTube downloading.
"""

import json
from datetime import datetime, timezone
from typing import Optional, Any
from dataclasses import dataclass, asdict, field
from uuid import UUID


# Terms that indicate video-related knowledge (not applicable to audio-only system)
INVALID_VIDEO_TERMS = [
    "max_height",
    "resolution",
    "1080p", "720p", "480p", "360p", "240p", "144p",
    "video codec",
    "video format",
    "mp4 video",
    "video quality",
    "format_preference",  # Old dead config field
    "bestvideo",
    "video bitrate",
]


def validate_knowledge_content(content: str) -> tuple[bool, Optional[str]]:
    """
    Check if knowledge content is valid for this audio-only system.

    SafePlay downloads AUDIO ONLY, so we filter out video-related knowledge.

    Returns:
        (is_valid, reason) - reason is None if valid, or explanation if invalid
    """
    content_lower = content.lower()

    # Check for invalid video terms
    for term in INVALID_VIDEO_TERMS:
        if term.lower() in content_lower:
            return False, f"Contains video-related term '{term}' - not applicable to audio-only system"

    return True, None


@dataclass
class KnowledgeEntry:
    """A single knowledge entry."""

    category: str
    title: str
    observation: str

    # Optional fields
    subcategory: Optional[str] = None
    explanation: Optional[str] = None
    solution: Optional[str] = None
    evidence: dict = field(default_factory=dict)
    example_errors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.5
    git_commits: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)

    # Set after creation
    id: Optional[str] = None
    status: str = "active"

    def to_db_record(self) -> dict:
        """Convert to database record format."""
        record = {
            "category": self.category,
            "subcategory": self.subcategory,
            "title": self.title,
            "observation": self.observation,
            "explanation": self.explanation,
            "solution": self.solution,
            "evidence": self.evidence,
            "example_errors": self.example_errors,
            "tags": self.tags,
            "confidence": self.confidence,
            "git_commits": self.git_commits,
            "files_modified": self.files_modified,
            "status": self.status,
        }
        if self.id:
            record["id"] = self.id
        return record


class KnowledgeManager:
    """Manages the agent's knowledge base."""

    CATEGORIES = [
        "youtube_behavior",
        "error_pattern",
        "success_pattern",
        "workaround",
        "hypothesis",
        "ytdlp_knowledge",
        "proxy_pattern",
        "temporal_pattern",
    ]

    def __init__(self, supabase_client):
        self.supabase = supabase_client

    async def add(self, entry: KnowledgeEntry) -> str:
        """Add a new knowledge entry."""
        if entry.category not in self.CATEGORIES:
            raise ValueError(f"Invalid category: {entry.category}. Must be one of {self.CATEGORIES}")

        record = entry.to_db_record()
        response = self.supabase.table("agent_knowledge").insert(record).execute()

        if response.data:
            return response.data[0]["id"]
        raise RuntimeError("Failed to insert knowledge entry")

    async def update(self, entry_id: str, updates: dict) -> None:
        """Update an existing knowledge entry."""
        self.supabase.table("agent_knowledge") \
            .update(updates) \
            .eq("id", entry_id) \
            .execute()

    async def get(self, entry_id: str) -> Optional[dict]:
        """Get a specific knowledge entry."""
        response = self.supabase.table("agent_knowledge") \
            .select("*") \
            .eq("id", entry_id) \
            .single() \
            .execute()
        return response.data if response.data else None

    async def search(
        self,
        query: str,
        category: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 10
    ) -> list[dict]:
        """Search knowledge base using full-text search."""
        try:
            # Use the search function we defined in the schema
            response = self.supabase.rpc(
                "search_agent_knowledge",
                {
                    "search_query": query,
                    "category_filter": category,
                    "min_confidence": min_confidence,
                    "max_results": limit
                }
            ).execute()
            return response.data or []
        except Exception:
            # RPC function might not exist, fall back to basic query
            return await self.get_active_knowledge(min_confidence=min_confidence, limit=limit)

    async def get_by_category(
        self,
        category: str,
        status: str = "active",
        min_confidence: float = 0.0,
        limit: int = 50
    ) -> list[dict]:
        """Get all knowledge entries in a category."""
        query = self.supabase.table("agent_knowledge") \
            .select("*") \
            .eq("category", category) \
            .eq("status", status) \
            .gte("confidence", min_confidence) \
            .order("confidence", desc=True) \
            .limit(limit)

        response = query.execute()
        return response.data or []

    async def get_active_knowledge(
        self,
        min_confidence: float = 0.3,
        limit: int = 100
    ) -> list[dict]:
        """Get all active knowledge entries."""
        response = self.supabase.table("agent_knowledge") \
            .select("*") \
            .eq("status", "active") \
            .gte("confidence", min_confidence) \
            .order("confidence", desc=True) \
            .limit(limit) \
            .execute()
        return response.data or []

    async def get_relevant_knowledge(
        self,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        player_client: Optional[str] = None,
        limit: int = 10
    ) -> list[dict]:
        """Get knowledge relevant to a specific situation."""
        # Build search query from available context
        search_terms = []
        if error_code:
            search_terms.append(error_code.replace("_", " "))
        if error_message:
            # Extract key terms from error message
            words = error_message.lower().split()
            key_terms = [w for w in words if len(w) > 4 and w not in ["error", "failed", "could"]]
            search_terms.extend(key_terms[:3])
        if player_client:
            search_terms.append(player_client)

        if not search_terms:
            # Return general high-confidence knowledge
            return await self.get_active_knowledge(min_confidence=0.6, limit=limit)

        query = " ".join(search_terms)
        return await self.search(query, min_confidence=0.3, limit=limit)

    async def validate(self, entry_id: str, is_valid: bool) -> None:
        """Validate or invalidate a knowledge entry."""
        try:
            self.supabase.rpc(
                "validate_knowledge",
                {"knowledge_id": entry_id, "is_valid": is_valid}
            ).execute()
        except Exception:
            # RPC function might not exist, update directly
            update_data = {"times_validated": 1} if is_valid else {}
            if is_valid:
                # Increase confidence slightly
                await self.update(entry_id, update_data)

    async def deprecate(self, entry_id: str, reason: str) -> None:
        """Mark a knowledge entry as deprecated."""
        await self.update(entry_id, {
            "status": "deprecated",
            "explanation": reason
        })

    async def supersede(self, old_entry_id: str, new_entry: KnowledgeEntry) -> str:
        """Create a new entry that supersedes an old one."""
        new_id = await self.add(new_entry)
        await self.update(old_entry_id, {
            "status": "superseded",
            "superseded_by": new_id
        })
        return new_id

    async def add_from_analysis(self, analysis_result: dict) -> Optional[str]:
        """Add knowledge from an LLM analysis result.

        Validates knowledge before storing to ensure it's relevant to
        this audio-only download system.
        """
        learning = analysis_result.get("learning", {})

        if not learning.get("should_document"):
            return None

        # Validate the learning content before storing
        title = learning.get("title", "Untitled learning")
        observation = learning.get("observation", "No observation recorded")

        # Check title
        is_valid, reason = validate_knowledge_content(title)
        if not is_valid:
            print(f"[Knowledge] Rejected invalid learning (title): {reason}")
            return None

        # Check observation
        is_valid, reason = validate_knowledge_content(observation)
        if not is_valid:
            print(f"[Knowledge] Rejected invalid learning (observation): {reason}")
            return None

        # Also check any solution/fix details
        solution = learning.get("solution") or ""
        fix_details = str(analysis_result.get("fix", {}))
        combined = f"{solution} {fix_details}"
        is_valid, reason = validate_knowledge_content(combined)
        if not is_valid:
            print(f"[Knowledge] Rejected invalid learning (fix): {reason}")
            return None

        entry = KnowledgeEntry(
            category=learning.get("category", "error_pattern"),
            title=title,
            observation=observation,
            evidence={
                "analysis": analysis_result.get("diagnosis", {}),
                "llm_metadata": analysis_result.get("_llm_metadata", {})
            },
            confidence=analysis_result.get("diagnosis", {}).get("confidence", 0.5),
            tags=self._extract_tags(analysis_result)
        )

        print(f"[Knowledge] Adding valid learning: {title}")
        return await self.add(entry)

    def _extract_tags(self, analysis_result: dict) -> list[str]:
        """Extract relevant tags from analysis result."""
        tags = []
        diagnosis = analysis_result.get("diagnosis", {})

        if diagnosis.get("error_type"):
            tags.append(diagnosis["error_type"])

        fix = analysis_result.get("fix", {})
        if fix.get("type"):
            tags.append(f"fix:{fix['type']}")

        return tags

    async def build_context_string(
        self,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        limit: int = 5
    ) -> str:
        """Build a context string of relevant knowledge for LLM prompts."""
        entries = await self.get_relevant_knowledge(
            error_code=error_code,
            error_message=error_message,
            limit=limit
        )

        if not entries:
            return "No relevant past learnings found."

        lines = ["## Relevant Past Learnings\n"]
        for entry in entries:
            confidence_pct = int(entry["confidence"] * 100)
            lines.append(f"### {entry['title']} (Confidence: {confidence_pct}%)")
            lines.append(f"**Category:** {entry['category']}")
            lines.append(f"**Observation:** {entry['observation']}")
            if entry.get("solution"):
                lines.append(f"**Solution:** {entry['solution']}")
            lines.append("")

        return "\n".join(lines)

    async def get_statistics(self) -> dict:
        """Get knowledge base statistics."""
        response = self.supabase.table("agent_knowledge") \
            .select("category, status, confidence") \
            .execute()

        data = response.data or []

        # Calculate stats
        total = len(data)
        by_category: dict[str, int] = {}
        by_status: dict[str, int] = {}
        high_confidence = 0

        for entry in data:
            cat = entry.get("category", "unknown")
            status = entry.get("status", "unknown")
            conf = entry.get("confidence", 0)

            by_category[cat] = by_category.get(cat, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1
            if conf >= 0.7:
                high_confidence += 1

        return {
            "total_entries": total,
            "by_category": by_category,
            "by_status": by_status,
            "high_confidence_count": high_confidence,
            "average_confidence": sum(e.get("confidence", 0) for e in data) / total if total > 0 else 0
        }

    async def export_for_journal(self) -> dict:
        """Export knowledge for journal generation."""
        entries = await self.get_active_knowledge(min_confidence=0.0, limit=500)

        # Organize by category
        by_category: dict[str, list] = {}
        for entry in entries:
            cat = entry["category"]
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(entry)

        return {
            "entries": entries,
            "by_category": by_category,
            "statistics": await self.get_statistics()
        }
