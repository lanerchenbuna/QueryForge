"""Definition-version fingerprints a session turn records.

Stage 13 gave ``SessionStore.invalidate_version`` the ability to mark exactly the
turns that used a superseded definition, but the references it matches against
were produced in one place only: ``AgentService`` annotated the session turn
*after* ``OrchestratorAgent`` had already written it. A session written by any
other entry point therefore recorded no version whatsoever, and two of the four
declared reference kinds (``glossary`` and ``knowledge``) had no producer at all,
so ``invalidate_version("glossary:...")`` could never match anything.

Both writers now build their references through this module, so a version is
computed one way: a content digest of the definition the run actually used — the
semantic-model file for ``model``/``metric`` references, the governed entry text
for ``glossary``/``knowledge`` references. A digest is the honest revision
identifier: any edit to a formula, a dimension, a glossary definition or a
governed document changes it, and ``KnowledgeVersionRef.matches`` also accepts
the bare id and the ``kind:id`` handle an operator reads off a session status.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from queryforge.domain.knowledge import content_version
from queryforge.orchestration.schemas.session import KnowledgeVersionRef


__all__ = [
    "RETRIEVAL_ORIGIN",
    "RETRIEVAL_REF_KINDS",
    "SEMANTIC_MODEL_ORIGIN",
    "knowledge_retrieval_version_refs",
    "knowledge_version_refs",
    "merge_version_refs",
]


#: Origin recorded on references derived from the semantic-model file.
SEMANTIC_MODEL_ORIGIN = "semantic_model"
#: Origin recorded on references derived from governed documents a run retrieved.
RETRIEVAL_ORIGIN = "vector_retrieval"
#: Retrieved source types that name a governed definition, and the reference kind
#: each one publishes. Everything else a run may retrieve (schema documents, SQL
#: examples, reference templates) is material generated or curated *per run*
#: rather than a versioned definition, so it publishes no reference at all.
RETRIEVAL_REF_KINDS = {"glossary": "glossary", "knowledge_document": "knowledge"}
#: Metadata keys that hold the governed identifier of a retrieved document, most
#: specific first.
_GOVERNED_ID_KEYS = ("term", "knowledge_id", "source_id")
#: The document-id prefix the knowledge producer uses (``knowledge:<source_id>``);
#: a reference renders its own ``kind:`` prefix, so it must not be stored twice.
_KNOWLEDGE_ID_PREFIX = "knowledge:"


def knowledge_version_refs(
    semantic_model_path: str | None,
    metric_ids: "list[str] | tuple[str, ...]" = (),
    *,
    origin: str = SEMANTIC_MODEL_ORIGIN,
) -> list[KnowledgeVersionRef]:
    """The semantic-model versions a turn relied on, for durable invalidation.

    Each turn records one ``model`` reference for the semantic model it ran
    against and one ``metric`` reference per governed metric it used, both
    versioned by the content digest of the semantic-model file. A digest is the
    honest definition revision: any formula, dimension or entity edit changes it,
    and ``KnowledgeVersionRef.matches`` also accepts the bare metric id, so an
    operator can invalidate by whichever identifier they have at hand.

    An unreadable or missing model file yields no references rather than a
    fabricated one: a session must never claim a version a run did not use.
    """

    if not semantic_model_path:
        return []
    path = Path(semantic_model_path).expanduser()
    try:
        digest = sha256(path.read_bytes()).hexdigest()[:12]
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return []
    name = path.stem
    if isinstance(payload, dict) and str(payload.get("name") or "").strip():
        name = str(payload["name"]).strip()
    refs = [
        KnowledgeVersionRef(kind="model", id=name, version=digest, origin=origin)
    ]
    for metric in metric_ids or ():
        if str(metric).strip():
            refs.append(
                KnowledgeVersionRef(
                    kind="metric", id=str(metric).strip(), version=digest, origin=origin
                )
            )
    return refs


def knowledge_retrieval_version_refs(
    documents: Iterable[Any] | None = None,
    *,
    origin: str = RETRIEVAL_ORIGIN,
) -> list[KnowledgeVersionRef]:
    """The versions of the governed knowledge a run actually retrieved.

    ``documents`` are the retrieval matches that survived governance filtering
    and the context budget (``Context.vector_schema_matches``), so a reference
    exists only for knowledge that really entered the run:

    * a ``glossary`` document publishes ``glossary:<term>@<digest>``, and
    * a ``knowledge_document`` publishes ``knowledge:<source_id>@<digest>``,

    both versioned by the content digest of the retrieved definition text — for a
    definition document that text *is* the governed entry, and for a chunked source
    document it is the chunk that entered the context while the id stays the source
    document's, so an id-level invalidation reaches every chunk. Metric definitions
    are deliberately *not* published here: they are already versioned by the
    semantic-model digest, and a second digest for the same metric id would make
    ``invalidate_version("metric:<id>@<version>")`` ambiguous. A run that retrieved
    no governed knowledge records no reference, and a document without a governed
    identifier records none either — the turn never invents knowledge it did not
    use.
    """

    references: list[KnowledgeVersionRef] = []
    for document in documents or ():
        kind = RETRIEVAL_REF_KINDS.get(str(_field(document, "source_type") or "").strip())
        if kind is None:
            continue
        identifier = _governed_identifier(document, kind)
        version = content_version(str(_field(document, "text") or ""))
        if not identifier or not version:
            continue
        references.append(
            KnowledgeVersionRef(kind=kind, id=identifier, version=version, origin=origin)
        )
    return merge_version_refs(
        sorted(references, key=lambda item: (item.kind, item.id, item.version))
    )


def merge_version_refs(
    *groups: Iterable[KnowledgeVersionRef] | None,
) -> list[KnowledgeVersionRef]:
    """Union of reference groups: first writer wins, no duplicate reference.

    WHY: the orchestrator records the versions it saw and the application service
    then re-annotates the *same* turn. Appending blindly would list every shared
    version twice, and rebinding the list wholesale from the second writer would
    drop the knowledge references only the orchestrator has. Merging by rendered
    reference keeps exactly one entry per definition version.
    """

    merged: list[KnowledgeVersionRef] = []
    seen: set[str] = set()
    for group in groups:
        for reference in group or ():
            key = reference.reference()
            if key in seen:
                continue
            seen.add(key)
            merged.append(reference)
    return merged


def _field(document: Any, name: str) -> Any:
    """Read one field of a retrieval match, whether it is a model or a mapping."""
    if isinstance(document, Mapping):
        return document.get(name)
    return getattr(document, name, None)


def _governed_identifier(document: Any, kind: str) -> str:
    """The governed id a reference names (a glossary term or a source document id)."""
    metadata = _field(document, "metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    for key in _GOVERNED_ID_KEYS:
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    if kind != "knowledge":
        return ""
    # The producer's document id is ``knowledge:<source_id>``; the reference
    # already renders a ``knowledge:`` prefix, so the source id is what is stored.
    identifier = str(_field(document, "id") or "").strip()
    if identifier.startswith(_KNOWLEDGE_ID_PREFIX):
        return identifier[len(_KNOWLEDGE_ID_PREFIX) :]
    return identifier
