"""Transport adapter protocol and CommandContext dataclass."""
import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Protocol, runtime_checkable


@dataclass
class CommandContext:
    """Passed from every transport adapter into CommandRouter for each user interaction."""
    args: List[str]                                      # Parsed command arguments
    user_id: str                                         # Transport-specific user identifier
    reply: Callable[[str], Awaitable[None]]              # Send text back to user
    send_typing: Callable[[], Awaitable[None]]           # Show typing indicator (no-op ok)


@runtime_checkable
class TransportAdapter(Protocol):
    """Protocol that every chat transport must implement."""
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_text(self, chat_id: str, text: str) -> None: ...
    async def send_typing(self, chat_id: str) -> None: ...
    def max_message_length(self) -> int: ...
