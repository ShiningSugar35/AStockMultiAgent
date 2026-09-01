"""Formal report publishing subsystem."""

from astock.reports.manifest_repository import ReportManifestRepository
from astock.reports.paths import (
    ReportPathResolver,
    ResolvedReportRoot,
    known_folder_desktop,
    safe_report_file_name,
)
from astock.reports.policy import ReportPolicy, load_report_policy
from astock.reports.preferences import PresentationPreferencesRepository
from astock.reports.service import ReportPublishError, ReportService
from astock.reports.validation import (
    DocxValidationReport,
    validate_docx,
    validate_pdf,
    visual_qa_summary,
)

__all__ = [
    "DocxValidationReport",
    "PresentationPreferencesRepository",
    "ReportManifestRepository",
    "ReportPathResolver",
    "ReportPolicy",
    "ReportPublishError",
    "ReportService",
    "ResolvedReportRoot",
    "known_folder_desktop",
    "load_report_policy",
    "safe_report_file_name",
    "validate_docx",
    "validate_pdf",
    "visual_qa_summary",
]
