"""
LLM integration for Claude and Gemini.

Provides a unified interface for both providers with automatic fallback.
"""

import json
import asyncio
from typing import Optional, Any
from dataclasses import dataclass
from abc import ABC, abstractmethod

from .config import settings, agent_state


@dataclass
class LLMResponse:
    """Standardized LLM response."""
    content: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    raw_response: Optional[Any] = None


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Generate a response from the LLM."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is configured and available."""
        pass


class ClaudeProvider(LLMProvider):
    """Anthropic Claude provider."""

    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        self.model = model
        self._client = None

    def is_available(self) -> bool:
        return bool(settings.ANTHROPIC_API_KEY)

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            except ImportError:
                raise ImportError("anthropic package not installed. Run: pip install anthropic")
        return self._client

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        client = self._get_client()

        # Run in thread pool since anthropic client is sync
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt or "You are a helpful assistant.",
                messages=[{"role": "user", "content": prompt}]
            )
        )

        return LLMResponse(
            content=response.content[0].text,
            provider="claude",
            model=self.model,
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
            raw_response=response
        )


class GeminiProvider(LLMProvider):
    """Google Gemini provider."""

    def __init__(self, model: str = "gemini-1.5-flash"):
        self.model = model
        self._client = None

    def is_available(self) -> bool:
        return bool(settings.GOOGLE_API_KEY)

    def _get_client(self):
        if self._client is None:
            try:
                import google.generativeai as genai
                genai.configure(api_key=settings.GOOGLE_API_KEY)
                self._client = genai.GenerativeModel(self.model)
            except ImportError:
                raise ImportError("google-generativeai package not installed. Run: pip install google-generativeai")
        return self._client

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        client = self._get_client()

        # Combine system prompt with user prompt for Gemini
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"

        # Run in thread pool
        loop = asyncio.get_event_loop()

        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError("google-generativeai package not installed")

        generation_config = genai.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )

        response = await loop.run_in_executor(
            None,
            lambda: client.generate_content(
                full_prompt,
                generation_config=generation_config
            )
        )

        # Extract token counts (Gemini provides these differently)
        prompt_tokens = 0
        completion_tokens = 0
        if hasattr(response, 'usage_metadata'):
            prompt_tokens = getattr(response.usage_metadata, 'prompt_token_count', 0)
            completion_tokens = getattr(response.usage_metadata, 'candidates_token_count', 0)

        return LLMResponse(
            content=response.text,
            provider="gemini",
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            raw_response=response
        )


