from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MessageContentPart(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = "text"
    text: str | None = None
    image_url: dict[str, Any] | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[MessageContentPart] | None = ""
    name: str | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    temperature: float | None = 1.0
    top_p: float | None = 1.0
    n: int | None = 1
    stream: bool = False
    max_tokens: int | None = None
    stop: str | list[str] | None = None
    user: str | None = None


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str
    permission: list[dict[str, Any]] = Field(default_factory=list)
    root: str
    parent: str | None = None


def content_to_text(content: str | list[MessageContentPart] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for part in content:
        if part.type in {"text", "input_text"} and part.text:
            parts.append(part.text)
    return "".join(parts)


def split_system_messages(messages: list[ChatMessage]) -> tuple[str | None, list[ChatMessage]]:
    system_parts: list[str] = []
    normal_messages: list[ChatMessage] = []

    for message in messages:
        if message.role == "system":
            text = content_to_text(message.content)
            if text:
                system_parts.append(text)
            continue
        normal_messages.append(message)

    system_text = "\n\n".join(system_parts).strip() or None
    return system_text, normal_messages
