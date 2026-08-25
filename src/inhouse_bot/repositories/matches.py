"""PostgreSQL 내전 데이터와 상태 전이 처리."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import random
from typing import Any, Mapping


RECRUITING = "RECRUITING"
READY_CHECK = "READY_CHECK"
PLAYING = "PLAYING"
FINISHED = "FINISHED"
CANCELLED = "CANCELLED"
ACTIVE_STATUSES = (RECRUITING, READY_CHECK, PLAYING)
WINNER_TEAMS = ("A", "B")
MEMBER = "PARTICIPANT"
PARTICIPANT = MEMBER
WAITLIST = "WAITLIST"
DEFAULT_READY_TIMEOUT_SECONDS = 120
DEFAULT_RECRUITMENT_MINUTES = 30


class MatchError(RuntimeError):
    code = "match_error"
    message = "내전 처리 중 오류가 발생했습니다."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message


class MatchNotFoundError(MatchError):
    code = "match_not_found"
    message = "내전을 찾을 수 없습니다."


class ActiveMatchExistsError(MatchError):
    code = "active_match_exists"
    message = "해당 채널에 이미 활성 내전이 있습니다."


class AlreadyJoinedError(MatchError):
    code = "already_joined"
    message = "이미 내전에 참가 중입니다."


class MatchFullError(MatchError):
    code = "match_full"
    message = "내전 정원이 모두 찼습니다."


class InvalidMatchStateError(MatchError):
    code = "invalid_match_state"
    message = "현재 상태에서는 이 작업을 수행할 수 없습니다."


class InvalidCapacityError(MatchError):
    code = "invalid_capacity"
    message = "롤 내전 정원은 10명으로 고정입니다."


class NotParticipantError(MatchError):
    code = "not_participant"
    message = "내전에 참가 중이 아닙니다."


class PermissionDeniedError(MatchError):
    code = "permission_denied"
    message = "내전 생성자 또는 서버 관리 권한이 필요합니다."


class InvalidWinnerTeamError(MatchError):
    code = "invalid_winner_team"
    message = "승리팀은 A 또는 B여야 합니다."


class ResultAlreadyRecordedError(MatchError):
    code = "result_already_recorded"
    message = "내전 결과가 이미 기록되었습니다."


class InvalidRecruitmentMinutesError(MatchError):
    code = "invalid_recruitment_minutes"
    message = "모집 시간은 5분에서 1440분 사이여야 합니다."


class InvalidTimeoutError(MatchError):
    code = "invalid_timeout"
    message = "시간 제한은 양수여야 합니다."


@dataclass(frozen=True, slots=True)
class Participant:
    id: int
    match_id: int
    user_id: int
    team: str | None
    membership: str = MEMBER
    joined_at: datetime | None = None
    ready_at: datetime | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class MatchResult:
    id: int
    match_id: int
    winner_team: str
    memo: str | None
    recorded_by: int
    created_at: datetime | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class Match:
    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    game_id: int
    creator_id: int
    title: str
    capacity: int
    status: str
    created_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    recruitment_deadline_at: datetime | None = None
    recruitment_reminded_at: datetime | None = None
    ready_deadline_at: datetime | None = None
    cancel_reason: str | None = None
    participants: tuple[Participant, ...] = field(default_factory=tuple)
    waitlist: tuple[Participant, ...] = field(default_factory=tuple)
    result: MatchResult | None = None
    # 바로 직전에 처리한 변경 결과만 담는다.
    waitlisted: bool | None = None
    ready: bool | None = None
    started: bool = False
    removed_user_id: int | None = None
    removed_membership: str | None = None
    promoted_user_ids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def participant_count(self) -> int:
        return len(self.participants)

    @property
    def waitlist_count(self) -> int:
        return len(self.waitlist)

    @property
    def ready_count(self) -> int:
        return sum(item.ready_at is not None for item in self.participants)

    @property
    def is_waitlisted(self) -> bool | None:
        return self.waitlisted

    @property
    def promoted(self) -> tuple[int, ...]:
        return self.promoted_user_ids

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class MatchStats:
    games: int
    wins: int
    losses: int
    rate: float

    @property
    def win_rate(self) -> float:
        return self.rate

    @property
    def win_rate_percent(self) -> float:
        return self.rate * 100

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class MatchEvent:
    match: Match
    kind: str
    removed_user_ids: tuple[int, ...] = field(default_factory=tuple)
    promoted_user_ids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def match_id(self) -> int:
        return self.match.id

    @property
    def user_ids(self) -> tuple[int, ...]:
        return self.removed_user_ids

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


def _row_value(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _is_unique_violation(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == "23505" or exc.__class__.__name__ == "UniqueViolationError"


def _now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _validate_timeout(seconds: int) -> int:
    if int(seconds) <= 0:
        raise InvalidTimeoutError()
    return int(seconds)


def _validate_recruitment_minutes(minutes: int) -> int:
    if not 5 <= int(minutes) <= 1440:
        raise InvalidRecruitmentMinutesError()
    return int(minutes)


class MatchRepository:
    """변경 트랜잭션에서 ``matches`` 행을 먼저 잠그는 저장소."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    @staticmethod
    def _participant(row: Mapping[str, Any] | Any) -> Participant:
        return Participant(
            id=int(_row_value(row, "id")),
            match_id=int(_row_value(row, "match_id")),
            user_id=int(_row_value(row, "user_id")),
            team=_row_value(row, "team"),
            membership=_row_value(row, "membership", MEMBER),
            joined_at=_row_value(row, "joined_at"),
            ready_at=_row_value(row, "ready_at"),
        )

    @staticmethod
    def _result(row: Mapping[str, Any] | Any) -> MatchResult:
        return MatchResult(
            id=int(_row_value(row, "id")),
            match_id=int(_row_value(row, "match_id")),
            winner_team=_row_value(row, "winner_team"),
            memo=_row_value(row, "memo"),
            recorded_by=int(_row_value(row, "recorded_by")),
            created_at=_row_value(row, "created_at"),
        )

    @staticmethod
    def _match_row_kwargs(row: Mapping[str, Any] | Any) -> dict[str, Any]:
        return {
            "id": int(_row_value(row, "id")),
            "guild_id": int(_row_value(row, "guild_id")),
            "channel_id": int(_row_value(row, "channel_id")),
            "message_id": _row_value(row, "message_id"),
            "game_id": int(_row_value(row, "game_id")),
            "creator_id": int(_row_value(row, "creator_id")),
            "title": _row_value(row, "title"),
            "capacity": int(_row_value(row, "capacity")),
            "status": _row_value(row, "status"),
            "created_at": _row_value(row, "created_at"),
            "started_at": _row_value(row, "started_at"),
            "ended_at": _row_value(row, "ended_at"),
            "recruitment_deadline_at": _row_value(row, "recruitment_deadline_at"),
            "recruitment_reminded_at": _row_value(row, "recruitment_reminded_at"),
            "ready_deadline_at": _row_value(row, "ready_deadline_at"),
            "cancel_reason": _row_value(row, "cancel_reason"),
        }

    async def _fetch_match(self, conn: Any, match_id: int) -> Match:
        row = await conn.fetchrow(
            """
            SELECT id, guild_id, channel_id, message_id, game_id, creator_id,
                   title, capacity, status, created_at, started_at, ended_at,
                   recruitment_deadline_at, recruitment_reminded_at,
                   ready_deadline_at, cancel_reason
            FROM matches WHERE id = $1
            """,
            match_id,
        )
        if row is None:
            raise MatchNotFoundError()
        items = await conn.fetch(
            """
            SELECT id, match_id, user_id, team, membership, joined_at, ready_at
            FROM match_participants WHERE match_id = $1 ORDER BY joined_at, id
            """,
            match_id,
        )
        result = await conn.fetchrow(
            """
            SELECT id, match_id, winner_team, memo, recorded_by, created_at
            FROM match_results WHERE match_id = $1
            """,
            match_id,
        )
        participants = tuple(self._participant(item) for item in items if _row_value(item, "membership", MEMBER) == MEMBER)
        waitlist = tuple(self._participant(item) for item in items if _row_value(item, "membership", MEMBER) == WAITLIST)
        return Match(
            **self._match_row_kwargs(row),
            participants=participants,
            waitlist=waitlist,
            result=self._result(result) if result is not None else None,
        )

    async def _locked_match(self, conn: Any, match_id: int) -> Any:
        """기존 내전을 바꿀 때 가장 먼저 실행하는 잠금 쿼리."""

        row = await conn.fetchrow(
            """
            SELECT id, guild_id, channel_id, message_id, game_id, creator_id,
                   title, capacity, status, created_at, started_at, ended_at,
                   recruitment_deadline_at, recruitment_reminded_at,
                   ready_deadline_at, cancel_reason
            FROM matches WHERE id = $1 FOR UPDATE
            """,
            match_id,
        )
        if row is None:
            raise MatchNotFoundError()
        return row

    @staticmethod
    def _require_state(row: Any, *states: str) -> None:
        if _row_value(row, "status") not in states:
            raise InvalidMatchStateError()

    @staticmethod
    def _require_manager(row: Any, actor_id: int, manage_guild: bool) -> None:
        if int(_row_value(row, "creator_id")) != int(actor_id) and not manage_guild:
            raise PermissionDeniedError()

    @staticmethod
    async def _participant_count(conn: Any, match_id: int) -> int:
        return int(await conn.fetchval(
            "SELECT count(*) FROM match_participants WHERE match_id = $1 AND membership = 'PARTICIPANT'",
            match_id,
        ))

    @staticmethod
    async def _promote_waitlist(conn: Any, match_id: int, capacity: int) -> tuple[int, ...]:
        promoted: list[int] = []
        while await MatchRepository._participant_count(conn, match_id) < capacity:
            waiting = await conn.fetchrow(
                """
                SELECT id, user_id FROM match_participants
                WHERE match_id = $1 AND membership = 'WAITLIST'
                ORDER BY joined_at, id LIMIT 1
                """,
                match_id,
            )
            if waiting is None:
                break
            user_id = int(_row_value(waiting, "user_id"))
            await conn.execute(
                """
                UPDATE match_participants
                SET membership = 'PARTICIPANT', ready_at = NULL, team = NULL
                WHERE id = $1
                """,
                int(_row_value(waiting, "id")),
            )
            promoted.append(user_id)
        return tuple(promoted)

    @staticmethod
    async def _reset_ready_roster(
        conn: Any,
        row: Any,
        match_id: int,
        now: datetime,
        ready_timeout_seconds: int,
        default_recruitment_minutes: int,
    ) -> None:
        await conn.execute(
            """
            UPDATE match_participants SET ready_at = NULL, team = NULL
            WHERE match_id = $1 AND membership = 'PARTICIPANT'
            """,
            match_id,
        )
        count = await MatchRepository._participant_count(conn, match_id)
        if count >= int(_row_value(row, "capacity")):
            await conn.execute(
                """
                UPDATE matches
                SET status = 'READY_CHECK', ready_deadline_at = $2,
                    recruitment_deadline_at = NULL
                WHERE id = $1
                """,
                match_id,
                now + timedelta(seconds=ready_timeout_seconds),
            )
        else:
            await conn.execute(
                """
                UPDATE matches
                SET status = 'RECRUITING', ready_deadline_at = NULL,
                    recruitment_deadline_at = $2, recruitment_reminded_at = NULL
                WHERE id = $1
                """,
                match_id,
                now + timedelta(minutes=default_recruitment_minutes),
            )

    async def create_match(
        self,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        title: str,
        *,
        game_key: str = "lol",
        capacity: int = 10,
        recruitment_minutes: int = DEFAULT_RECRUITMENT_MINUTES,
        now: datetime | None = None,
    ) -> Match:
        if capacity != 10:
            raise InvalidCapacityError()
        recruitment_minutes = _validate_recruitment_minutes(recruitment_minutes)
        created_at = _now(now)
        deadline = created_at + timedelta(minutes=recruitment_minutes)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    game = await conn.fetchrow('SELECT id FROM games WHERE "key" = $1', game_key)
                    if game is None:
                        raise MatchNotFoundError("게임을 찾을 수 없습니다.")
                    row = await conn.fetchrow(
                        """
                        INSERT INTO matches
                            (guild_id, channel_id, game_id, creator_id, title, capacity,
                             created_at, recruitment_deadline_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        RETURNING id
                        """,
                        guild_id,
                        channel_id,
                        _row_value(game, "id"),
                        creator_id,
                        title,
                        capacity,
                        created_at,
                        deadline,
                    )
                    match_id = int(_row_value(row, "id"))
                    await conn.execute(
                        """
                        INSERT INTO match_participants (match_id, user_id, membership, joined_at)
                        VALUES ($1, $2, 'PARTICIPANT', $3)
                        """,
                        match_id,
                        creator_id,
                        created_at,
                    )
                    return await self._fetch_match(conn, match_id)
        except MatchError:
            raise
        except Exception as exc:
            if _is_unique_violation(exc):
                raise ActiveMatchExistsError() from exc
            raise

    async def join_match(
        self,
        match_id: int,
        user_id: int,
        *,
        now: datetime | None = None,
        ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT_SECONDS,
        default_recruitment_minutes: int = DEFAULT_RECRUITMENT_MINUTES,
    ) -> Match:
        ready_timeout_seconds = _validate_timeout(ready_timeout_seconds)
        default_recruitment_minutes = _validate_recruitment_minutes(default_recruitment_minutes)
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                self._require_state(row, RECRUITING, READY_CHECK)
                existing = await conn.fetchrow(
                    "SELECT 1 FROM match_participants WHERE match_id = $1 AND user_id = $2",
                    match_id,
                    user_id,
                )
                if existing is not None:
                    raise AlreadyJoinedError()
                count = await self._participant_count(conn, match_id)
                membership = MEMBER if count < int(_row_value(row, "capacity")) else WAITLIST
                try:
                    await conn.execute(
                        """
                        INSERT INTO match_participants (match_id, user_id, membership, joined_at)
                        VALUES ($1, $2, $3, $4)
                        """,
                        match_id,
                        user_id,
                        membership,
                        current,
                    )
                except Exception as exc:
                    if _is_unique_violation(exc):
                        raise AlreadyJoinedError() from exc
                    raise
                if membership == MEMBER and _row_value(row, "status") == READY_CHECK:
                    await self._reset_ready_roster(
                        conn, row, match_id, current,
                        ready_timeout_seconds, default_recruitment_minutes,
                    )
                match = await self._fetch_match(conn, match_id)
                return replace(match, waitlisted=membership == WAITLIST)

    async def leave_match(
        self,
        match_id: int,
        user_id: int,
        *,
        now: datetime | None = None,
        ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT_SECONDS,
        default_recruitment_minutes: int = DEFAULT_RECRUITMENT_MINUTES,
    ) -> Match:
        ready_timeout_seconds = _validate_timeout(ready_timeout_seconds)
        default_recruitment_minutes = _validate_recruitment_minutes(default_recruitment_minutes)
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                self._require_state(row, RECRUITING, READY_CHECK)
                member = await conn.fetchrow(
                    "SELECT membership FROM match_participants WHERE match_id = $1 AND user_id = $2",
                    match_id, user_id,
                )
                if member is None:
                    raise NotParticipantError()
                membership = _row_value(member, "membership", MEMBER)
                await conn.execute(
                    "DELETE FROM match_participants WHERE match_id = $1 AND user_id = $2",
                    match_id, user_id,
                )
                promoted = ()
                if membership == MEMBER:
                    promoted = await self._promote_waitlist(conn, match_id, int(_row_value(row, "capacity")))
                    if _row_value(row, "status") == READY_CHECK:
                        await self._reset_ready_roster(
                            conn, row, match_id, current,
                            ready_timeout_seconds, default_recruitment_minutes,
                        )
                match = await self._fetch_match(conn, match_id)
                return replace(
                    match,
                    removed_user_id=int(user_id),
                    removed_membership=membership,
                    promoted_user_ids=promoted,
                )

    async def begin_ready_check(
        self,
        match_id: int,
        actor_id: int,
        *,
        manage_guild: bool = False,
        now: datetime | None = None,
        ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT_SECONDS,
    ) -> Match:
        ready_timeout_seconds = _validate_timeout(ready_timeout_seconds)
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                self._require_manager(row, actor_id, manage_guild)
                self._require_state(row, RECRUITING)
                count = await self._participant_count(conn, match_id)
                capacity = int(_row_value(row, "capacity"))
                if count != capacity:
                    raise MatchFullError("정확히 10명이 참가해야 준비 확인을 시작할 수 있습니다.")
                await conn.execute(
                    """
                    UPDATE match_participants SET ready_at = NULL, team = NULL
                    WHERE match_id = $1 AND membership = 'PARTICIPANT'
                    """,
                    match_id,
                )
                await conn.execute(
                    """
                    UPDATE matches
                    SET status = 'READY_CHECK', ready_deadline_at = $2,
                        recruitment_deadline_at = NULL
                    WHERE id = $1
                    """,
                    match_id, current + timedelta(seconds=ready_timeout_seconds),
                )
                return await self._fetch_match(conn, match_id)

    async def start_match(
        self,
        match_id: int,
        actor_id: int,
        *,
        manage_guild: bool = False,
        now: datetime | None = None,
        ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT_SECONDS,
    ) -> Match:
        """기존 호출부에서 쓰는 준비 확인 시작 메서드명."""
        return await self.begin_ready_check(
            match_id, actor_id, manage_guild=manage_guild,
            now=now, ready_timeout_seconds=ready_timeout_seconds,
        )

    async def toggle_ready(
        self,
        match_id: int,
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> Match:
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                self._require_state(row, READY_CHECK)
                participant = await conn.fetchrow(
                    """
                    SELECT ready_at FROM match_participants
                    WHERE match_id = $1 AND user_id = $2 AND membership = 'PARTICIPANT'
                    """,
                    match_id, user_id,
                )
                if participant is None:
                    raise NotParticipantError()
                was_ready = _row_value(participant, "ready_at") is not None
                await conn.execute(
                    """
                    UPDATE match_participants SET ready_at = $3
                    WHERE match_id = $1 AND user_id = $2 AND membership = 'PARTICIPANT'
                    """,
                    match_id, user_id, None if was_ready else current,
                )
                ready_count = int(await conn.fetchval(
                    """
                    SELECT count(*) FROM match_participants
                    WHERE match_id = $1 AND membership = 'PARTICIPANT' AND ready_at IS NOT NULL
                    """,
                    match_id,
                ))
                started = False
                if not was_ready and ready_count == int(_row_value(row, "capacity")):
                    rows = await conn.fetch(
                        """
                        SELECT user_id FROM match_participants
                        WHERE match_id = $1 AND membership = 'PARTICIPANT'
                        ORDER BY joined_at, id
                        """,
                        match_id,
                    )
                    user_ids = [int(_row_value(item, "user_id")) for item in rows]
                    if len(user_ids) != int(_row_value(row, "capacity")):
                        raise InvalidMatchStateError()
                    random.shuffle(user_ids)
                    half = len(user_ids) // 2
                    for team, members in (("A", user_ids[:half]), ("B", user_ids[half:])):
                        for member_id in members:
                            await conn.execute(
                                """
                                UPDATE match_participants SET team = $3
                                WHERE match_id = $1 AND user_id = $2 AND membership = 'PARTICIPANT'
                                """,
                                match_id, member_id, team,
                            )
                    await conn.execute(
                        """
                        UPDATE matches
                        SET status = 'PLAYING', started_at = $2,
                            ready_deadline_at = NULL, recruitment_deadline_at = NULL
                        WHERE id = $1 AND status = 'READY_CHECK'
                        """,
                        match_id, current,
                    )
                    started = True
                match = await self._fetch_match(conn, match_id)
                return replace(match, ready=not was_ready, started=started)

    async def kick_match_member(
        self,
        match_id: int,
        target_user_id: int,
        actor_id: int,
        *,
        manage_guild: bool = False,
        now: datetime | None = None,
        ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT_SECONDS,
        default_recruitment_minutes: int = DEFAULT_RECRUITMENT_MINUTES,
    ) -> Match:
        ready_timeout_seconds = _validate_timeout(ready_timeout_seconds)
        default_recruitment_minutes = _validate_recruitment_minutes(default_recruitment_minutes)
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                self._require_manager(row, actor_id, manage_guild)
                self._require_state(row, RECRUITING, READY_CHECK)
                member = await conn.fetchrow(
                    "SELECT membership FROM match_participants WHERE match_id = $1 AND user_id = $2",
                    match_id, target_user_id,
                )
                if member is None:
                    raise NotParticipantError()
                membership = _row_value(member, "membership", MEMBER)
                await conn.execute(
                    "DELETE FROM match_participants WHERE match_id = $1 AND user_id = $2",
                    match_id, target_user_id,
                )
                promoted = ()
                if membership == MEMBER:
                    promoted = await self._promote_waitlist(conn, match_id, int(_row_value(row, "capacity")))
                    if _row_value(row, "status") == READY_CHECK:
                        await self._reset_ready_roster(
                            conn, row, match_id, current,
                            ready_timeout_seconds, default_recruitment_minutes,
                        )
                match = await self._fetch_match(conn, match_id)
                return replace(
                    match,
                    removed_user_id=int(target_user_id),
                    removed_membership=membership,
                    promoted_user_ids=promoted,
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
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                self._require_manager(row, actor_id, manage_guild)
                self._require_state(row, RECRUITING, READY_CHECK, PLAYING)
                await conn.execute(
                    """
                    UPDATE matches
                    SET status = 'CANCELLED', ended_at = $2,
                        ready_deadline_at = NULL, cancel_reason = $3
                    WHERE id = $1
                    """,
                    match_id, current, reason or "직접 취소",
                )
                return await self._fetch_match(conn, match_id)

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
        if winner_team not in WINNER_TEAMS:
            raise InvalidWinnerTeamError()
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                self._require_manager(row, actor_id, manage_guild)
                if _row_value(row, "status") != PLAYING:
                    if _row_value(row, "status") == FINISHED:
                        prior = await conn.fetchrow(
                            "SELECT 1 FROM match_results WHERE match_id = $1", match_id
                        )
                        if prior is not None:
                            raise ResultAlreadyRecordedError()
                    raise InvalidMatchStateError()
                try:
                    await conn.execute(
                        """
                        INSERT INTO match_results (match_id, winner_team, memo, recorded_by)
                        VALUES ($1, $2, $3, $4)
                        """,
                        match_id, winner_team, memo, actor_id,
                    )
                except Exception as exc:
                    if _is_unique_violation(exc):
                        raise ResultAlreadyRecordedError() from exc
                    raise
                await conn.execute(
                    """
                    UPDATE matches SET status = 'FINISHED', ended_at = $2,
                        ready_deadline_at = NULL WHERE id = $1
                    """,
                    match_id, current,
                )
                return await self._fetch_match(conn, match_id)

    async def stats(self, guild_id: int, user_id: int) -> MatchStats:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT count(*) AS games,
                       count(*) FILTER (WHERE p.team = r.winner_team) AS wins
                FROM matches m
                JOIN match_participants p ON p.match_id = m.id
                JOIN match_results r ON r.match_id = m.id
                WHERE m.guild_id = $1 AND m.status = 'FINISHED'
                  AND p.user_id = $2 AND p.membership = 'PARTICIPANT'
                  AND p.team IS NOT NULL
                """,
                guild_id, user_id,
            )
            games = int(_row_value(row, "games", 0) or 0)
            wins = int(_row_value(row, "wins", 0) or 0)
            losses = games - wins
            return MatchStats(games, wins, losses, wins / games if games else 0.0)

    async def process_due_matches(
        self,
        now: datetime | None = None,
        *,
        ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT_SECONDS,
        default_recruitment_minutes: int = DEFAULT_RECRUITMENT_MINUTES,
        reminder_before_seconds: int = 300,
    ) -> list[MatchEvent]:
        current = _now(now)
        ready_timeout_seconds = _validate_timeout(ready_timeout_seconds)
        default_recruitment_minutes = _validate_recruitment_minutes(default_recruitment_minutes)
        if int(reminder_before_seconds) < 0:
            raise InvalidTimeoutError()
        async with self.pool.acquire() as conn:
            ids = await conn.fetch(
                """
                SELECT id FROM matches
                WHERE ended_at IS NULL AND (
                    (status = 'RECRUITING' AND recruitment_deadline_at IS NOT NULL AND
                        (recruitment_deadline_at <= $1 OR
                         (recruitment_reminded_at IS NULL AND
                          recruitment_deadline_at <= $1 + ($2 * interval '1 second'))))
                    OR (status = 'READY_CHECK' AND ready_deadline_at IS NOT NULL
                        AND ready_deadline_at <= $1)
                )
                ORDER BY id
                """,
                current, int(reminder_before_seconds),
            )
            events: list[MatchEvent] = []
            for item in ids:
                match_id = int(_row_value(item, "id"))
                async with conn.transaction():
                    # 이 트랜잭션도 내전 행부터 잠근다.
                    row = await self._locked_match(conn, match_id)
                    status = _row_value(row, "status")
                    kind: str | None = None
                    removed: tuple[int, ...] = ()
                    promoted: tuple[int, ...] = ()
                    if status == RECRUITING:
                        deadline = _row_value(row, "recruitment_deadline_at")
                        if deadline is None:
                            continue
                        if deadline <= current:
                            count = await self._participant_count(conn, match_id)
                            if count >= int(_row_value(row, "capacity")):
                                await conn.execute(
                                    """
                                    UPDATE match_participants
                                    SET ready_at = NULL, team = NULL
                                    WHERE match_id = $1 AND membership = 'PARTICIPANT'
                                    """,
                                    match_id,
                                )
                                await conn.execute(
                                    """
                                    UPDATE matches
                                    SET status = 'READY_CHECK', ready_deadline_at = $2,
                                        recruitment_deadline_at = NULL
                                    WHERE id = $1
                                    """,
                                    match_id, current + timedelta(seconds=ready_timeout_seconds),
                                )
                            else:
                                await conn.execute(
                                    """
                                    UPDATE matches
                                    SET status = 'CANCELLED', ended_at = $2,
                                        cancel_reason = '모집 시간 만료', ready_deadline_at = NULL
                                    WHERE id = $1
                                    """,
                                    match_id, current,
                                )
                            kind = "recruitment_expired"
                        elif (
                            _row_value(row, "recruitment_reminded_at") is None
                            and int(reminder_before_seconds) > 0
                            and deadline <= current + timedelta(seconds=int(reminder_before_seconds))
                            and _row_value(row, "created_at") is not None
                            and deadline - _row_value(row, "created_at")
                            > timedelta(seconds=int(reminder_before_seconds))
                        ):
                            updated = await conn.execute(
                                """
                                UPDATE matches SET recruitment_reminded_at = $2
                                WHERE id = $1 AND recruitment_reminded_at IS NULL
                                """,
                                match_id, current,
                            )
                            if updated.endswith("1"):
                                kind = "recruitment_reminder"
                    elif status == READY_CHECK:
                        deadline = _row_value(row, "ready_deadline_at")
                        if deadline is None or deadline > current:
                            continue
                        rows = await conn.fetch(
                            """
                            SELECT user_id FROM match_participants
                            WHERE match_id = $1 AND membership = 'PARTICIPANT'
                              AND ready_at IS NULL ORDER BY joined_at, id
                            """,
                            match_id,
                        )
                        removed = tuple(int(_row_value(participant, "user_id")) for participant in rows)
                        for user_id in removed:
                            await conn.execute(
                                "DELETE FROM match_participants WHERE match_id = $1 AND user_id = $2",
                                match_id, user_id,
                            )
                        promoted = await self._promote_waitlist(
                            conn, match_id, int(_row_value(row, "capacity"))
                        )
                        await self._reset_ready_roster(
                            conn, row, match_id, current,
                            ready_timeout_seconds, default_recruitment_minutes,
                        )
                        kind = "ready_expired"
                    if kind is not None:
                        events.append(MatchEvent(
                            await self._fetch_match(conn, match_id), kind,
                            removed, promoted,
                        ))
            return events

    async def get_match(self, match_id: int) -> Match | None:
        async with self.pool.acquire() as conn:
            return await self._fetch_match_or_none(conn, match_id)

    async def _fetch_match_or_none(self, conn: Any, match_id: int) -> Match | None:
        row = await conn.fetchrow("SELECT id FROM matches WHERE id = $1", match_id)
        if row is None:
            return None
        return await self._fetch_match(conn, match_id)

    async def get_active_match(self, guild_id: int, channel_id: int) -> Match | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id FROM matches
                WHERE guild_id = $1 AND channel_id = $2 AND ended_at IS NULL
                  AND status = ANY($3::text[])
                ORDER BY id DESC LIMIT 1
                """,
                guild_id, channel_id, list(ACTIVE_STATUSES),
            )
            return await self._fetch_match(conn, int(_row_value(row, "id"))) if row else None

    async def list_active(self, guild_id: int | None = None) -> list[Match]:
        async with self.pool.acquire() as conn:
            if guild_id is None:
                rows = await conn.fetch(
                    """
                    SELECT id FROM matches
                    WHERE ended_at IS NULL AND status = ANY($1::text[])
                    ORDER BY id
                    """,
                    list(ACTIVE_STATUSES),
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id FROM matches
                    WHERE guild_id = $1 AND ended_at IS NULL
                      AND status = ANY($2::text[])
                    ORDER BY id
                    """,
                    guild_id, list(ACTIVE_STATUSES),
                )
            return [await self._fetch_match(conn, int(_row_value(row, "id"))) for row in rows]

    async def update_message_id(self, match_id: int, message_id: int | None) -> Match:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._locked_match(conn, match_id)
                await conn.execute(
                    "UPDATE matches SET message_id = $2 WHERE id = $1", match_id, message_id
                )
                return await self._fetch_match(conn, match_id)

    async def cancel_missing_message(self, match_id: int) -> Match:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                status = _row_value(row, "status")
                if _row_value(row, "ended_at") is None and status in ACTIVE_STATUSES:
                    await conn.execute(
                        """
                        UPDATE matches SET status = 'CANCELLED', ended_at = now(),
                            ready_deadline_at = NULL, cancel_reason = '모집 메시지를 찾을 수 없음'
                        WHERE id = $1 AND ended_at IS NULL
                        """,
                        match_id,
                    )
                return await self._fetch_match(conn, match_id)
