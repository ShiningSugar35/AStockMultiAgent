"""Strict Zhihu API response adapter with no silent empty/failure coercion."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.knowledge.transport import PersistedZhihuResponse, classify_response_failure
from astock.schemas import (
    KnowledgeIdentityStatus,
    KnowledgeSourceDefinition,
    ZhihuAuthorIdentity,
    ZhihuCommentNode,
    ZhihuCommentPage,
    ZhihuContentRecord,
    ZhihuContentType,
    ZhihuListingPage,
)


class ZhihuResponseAdapter:
    STRUCTURE_VERSION = "zhihu-api-v4-listing-v1"
    COMMENT_STRUCTURE_VERSION = "zhihu-comment-v5-v1"

    def __init__(self, object_store: ObjectStore) -> None:
        self.object_store = object_store

    def parse_identity(
        self,
        source: KnowledgeSourceDefinition,
        response: PersistedZhihuResponse,
    ) -> ZhihuAuthorIdentity:
        self._assert_success(response)
        payload = _json_mapping(response)
        platform_user_id = _required_text(payload, "id")
        url_token = _required_text(payload, "url_token")
        display_name = _required_text(payload, "name")
        if source.url_token and url_token != source.url_token:
            raise ProviderError(
                "Zhihu profile token does not match the allowlisted identity",
                failure_class=FailureClass.CONFLICT,
                details={"snapshot_id": response.snapshot.snapshot_id},
            )
        return ZhihuAuthorIdentity(
            author_source_id=source.source_id,
            platform_user_id=platform_user_id,
            url_token=url_token,
            display_name=display_name,
            profile_url=f"https://www.zhihu.com/people/{url_token}",
            identity_status=KnowledgeIdentityStatus.CONFIRMED,
            profile_snapshot_id=response.snapshot.snapshot_id,
            profile_object_sha256=response.snapshot.object_sha256,
            verified_at=response.snapshot.fetched_at,
        )

    def parse_listing(
        self,
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
        *,
        listing_page: int,
        request_cursor: str | None,
        response: PersistedZhihuResponse,
    ) -> tuple[ZhihuListingPage, list[ZhihuContentRecord]]:
        self._assert_success(response)
        payload = _json_mapping(response)
        raw_data = payload.get("data")
        paging = payload.get("paging")
        if not isinstance(raw_data, list) or not isinstance(paging, dict):
            raise self._invalid(response, "Zhihu listing lacks data or paging")
        is_end = paging.get("is_end")
        next_cursor = paging.get("next")
        if not isinstance(is_end, bool):
            raise self._invalid(response, "Zhihu paging is_end is not boolean")
        if not is_end and not isinstance(next_cursor, str):
            raise self._invalid(response, "Zhihu non-terminal page lacks a next cursor")
        if not raw_data and not is_end:
            raise self._invalid(response, "Zhihu returned an unexpected empty non-terminal page")
        records = [
            self._parse_content(source, content_type, item, response)
            for item in raw_data
            if isinstance(item, dict)
        ]
        if len(records) != len(raw_data):
            raise self._invalid(response, "Zhihu listing contains a non-object content item")
        content_ids = [record.content_id for record in records]
        if len(content_ids) != len(set(content_ids)):
            raise self._invalid(response, "Zhihu listing repeats a content id on one page")
        page_identity = {
            "source_id": source.source_id,
            "content_type": content_type.value,
            "listing_page": listing_page,
            "request_cursor": request_cursor,
            "raw_object_sha256": response.snapshot.object_sha256,
        }
        page = ZhihuListingPage(
            page_id=f"zhihu-listing:{content_hash(page_identity)}",
            author_source_id=source.source_id,
            content_type=content_type,
            listing_page=listing_page,
            request_url=response.requested_url,
            request_cursor=request_cursor,
            next_cursor=str(next_cursor) if isinstance(next_cursor, str) else None,
            is_end=is_end,
            content_ids=content_ids,
            source_snapshot_id=response.snapshot.snapshot_id,
            raw_object_sha256=response.snapshot.object_sha256,
            transport=response.transport,
            http_status=response.status_code,
            response_structure_version=self.STRUCTURE_VERSION,
            fetched_at=response.snapshot.fetched_at,
        )
        return page, records

    def parse_comment_page(
        self,
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
        content_id: str,
        *,
        parent_comment_id: str | None,
        comment_page: int,
        request_cursor: str | None,
        response: PersistedZhihuResponse,
    ) -> tuple[ZhihuCommentPage, list[ZhihuCommentNode]]:
        self._assert_success(response)
        payload = _json_mapping(response)
        raw_data = payload.get("data")
        paging = payload.get("paging")
        if not isinstance(raw_data, list) or not isinstance(paging, dict):
            raise self._invalid(response, "Zhihu comment page lacks data or paging")
        is_end = paging.get("is_end")
        next_cursor = paging.get("next")
        if not isinstance(is_end, bool):
            raise self._invalid(response, "Zhihu comment paging is_end is not boolean")
        if not is_end and not isinstance(next_cursor, str):
            raise self._invalid(response, "Zhihu comment page lacks its next cursor")
        if not raw_data and not is_end:
            raise self._invalid(
                response, "Zhihu returned an empty non-terminal comment page"
            )
        nodes: list[ZhihuCommentNode] = []
        for item in raw_data:
            if not isinstance(item, dict):
                raise self._invalid(response, "Zhihu comment item is not an object")
            root = self._parse_comment(
                source,
                content_type,
                content_id,
                item,
                response,
                fallback_parent_comment_id=parent_comment_id,
                fallback_root_comment_id=parent_comment_id,
            )
            nodes.append(root)
            preview = item.get("child_comments")
            if preview is None:
                preview = []
            if not isinstance(preview, list):
                raise self._invalid(response, "Zhihu child comment preview is not a list")
            for child in preview:
                if not isinstance(child, dict):
                    raise self._invalid(
                        response, "Zhihu child comment preview item is invalid"
                    )
                nodes.append(
                    self._parse_comment(
                        source,
                        content_type,
                        content_id,
                        child,
                        response,
                        fallback_parent_comment_id=root.comment_id,
                        fallback_root_comment_id=root.root_comment_id,
                    )
                )
        comment_ids = [node.comment_id for node in nodes]
        if len(comment_ids) != len(set(comment_ids)):
            raise self._invalid(response, "Zhihu comment page repeats a comment id")
        page_identity = {
            "source_id": source.source_id,
            "content_type": content_type.value,
            "content_id": content_id,
            "parent_comment_id": parent_comment_id,
            "comment_page": comment_page,
            "request_cursor": request_cursor,
            "raw_object_sha256": response.snapshot.object_sha256,
        }
        page = ZhihuCommentPage(
            page_id=f"zhihu-comment-page:{content_hash(page_identity)}",
            author_source_id=source.source_id,
            content_type=content_type,
            content_id=content_id,
            parent_comment_id=parent_comment_id,
            comment_page=comment_page,
            request_url=response.requested_url,
            request_cursor=request_cursor,
            next_cursor=str(next_cursor) if isinstance(next_cursor, str) else None,
            is_end=is_end,
            comment_ids=comment_ids,
            source_snapshot_id=response.snapshot.snapshot_id,
            raw_object_sha256=response.snapshot.object_sha256,
            transport=response.transport,
            http_status=response.status_code,
            response_structure_version=self.COMMENT_STRUCTURE_VERSION,
            fetched_at=response.snapshot.fetched_at,
        )
        return page, nodes

    def _parse_comment(
        self,
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
        content_id: str,
        item: dict[str, Any],
        response: PersistedZhihuResponse,
        *,
        fallback_parent_comment_id: str | None,
        fallback_root_comment_id: str | None,
    ) -> ZhihuCommentNode:
        comment_id = _required_identifier(item, "id")
        raw_author = item.get("author") or item.get("member")
        author = raw_author if isinstance(raw_author, dict) else {}
        platform_author_id = _optional_identifier(author.get("id"))
        author_url_token = _optional_text(author.get("url_token"))
        author_display_name = _optional_text(author.get("name"))
        body = item.get("content")
        if not isinstance(body, str):
            raise self._invalid(response, "Zhihu comment lacks string content")
        body_object = self.object_store.put_bytes(body.encode("utf-8"))
        raw_parent = _optional_identifier(item.get("parent_comment_id"))
        parent_comment_id = (
            None if raw_parent in {None, "0"} else raw_parent
        ) or fallback_parent_comment_id
        raw_root = _optional_identifier(item.get("root_comment_id"))
        if parent_comment_id is None:
            root_comment_id = comment_id
        else:
            root_comment_id = raw_root or fallback_root_comment_id or parent_comment_id
        reply_to_comment_id = _optional_identifier(
            item.get("reply_to_comment_id") or item.get("reply_comment_id")
        )
        published_at = _optional_timestamp(item.get("created_time") or item.get("created"))
        updated_at = _optional_timestamp(item.get("updated_time") or item.get("updated"))
        is_target_author = bool(
            (source.platform_user_id and platform_author_id == source.platform_user_id)
            or (source.url_token and author_url_token == source.url_token)
        )
        like_count = _nonnegative_int(item.get("like_count"))
        child_comment_count = _nonnegative_int(item.get("child_comment_count"))
        metadata = {
            "author_source_id": source.source_id,
            "content_type": content_type.value,
            "content_id": content_id,
            "comment_id": comment_id,
            "platform_author_id": platform_author_id,
            "author_url_token": author_url_token,
            "parent_comment_id": parent_comment_id,
            "reply_to_comment_id": reply_to_comment_id,
            "root_comment_id": root_comment_id,
            "published_at": published_at.isoformat() if published_at else None,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "like_count": like_count,
            "child_comment_count": child_comment_count,
            "is_target_author": is_target_author,
        }
        metadata_sha256 = content_hash(metadata)
        return ZhihuCommentNode(
            version_id=f"zhihu-comment:{content_hash({**metadata, 'body': body_object.sha256})}",
            author_source_id=source.source_id,
            content_type=content_type,
            content_id=content_id,
            comment_id=comment_id,
            platform_author_id=platform_author_id,
            author_url_token=author_url_token,
            author_display_name=author_display_name,
            parent_comment_id=parent_comment_id,
            reply_to_comment_id=reply_to_comment_id,
            root_comment_id=root_comment_id,
            published_at=published_at,
            updated_at=updated_at,
            collected_at=response.snapshot.fetched_at,
            like_count=like_count,
            child_comment_count=child_comment_count,
            is_target_author=is_target_author,
            body_object_sha256=body_object.sha256,
            metadata_sha256=metadata_sha256,
            raw_source_snapshot_id=response.snapshot.snapshot_id,
        )

    def _parse_content(
        self,
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
        item: dict[str, Any],
        response: PersistedZhihuResponse,
    ) -> ZhihuContentRecord:
        content_id = _required_identifier(item, "id")
        raw_author = item.get("author")
        author = raw_author if isinstance(raw_author, dict) else {}
        platform_author_id = _optional_identifier(author.get("id"))
        body = item.get("content")
        if isinstance(body, str):
            body_bytes = body.encode("utf-8")
        elif isinstance(body, (dict, list)):
            body_bytes = canonical_json_bytes(body)
        else:
            raise self._invalid(response, "Zhihu content item lacks a supported body")
        body_object = self.object_store.put_bytes(body_bytes)
        question = item.get("question") if isinstance(item.get("question"), dict) else None
        question_id = (
            _optional_identifier(question.get("id")) if question is not None else None
        )
        question_title = (
            _optional_text(question.get("title")) if question is not None else None
        )
        if content_type is ZhihuContentType.ANSWERS and (
            question_id is None or question_title is None
        ):
            raise self._invalid(response, "Zhihu answer lacks required question context")
        published_at = _optional_timestamp(item.get("created_time") or item.get("created"))
        updated_at = _optional_timestamp(item.get("updated_time") or item.get("updated"))
        title = _optional_text(item.get("title"))
        canonical_url = _canonical_url(content_type, content_id, question_id)
        metadata = {
            "author_source_id": source.source_id,
            "platform_author_id": platform_author_id,
            "content_id": content_id,
            "content_type": content_type.value,
            "canonical_url": canonical_url,
            "title": title,
            "question_id": question_id,
            "question_title": question_title,
            "published_at": published_at.isoformat() if published_at else None,
            "updated_at": updated_at.isoformat() if updated_at else None,
        }
        metadata_sha256 = content_hash(metadata)
        version_id = f"zhihu-content:{content_hash({**metadata, 'body': body_object.sha256})}"
        return ZhihuContentRecord(
            version_id=version_id,
            author_source_id=source.source_id,
            platform_author_id=platform_author_id,
            content_id=content_id,
            content_type=content_type,
            canonical_url=canonical_url,
            title=title,
            question_id=question_id,
            question_title=question_title,
            published_at=published_at,
            updated_at=updated_at,
            collected_at=response.snapshot.fetched_at,
            body_object_sha256=body_object.sha256,
            metadata_sha256=metadata_sha256,
            raw_source_snapshot_id=response.snapshot.snapshot_id,
        )

    @staticmethod
    def _assert_success(response: PersistedZhihuResponse) -> None:
        failure = classify_response_failure(response)
        if failure is not None:
            raise ProviderError(
                "Zhihu response is not usable for structured collection",
                failure_class=failure,
                retryable=failure in {FailureClass.NETWORK, FailureClass.TIMEOUT},
                details={
                    "status_code": response.status_code,
                    "snapshot_id": response.snapshot.snapshot_id,
                },
            )

    @staticmethod
    def _invalid(response: PersistedZhihuResponse, message: str) -> ProviderError:
        return ProviderError(
            message,
            failure_class=FailureClass.INVALID_RESPONSE,
            retryable=False,
            details={"snapshot_id": response.snapshot.snapshot_id},
        )


def _json_mapping(response: PersistedZhihuResponse) -> dict[str, Any]:
    try:
        value = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(
            "Zhihu returned invalid JSON",
            failure_class=FailureClass.INVALID_RESPONSE,
            details={"snapshot_id": response.snapshot.snapshot_id},
        ) from exc
    if not isinstance(value, dict):
        raise ProviderError(
            "Zhihu JSON root must be an object",
            failure_class=FailureClass.INVALID_RESPONSE,
            details={"snapshot_id": response.snapshot.snapshot_id},
        )
    return value


def _required_text(value: dict[str, Any], key: str) -> str:
    result = _optional_text(value.get(key))
    if result is None:
        raise ProviderError(
            f"Zhihu profile is missing {key}",
            failure_class=FailureClass.INVALID_RESPONSE,
        )
    return result


def _required_identifier(value: dict[str, Any], key: str) -> str:
    result = _optional_identifier(value.get(key))
    if result is None:
        raise ProviderError(
            f"Zhihu content is missing {key}",
            failure_class=FailureClass.INVALID_RESPONSE,
        )
    return result


def _optional_identifier(value: Any) -> str | None:
    if isinstance(value, (str, int)) and str(value):
        return str(value)
    return None


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _canonical_url(
    content_type: ZhihuContentType,
    content_id: str,
    question_id: str | None,
) -> str:
    if content_type is ZhihuContentType.ANSWERS:
        if question_id is None:  # pragma: no cover - caller validates answer context
            raise ValueError("answer canonical URL requires question_id")
        return f"https://www.zhihu.com/question/{question_id}/answer/{content_id}"
    if content_type is ZhihuContentType.ARTICLES:
        return f"https://zhuanlan.zhihu.com/p/{content_id}"
    return f"https://www.zhihu.com/pin/{content_id}"


__all__ = ["ZhihuResponseAdapter"]
