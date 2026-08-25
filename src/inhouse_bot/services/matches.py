"""내전 서비스. Discord 호출은 DB 작업 밖에서 한다."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from inhouse_bot.repositories.matches import (
    ACTIVE_STATUSES,
    ActiveMatchExistsError,
    AlreadyJoinedError,
    CANCELLED,
    FINISHED,
    InvalidCapacityError,
    InvalidMatchStateError,
    InvalidRecruitmentMinutesError,
    InvalidTimeoutError,
    InvalidWinnerTeamError,
    Match,
    MatchError,
    MatchEvent,
    MatchFullError,
    MatchNotFoundError,
    MatchRepository,
    MatchResult,
    MatchStats,
    NotParticipantError,
    Participant,
    PermissionDeniedError,
    ResultAlreadyRecordedError,
)


class MatchService:
    """내전 저장소를 호출하는 얇은 서비스."""

    def __init__(
        self,
        pool: Any,
        *,
        ready_timeout_seconds: int = 120,
        default_recruitment_minutes: int = 30,
        reminder_before_seconds: int = 300,
    ) -> None:
        self.repository = MatchRepository(pool)
        self.ready_timeout_seconds = int(ready_timeout_seconds)
        self.default_recruitment_minutes = int(default_recruitment_minutes)
        self.reminder_before_seconds = int(reminder_before_seconds)

    async def create_match(
        self,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        title: str,
        *,
        game_key: str = "lol",
        capacity: int = 10,
        recruitment_minutes: int | None = None,
        now: datetime | None = None,
    ) -> Match:
        return await self.repository.create_match(
            guild_id,
            channel_id,
            creator_id,
            title,
            game_key=game_key,
            capacity=capacity,
            recruitment_minutes=(
                self.default_recruitment_minutes
                if recruitment_minutes is None
                else recruitment_minutes
            ),
            now=now,
        )

    async def join_match(self, match_id: int, user_id: int, *, now: datetime | None = None) -> Match:
        return await self.repository.join_match(
            match_id,
            user_id,
            now=now,
            ready_timeout_seconds=self.ready_timeout_seconds,
            default_recruitment_minutes=self.default_recruitment_minutes,
        )

    async def leave_match(self, match_id: int, user_id: int, *, now: datetime | None = None) -> Match:
        return await self.repository.leave_match(
            match_id,
            user_id,
            now=now,
            ready_timeout_seconds=self.ready_timeout_seconds,
            default_recruitment_minutes=self.default_recruitment_minutes,
        )

    async def begin_ready_check(
        self,
        match_id: int,
        actor_id: int,
        *,
        manage_guild: bool = False,
        now: datetime | None = None,
    ) -> Match:
        return await self.repository.begin_ready_check(
            match_id,
            actor_id,
            manage_guild=manage_guild,
            now=now,
            ready_timeout_seconds=self.ready_timeout_seconds,
        )

    async def start_match(
        self,
        match_id: int,
        actor_id: int,
        *,
        manage_guild: bool = False,
        now: datetime | None = None,
    ) -> Match:
        return await self.begin_ready_check(
            match_id,
            actor_id,
            manage_guild=manage_guild,
            now=now,
        )

    async def toggle_ready(self, match_id: int, user_id: int, *, now: datetime | None = None) -> Match:
        return await self.repository.toggle_ready(match_id, user_id, now=now)

    async def kick_match_member(
        self,
        match_id: int,
        target_user_id: int,
        actor_id: int,
        *,
        manage_guild: bool = False,
        now: datetime | None = None,
    ) -> Match:
        return await self.repository.kick_match_member(
            match_id,
            target_user_id,
            actor_id,
            manage_guild=manage_guild,
            now=now,
            ready_timeout_seconds=self.ready_timeout_seconds,
            default_recruitment_minutes=self.default_recruitment_minutes,
        )

    async def cancel_match(
        self,
        match_id: int,
        actor_id: int,
        *,
        manage_guild: bool = False,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> Match:
        return await self.repository.cancel_match(
            match_id,
            actor_id,
            manage_guild=manage_guild,
            reason=reason,
            now=now,
        )

    async def finish_match(
        self,
        match_id: int,
        actor_id: int,
        winner_team: str,
        memo: str | None = None,
        *,
        manage_guild: bool = False,
        now: datetime | None = None,
    ) -> Match:
        return await self.repository.finish_match(
            match_id,
            actor_id,
            winner_team,
            memo,
            manage_guild=manage_guild,
            now=now,
        )

    async def stats(self, guild_id: int, user_id: int) -> MatchStats:
        return await self.repository.stats(guild_id, user_id)

    async def process_due_matches(self, now: datetime | None = None) -> list[MatchEvent]:
        return await self.repository.process_due_matches(
            now=now,
            ready_timeout_seconds=self.ready_timeout_seconds,
            default_recruitment_minutes=self.default_recruitment_minutes,
            reminder_before_seconds=self.reminder_before_seconds,
        )

    async def get_match(self, match_id: int) -> Match | None:
        return await self.repository.get_match(match_id)

    async def get_active_match(self, guild_id: int, channel_id: int) -> Match | None:
        return await self.repository.get_active_match(guild_id, channel_id)

    async def list_active(self, guild_id: int | None = None) -> list[Match]:
        return await self.repository.list_active(guild_id)

    async def update_message_id(self, match_id: int, message_id: int | None) -> Match:
        return await self.repository.update_message_id(match_id, message_id)

    async def cancel_missing_message(self, match_id: int) -> Match:
        return await self.repository.cancel_missing_message(match_id)



__all__ = [
    "ACTIVE_STATUSES",
    "ActiveMatchExistsError",
    "AlreadyJoinedError",
    "CANCELLED",
    "FINISHED",
    "InvalidCapacityError",
    "InvalidMatchStateError",
    "InvalidRecruitmentMinutesError",
    "InvalidTimeoutError",
    "InvalidWinnerTeamError",
    "Match",
    "MatchError",
    "MatchEvent",
    "MatchFullError",
    "MatchNotFoundError",
    "MatchRepository",
    "MatchResult",
    "MatchService",
    "MatchStats",
    "NotParticipantError",
    "Participant",
    "PermissionDeniedError",
    "ResultAlreadyRecordedError",
]
