"""Transactional notification outbox; research completion is not delivery.

All attempts carry the same receiver idempotency key. An external receiver must
honour that key to make retry after an ambiguous network result duplicate-safe.
This module never creates a task, performs research, or writes economic facts.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Protocol

from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import canonical_json, utc_now


class DeliverySink(Protocol):
    def publish(
        self,
        *,
        notification_key: str,
        title: str,
        body: str,
        metadata: Mapping[str, Any],
    ) -> str: ...


class DurableNotificationOutbox:
    """Persist messages atomically with the enclosing store transaction."""

    def __init__(self, store: InvestorOrchestrationStore) -> None:
        self.store = store

    def publish(
        self,
        *,
        notification_key: str,
        title: str,
        body: str,
        metadata: Mapping[str, Any],
    ) -> str:
        if not notification_key.strip() or not title.strip() or not body.strip():
            raise ValueError("notification identity and content must be non-empty")
        notification_id = f"notification-{uuid.uuid5(uuid.NAMESPACE_URL, notification_key)}"
        metadata_json = canonical_json(dict(metadata))
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT notification_id,title,body,metadata_json FROM scheduled_notifications "
                "WHERE notification_key=?",
                (notification_key,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (notification_id, title, body, metadata_json):
                    raise ValueError("notification key is already bound to different content")
            else:
                connection.execute(
                    "INSERT INTO scheduled_notifications "
                    "(notification_id,notification_key,title,body,metadata_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        notification_id,
                        notification_key,
                        title,
                        body,
                        metadata_json,
                        utc_now().isoformat(),
                    ),
                )
            connection.execute(
                "INSERT OR IGNORE INTO scheduled_notification_delivery(notification_id) VALUES(?)",
                (notification_id,),
            )
        return notification_id

    def get(self, notification_key: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT n.*,d.status,d.attempts,d.delivery_reference,d.sent_at "
                "FROM scheduled_notifications n JOIN scheduled_notification_delivery d "
                "ON d.notification_id=n.notification_id WHERE n.notification_key=?",
                (notification_key,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(str(result.pop("metadata_json")))
        return result

    def list_pending(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("pending notification limit must be within [1, 1000]")
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT n.*,d.status,d.attempts FROM scheduled_notifications n "
                "JOIN scheduled_notification_delivery d ON d.notification_id=n.notification_id "
                "WHERE d.status<>'SENT' ORDER BY n.created_at,n.notification_id LIMIT ?",
                (limit,),
            ).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result["metadata"] = json.loads(str(result.pop("metadata_json")))
            results.append(result)
        return tuple(results)

    def deliver(
        self,
        notification_key: str,
        sink: DeliverySink,
        *,
        lease_seconds: int = 120,
    ) -> str | None:
        if isinstance(sink, DurableNotificationOutbox):
            # Queuing into a local table is not evidence of external delivery.
            return None
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("delivery lease must be within [1, 3600] seconds")
        owner_id = str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT n.*,d.status,d.claim_expires_at,d.delivery_reference "
                "FROM scheduled_notifications n JOIN scheduled_notification_delivery d "
                "ON d.notification_id=n.notification_id WHERE n.notification_key=?",
                (notification_key,),
            ).fetchone()
            if row is None:
                raise ValueError("notification was not atomically persisted")
            if row["status"] == "SENT":
                return str(row["delivery_reference"])
            claimed = connection.execute(
                "UPDATE scheduled_notification_delivery SET status='SENDING',attempts=attempts+1,"
                "owner_id=?,claim_expires_at=?,last_error_class=NULL WHERE notification_id=? "
                "AND (status='PENDING' OR (status='SENDING' "
                "AND julianday(claim_expires_at)<=julianday(?)))",
                (
                    owner_id,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    row["notification_id"],
                    now.isoformat(),
                ),
            ).rowcount
            if not claimed:
                return None
            message = dict(row)
        try:
            reference = sink.publish(
                notification_key=notification_key,
                title=str(message["title"]),
                body=str(message["body"]),
                metadata=json.loads(str(message["metadata_json"])),
            )
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError("notification receiver returned no delivery reference")
        except Exception as exc:
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE scheduled_notification_delivery SET status='PENDING',owner_id=NULL,"
                    "claim_expires_at=NULL,last_error_class=? "
                    "WHERE notification_id=? AND owner_id=?",
                    (type(exc).__name__, message["notification_id"], owner_id),
                )
            raise
        with self.store.transaction() as connection:
            changed = connection.execute(
                "UPDATE scheduled_notification_delivery SET status='SENT',owner_id=NULL,"
                "claim_expires_at=NULL,delivery_reference=?,sent_at=? "
                "WHERE notification_id=? AND owner_id=? AND status='SENDING'",
                (reference, utc_now().isoformat(), message["notification_id"], owner_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("delivery ownership changed before acknowledgment")
        return reference
