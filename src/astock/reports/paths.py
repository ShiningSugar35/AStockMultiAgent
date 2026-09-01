"""Safe report-root discovery and canonical output-path construction."""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from astock.core.errors import FailureClass, PolicyError, StorageError
from astock.schemas.reports import ReportDirectoryPolicy

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_INVALID_RE = re.compile(r"[<>:\"/\\|?*]+")
_DEVICE_RE = re.compile(r"^(?:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)


def safe_report_file_name(value: str, *, max_length: int = 255) -> str:
    """Return a deterministic cross-platform file name without path semantics."""

    candidate = _CONTROL_RE.sub("", value or "")
    candidate = candidate.replace("..", "_")
    candidate = _INVALID_RE.sub("_", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" ._")
    if not candidate:
        candidate = "研究报告"
    if _DEVICE_RE.fullmatch(candidate):
        candidate = f"研究报告-{candidate.lower()}"
    if len(candidate) > max_length:
        candidate = candidate[:max_length].rstrip(" ._") or "研究报告"
    return candidate


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def known_folder_desktop() -> Path | None:
    """Resolve Desktop through Windows shell APIs without guessing a user name."""

    if sys.platform != "win32":
        return None
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        folder_id = _GUID(
            0xB4BFCC3A,
            0xDB2C,
            0x424C,
            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
        )
        raw = ctypes.c_wchar_p()
        result = shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(raw))
        if result == 0 and raw.value:
            resolved = Path(raw.value)
            ole32.CoTaskMemFree(ctypes.cast(raw, ctypes.c_void_p))
            return resolved
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buffer)
        if result == 0 and buffer.value:
            return Path(buffer.value)
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return None


@dataclass(frozen=True, slots=True)
class ResolvedReportRoot:
    root: Path
    policy: ReportDirectoryPolicy


class ReportPathResolver:
    def __init__(
        self,
        *,
        controlled_root: Path,
        configured_root: Path | None = None,
        known_folder_resolver: Callable[[], Path | None] = known_folder_desktop,
        allow_env_override: bool = True,
        env_name: str = "ASTOCK_REPORT_ROOT",
    ) -> None:
        self.controlled_root = controlled_root.resolve()
        self.configured_root = configured_root
        self.known_folder_resolver = known_folder_resolver
        self.allow_env_override = allow_env_override
        self.env_name = env_name

    def resolve(
        self,
        preferred: ReportDirectoryPolicy | None = None,
        *,
        env_override: str | None = None,
        custom_root: Path | None = None,
    ) -> ResolvedReportRoot:
        candidates: list[tuple[Path, ReportDirectoryPolicy]] = []
        env_value = env_override if env_override is not None else os.environ.get(self.env_name)
        if self.allow_env_override and env_value:
            candidates.append((Path(env_value).expanduser(), ReportDirectoryPolicy.ENV_OVERRIDE))
        if preferred is ReportDirectoryPolicy.CUSTOM:
            if custom_root is None:
                raise PolicyError(
                    "custom report directory was requested without a configured root",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
            candidates.append((custom_root.expanduser(), ReportDirectoryPolicy.CUSTOM))
        elif preferred is ReportDirectoryPolicy.CONTROLLED_DIRECTORY:
            candidates.append((self.controlled_root, ReportDirectoryPolicy.CONTROLLED_DIRECTORY))
        else:
            if preferred in {
                None,
                ReportDirectoryPolicy.DEFAULT,
                ReportDirectoryPolicy.KNOWN_FOLDER_DESKTOP,
            }:
                known = self.known_folder_resolver()
                if known is not None:
                    candidates.append((known, ReportDirectoryPolicy.KNOWN_FOLDER_DESKTOP))
            if self.configured_root is not None:
                candidates.append(
                    (
                        self.configured_root.expanduser(),
                        ReportDirectoryPolicy.CONFIGURED_REPORT_ROOT,
                    )
                )
        candidates.append((self.controlled_root, ReportDirectoryPolicy.CONTROLLED_DIRECTORY))
        seen: set[Path] = set()
        for candidate, policy in candidates:
            try:
                root = candidate.resolve()
            except OSError:
                continue
            if root in seen:
                continue
            seen.add(root)
            if self._probe_writable(root):
                return ResolvedReportRoot(root=root, policy=policy)
        raise StorageError(
            "no writable report destination is available",
            failure_class=FailureClass.STORAGE,
        )

    @staticmethod
    def _probe_writable(root: Path) -> bool:
        probe = root / f".astock-report-probe-{uuid4().hex}"
        try:
            root.mkdir(parents=True, exist_ok=True)
            with probe.open("xb") as handle:
                handle.write(b"ok")
                handle.flush()
                os.fsync(handle.fileno())
            probe.unlink()
            return True
        except OSError:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    @staticmethod
    def contained(root: Path, candidate: Path) -> Path:
        resolved_root = root.resolve()
        try:
            resolved_candidate = candidate.resolve()
            resolved_candidate.relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise PolicyError(
                "report path escapes the selected report root",
                failure_class=FailureClass.POLICY_REJECTED,
            ) from exc
        return resolved_candidate

    def final_path(self, resolved: ResolvedReportRoot, file_name: str) -> Path:
        if (
            not file_name
            or Path(file_name).is_absolute()
            or "/" in file_name
            or "\\" in file_name
            or ".." in file_name
            or _CONTROL_RE.search(file_name)
        ):
            raise PolicyError(
                "unsafe report file name",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        safe_name = safe_report_file_name(file_name)
        if safe_name != file_name:
            raise PolicyError(
                "report file name is not canonical",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        return self.contained(resolved.root, resolved.root / safe_name)


__all__ = [
    "ReportPathResolver",
    "ResolvedReportRoot",
    "known_folder_desktop",
    "safe_report_file_name",
]
