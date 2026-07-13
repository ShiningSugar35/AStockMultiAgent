"""Private PDF/book ingestion and durable manifest services."""

from astock.books.repository import BookRepository
from astock.books.service import PrivatePdfIngestService

__all__ = ["BookRepository", "PrivatePdfIngestService"]
