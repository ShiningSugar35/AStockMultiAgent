"""Private PDF/book ingestion and durable manifest services."""

from astock.books.docx_repository import PrivateDocxRepository
from astock.books.docx_service import PrivateDocxIngestService
from astock.books.repository import BookRepository
from astock.books.service import PrivatePdfIngestService

__all__ = [
    "BookRepository",
    "PrivateDocxIngestService",
    "PrivateDocxRepository",
    "PrivatePdfIngestService",
]
