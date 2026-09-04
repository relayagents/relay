"""A2A wire types (JSON, camelCase) as Pydantic models.

These mirror the A2A protocol's AgentCard/Message/Task shapes closely enough for any A2A
client to interoperate with the broker. We deliberately do not depend on ``a2a-sdk`` at
runtime: its 1.x types are protobuf messages, and the broker is a small JSON service.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from relayagents.core.ids import new_id

TaskState = Literal[
    "submitted",
    "working",
    "input_required",
    "completed",
    "failed",
    "canceled",
    "rejected",
    "auth_required",
]


class A2AModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow", serialize_by_alias=True)


class AgentCapabilities(A2AModel):
    streaming: bool = False
    push_notifications: bool = Field(default=False, alias="pushNotifications")
    extensions: list[dict[str, Any]] = Field(default_factory=list)


class AgentSkill(A2AModel):
    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class AgentInterface(A2AModel):
    url: str
    protocol_binding: str = Field(default="JSONRPC", alias="protocolBinding")
    protocol_version: str = Field(default="1.0", alias="protocolVersion")


class AgentProvider(A2AModel):
    organization: str
    url: str | None = None


class AgentCard(A2AModel):
    name: str
    description: str = ""
    version: str = "0.1.0"
    supported_interfaces: list[AgentInterface] = Field(
        default_factory=list, alias="supportedInterfaces"
    )
    provider: AgentProvider | None = None
    documentation_url: str | None = Field(default=None, alias="documentationUrl")
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    default_input_modes: list[str] = Field(
        default_factory=lambda: ["text/plain"], alias="defaultInputModes"
    )
    default_output_modes: list[str] = Field(
        default_factory=lambda: ["text/plain"], alias="defaultOutputModes"
    )
    skills: list[AgentSkill] = Field(default_factory=list)


class Part(A2AModel):
    text: str | None = None
    data: dict[str, Any] | None = None
    media_type: str | None = Field(default=None, alias="mediaType")


class Message(A2AModel):
    message_id: str = Field(default_factory=lambda: new_id("msg"), alias="messageId")
    context_id: str | None = Field(default=None, alias="contextId")
    task_id: str | None = Field(default=None, alias="taskId")
    role: Literal["user", "agent"] = "user"
    parts: list[Part] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.parts if p.text)

    @classmethod
    def from_text(cls, text: str, *, role: Literal["user", "agent"] = "user", **kw: Any) -> Message:
        return cls(role=role, parts=[Part(text=text)], **kw)


class TaskStatus(A2AModel):
    state: TaskState = "submitted"
    message: Message | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Artifact(A2AModel):
    artifact_id: str = Field(default_factory=lambda: new_id("art"), alias="artifactId")
    name: str | None = None
    parts: list[Part] = Field(default_factory=list)


class Task(A2AModel):
    id: str
    context_id: str = Field(alias="contextId")
    status: TaskStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SendMessageParams(A2AModel):
    """Params for ``message/send``. ``metadata.to`` selects the destination agent."""

    message: Message
    configuration: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskUpdate(A2AModel):
    """Broker extension: an agent reports progress/completion on a task in its inbox."""

    state: TaskState
    message: Message | None = None
    artifacts: list[Artifact] = Field(default_factory=list)


def default_agent_card(agent_id: str, user_display: str, relay_url: str, harness: str) -> AgentCard:
    return AgentCard(
        name=agent_id,
        description=f"{user_display}'s {harness} agent, reachable through the Relay broker.",
        supported_interfaces=[AgentInterface(url=f"{relay_url}/a2a/agents/{agent_id}")],
        provider=AgentProvider(organization="relay", url=relay_url),
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        skills=[
            AgentSkill(
                id="answer",
                name="Answer a question",
                description="Answer a teammate's question using its human's context.",
            ),
            AgentSkill(
                id="action_item",
                name="Work an action item",
                description="Take an assigned action item, request approvals, and report back.",
            ),
        ],
    )
