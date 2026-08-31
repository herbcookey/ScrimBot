"""내전 서비스. Discord 호출은 DB 작업 밖에서 한다."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from inhouse_bot.repositories.matches import (
    ACTIVE_STATUSES,
    ActiveMatchExistsError,
    ActiveMembershipError,
    AlreadyJoinedError,
    BotAdminAlreadyExistsError,
    BotAdminNotFoundError,
    BotOwnerProtectedError,
    CANCELLED,
    DEFAULT_DRAFT_TIMEOUT_SECONDS,
    DEFAULT_REMINDER_RETRY_SECONDS,
    DRAFTING,
    FINISHED,
    Game,
    GameNotFoundError,
    InvalidAssignmentModeError,
    InvalidCapacityError,
    InvalidDraftPickError,
    InvalidMatchStateError,
    InvalidRecruitmentMinutesError,
    InvalidTimeoutError,
    InvalidWinnerTeamError,
    InvalidRolePreferencesError,
    Match,
    MatchError,
    MatchEvent,
    MatchFullError,
    MatchNotFoundError,
    MatchRepository,
    SEASON_NAME_MAX_LENGTH,
    MatchResult,
    MatchStats,
    RoleAssignmentImpossibleError,
    RankingEntry,
    RoleStats,
    NotParticipantError,
    Participant,
    PermissionDeniedError,
    ResultAlreadyRecordedError,
    RoleRatingAlreadyExistsError,
    Season,
    SeasonNotFoundError,
    UnplacedRoleError,
    InvalidRankingLimitError,
    InvalidSeasonStateError,
    calculate_rating_delta,
)


# These values mirror the Discord fields produced by ``render_match``.  The
# service validates them before calling the repository so callers other than
# slash-command interactions cannot persist text Discord would reject.
MATCH_TITLE_MAX_LENGTH = 4096
RESULT_MEMO_MAX_LENGTH = 1024


def _validate_embed_text(
    value: str | None,
    *,
    field: str,
    max_length: int,
    allow_empty: bool = True,
) -> str | None:
    if value is None:
        if not allow_empty:
            raise ValueError(f"{field}을 입력해 주세요.")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field}은 문자열이어야 합니다.")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field}을 입력해 주세요.")
    if len(value) > max_length:
        raise ValueError(f"{field}은 {max_length}자 이내로 입력해 주세요.")
    return value


class MatchService:
    """내전 저장소를 호출하는 얇은 서비스."""

    def __init__(
        self,
        pool: Any,
        *,
        bot_owner_id: int,
        ready_timeout_seconds: int = 120,
        draft_timeout_seconds: int = DEFAULT_DRAFT_TIMEOUT_SECONDS,
        default_recruitment_minutes: int = 30,
        reminder_before_seconds: int = 300,
        reminder_retry_seconds: int = DEFAULT_REMINDER_RETRY_SECONDS,
        voice_cleanup_delay_seconds: int = 600,
    ) -> None:
        self.repository = MatchRepository(pool)
        self.bot_owner_id = int(bot_owner_id)
        if self.bot_owner_id <= 0:
            raise ValueError("bot_owner_id는 양의 정수여야 합니다")
        self.ready_timeout_seconds = int(ready_timeout_seconds)
        self.draft_timeout_seconds = int(draft_timeout_seconds)
        self.default_recruitment_minutes = int(default_recruitment_minutes)
        self.reminder_before_seconds = int(reminder_before_seconds)
        self.reminder_retry_seconds = int(reminder_retry_seconds)
        self._voice_retry_match_ids: set[int] = set()
        self.voice_cleanup_delay_seconds = int(voice_cleanup_delay_seconds)

    def retry_voice_for(self, match_id: int) -> None:
        self._voice_retry_match_ids.add(int(match_id))

    def clear_voice_retry(self, match_id: int) -> None:
        self._voice_retry_match_ids.discard(int(match_id))

    def voice_retry_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._voice_retry_match_ids))

    async def is_bot_owner(self, user_id: int) -> bool:
        return int(user_id) == self.bot_owner_id

    async def is_bot_admin(self, guild_id: int, user_id: int) -> bool:
        if await self.is_bot_owner(user_id):
            return True
        return await self.repository.is_bot_admin(guild_id, user_id)

    async def add_bot_admin(self, guild_id: int, actor_id: int, target_id: int) -> None:
        if not await self.is_bot_owner(actor_id):
            raise PermissionDeniedError("봇 최고 관리자만 다른 봇 관리자를 추가할 수 있습니다.")
        if await self.is_bot_owner(target_id):
            raise BotOwnerProtectedError()
        await self.repository.add_bot_admin(guild_id, actor_id, target_id)

    async def remove_bot_admin(self, guild_id: int, actor_id: int, target_id: int) -> None:
        if not await self.is_bot_owner(actor_id):
            raise PermissionDeniedError("봇 최고 관리자만 다른 봇 관리자를 삭제할 수 있습니다.")
        if await self.is_bot_owner(target_id):
            raise BotOwnerProtectedError()
        await self.repository.remove_bot_admin(guild_id, target_id)

    async def list_bot_admins(self, guild_id: int) -> list[int]:
        return await self.repository.list_bot_admins(guild_id)

    async def create_match(
        self,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        title: str,
        *,
        game_key: str = "lol",
        assignment_mode: str = "BALANCED",
        preferred_role_1: str | None = None,
        preferred_role_2: str | None = None,
        preferred_role_3: str | None = None,
        voice_category_id: int | None = None,
        capacity: int | None = None,
        recruitment_minutes: int | None = None,
        now: datetime | None = None,
    ) -> Match:
        title = _validate_embed_text(
            title,
            field="제목",
            max_length=MATCH_TITLE_MAX_LENGTH,
            allow_empty=False,
        )
        return await self.repository.create_match(
            guild_id,
            channel_id,
            creator_id,
            title,
            game_key=game_key,
            assignment_mode=assignment_mode,
            preferred_role_1=preferred_role_1,
            preferred_role_2=preferred_role_2,
            preferred_role_3=preferred_role_3,
            voice_category_id=voice_category_id,
            capacity=capacity,
            recruitment_minutes=(
                self.default_recruitment_minutes
                if recruitment_minutes is None
                else recruitment_minutes
            ),
            now=now,
        )

    async def join_match(
        self,
        match_id: int,
        user_id: int,
        *,
        preferred_role_1: str | None = None,
        preferred_role_2: str | None = None,
        preferred_role_3: str | None = None,
        now: datetime | None = None,
    ) -> Match:
        return await self.repository.join_match(
            match_id,
            user_id,
            preferred_role_1=preferred_role_1,
            preferred_role_2=preferred_role_2,
            preferred_role_3=preferred_role_3,
            now=now,
            ready_timeout_seconds=self.ready_timeout_seconds,
            default_recruitment_minutes=self.default_recruitment_minutes,
        )

    async def update_preferences(
        self,
        match_id: int,
        user_id: int,
        preferred_role_1: str,
        preferred_role_2: str,
        preferred_role_3: str | None = None,
        *,
        now: datetime | None = None,
    ) -> Match:
        return await self.repository.update_preferences(
            match_id, user_id, preferred_role_1, preferred_role_2, preferred_role_3,
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
        manager_override: bool = False,
        now: datetime | None = None,
    ) -> Match:
        return await self.repository.begin_ready_check(
            match_id,
            actor_id,
            manager_override=manager_override,
            now=now,
            ready_timeout_seconds=self.ready_timeout_seconds,
        )

    async def start_match(
        self,
        match_id: int,
        actor_id: int,
        *,
        manager_override: bool = False,
        now: datetime | None = None,
    ) -> Match:
        return await self.begin_ready_check(
            match_id,
            actor_id,
            manager_override=manager_override,
            now=now,
        )

    async def toggle_ready(self, match_id: int, user_id: int, *, now: datetime | None = None) -> Match:
        return await self.repository.toggle_ready(
            match_id, user_id, now=now, draft_timeout_seconds=self.draft_timeout_seconds
        )

    async def draft_pick(
        self, match_id: int, actor_id: int, target_user_id: int, *, now: datetime | None = None
    ) -> Match:
        return await self.repository.draft_pick(
            match_id, actor_id, target_user_id, now=now,
            draft_timeout_seconds=self.draft_timeout_seconds,
        )

    async def kick_match_member(
        self,
        match_id: int,
        target_user_id: int,
        actor_id: int,
        *,
        manager_override: bool = False,
        now: datetime | None = None,
    ) -> Match:
        return await self.repository.kick_match_member(
            match_id,
            target_user_id,
            actor_id,
            manager_override=manager_override,
            now=now,
            ready_timeout_seconds=self.ready_timeout_seconds,
            default_recruitment_minutes=self.default_recruitment_minutes,
        )

    async def cancel_match(
        self,
        match_id: int,
        actor_id: int,
        *,
        manager_override: bool = False,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> Match:
        return await self.repository.cancel_match(
            match_id,
            actor_id,
            manager_override=manager_override,
            reason=reason,
            now=now,
            voice_cleanup_delay_seconds=self.voice_cleanup_delay_seconds,
        )

    async def finish_match(
        self,
        match_id: int,
        actor_id: int,
        winner_team: str,
        memo: str | None = None,
        *,
        manager_override: bool = False,
        now: datetime | None = None,
    ) -> Match:
        memo = _validate_embed_text(
            memo,
            field="결과 메모",
            max_length=RESULT_MEMO_MAX_LENGTH,
        )
        return await self.repository.finish_match(
            match_id,
            actor_id,
            winner_team,
            memo,
            manager_override=manager_override,
            now=now,
            voice_cleanup_delay_seconds=self.voice_cleanup_delay_seconds,
        )

    async def stats(
        self,
        guild_id: int,
        user_id: int,
        *,
        game_key: str = "lol",
        season_id: int | None = None,
    ) -> MatchStats:
        return await self.repository.stats(
            guild_id, user_id, game_key=game_key, season_id=season_id
        )

    async def list_games(self) -> list[Game]:
        return await self.repository.list_games()

    async def set_role_rating(
        self,
        guild_id: int,
        user_id: int,
        role: str,
        *,
        game_key: str = "lol",
        tier: str | None = None,
        detail: str | int | None = None,
        rating: int | None = None,
        manager_override: bool = False,
        now: datetime | None = None,
    ) -> int:
        return await self.repository.set_role_rating(
            guild_id, user_id, role, game_key=game_key, tier=tier,
            detail=detail, rating=rating, manager_override=manager_override, now=now,
        )

    async def register_role_rating(
        self,
        guild_id: int,
        user_id: int,
        role: str,
        tier: str,
        *,
        game_key: str = "lol",
        now: datetime | None = None,
    ) -> int:
        return await self.repository.register_role_rating(
            guild_id, user_id, role, tier, game_key=game_key, now=now
        )

    async def list_seasons(self, guild_id: int, game_key: str = "lol") -> list[Season]:
        return await self.repository.list_seasons(guild_id, game_key)

    async def start_season(
        self,
        guild_id: int,
        name: str,
        *,
        game_key: str = "lol",
        manager_override: bool = False,
        now: datetime | None = None,
    ) -> Season:
        if not manager_override:
            raise PermissionDeniedError("봇 관리자 권한이 필요합니다.")
        name = name.strip()
        if not name:
            raise InvalidSeasonStateError("시즌 이름을 입력해야 합니다.")
        if len(name) > SEASON_NAME_MAX_LENGTH:
            raise InvalidSeasonStateError(
                f"시즌 이름은 {SEASON_NAME_MAX_LENGTH}자 이내로 입력해 주세요."
            )
        return await self.repository.start_season(
            guild_id, name, game_key=game_key, manager_override=manager_override, now=now
        )

    async def end_season(
        self,
        guild_id: int,
        *,
        game_key: str = "lol",
        manager_override: bool = False,
        now: datetime | None = None,
    ) -> Season:
        return await self.repository.end_season(
            guild_id, game_key=game_key, manager_override=manager_override, now=now
        )

    async def ranking(
        self,
        guild_id: int,
        *,
        game_key: str = "lol",
        season_id: int | None = None,
        role: str | None = None,
        limit: int = 10,
    ) -> list[RankingEntry]:
        return await self.repository.ranking(
            guild_id, game_key=game_key, season_id=season_id, role=role, limit=limit
        )

    async def process_due_matches(self, now: datetime | None = None) -> list[MatchEvent]:
        return await self.repository.process_due_matches(
            now=now,
            ready_timeout_seconds=self.ready_timeout_seconds,
            default_recruitment_minutes=self.default_recruitment_minutes,
            reminder_before_seconds=self.reminder_before_seconds,
            reminder_retry_seconds=self.reminder_retry_seconds,
        )

    async def get_match(self, match_id: int) -> Match | None:
        return await self.repository.get_match(match_id)

    async def get_active_match(self, guild_id: int, channel_id: int) -> Match | None:
        return await self.repository.get_active_match(guild_id, channel_id)

    async def list_active(self, guild_id: int | None = None) -> list[Match]:
        return await self.repository.list_active(guild_id)

    async def set_voice_channel_id(
        self, match_id: int, team: str, channel_id: int, *, replace_missing: bool = False
    ) -> Match:
        return await self.repository.set_voice_channel_id(
            match_id, team, channel_id, replace_missing=replace_missing
        )

    async def list_due_voice_cleanups(self, now: datetime | None = None) -> list[Match]:
        return await self.repository.list_due_voice_cleanups(now)

    async def claim_empty_voice_channel(
        self, guild_id: int, channel_id: int, *, now: datetime | None = None
    ) -> tuple[Match, str] | None:
        return await self.repository.claim_empty_voice_channel(
            guild_id, channel_id, now=now
        )

    async def reopen_empty_voice_channel(
        self, match_id: int, team: str, channel_id: int
    ) -> Match:
        return await self.repository.reopen_empty_voice_channel(
            match_id, team, channel_id
        )

    async def complete_empty_voice_channel(
        self, match_id: int, team: str, channel_id: int
    ) -> Match:
        return await self.repository.complete_empty_voice_channel(
            match_id, team, channel_id
        )

    async def record_voice_cleanup(
        self,
        match_id: int,
        *,
        clear_team_a: bool,
        clear_team_b: bool,
        retry_at: datetime | None,
    ) -> Match:
        return await self.repository.record_voice_cleanup(
            match_id,
            clear_team_a=clear_team_a,
            clear_team_b=clear_team_b,
            retry_at=retry_at,
        )

    async def update_message_id(self, match_id: int, message_id: int | None) -> Match:
        return await self.repository.update_message_id(match_id, message_id)

    async def attach_message_id(
        self,
        match_id: int,
        message_id: int,
        *,
        expected_old: int | None = None,
    ) -> Match:
        return await self.repository.attach_message_id(
            match_id, message_id, expected_old=expected_old
        )

    async def acknowledge_recruitment_reminder(
        self,
        match_id: int,
        token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        return await self.repository.acknowledge_recruitment_reminder(
            match_id, token, now=now
        )

    async def retry_recruitment_reminder(
        self,
        match_id: int,
        token: str,
        retry_at: datetime,
    ) -> bool:
        return await self.repository.retry_recruitment_reminder(
            match_id, token, retry_at
        )

    async def cancel_missing_message(self, match_id: int) -> Match:
        return await self.repository.cancel_missing_message(
            match_id,
            voice_cleanup_delay_seconds=self.voice_cleanup_delay_seconds,
        )



__all__ = [
    "MATCH_TITLE_MAX_LENGTH",
    "RESULT_MEMO_MAX_LENGTH",
    "ACTIVE_STATUSES",
    "ActiveMatchExistsError",
    "ActiveMembershipError",
    "AlreadyJoinedError",
    "BotAdminAlreadyExistsError",
    "BotAdminNotFoundError",
    "BotOwnerProtectedError",
    "CANCELLED",
    "DRAFTING",
    "FINISHED",
    "Game",
    "GameNotFoundError",
    "InvalidAssignmentModeError",
    "InvalidCapacityError",
    "InvalidDraftPickError",
    "InvalidMatchStateError",
    "InvalidRecruitmentMinutesError",
    "InvalidTimeoutError",
    "InvalidWinnerTeamError",
    "InvalidRolePreferencesError",
    "InvalidRankingLimitError",
    "InvalidSeasonStateError",
    "Match",
    "MatchError",
    "MatchEvent",
    "MatchFullError",
    "MatchNotFoundError",
    "MatchRepository",
    "MatchResult",
    "MatchService",
    "MatchStats",
    "RoleAssignmentImpossibleError",
    "RankingEntry",
    "RoleStats",
    "NotParticipantError",
    "Participant",
    "PermissionDeniedError",
    "ResultAlreadyRecordedError",
    "RoleRatingAlreadyExistsError",
    "Season",
    "SeasonNotFoundError",
    "UnplacedRoleError",
    "calculate_rating_delta",
]
