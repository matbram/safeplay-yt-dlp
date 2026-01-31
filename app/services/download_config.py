"""
Shared download configuration that can be updated at runtime.

This config is used by:
- youtube.py (reads config for download settings)
- admin.py (updates config via API)
- agent (updates config via API when applying fixes)
"""

from typing import Optional
from pydantic import BaseModel


class DownloadConfig(BaseModel):
    """Download configuration options.

    NOTE: SafePlay currently downloads AUDIO ONLY.
    The format is set to: worstaudio[protocol=https]/worstaudio/bestaudio[ext=m4a]/bestaudio
    """
    # Retry settings
    retries: int = 2
    fragment_retries: int = 2

    # Network settings
    socket_timeout: int = 30
    rate_limit: Optional[str] = None  # e.g., "50M" for 50MB/s
    geo_bypass: bool = True

    # Download behavior
    concurrent_fragments: int = 8
    no_playlist: bool = True

    # Player client preference (for agent to optimize)
    # Options: ios, mweb, android, tv_embedded, web
    preferred_player_client: Optional[str] = None


# Global runtime config - this is what youtube.py reads
_current_config = DownloadConfig()


def get_config() -> DownloadConfig:
    """Get the current download configuration."""
    return _current_config


def update_config(updates: dict) -> DownloadConfig:
    """
    Update the download configuration.

    Args:
        updates: Dictionary of config values to update

    Returns:
        The updated configuration
    """
    global _current_config

    # Create new config with updates
    current_dict = _current_config.model_dump()
    current_dict.update(updates)
    _current_config = DownloadConfig(**current_dict)

    return _current_config


def reset_config() -> DownloadConfig:
    """Reset configuration to defaults."""
    global _current_config
    _current_config = DownloadConfig()
    return _current_config
