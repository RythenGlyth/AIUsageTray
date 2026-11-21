from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from ai_usage_tray.core.models import ProviderUsageSnapshot


@dataclass(slots=True)
class AuthStatus:
    """Current authentication/discovery state for a provider."""

    authenticated: bool
    source_description: str
    needs_interactive_login: bool = False


class UsageProvider(ABC):
    """Abstract base class for AI usage limit providers."""

    provider_id: str = ""
    display_name: str = ""
    supports_oauth: bool = False
    supports_token_tracking: bool = False
    default_poll_interval_s: int = 300

    @abstractmethod
    def fetch_usage(self) -> ProviderUsageSnapshot:
        """Fetch the latest usage snapshot synchronously (called on worker thread)."""

    @abstractmethod
    def get_auth_status(self) -> AuthStatus:
        """Return whether credentials or a local endpoint are available."""

    def start_oauth_login(self) -> tuple[bool, str]:
        """Run interactive OAuth login if supported. Returns (success, message)."""
        return False, f"{self.display_name} does not support interactive OAuth login."

    def disconnect_oauth(self) -> None:
        """Clear stored OAuth credentials for this provider."""
        return None


class ProviderRegistry:
    """Ordered registry of active UsageProvider instances."""

    def __init__(self) -> None:
        self._providers: dict[str, UsageProvider] = {}

    def register(self, provider: UsageProvider) -> None:
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> Optional[UsageProvider]:
        return self._providers.get(provider_id)

    def all(self) -> list[UsageProvider]:
        return list(self._providers.values())
