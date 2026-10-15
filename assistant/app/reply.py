"""What a skill hands back to the conversation loop."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


@dataclass
class Reply:
    text: str = ""                     # spoken (may be several sentences)
    emotion: str = "happy"             # face shown while speaking
    # The next thing the user says goes here first (confirmations, follow-up questions).
    # Return None from it to fall back to normal handling.
    expect: Optional[Callable[[str], Awaitable[Optional["Reply"]]]] = None
    after: Optional[Callable[[], Awaitable[None]]] = None   # runs after speaking
    llm_context: Optional[str] = None  # answer with the local model using this data
    question: Optional[str] = None     # the question for llm_context / claude
    claude: bool = False               # answer with Claude (+ web search)
    end: bool = False                  # hang up after speaking
