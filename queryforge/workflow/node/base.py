"""Base interface shared by all QueryForge nodes."""

from __future__ import annotations

from abc import ABC, abstractmethod

from queryforge.core.schemas.models import Context, NodeResult


class Node(ABC):
    name = "node"
    description = "Base workflow node"
    status = "ready"

    @abstractmethod
    def execute(self, context: Context) -> NodeResult:
        """Read and update the shared context, then report node status."""

    def success(self, message: str) -> NodeResult:
        return NodeResult(
            node_name=self.name,
            success=True,
            status="success",
            message=message,
        )

    def failure(self, error: str) -> NodeResult:
        return NodeResult(
            node_name=self.name,
            success=False,
            status="failed",
            message=f"{self.name} failed",
            error=error,
        )
