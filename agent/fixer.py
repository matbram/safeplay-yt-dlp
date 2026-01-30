"""
Fixer module - Applies fixes to the downloader.

Handles configuration changes, code modifications, and git operations.
"""

import os
import asyncio
import subprocess
import re
from datetime import datetime, timezone
from typing import Optional, Any
from dataclasses import dataclass
from pathlib import Path

from .config import settings, agent_state


@dataclass
class FixResult:
    """Result of applying a fix."""
    success: bool
    fix_type: str
    description: str
    details: dict
    git_commit: Optional[str] = None
    requires_restart: bool = False
    error: Optional[str] = None


class Fixer:
    """Applies fixes to the downloader system."""

    def __init__(self, monitor, supabase_client):
        """
        Args:
            monitor: LogMonitor instance for config updates
            supabase_client: Supabase client for logging actions
        """
        self.monitor = monitor
        self.supabase = supabase_client
        self.repo_path = Path(settings.GIT_REPO_PATH)

    async def apply_fix(self, analysis_result: dict) -> FixResult:
        """
        Apply a fix based on LLM analysis.

        Args:
            analysis_result: The result from llm.analyze_error()

        Returns:
            FixResult with outcome details
        """
        fix = analysis_result.get("fix", {})
        fix_type = fix.get("type", "unknown")

        if not agent_state.can_make_fix_attempt():
            return FixResult(
                success=False,
                fix_type=fix_type,
                description="Rate limit exceeded",
                details={"reason": "max_fix_attempts_per_hour reached"},
                error="Rate limit exceeded"
            )

        agent_state.record_fix_attempt()

        try:
            if fix_type == "config_change":
                return await self._apply_config_change(fix)
            elif fix_type == "code_fix":
                return await self._apply_code_fix(fix)
            elif fix_type == "ytdlp_update":
                return await self._update_ytdlp()
            elif fix_type == "retry_later":
                return FixResult(
                    success=True,
                    fix_type=fix_type,
                    description="Recommended to retry later",
                    details={"action": "wait and retry"}
                )
            elif fix_type == "escalate":
                return FixResult(
                    success=False,
                    fix_type=fix_type,
                    description="Escalating to human",
                    details={"reason": fix.get("description", "Unknown")},
                    error="Requires human intervention"
                )
            else:
                return FixResult(
                    success=False,
                    fix_type=fix_type,
                    description=f"Unknown fix type: {fix_type}",
                    details={},
                    error=f"Unknown fix type: {fix_type}"
                )
        except Exception as e:
            return FixResult(
                success=False,
                fix_type=fix_type,
                description=f"Fix failed: {str(e)}",
                details={"exception": str(e)},
                error=str(e)
            )

    async def _apply_config_change(self, fix: dict) -> FixResult:
        """Apply a configuration change via the admin API."""
        config_changes = fix.get("config_changes", {})

        if not config_changes:
            return FixResult(
                success=False,
                fix_type="config_change",
                description="No config changes specified",
                details={},
                error="Empty config_changes"
            )

        # Apply via monitor's update_config
        success = await self.monitor.update_config(config_changes)

        return FixResult(
            success=success,
            fix_type="config_change",
            description=fix.get("description", "Applied config changes"),
            details={"changes": config_changes},
            requires_restart=fix.get("requires_restart", False)
        )

    async def _apply_code_fix(self, fix: dict) -> FixResult:
        """Apply code modifications and commit to git."""
        code_changes = fix.get("code_changes", [])

        if not code_changes:
            return FixResult(
                success=False,
                fix_type="code_fix",
                description="No code changes specified",
                details={},
                error="Empty code_changes"
            )

        # Ensure we're on the correct branch
        await self._ensure_branch()

        modified_files = []
        applied_changes = []

        for change in code_changes:
            file_path = self.repo_path / change["file"]

            if not file_path.exists():
                return FixResult(
                    success=False,
                    fix_type="code_fix",
                    description=f"File not found: {change['file']}",
                    details={"file": change["file"]},
                    error=f"File not found: {change['file']}"
                )

            try:
                # Read current content
                content = file_path.read_text()

                # Find and replace
                old_code = change["old_code"]
                new_code = change["new_code"]

                if old_code not in content:
                    return FixResult(
                        success=False,
                        fix_type="code_fix",
                        description=f"Code to replace not found in {change['file']}",
                        details={
                            "file": change["file"],
                            "old_code_snippet": old_code[:100] + "..." if len(old_code) > 100 else old_code
                        },
                        error="Old code not found"
                    )

                # Apply change
                new_content = content.replace(old_code, new_code, 1)
                file_path.write_text(new_content)

                modified_files.append(change["file"])
                applied_changes.append(change["description"])

            except Exception as e:
                return FixResult(
                    success=False,
                    fix_type="code_fix",
                    description=f"Failed to modify {change['file']}: {e}",
                    details={"file": change["file"], "error": str(e)},
                    error=str(e)
                )

        # Commit changes
        commit_msg = fix.get("description", "Agent auto-fix")
        commit_hash = await self._git_commit(modified_files, commit_msg)

        if not commit_hash:
            return FixResult(
                success=False,
                fix_type="code_fix",
                description="Failed to commit changes",
                details={"files": modified_files},
                error="Git commit failed"
            )

        # Push if enabled
        if settings.GIT_AUTO_PUSH:
            push_success = await self._git_push()
            if not push_success:
                return FixResult(
                    success=True,  # Changes applied, just push failed
                    fix_type="code_fix",
                    description="Changes committed but push failed",
                    details={"files": modified_files, "changes": applied_changes},
                    git_commit=commit_hash,
                    requires_restart=True,
                    error="Push failed - changes committed locally"
                )

        return FixResult(
            success=True,
            fix_type="code_fix",
            description=commit_msg,
            details={"files": modified_files, "changes": applied_changes},
            git_commit=commit_hash,
            requires_restart=True
        )

    async def _update_ytdlp(self) -> FixResult:
        """Update yt-dlp to the latest version."""
        try:
            # Get current version
            result = await self._run_command(["pip", "show", "yt-dlp"])
            current_version = "unknown"
            for line in result.stdout.split("\n"):
                if line.startswith("Version:"):
                    current_version = line.split(":")[1].strip()
                    break

            # Update
            result = await self._run_command(
                ["pip", "install", "--upgrade", "yt-dlp"],
                timeout=120
            )

            if result.returncode != 0:
                return FixResult(
                    success=False,
                    fix_type="ytdlp_update",
                    description="Failed to update yt-dlp",
                    details={"error": result.stderr},
                    error=result.stderr
                )

            # Get new version
            result = await self._run_command(["pip", "show", "yt-dlp"])
            new_version = "unknown"
            for line in result.stdout.split("\n"):
                if line.startswith("Version:"):
                    new_version = line.split(":")[1].strip()
                    break

            return FixResult(
                success=True,
                fix_type="ytdlp_update",
                description=f"Updated yt-dlp from {current_version} to {new_version}",
                details={
                    "old_version": current_version,
                    "new_version": new_version
                },
                requires_restart=True
            )

        except Exception as e:
            return FixResult(
                success=False,
                fix_type="ytdlp_update",
                description=f"Failed to update yt-dlp: {e}",
                details={"exception": str(e)},
                error=str(e)
            )

    async def restart_service(self) -> bool:
        """Restart the downloader service."""
        try:
            result = await self._run_command(
                settings.SUDO_RESTART_COMMAND.split(),
                timeout=30
            )
            return result.returncode == 0
        except Exception as e:
            print(f"Failed to restart service: {e}")
            return False

    async def _ensure_branch(self) -> None:
        """Ensure we're on the correct git branch."""
        # Check current branch
        result = await self._run_command(
            ["git", "branch", "--show-current"],
            cwd=self.repo_path
        )
        current_branch = result.stdout.strip()

        if current_branch != settings.GIT_BRANCH:
            # Check if branch exists
            result = await self._run_command(
                ["git", "branch", "--list", settings.GIT_BRANCH],
                cwd=self.repo_path
            )

            if not result.stdout.strip():
                # Create branch
                await self._run_command(
                    ["git", "checkout", "-b", settings.GIT_BRANCH],
                    cwd=self.repo_path
                )
            else:
                # Switch to branch
                await self._run_command(
                    ["git", "checkout", settings.GIT_BRANCH],
                    cwd=self.repo_path
                )

    async def _git_commit(self, files: list[str], message: str) -> Optional[str]:
        """Commit changes to git."""
        try:
            # Stage files
            for file in files:
                await self._run_command(
                    ["git", "add", file],
                    cwd=self.repo_path
                )

            # Create commit
            full_message = f"[Agent] {message}\n\nAutomatically applied by SafePlay AI Agent"
            result = await self._run_command(
                ["git", "commit", "-m", full_message],
                cwd=self.repo_path
            )

            if result.returncode != 0:
                return None

            # Get commit hash
            result = await self._run_command(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_path
            )
            return result.stdout.strip()

        except Exception as e:
            print(f"Git commit failed: {e}")
            return None

    async def _git_push(self) -> bool:
        """Push to remote with retry logic."""
        delays = [2, 4, 8, 16]  # Exponential backoff

        for attempt, delay in enumerate(delays):
            try:
                result = await self._run_command(
                    ["git", "push", "-u", settings.GIT_REMOTE, settings.GIT_BRANCH],
                    cwd=self.repo_path,
                    timeout=60
                )

                if result.returncode == 0:
                    return True

                # Check if it's a network error worth retrying
                if "network" in result.stderr.lower() or "connection" in result.stderr.lower():
                    print(f"Push attempt {attempt + 1} failed, retrying in {delay}s...")
                    await asyncio.sleep(delay)
                else:
                    # Non-retryable error
                    print(f"Push failed with non-retryable error: {result.stderr}")
                    return False

            except Exception as e:
                print(f"Push attempt {attempt + 1} failed: {e}")
                if attempt < len(delays) - 1:
                    await asyncio.sleep(delay)

        return False

    async def _run_command(
        self,
        cmd: list[str],
        cwd: Optional[Path] = None,
        timeout: int = 60
    ) -> subprocess.CompletedProcess:
        """Run a command asynchronously."""
        loop = asyncio.get_event_loop()

        def run():
            return subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout
            )

        return await loop.run_in_executor(None, run)

    async def log_action(
        self,
        trigger_type: str,
        action_type: str,
        description: str,
        result: FixResult,
        trigger_job_id: Optional[str] = None,
        trigger_error_code: Optional[str] = None,
        trigger_error_message: Optional[str] = None,
        llm_metadata: Optional[dict] = None,
        knowledge_entry_id: Optional[str] = None,
        used_knowledge_ids: Optional[list[str]] = None,
    ) -> str:
        """Log an agent action to the database."""
        record = {
            "trigger_type": trigger_type,
            "trigger_job_id": trigger_job_id,
            "trigger_error_code": trigger_error_code,
            "trigger_error_message": trigger_error_message,
            "action_type": action_type,
            "action_description": description,
            "config_changes": result.details.get("changes") if result.fix_type == "config_change" else None,
            "files_modified": result.details.get("files"),
            "git_commit": result.git_commit,
            "git_branch": settings.GIT_BRANCH if result.git_commit else None,
            "outcome": "success" if result.success else "failure",
            "outcome_details": result.error if not result.success else None,
            "knowledge_entry_id": knowledge_entry_id,
            "used_knowledge_ids": used_knowledge_ids,
        }

        if llm_metadata:
            record["llm_provider"] = llm_metadata.get("provider")
            record["llm_model"] = llm_metadata.get("model")
            record["llm_prompt_tokens"] = llm_metadata.get("prompt_tokens")
            record["llm_completion_tokens"] = llm_metadata.get("completion_tokens")
            record["llm_reasoning"] = llm_metadata.get("reasoning")

        try:
            response = self.supabase.table("agent_actions").insert(record).execute()

            if response.data:
                return response.data[0]["id"]
            return ""
        except Exception as e:
            print(f"Warning: Could not log action to database: {e}")
            return ""

    async def check_ytdlp_version(self) -> dict:
        """Check current and latest yt-dlp versions."""
        try:
            # Get current version
            result = await self._run_command(["pip", "show", "yt-dlp"])
            current_version = "unknown"
            for line in result.stdout.split("\n"):
                if line.startswith("Version:"):
                    current_version = line.split(":")[1].strip()
                    break

            # Check for updates
            result = await self._run_command(
                ["pip", "index", "versions", "yt-dlp"],
                timeout=30
            )

            latest_version = current_version
            if result.returncode == 0:
                # Parse output to find latest version
                match = re.search(r"Available versions: ([\d.]+)", result.stdout)
                if match:
                    latest_version = match.group(1)

            return {
                "current": current_version,
                "latest": latest_version,
                "update_available": current_version != latest_version
            }

        except Exception as e:
            return {
                "current": "unknown",
                "latest": "unknown",
                "update_available": False,
                "error": str(e)
            }
