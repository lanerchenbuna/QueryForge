"""Select a declarative subject scope before loading schemas."""

from __future__ import annotations

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import Context, NodeResult
from queryforge.domain.semantic import SubjectTreeLoader


class SubjectSelectionNode(Node):
    name = "subject_selection"
    description = "Select an optional bounded subject scope before schema linking"

    def __init__(
        self,
        *,
        enabled: bool = False,
        subject_tree_path: str | None = None,
        requested_subject: str | None = None,
        default_subject: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.subject_tree_path = subject_tree_path
        self.requested_subject = requested_subject
        self.default_subject = default_subject

    def execute(self, context: Context) -> NodeResult:
        if not self.enabled:
            return self.success("Subject tree is disabled")
        if not self.subject_tree_path:
            return self.failure("subject_tree_enabled requires subject_tree_path")
        try:
            tree = SubjectTreeLoader.load(self.subject_tree_path)
            context.subject_selection = SubjectTreeLoader.select(
                tree,
                context.task.question,
                requested_subject=self.requested_subject,
                default_subject=self.default_subject,
            )
        except Exception as exc:
            return self.failure(f"Could not select subject scope: {exc}")
        selection = context.subject_selection
        if selection.status == "fallback_all":
            return self.success("Subject selection fell back to the full schema")
        assert selection.subject is not None
        return self.success(f"Selected subject {selection.subject.id!r}")
