"""Official and private document ingestion services."""

from astock.documents.block_repository import DocumentBlockRepository
from astock.documents.cninfo import CninfoDisclosureProvider
from astock.documents.official_web import OfficialWebDocumentCaptureService
from astock.documents.page_repository import DocumentPageRepository
from astock.documents.pdf_parser import PdfParseService
from astock.documents.provider_contracts import DisclosureEnumerationProvider
from astock.documents.repository import DocumentRepository
from astock.documents.service import DisclosureSyncService

__all__ = [
    "CninfoDisclosureProvider",
    "DocumentBlockRepository",
    "DisclosureEnumerationProvider",
    "DisclosureSyncService",
    "DocumentPageRepository",
    "DocumentRepository",
    "OfficialWebDocumentCaptureService",
    "PdfParseService",
]
