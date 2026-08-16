from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


BlockedAction = Literal["delete", "submit_payment", "export_data", "admin_change"]
StepKind = Literal[
    "goto",
    "click",
    "fill",
    "press",
    "waitFor",
    "extractText",
    "assertText",
    "humanApproval",
    "screenshot",
    "done",
]
RiskLevel = Literal["low", "medium", "high"]
ParameterType = Literal["string", "number", "boolean"]
ReplayStatus = Literal["success", "business_outcome", "recoverable", "failure"]


class SensitivePolicy(BaseModel):
    redactInputs: bool = True
    redactOutputs: bool = True
    allowedDomains: list[str] = Field(default_factory=list)
    blockedActions: list[BlockedAction] = Field(default_factory=list)


class ParameterSpec(BaseModel):
    name: str
    type: ParameterType
    required: bool = True
    secret: bool = False
    description: str | None = None


class OutputSpec(BaseModel):
    name: str
    type: ParameterType
    description: str | None = None
    sensitive: bool = False


class SuccessCondition(BaseModel):
    type: Literal["text_not_equals", "text_contains"]
    selector: str
    expectedText: str
    description: str | None = None


class TargetSpec(BaseModel):
    strategy: Literal["css"] = "css"
    primary: str
    fallbacks: list[str] = Field(default_factory=list)
    robustness: Literal["low", "medium", "high"] = "medium"
    reasoning: str | None = None


class Step(BaseModel):
    id: str
    kind: StepKind
    selector: str | None = None
    target: TargetSpec | None = None
    url: str | None = None
    value: str | None = None
    key: str | None = None
    timeoutMs: int | None = None
    outputKey: str | None = None
    expectedText: str | None = None
    risk: RiskLevel = "low"
    sensitive: bool = False
    continueOnError: bool = False
    description: str | None = None


class Artifact(BaseModel):
    version: Literal["1.0"] = "1.0"
    artifactId: str
    name: str
    description: str
    taskTemplate: str
    createdAt: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    startUrl: str
    parameters: list[ParameterSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    successCondition: SuccessCondition | None = None
    safety: SensitivePolicy
    steps: list[Step] = Field(default_factory=list)


class ReplayResult(BaseModel):
    status: ReplayStatus
    artifactId: str
    outputs: dict[str, str] = Field(default_factory=dict)
    businessOutcomeCode: str | None = None
    message: str | None = None
    failedStepId: str | None = None
    expected: str | None = None
    observed: str | None = None
    evidence: dict[str, str] = Field(default_factory=dict)


class DiscoveryAction(BaseModel):
    kind: Literal[
        "goto",
        "click",
        "fill",
        "press",
        "waitFor",
        "extractText",
        "assertText",
        "humanApproval",
        "screenshot",
        "done",
    ]
    selector: str | None = None
    url: str | None = None
    value: str | None = None
    key: str | None = None
    outputKey: str | None = None
    expectedText: str | None = None
    description: str | None = None
    risk: RiskLevel = "low"
    sensitive: bool = False
    continueOnError: bool = False


class DiscoveryDecision(BaseModel):
    thought: str
    action: DiscoveryAction