class LLMClient:
    """
    Unified LLM client that supports multiple providers with fallback.
    """

    def __init__(self):
        self.providers: dict[str, LLMProvider] = {}
        self._initialize_providers()

    def _initialize_providers(self):
        """Initialize available providers."""
        # Claude
        if settings.ANTHROPIC_API_KEY:
            self.providers["claude"] = ClaudeProvider(
                model="claude-sonnet-4-20250514"  # Default to Sonnet for analysis
            )
            self.providers["claude-opus"] = ClaudeProvider(
                model="claude-opus-4-20250514"  # Opus for complex reasoning
            )

        # Gemini
        if settings.GOOGLE_API_KEY:
            self.providers["gemini"] = GeminiProvider(
                model=settings.LLM_MODEL_ANALYSIS
            )
            self.providers["gemini-pro"] = GeminiProvider(
                model=settings.LLM_MODEL_SYNTHESIS
            )

    def get_available_providers(self) -> list[str]:
        """Get list of available provider names."""
        return [name for name, provider in self.providers.items() if provider.is_available()]

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        provider: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        fallback: bool = True,
    ) -> LLMResponse:
        """
        Generate a response using the specified or default provider.

        Args:
            prompt: The user prompt
            system_prompt: Optional system prompt
            provider: Provider to use ('claude', 'gemini', etc.). Defaults to settings.
            temperature: Sampling temperature (0-1)
            max_tokens: Maximum tokens in response
            fallback: If True, try other providers on failure

        Returns:
            LLMResponse with the generated content

        Raises:
            RuntimeError: If no providers are available or all fail
        """
        # Check rate limits
        if not agent_state.can_call_llm():
            raise RuntimeError("LLM rate limit exceeded")

        # Determine provider
        if provider is None:
            provider = settings.LLM_PROVIDER

        # Build provider order for fallback
        providers_to_try = []
        if provider in self.providers and self.providers[provider].is_available():
            providers_to_try.append(provider)

        if fallback:
            # Add other available providers
            for name, p in self.providers.items():
                if name not in providers_to_try and p.is_available():
                    providers_to_try.append(name)

        if not providers_to_try:
            raise RuntimeError("No LLM providers available. Configure ANTHROPIC_API_KEY or GOOGLE_API_KEY.")

        # Try each provider
        last_error = None
        for provider_name in providers_to_try:
            try:
                llm_provider = self.providers[provider_name]
                response = await llm_provider.generate(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                agent_state.record_llm_call()
                return response
            except Exception as e:
                last_error = e
                print(f"Provider {provider_name} failed: {e}")
                continue

        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")

    async def analyze_error(
        self,
        error_logs: str,
        system_state: dict,
        knowledge_context: str,
        current_config: dict,
    ) -> dict:
        """
        Analyze a download error and suggest a fix.

        Returns a structured diagnosis with recommended actions.
        """
        system_prompt = """You are an expert YouTube download troubleshooter for the SafePlay downloader service.
Your job is to analyze error logs, diagnose issues, and recommend fixes.

You have deep knowledge of:
- yt-dlp internals and configuration options
- YouTube's player clients (ios, mweb, android, tv_embedded)
- Proxy configuration and IP rotation
- Bot detection and rate limiting patterns
- CDN download mechanics

When analyzing errors, you should:
1. Identify the root cause from the logs
2. Check if this matches any known patterns from the knowledge base
3. Recommend a specific fix (configuration change or code modification)
4. Explain your reasoning

Always respond with valid JSON in this exact format:
{
    "diagnosis": {
        "error_type": "string - category of error",
        "root_cause": "string - what's actually causing the issue",
        "confidence": 0.0-1.0,
        "reasoning": "string - your analysis"
    },
    "fix": {
        "type": "config_change|code_fix|ytdlp_update|retry_later|escalate",
        "description": "string - what to do",
        "config_changes": {"key": "value"} or null,
        "code_changes": [
            {
                "file": "relative/path/to/file.py",
                "description": "what to change",
                "old_code": "exact code to find",
                "new_code": "replacement code"
            }
        ] or null,
        "requires_restart": true/false
    },
    "learning": {
        "should_document": true/false,
        "category": "error_pattern|youtube_behavior|workaround|etc",
        "title": "string - short title for knowledge base",
        "observation": "string - what we learned"
    }
}"""

        prompt = f"""Analyze this download failure and recommend a fix.

## Error Logs
```
{error_logs}
```

## Current System State
```json
{json.dumps(system_state, indent=2)}
```

## Current Configuration
```json
{json.dumps(current_config, indent=2)}
```

## Relevant Knowledge from Past Learnings
{knowledge_context}

Based on this information, diagnose the issue and recommend a fix.
Respond with JSON only, no markdown formatting."""

        response = await self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.2,  # Lower temperature for more consistent analysis
            max_tokens=2000,
        )

        # Parse the JSON response
        try:
            # Clean up response if needed (remove markdown code blocks if present)
            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()

            result = json.loads(content)
            result["_llm_metadata"] = {
                "provider": response.provider,
                "model": response.model,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
            }
            return result
        except json.JSONDecodeError as e:
            # If JSON parsing fails, return a structured error
            return {
                "diagnosis": {
                    "error_type": "analysis_failed",
                    "root_cause": f"Failed to parse LLM response: {e}",
                    "confidence": 0.0,
                    "reasoning": response.content
                },
                "fix": {
                    "type": "escalate",
                    "description": "Could not parse LLM analysis, escalating to human",
                    "config_changes": None,
                    "code_changes": None,
                    "requires_restart": False
                },
                "learning": {
                    "should_document": False,
                    "category": None,
                    "title": None,
                    "observation": None
                },
                "_llm_metadata": {
                    "provider": response.provider,
                    "model": response.model,
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "parse_error": str(e),
                    "raw_response": response.content
                }
            }

    async def synthesize_knowledge(
        self,
        telemetry_data: list[dict],
        existing_knowledge: list[dict],
        patterns: dict,
    ) -> dict:
        """
        Synthesize new knowledge from telemetry data and patterns.

        This is the "explorer's journal" function - it looks at accumulated
        data and forms insights about how YouTube works.
        """
        system_prompt = """You are a research scientist studying YouTube's download infrastructure.
Your job is to analyze telemetry data and patterns to form insights about how YouTube works.

You should:
1. Look for patterns in success/failure data
2. Form hypotheses about YouTube's behavior
3. Identify new learnings that should be documented
4. Update confidence in existing knowledge based on new evidence

Write like a scientist documenting discoveries - be precise, cite evidence, and acknowledge uncertainty.

Respond with valid JSON:
{
    "insights": [
        {
            "category": "youtube_behavior|error_pattern|success_pattern|temporal_pattern|proxy_pattern",
            "title": "Short descriptive title",
            "observation": "What was observed",
            "explanation": "Why this might happen",
            "evidence": "Data points supporting this",
            "confidence": 0.0-1.0,
            "actionable": true/false,
            "suggested_action": "What to do about it" or null
        }
    ],
    "knowledge_updates": [
        {
            "knowledge_id": "uuid or null for new",
            "action": "validate|invalidate|update|supersede",
            "reason": "Why this update"
        }
    ],
    "journal_entry": "A paragraph written like a research journal entry, documenting today's findings"
}"""

        prompt = f"""Analyze this telemetry data and generate insights.

## Recent Telemetry Summary (last 24 hours)
```json
{json.dumps(telemetry_data[:50], indent=2)}
```

## Computed Patterns
```json
{json.dumps(patterns, indent=2)}
```

## Existing Knowledge Base
```json
{json.dumps(existing_knowledge[:20], indent=2)}
```

Based on this data, identify new insights and update existing knowledge.
Respond with JSON only."""

        response = await self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            provider="gemini-pro" if "gemini-pro" in self.providers else None,  # Use stronger model for synthesis
            temperature=0.4,  # Slightly higher for more creative insights
            max_tokens=3000,
        )

        try:
            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()

            result = json.loads(content)
            result["_llm_metadata"] = {
                "provider": response.provider,
                "model": response.model,
                "tokens_used": response.total_tokens,
            }
            return result
        except json.JSONDecodeError:
            return {
                "insights": [],
                "knowledge_updates": [],
                "journal_entry": "Failed to generate insights due to parsing error.",
                "_llm_metadata": {
                    "provider": response.provider,
                    "model": response.model,
                    "parse_error": True,
                    "raw_response": response.content
                }
            }


# Global LLM client instance
llm_client = LLMClient()
