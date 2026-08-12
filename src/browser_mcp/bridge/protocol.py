"""Typed messages and connection metadata for the extension protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExtensionHello(BaseModel):
    """Authenticated first message required from every extension connection."""

    model_config = ConfigDict(populate_by_name=True)

    type: str
    token: str
    version: str
    build_id: str = Field(alias="buildId")
    extension_id: str | None = Field(default=None, alias="extensionId")
    user_agent: str | None = Field(default=None, alias="userAgent")
    port: int | None = None


@dataclass(frozen=True, slots=True)
class ConnectionMetadata:
    """Non-secret information retained after a successful handshake."""

    connected_at: datetime
    last_seen_at: datetime
    version: str
    build_id: str
    extension_id: str | None
    user_agent: str | None


JsonObject = dict[str, Any]
