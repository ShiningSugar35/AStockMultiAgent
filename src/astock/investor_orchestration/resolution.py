from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astock.investor_orchestration.models import DatePrecision, ResolvedDate


class RelativeDateResolver:
    _today_tokens = {"today", "今天", "今日"}
    _yesterday_tokens = {"yesterday", "昨天", "昨日"}

    def resolve(
        self,
        text: str,
        *,
        question_time: datetime,
        user_timezone: str,
        market_timezone: str = "Asia/Shanghai",
    ) -> ResolvedDate:
        if question_time.tzinfo is None:
            raise ValueError("question_time must be timezone-aware")
        user_now = question_time.astimezone(ZoneInfo(user_timezone))
        normalized = text.strip().lower()
        if normalized in self._today_tokens:
            civil_date = user_now.date()
        elif normalized in self._yesterday_tokens:
            civil_date = user_now.date() - timedelta(days=1)
        else:
            try:
                exact = datetime.fromisoformat(text)
            except ValueError:
                exact = None
            if exact is not None and exact.tzinfo is not None:
                return ResolvedDate(
                    original_text=text,
                    user_timezone=user_timezone,
                    market_timezone=market_timezone,
                    civil_date=exact.astimezone(ZoneInfo(user_timezone)).date(),
                    exact_timestamp=exact,
                    precision=DatePrecision.EXACT,
                )
            try:
                civil_date = datetime.fromisoformat(text).date()
            except ValueError:
                return ResolvedDate(
                    original_text=text,
                    user_timezone=user_timezone,
                    market_timezone=market_timezone,
                    precision=DatePrecision.UNKNOWN,
                )
        return ResolvedDate(
            original_text=text,
            user_timezone=user_timezone,
            market_timezone=market_timezone,
            civil_date=civil_date,
            precision=DatePrecision.DATE_ONLY,
        )


class AccountResolver:
    @staticmethod
    def resolve(
        *,
        requested_account_id: str | None,
        active_account_ids: tuple[str, ...],
        default_account_id: str | None,
    ) -> str:
        active = tuple(dict.fromkeys(active_account_ids))
        if requested_account_id is not None:
            if requested_account_id not in active:
                raise ValueError("requested account is not active")
            return requested_account_id
        if default_account_id is not None:
            if default_account_id not in active:
                raise ValueError("configured default account is not active")
            return default_account_id
        if len(active) == 1:
            return active[0]
        if not active:
            raise ValueError("no active account is available")
        raise ValueError("multiple active accounts exist and no default is configured")
