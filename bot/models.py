from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Plan(StrictModel):
    intent: Literal[
        "summary",
        "promo_rule",
        "target_rule",
        "inline_rule",
        "media_rule",
        "copy_message",
        "delete_message",
        "read_message",
        "help",
        "unknown",
    ]
    count: int = Field(ge=1, le=1000)
    action: Literal["delete", "mute_review"]
    media: Literal["all", "sticker", "animation", "document", "video", "photo", "audio", "voice"]
    mention: str
    inline_username: str
    target_username: str
    policy: str
    detailed: bool
    rate: bool
    future_only: bool
    clarification: str
    media_types: list[Literal["sticker", "animation", "document", "video", "photo", "audio", "voice"]] = (
        Field(default_factory=list)
    )
    warn: bool = False
    kick_after: int = Field(default=0, ge=0, le=100)
    message_link: str = ""


class Verdict(StrictModel):
    promo: bool
    confidence: float = Field(ge=0, le=1)
    reason: str
    evidence: str


class Correction(StrictModel):
    lesson: str
    scope: str
