"""Allowlisted, immutable and resumable knowledge ingestion."""

from astock.knowledge.article_recovery import (
    ZhihuArticleRecoveryExecution,
    ZhihuArticleRecoveryService,
)
from astock.knowledge.audit import KnowledgeCoverageAuditService
from astock.knowledge.capture import (
    ZhihuCaptureAck,
    ZhihuLoopbackCaptureSession,
    create_loopback_capture_server,
    loopback_installer_url,
    loopback_status_url,
    serve_loopback_capture,
)
from astock.knowledge.capture_coordinator import (
    ZhihuCaptureRequest,
    ZhihuCoordinatorAck,
    ZhihuFullCaptureSession,
    build_coordinator_capture_extension,
)
from astock.knowledge.comments import (
    ZhihuCommentIngestExecution,
    ZhihuCommentService,
    derive_author_participation_chains,
    derive_keyword_filtered_author_participation_chains,
)
from astock.knowledge.config import (
    get_knowledge_source,
    load_distillation_rules,
    load_knowledge_sources,
    load_zhihu_endpoint_templates,
)
from astock.knowledge.distillation import (
    DistillationExecution,
    KnowledgeDistillationService,
)
from astock.knowledge.distillation_repository import DistillationRepository
from astock.knowledge.distillation_storage import ParquetDistillationStore
from astock.knowledge.draft_repository import KnowledgeDraftRepository
from astock.knowledge.drafts import KnowledgeDraftExecution, KnowledgeDraftService
from astock.knowledge.imports import (
    ZhihuCommentReplayExecution,
    ZhihuDetailReplayExecution,
    ZhihuImportExecution,
    ZhihuReplayExecution,
    ZhihuResponseImportService,
)
from astock.knowledge.manual_tasks import ZhihuManualTaskService
from astock.knowledge.python_recovery import (
    ZhihuPythonRecoveryExecution,
    ZhihuPythonRecoveryService,
)
from astock.knowledge.repository import ContentRegistration, KnowledgeRepository
from astock.knowledge.semantic_embedding import (
    MODEL_ID,
    MODEL_REVISION,
    EmbeddingBackend,
    EncodedText,
    RecordedEmbeddingBackend,
    SemanticEmbeddingExecution,
    SemanticEmbeddingService,
    SentenceTransformerBackend,
    default_model_directory,
    install_local_model,
    verify_local_model,
)
from astock.knowledge.semantic_funnel import (
    ParagraphizedContent,
    build_argument_units,
    load_semantic_funnel_config,
    local_context_paragraph_ids,
    method_keyword_terms,
    paragraphize_zhihu_content,
)
from astock.knowledge.semantic_packets import (
    SemanticPacketExecution,
    SemanticPacketService,
)
from astock.knowledge.semantic_repository import SemanticFunnelRepository
from astock.knowledge.semantic_service import (
    SemanticFunnelExecution,
    SemanticFunnelService,
)
from astock.knowledge.semantic_storage import (
    ParquetSemanticStore,
    SemanticParquetWrite,
    SemanticVectorRecord,
)
from astock.knowledge.service import ZhihuCollectionService, ZhihuSyncExecution
from astock.knowledge.storage import ParquetKnowledgeStore
from astock.knowledge.structure_profiles import (
    KnowledgeStructureProfileService,
    KnowledgeStructureRepository,
)
from astock.knowledge.transport import (
    PersistedZhihuResponse,
    ZhihuArticleHtmlTransport,
    ZhihuHttpTransport,
    ZhihuResponseTransport,
)

__all__ = [
    "ContentRegistration",
    "DistillationExecution",
    "DistillationRepository",
    "KnowledgeCoverageAuditService",
    "KnowledgeDraftExecution",
    "KnowledgeDraftRepository",
    "KnowledgeDraftService",
    "KnowledgeDistillationService",
    "KnowledgeRepository",
    "KnowledgeStructureProfileService",
    "KnowledgeStructureRepository",
    "MODEL_ID",
    "MODEL_REVISION",
    "EmbeddingBackend",
    "EncodedText",
    "ParquetKnowledgeStore",
    "ParquetDistillationStore",
    "ParquetSemanticStore",
    "ParagraphizedContent",
    "PersistedZhihuResponse",
    "ZhihuArticleHtmlTransport",
    "ZhihuArticleRecoveryExecution",
    "ZhihuArticleRecoveryService",
    "ZhihuCollectionService",
    "ZhihuCaptureAck",
    "ZhihuCaptureRequest",
    "ZhihuCommentIngestExecution",
    "ZhihuCommentService",
    "ZhihuCommentReplayExecution",
    "ZhihuDetailReplayExecution",
    "ZhihuHttpTransport",
    "ZhihuImportExecution",
    "ZhihuReplayExecution",
    "ZhihuResponseImportService",
    "ZhihuResponseTransport",
    "ZhihuLoopbackCaptureSession",
    "ZhihuCoordinatorAck",
    "ZhihuFullCaptureSession",
    "build_coordinator_capture_extension",
    "ZhihuManualTaskService",
    "ZhihuPythonRecoveryExecution",
    "ZhihuPythonRecoveryService",
    "ZhihuSyncExecution",
    "SemanticFunnelExecution",
    "SemanticFunnelRepository",
    "SemanticFunnelService",
    "SemanticEmbeddingExecution",
    "SemanticEmbeddingService",
    "SemanticParquetWrite",
    "SemanticPacketExecution",
    "SemanticPacketService",
    "SemanticVectorRecord",
    "SentenceTransformerBackend",
    "RecordedEmbeddingBackend",
    "build_argument_units",
    "default_model_directory",
    "get_knowledge_source",
    "create_loopback_capture_server",
    "derive_author_participation_chains",
    "derive_keyword_filtered_author_participation_chains",
    "load_knowledge_sources",
    "load_semantic_funnel_config",
    "local_context_paragraph_ids",
    "loopback_installer_url",
    "loopback_status_url",
    "load_distillation_rules",
    "load_zhihu_endpoint_templates",
    "method_keyword_terms",
    "paragraphize_zhihu_content",
    "install_local_model",
    "verify_local_model",
    "serve_loopback_capture",
]
