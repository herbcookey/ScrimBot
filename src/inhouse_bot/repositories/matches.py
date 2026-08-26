"""PostgreSQL 내전 데이터와 상태 전이 처리."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import random
from typing import Any, Mapping

from inhouse_bot.role_assignment import (
    DRAFT_TEAMS,
    ROLES,
    ROLE_LABELS,
    RoleAssignmentError,
    RolePlayer,
    balanced_assignment,
    draft_completion_possible,
    finalize_draft,
    initial_role_rating,
    normalize_role,
    positive_rating_average,
    select_captains,
    validate_preferences,
)


RECRUITING = "RECRUITING"
READY_CHECK = "READY_CHECK"
DRAFTING = "DRAFTING"
PLAYING = "PLAYING"
FINISHED = "FINISHED"
CANCELLED = "CANCELLED"
ACTIVE_STATUSES = (RECRUITING, READY_CHECK, DRAFTING, PLAYING)
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


class ActiveMembershipError(MatchError):
    code = "active_membership"
    message = "같은 서버의 다른 활성 내전에 이미 참가 중입니다."


class MatchFullError(MatchError):
    code = "match_full"
    message = "내전 정원이 모두 찼습니다."


class InvalidMatchStateError(MatchError):
    code = "invalid_match_state"
    message = "현재 상태에서는 이 작업을 수행할 수 없습니다."


class InvalidCapacityError(MatchError):
    code = "invalid_capacity"
    message = "게임 정원 설정이 올바르지 않습니다."


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


class GameNotFoundError(MatchError):
    code = "game_not_found"
    message = "게임을 찾을 수 없습니다."


class SeasonNotFoundError(MatchError):
    code = "season_not_found"
    message = "시즌을 찾을 수 없습니다."


class InvalidSeasonStateError(MatchError):
    code = "invalid_season_state"
    message = "현재 종료할 활성 시즌이 없습니다."


class InvalidRankingLimitError(MatchError):
    code = "invalid_ranking_limit"
    message = "랭킹 인원수는 1명에서 25명 사이여야 합니다."


class InvalidRolePreferencesError(MatchError):
    code = "invalid_role_preferences"
    message = "라인 지망을 확인해 주세요."


class UnplacedRoleError(MatchError):
    code = "unplaced_role"
    message = "배치가 끝나지 않은 라인은 지망으로 제출할 수 없습니다."


class RoleAssignmentImpossibleError(MatchError):
    code = "role_assignment_impossible"
    message = "현재 지망으로는 양 팀 라인을 완성할 수 없습니다. 라인을 변경해 주세요."


class InvalidAssignmentModeError(MatchError):
    code = "invalid_assignment_mode"
    message = "배정 방식은 BALANCED 또는 DRAFT여야 합니다."


class InvalidDraftPickError(MatchError):
    code = "invalid_draft_pick"
    message = "현재 지명할 수 없는 사용자입니다."


@dataclass(frozen=True, slots=True)
class Game:
    id: int
    key: str
    name: str
    team_size: int
    capacity: int
    default_rating: int
    k_factor: int
    rating_enabled: bool
    role_rating_enabled: bool = False

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class Season:
    id: int
    guild_id: int
    game_id: int
    name: str
    started_at: datetime
    ended_at: datetime | None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class RankingEntry:
    user_id: int
    rating: int
    games_played: int
    role: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class Participant:
    id: int
    match_id: int
    user_id: int
    team: str | None
    membership: str = MEMBER
    joined_at: datetime | None = None
    ready_at: datetime | None = None
    rating_snapshot: int | None = None
    preferred_role_1: str | None = None
    preferred_role_2: str | None = None
    preferred_role_3: str | None = None
    assigned_role: str | None = None
    role_rating_snapshot: int | None = None
    draft_order: int | None = None
    role_ratings: tuple[tuple[str, int, int], ...] = field(default_factory=tuple)

    @property
    def preferences(self) -> tuple[str, ...]:
        return tuple(
            role for role in (
                self.preferred_role_1,
                self.preferred_role_2,
                self.preferred_role_3,
            ) if role is not None
        )

    @property
    def average_role_rating(self) -> int:
        return positive_rating_average(rating for _, rating, _ in self.role_ratings)

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
    season_id: int | None = None
    game_key: str = "lol"
    game_name: str = "리그 오브 레전드"
    team_size: int = 5
    season_name: str = "Legacy"
    role_rating_enabled: bool = False
    assignment_mode: str = "BALANCED"
    captain_a_id: int | None = None
    captain_b_id: int | None = None
    draft_pick_index: int = 0
    voice_category_id: int | None = None
    team_a_voice_channel_id: int | None = None
    team_b_voice_channel_id: int | None = None
    voice_cleanup_at: datetime | None = None
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
    average_role_rating: int = 0
    role_stats: tuple["RoleStats", ...] = field(default_factory=tuple)
    highest_role_rating: int = 0

    @property
    def win_rate(self) -> float:
        return self.rate

    @property
    def win_rate_percent(self) -> float:
        return self.rate * 100

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class RoleStats:
    role: str
    rating: int
    placed: bool
    games: int
    wins: int
    losses: int
    rate: float

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


def calculate_rating_delta(
    avg_a: float,
    avg_b: float,
    winner_team: str,
    k_factor: int,
) -> tuple[int, int]:
    """두 팀 평균 점수로 A/B 변동값을 계산한다."""

    if winner_team not in WINNER_TEAMS:
        raise InvalidWinnerTeamError()
    expected_a = 1 / (1 + 10 ** ((float(avg_b) - float(avg_a)) / 400))
    delta_a = round(int(k_factor) * ((1 if winner_team == "A" else 0) - expected_a))
    return delta_a, -delta_a


class MatchRepository:
    """변경 트랜잭션에서 ``matches`` 행을 먼저 잠그는 저장소."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    @staticmethod
    def _participant(
        row: Mapping[str, Any] | Any,
        role_ratings: tuple[tuple[str, int, int], ...] = (),
    ) -> Participant:
        return Participant(
            id=int(_row_value(row, "id")),
            match_id=int(_row_value(row, "match_id")),
            user_id=int(_row_value(row, "user_id")),
            team=_row_value(row, "team"),
            membership=_row_value(row, "membership", MEMBER),
            joined_at=_row_value(row, "joined_at"),
            ready_at=_row_value(row, "ready_at"),
            rating_snapshot=_row_value(row, "rating_snapshot"),
            preferred_role_1=_row_value(row, "preferred_role_1"),
            preferred_role_2=_row_value(row, "preferred_role_2"),
            preferred_role_3=_row_value(row, "preferred_role_3"),
            assigned_role=_row_value(row, "assigned_role"),
            role_rating_snapshot=_row_value(row, "role_rating_snapshot"),
            draft_order=_row_value(row, "draft_order"),
            role_ratings=role_ratings,
        )

    @staticmethod
    def _game(row: Mapping[str, Any] | Any) -> Game:
        return Game(
            id=int(_row_value(row, "id")),
            key=str(_row_value(row, "key")),
            name=str(_row_value(row, "name")),
            team_size=int(_row_value(row, "team_size")),
            capacity=int(_row_value(row, "capacity")),
            default_rating=int(_row_value(row, "default_rating")),
            k_factor=int(_row_value(row, "k_factor")),
            rating_enabled=bool(_row_value(row, "rating_enabled")),
            role_rating_enabled=bool(_row_value(row, "role_rating_enabled", False)),
        )

    @staticmethod
    def _season(row: Mapping[str, Any] | Any) -> Season:
        return Season(
            id=int(_row_value(row, "id")),
            guild_id=int(_row_value(row, "guild_id")),
            game_id=int(_row_value(row, "game_id")),
            name=str(_row_value(row, "name")),
            started_at=_row_value(row, "started_at"),
            ended_at=_row_value(row, "ended_at"),
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
            "season_id": int(_row_value(row, "season_id")),
            "game_key": str(_row_value(row, "game_key")),
            "game_name": str(_row_value(row, "game_name")),
            "team_size": int(_row_value(row, "team_size")),
            "season_name": str(_row_value(row, "season_name")),
            "role_rating_enabled": bool(_row_value(row, "role_rating_enabled", False)),
            "assignment_mode": str(_row_value(row, "assignment_mode", "BALANCED")),
            "captain_a_id": _row_value(row, "captain_a_id"),
            "captain_b_id": _row_value(row, "captain_b_id"),
            "draft_pick_index": int(_row_value(row, "draft_pick_index", 0)),
            "voice_category_id": _row_value(row, "voice_category_id"),
            "team_a_voice_channel_id": _row_value(row, "team_a_voice_channel_id"),
            "team_b_voice_channel_id": _row_value(row, "team_b_voice_channel_id"),
            "voice_cleanup_at": _row_value(row, "voice_cleanup_at"),
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
            SELECT m.id, m.guild_id, m.channel_id, m.message_id, m.game_id,
                   m.season_id, g."key" AS game_key, g.name AS game_name,
                   g.team_size, m.role_rating_enabled, s.name AS season_name,
                   m.assignment_mode, m.captain_a_id, m.captain_b_id,
                   m.draft_pick_index, m.voice_category_id,
                   m.team_a_voice_channel_id, m.team_b_voice_channel_id,
                   m.voice_cleanup_at, m.creator_id, m.title, m.capacity, m.status,
                   m.created_at, m.started_at, m.ended_at,
                   m.recruitment_deadline_at, m.recruitment_reminded_at,
                   m.ready_deadline_at, m.cancel_reason
            FROM matches AS m
            JOIN games AS g ON g.id = m.game_id
            JOIN seasons AS s ON s.id = m.season_id
            WHERE m.id = $1
            """,
            match_id,
        )
        if row is None:
            raise MatchNotFoundError()
        items = await conn.fetch(
            """
            SELECT id, match_id, user_id, team, membership, joined_at, ready_at,
                   rating_snapshot, preferred_role_1, preferred_role_2,
                   preferred_role_3, assigned_role, role_rating_snapshot, draft_order
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
        ratings = await conn.fetch(
            """SELECT user_id, role, rating, games_played FROM player_role_ratings
               WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                 AND user_id = ANY($4::bigint[]) ORDER BY user_id, role""",
            int(_row_value(row, "guild_id")), int(_row_value(row, "game_id")),
            int(_row_value(row, "season_id")),
            [int(_row_value(item, "user_id")) for item in items],
        ) if items else []
        ratings_by_user: dict[int, list[tuple[str, int, int]]] = {}
        for rating in ratings:
            ratings_by_user.setdefault(int(_row_value(rating, "user_id")), []).append((
                str(_row_value(rating, "role")), int(_row_value(rating, "rating")),
                int(_row_value(rating, "games_played")),
            ))
        converted = tuple(
            self._participant(
                item, tuple(ratings_by_user.get(int(_row_value(item, "user_id")), ()))
            )
            for item in items
        )
        participants = tuple(item for item in converted if item.membership == MEMBER)
        waitlist = tuple(item for item in converted if item.membership == WAITLIST)
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
            SELECT id, guild_id, channel_id, message_id, game_id, season_id, creator_id,
                   title, capacity, status, assignment_mode, role_rating_enabled, captain_a_id,
                   captain_b_id, draft_pick_index, voice_category_id,
                   team_a_voice_channel_id, team_b_voice_channel_id, voice_cleanup_at,
                   created_at, started_at, ended_at,
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
    async def _lock_membership(conn: Any, guild_id: int, user_id: int) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"{int(guild_id)}:{int(user_id)}",
        )

    @staticmethod
    async def _other_active_match(
        conn: Any, guild_id: int, user_id: int, exclude_match_id: int | None = None
    ) -> int | None:
        return await conn.fetchval(
            """SELECT m.id
               FROM matches m
               JOIN match_participants p ON p.match_id = m.id
               WHERE m.guild_id = $1 AND p.user_id = $2
                 AND m.ended_at IS NULL AND m.status = ANY($3::text[])
                 AND ($4::bigint IS NULL OR m.id <> $4)
               ORDER BY m.id LIMIT 1""",
            guild_id, user_id, list(ACTIVE_STATUSES), exclude_match_id,
        )

    @classmethod
    async def _require_membership_available(
        cls, conn: Any, guild_id: int, user_id: int, exclude_match_id: int | None = None
    ) -> None:
        await cls._lock_membership(conn, guild_id, user_id)
        other = await cls._other_active_match(
            conn, guild_id, user_id, exclude_match_id
        )
        if other is not None:
            raise ActiveMembershipError(f"이미 {int(other)}번째 내전에 참가 중입니다.")

    @classmethod
    async def _promote_waitlist(
        cls, conn: Any, match_id: int, guild_id: int, capacity: int
    ) -> tuple[int, ...]:
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
            await cls._lock_membership(conn, guild_id, user_id)
            if await cls._other_active_match(conn, guild_id, user_id, match_id) is not None:
                break
            await conn.execute(
                """
                UPDATE match_participants
                SET membership = 'PARTICIPANT', ready_at = NULL, team = NULL,
                    assigned_role = NULL, rating_snapshot = NULL,
                    role_rating_snapshot = NULL, draft_order = NULL
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
            UPDATE match_participants
            SET ready_at = NULL, team = NULL, assigned_role = NULL,
                rating_snapshot = NULL, role_rating_snapshot = NULL, draft_order = NULL
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

    @staticmethod
    async def _validated_role_preferences(
        conn: Any,
        guild_id: int,
        game_id: int,
        season_id: int,
        user_id: int,
        enabled: bool,
        first: str | None,
        second: str | None,
        third: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        if not enabled and first is None and second is None and third is None:
            return None, None, None
        try:
            preferences = validate_preferences(first or "", second or "", third)
        except RoleAssignmentError as exc:
            raise InvalidRolePreferencesError(str(exc)) from exc
        if enabled:
            placed = await conn.fetch(
                """SELECT role FROM player_role_ratings
                   WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                     AND user_id = $4 AND role = ANY($5::text[])""",
                guild_id, game_id, season_id, user_id, list(preferences),
            )
            placed_roles = {
                str(_row_value(row, "role")) for row in placed
            }
            missing = [role for role in preferences if role not in placed_roles]
            if missing:
                raise UnplacedRoleError(
                    "배치가 필요한 라인: "
                    + ", ".join(ROLE_LABELS[role] for role in missing)
                )
        return (
            preferences[0], preferences[1],
            preferences[2] if len(preferences) == 3 else None,
        )

    @staticmethod
    async def _role_players(conn: Any, row: Any, match_id: int) -> tuple[RolePlayer, ...]:
        participants = await conn.fetch(
            """SELECT user_id, preferred_role_1, preferred_role_2, preferred_role_3,
                      joined_at, team
               FROM match_participants
               WHERE match_id = $1 AND membership = 'PARTICIPANT'
               ORDER BY user_id""",
            match_id,
        )
        user_ids = [int(_row_value(item, "user_id")) for item in participants]
        ratings = await conn.fetch(
            """SELECT user_id, role, rating FROM player_role_ratings
               WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                 AND user_id = ANY($4::bigint[]) ORDER BY user_id, role""",
            int(_row_value(row, "guild_id")), int(_row_value(row, "game_id")),
            int(_row_value(row, "season_id")), user_ids,
        )
        by_user: dict[int, dict[str, int]] = {}
        for rating in ratings:
            by_user.setdefault(int(_row_value(rating, "user_id")), {})[
                str(_row_value(rating, "role"))
            ] = int(_row_value(rating, "rating"))
        players: list[RolePlayer] = []
        for participant in participants:
            user_id = int(_row_value(participant, "user_id"))
            preferences = tuple(
                role for role in (
                    _row_value(participant, "preferred_role_1"),
                    _row_value(participant, "preferred_role_2"),
                    _row_value(participant, "preferred_role_3"),
                ) if role is not None
            )
            if len(preferences) < 2:
                raise InvalidRolePreferencesError(f"<@{user_id}>님의 라인 지망이 없습니다.")
            missing = [role for role in preferences if role not in by_user.get(user_id, {})]
            if missing:
                raise UnplacedRoleError(
                    f"<@{user_id}> 배치가 필요한 라인: "
                    + ", ".join(ROLE_LABELS[role] for role in missing)
                )
            players.append(RolePlayer(
                user_id, preferences, by_user[user_id], _row_value(participant, "joined_at")
            ))
        return tuple(players)

    @staticmethod
    async def _apply_role_assignments(
        conn: Any,
        match_id: int,
        assignments: Any,
        current: datetime,
    ) -> None:
        for assignment in assignments:
            await conn.execute(
                """UPDATE match_participants
                   SET team = $3, assigned_role = $4, role_rating_snapshot = $5,
                       rating_snapshot = NULL
                   WHERE match_id = $1 AND user_id = $2
                     AND membership = 'PARTICIPANT'""",
                match_id, assignment.user_id, assignment.team,
                assignment.role, assignment.rating,
            )
        await conn.execute(
            """UPDATE matches
               SET status = 'PLAYING', started_at = $2, ready_deadline_at = NULL,
                   recruitment_deadline_at = NULL
               WHERE id = $1""",
            match_id, current,
        )

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
        manage_guild: bool = False,
        now: datetime | None = None,
    ) -> int:
        if not manage_guild:
            raise PermissionDeniedError("서버 관리 권한이 필요합니다.")
        try:
            role = normalize_role(role)
            value = int(rating) if rating is not None else initial_role_rating(tier or "", detail)
        except (RoleAssignmentError, TypeError, ValueError) as exc:
            raise InvalidRolePreferencesError(str(exc)) from exc
        if not 0 <= value <= 10000:
            raise InvalidRolePreferencesError("MMR은 0점에서 10000점 사이여야 합니다.")
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                game = await conn.fetchrow(
                    'SELECT id, role_rating_enabled FROM games WHERE "key" = $1 FOR UPDATE',
                    game_key,
                )
                if game is None:
                    raise GameNotFoundError()
                if not bool(_row_value(game, "role_rating_enabled")):
                    raise InvalidRolePreferencesError("이 게임은 라인 MMR을 사용하지 않습니다.")
                season = await conn.fetchrow(
                    """SELECT id FROM seasons WHERE guild_id = $1 AND game_id = $2
                       AND ended_at IS NULL FOR UPDATE""",
                    guild_id, int(_row_value(game, "id")),
                )
                if season is None:
                    raise SeasonNotFoundError("활성 시즌이 없습니다.")
                await conn.execute(
                    """INSERT INTO player_role_ratings
                           (guild_id, game_id, season_id, user_id, role, rating,
                            games_played, updated_at)
                       VALUES ($1, $2, $3, $4, $5, $6, 0, $7)
                       ON CONFLICT (guild_id, game_id, season_id, user_id, role)
                       DO UPDATE SET rating = EXCLUDED.rating, updated_at = EXCLUDED.updated_at""",
                    guild_id, int(_row_value(game, "id")), int(_row_value(season, "id")),
                    user_id, role, value, current,
                )
                return value

    async def list_games(self) -> list[Game]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, "key", name, team_size, capacity, default_rating, '
                'k_factor, rating_enabled, role_rating_enabled FROM games ORDER BY id'
            )
            return [self._game(row) for row in rows]

    async def list_seasons(self, guild_id: int, game_key: str = "lol") -> list[Season]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.id, s.guild_id, s.game_id, s.name, s.started_at, s.ended_at
                FROM seasons AS s JOIN games AS g ON g.id = s.game_id
                WHERE s.guild_id = $1 AND g."key" = $2
                ORDER BY s.started_at DESC, s.id DESC
                """,
                guild_id, game_key,
            )
            return [self._season(row) for row in rows]

    async def start_season(
        self,
        guild_id: int,
        name: str,
        *,
        game_key: str = "lol",
        manage_guild: bool = False,
        now: datetime | None = None,
    ) -> Season:
        if not manage_guild:
            raise PermissionDeniedError("서버 관리 권한이 필요합니다.")
        name = name.strip()
        if not name:
            raise InvalidSeasonStateError("시즌 이름을 입력해야 합니다.")
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                game = await conn.fetchrow(
                    'SELECT id FROM games WHERE "key" = $1 FOR UPDATE', game_key
                )
                if game is None:
                    raise GameNotFoundError()
                game_id = int(_row_value(game, "id"))
                await conn.execute(
                    """UPDATE seasons SET ended_at = $3
                       WHERE guild_id = $1 AND game_id = $2 AND ended_at IS NULL""",
                    guild_id, game_id, current,
                )
                row = await conn.fetchrow(
                    """INSERT INTO seasons (guild_id, game_id, name, started_at)
                       VALUES ($1, $2, $3, $4)
                       RETURNING id, guild_id, game_id, name, started_at, ended_at""",
                    guild_id, game_id, name, current,
                )
                return self._season(row)

    async def end_season(
        self,
        guild_id: int,
        *,
        game_key: str = "lol",
        manage_guild: bool = False,
        now: datetime | None = None,
    ) -> Season:
        if not manage_guild:
            raise PermissionDeniedError("서버 관리 권한이 필요합니다.")
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                game = await conn.fetchrow(
                    'SELECT id FROM games WHERE "key" = $1 FOR UPDATE', game_key
                )
                if game is None:
                    raise GameNotFoundError()
                row = await conn.fetchrow(
                    """UPDATE seasons SET ended_at = $3
                       WHERE guild_id = $1 AND game_id = $2 AND ended_at IS NULL
                       RETURNING id, guild_id, game_id, name, started_at, ended_at""",
                    guild_id, int(_row_value(game, "id")), current,
                )
                if row is None:
                    raise InvalidSeasonStateError()
                return self._season(row)

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
        recruitment_minutes: int = DEFAULT_RECRUITMENT_MINUTES,
        now: datetime | None = None,
    ) -> Match:
        recruitment_minutes = _validate_recruitment_minutes(recruitment_minutes)
        created_at = _now(now)
        deadline = created_at + timedelta(minutes=recruitment_minutes)
        assignment_mode = assignment_mode.upper()
        if assignment_mode not in ("BALANCED", "DRAFT"):
            raise InvalidAssignmentModeError()
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await self._require_membership_available(
                        conn, guild_id, creator_id
                    )
                    game = await conn.fetchrow(
                        'SELECT id, "key", name, team_size, capacity, default_rating, '
                        'k_factor, rating_enabled, role_rating_enabled '
                        'FROM games WHERE "key" = $1 FOR UPDATE',
                        game_key,
                    )
                    if game is None:
                        raise GameNotFoundError()
                    if assignment_mode == "DRAFT" and not bool(
                        _row_value(game, "role_rating_enabled")
                    ):
                        raise InvalidAssignmentModeError(
                            "Draft 방식은 라인 MMR을 쓰는 게임에서만 사용할 수 있습니다."
                        )
                    game_capacity = int(_row_value(game, "capacity"))
                    if capacity is not None and int(capacity) != game_capacity:
                        raise InvalidCapacityError(
                            f"{_row_value(game, 'name')} 내전 정원은 {game_capacity}명입니다."
                        )
                    capacity = game_capacity
                    season = await conn.fetchrow(
                        """SELECT id FROM seasons
                           WHERE guild_id = $1 AND game_id = $2 AND ended_at IS NULL
                           FOR UPDATE""",
                        guild_id, int(_row_value(game, "id")),
                    )
                    if season is None:
                        season = await conn.fetchrow(
                            """INSERT INTO seasons (guild_id, game_id, name, started_at)
                               VALUES ($1, $2, '시즌 1', $3) RETURNING id""",
                            guild_id, int(_row_value(game, "id")), created_at,
                        )
                    preferences = await self._validated_role_preferences(
                        conn, guild_id, int(_row_value(game, "id")),
                        int(_row_value(season, "id")), creator_id,
                        bool(_row_value(game, "role_rating_enabled")),
                        preferred_role_1, preferred_role_2, preferred_role_3,
                    )
                    row = await conn.fetchrow(
                        """
                        INSERT INTO matches
                            (guild_id, channel_id, game_id, season_id, creator_id, title,
                             capacity, assignment_mode, role_rating_enabled,
                             voice_category_id, created_at, recruitment_deadline_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                        RETURNING id
                        """,
                        guild_id,
                        channel_id,
                        _row_value(game, "id"),
                        _row_value(season, "id"),
                        creator_id,
                        title,
                        capacity,
                        assignment_mode,
                        bool(_row_value(game, "role_rating_enabled")),
                        voice_category_id,
                        created_at,
                        deadline,
                    )
                    match_id = int(_row_value(row, "id"))
                    await conn.execute(
                        """
                        INSERT INTO match_participants
                            (match_id, user_id, membership, joined_at,
                             preferred_role_1, preferred_role_2, preferred_role_3)
                        VALUES ($1, $2, 'PARTICIPANT', $3, $4, $5, $6)
                        """,
                        match_id,
                        creator_id,
                        created_at,
                        *preferences,
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
        preferred_role_1: str | None = None,
        preferred_role_2: str | None = None,
        preferred_role_3: str | None = None,
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
                await self._require_membership_available(
                    conn, int(_row_value(row, "guild_id")), user_id, match_id
                )
                existing = await conn.fetchrow(
                    "SELECT 1 FROM match_participants WHERE match_id = $1 AND user_id = $2",
                    match_id,
                    user_id,
                )
                if existing is not None:
                    raise AlreadyJoinedError()
                preferences = await self._validated_role_preferences(
                    conn, int(_row_value(row, "guild_id")),
                    int(_row_value(row, "game_id")), int(_row_value(row, "season_id")),
                    user_id, bool(_row_value(row, "role_rating_enabled")),
                    preferred_role_1, preferred_role_2, preferred_role_3,
                )
                count = await self._participant_count(conn, match_id)
                membership = MEMBER if count < int(_row_value(row, "capacity")) else WAITLIST
                try:
                    await conn.execute(
                        """
                        INSERT INTO match_participants
                            (match_id, user_id, membership, joined_at,
                             preferred_role_1, preferred_role_2, preferred_role_3)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        match_id,
                        user_id,
                        membership,
                        current,
                        *preferences,
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

    async def update_preferences(
        self,
        match_id: int,
        user_id: int,
        preferred_role_1: str,
        preferred_role_2: str,
        preferred_role_3: str | None = None,
        *,
        now: datetime | None = None,
        ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT_SECONDS,
        default_recruitment_minutes: int = DEFAULT_RECRUITMENT_MINUTES,
    ) -> Match:
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                self._require_state(row, RECRUITING, READY_CHECK)
                participant = await conn.fetchrow(
                    "SELECT 1 FROM match_participants WHERE match_id = $1 AND user_id = $2",
                    match_id, user_id,
                )
                if participant is None:
                    raise NotParticipantError()
                preferences = await self._validated_role_preferences(
                    conn, int(_row_value(row, "guild_id")), int(_row_value(row, "game_id")),
                    int(_row_value(row, "season_id")), user_id,
                    bool(_row_value(row, "role_rating_enabled")),
                    preferred_role_1, preferred_role_2, preferred_role_3,
                )
                await conn.execute(
                    """UPDATE match_participants
                       SET preferred_role_1 = $3, preferred_role_2 = $4,
                           preferred_role_3 = $5
                       WHERE match_id = $1 AND user_id = $2""",
                    match_id, user_id, *preferences,
                )
                if _row_value(row, "status") == READY_CHECK:
                    await self._reset_ready_roster(
                        conn, row, match_id, current,
                        ready_timeout_seconds, default_recruitment_minutes,
                    )
                return await self._fetch_match(conn, match_id)

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
                    promoted = await self._promote_waitlist(
                        conn, match_id, int(_row_value(row, "guild_id")),
                        int(_row_value(row, "capacity")),
                    )
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
                    raise MatchFullError(
                        f"정확히 {capacity}명이 참가해야 준비 확인을 시작할 수 있습니다."
                    )
                await conn.execute(
                    """
                    UPDATE match_participants
                    SET ready_at = NULL, team = NULL, assigned_role = NULL,
                        rating_snapshot = NULL, role_rating_snapshot = NULL,
                        draft_order = NULL
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
                    game = await conn.fetchrow(
                        """SELECT team_size, capacity, default_rating, role_rating_enabled
                           FROM games WHERE id = $1""",
                        int(_row_value(row, "game_id")),
                    )
                    team_size = int(_row_value(game, "team_size"))
                    if int(_row_value(row, "capacity")) != team_size * 2:
                        raise InvalidCapacityError()
                    if bool(_row_value(row, "role_rating_enabled")):
                        if int(_row_value(row, "capacity")) != 10 or team_size != 5:
                            raise InvalidCapacityError("라인 배정 게임은 현재 5대5만 지원합니다.")
                        players = await self._role_players(conn, row, match_id)
                        try:
                            if _row_value(row, "assignment_mode") == "DRAFT":
                                captain_a, captain_b = select_captains(players)
                                teams = {captain_a: "A", captain_b: "B"}
                                if not draft_completion_possible(players, teams, match_id):
                                    raise RoleAssignmentError(
                                        "주장 배치 후 유효한 라인 조합을 만들 수 없습니다."
                                    )
                                await conn.execute(
                                    """UPDATE match_participants
                                       SET team = NULL, assigned_role = NULL,
                                           role_rating_snapshot = NULL, draft_order = NULL
                                       WHERE match_id = $1 AND membership = 'PARTICIPANT'""",
                                    match_id,
                                )
                                await conn.execute(
                                    """UPDATE match_participants SET team = 'A'
                                       WHERE match_id = $1 AND user_id = $2""",
                                    match_id, captain_a,
                                )
                                await conn.execute(
                                    """UPDATE match_participants SET team = 'B'
                                       WHERE match_id = $1 AND user_id = $2""",
                                    match_id, captain_b,
                                )
                                await conn.execute(
                                    """UPDATE matches SET status = 'DRAFTING',
                                           captain_a_id = $2, captain_b_id = $3,
                                           draft_pick_index = 0, ready_deadline_at = NULL,
                                           recruitment_deadline_at = NULL
                                       WHERE id = $1""",
                                    match_id, captain_a, captain_b,
                                )
                            else:
                                assignments = balanced_assignment(players, match_id)
                                await self._apply_role_assignments(
                                    conn, match_id, assignments, current
                                )
                                started = True
                        except RoleAssignmentError as exc:
                            raise RoleAssignmentImpossibleError(str(exc)) from exc
                    else:
                        rows = await conn.fetch(
                            """SELECT user_id FROM match_participants
                               WHERE match_id = $1 AND membership = 'PARTICIPANT'
                               ORDER BY joined_at, id""",
                            match_id,
                        )
                        user_ids = [int(_row_value(item, "user_id")) for item in rows]
                        if len(user_ids) != int(_row_value(row, "capacity")):
                            raise InvalidMatchStateError()
                        random.shuffle(user_ids)
                        ratings = await conn.fetch(
                            """SELECT user_id, rating FROM player_ratings
                               WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                                 AND user_id = ANY($4::bigint[])""",
                            int(_row_value(row, "guild_id")),
                            int(_row_value(row, "game_id")),
                            int(_row_value(row, "season_id")), user_ids,
                        )
                        rating_by_user = {
                            int(_row_value(item, "user_id")): int(_row_value(item, "rating"))
                            for item in ratings
                        }
                        default_rating = int(_row_value(game, "default_rating"))
                        for team, members in (("A", user_ids[:team_size]), ("B", user_ids[team_size:])):
                            for member_id in members:
                                await conn.execute(
                                    """UPDATE match_participants
                                       SET team = $3, rating_snapshot = $4
                                       WHERE match_id = $1 AND user_id = $2
                                         AND membership = 'PARTICIPANT'""",
                                    match_id, member_id, team,
                                    rating_by_user.get(member_id, default_rating),
                                )
                        await conn.execute(
                            """UPDATE matches SET status = 'PLAYING', started_at = $2,
                                   ready_deadline_at = NULL, recruitment_deadline_at = NULL
                               WHERE id = $1 AND status = 'READY_CHECK'""",
                            match_id, current,
                        )
                        started = True
                match = await self._fetch_match(conn, match_id)
                return replace(match, ready=not was_ready, started=started)

    async def draft_pick(
        self,
        match_id: int,
        actor_id: int,
        target_user_id: int,
        *,
        now: datetime | None = None,
    ) -> Match:
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                self._require_state(row, DRAFTING)
                index = int(_row_value(row, "draft_pick_index"))
                if index >= len(DRAFT_TEAMS):
                    raise InvalidDraftPickError("지명이 이미 끝났습니다.")
                team = DRAFT_TEAMS[index]
                captain_id = int(_row_value(row, "captain_a_id" if team == "A" else "captain_b_id"))
                if int(actor_id) != captain_id:
                    raise PermissionDeniedError("현재 차례의 주장만 지명할 수 있습니다.")
                target = await conn.fetchrow(
                    """SELECT team FROM match_participants
                       WHERE match_id = $1 AND user_id = $2
                         AND membership = 'PARTICIPANT' FOR UPDATE""",
                    match_id, target_user_id,
                )
                if target is None or _row_value(target, "team") is not None:
                    raise InvalidDraftPickError()
                await conn.execute(
                    """UPDATE match_participants SET team = $3, draft_order = $4
                       WHERE match_id = $1 AND user_id = $2""",
                    match_id, target_user_id, team, index + 1,
                )
                players = await self._role_players(conn, row, match_id)
                teams = {
                    int(_row_value(item, "user_id")): str(_row_value(item, "team"))
                    for item in await conn.fetch(
                        """SELECT user_id, team FROM match_participants
                           WHERE match_id = $1 AND membership = 'PARTICIPANT'
                             AND team IS NOT NULL ORDER BY user_id""",
                        match_id,
                    )
                }
                if not draft_completion_possible(players, teams, match_id):
                    raise InvalidDraftPickError(
                        "이 사용자를 지명하면 유효한 라인 배정을 만들 수 없습니다."
                    )
                started = False
                if index + 1 == len(DRAFT_TEAMS):
                    try:
                        assignments = finalize_draft(players, teams, match_id)
                    except RoleAssignmentError as exc:
                        raise InvalidDraftPickError(str(exc)) from exc
                    await self._apply_role_assignments(
                        conn, match_id, assignments, current
                    )
                    await conn.execute(
                        "UPDATE matches SET draft_pick_index = $2 WHERE id = $1",
                        match_id, len(DRAFT_TEAMS),
                    )
                    started = True
                else:
                    await conn.execute(
                        "UPDATE matches SET draft_pick_index = $2 WHERE id = $1",
                        match_id, index + 1,
                    )
                return replace(await self._fetch_match(conn, match_id), started=started)

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
                    promoted = await self._promote_waitlist(
                        conn, match_id, int(_row_value(row, "guild_id")),
                        int(_row_value(row, "capacity")),
                    )
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
        voice_cleanup_delay_seconds: int = 600,
    ) -> Match:
        current = _now(now)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                self._require_manager(row, actor_id, manage_guild)
                self._require_state(row, RECRUITING, READY_CHECK, DRAFTING, PLAYING)
                await conn.execute(
                    """
                    UPDATE matches
                    SET status = 'CANCELLED', ended_at = $2,
                        ready_deadline_at = NULL, cancel_reason = $3,
                        voice_cleanup_at = CASE
                            WHEN team_a_voice_channel_id IS NOT NULL
                              OR team_b_voice_channel_id IS NOT NULL
                            THEN $2 + ($4 * interval '1 second')
                            ELSE NULL END
                    WHERE id = $1
                    """,
                    match_id, current, reason or "직접 취소",
                    int(voice_cleanup_delay_seconds),
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
        voice_cleanup_delay_seconds: int = 600,
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
                game = await conn.fetchrow(
                    """SELECT default_rating, k_factor, rating_enabled, role_rating_enabled
                       FROM games WHERE id = $1""",
                    int(_row_value(row, "game_id")),
                )
                participants = await conn.fetch(
                    """SELECT user_id, team, rating_snapshot, assigned_role,
                              role_rating_snapshot
                       FROM match_participants
                       WHERE match_id = $1 AND membership = 'PARTICIPANT'
                         AND team IN ('A', 'B')
                       ORDER BY user_id""",
                    match_id,
                )
                if len(participants) != int(_row_value(row, "capacity")):
                    raise InvalidMatchStateError("팀 배정 정보가 올바르지 않습니다.")
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
                if bool(_row_value(row, "role_rating_enabled")):
                    user_ids = sorted(int(_row_value(item, "user_id")) for item in participants)
                    if any(
                        _row_value(item, "assigned_role") not in ROLES
                        or _row_value(item, "role_rating_snapshot") is None
                        for item in participants
                    ):
                        raise InvalidMatchStateError("라인 배정 정보가 올바르지 않습니다.")
                    rating_rows = await conn.fetch(
                        """SELECT user_id, role, rating FROM player_role_ratings
                           WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                             AND user_id = ANY($4::bigint[])
                           ORDER BY user_id, role FOR UPDATE""",
                        int(_row_value(row, "guild_id")), int(_row_value(row, "game_id")),
                        int(_row_value(row, "season_id")), user_ids,
                    )
                    current_ratings = {
                        (int(_row_value(item, "user_id")), str(_row_value(item, "role"))):
                            int(_row_value(item, "rating"))
                        for item in rating_rows
                    }
                    by_team: dict[str, list[int]] = {"A": [], "B": []}
                    participant_data: dict[int, tuple[str, str]] = {}
                    for participant in participants:
                        participant_id = int(_row_value(participant, "user_id"))
                        team = str(_row_value(participant, "team"))
                        role = str(_row_value(participant, "assigned_role"))
                        if (participant_id, role) not in current_ratings:
                            raise UnplacedRoleError(
                                f"<@{participant_id}>님의 {ROLE_LABELS[role]} MMR이 없습니다."
                            )
                        participant_data[participant_id] = team, role
                        by_team[team].append(int(_row_value(participant, "role_rating_snapshot")))
                    if len(by_team["A"]) != len(by_team["B"]) or not by_team["A"]:
                        raise InvalidMatchStateError("팀 인원이 올바르지 않습니다.")
                    delta_a, delta_b = calculate_rating_delta(
                        sum(by_team["A"]) / len(by_team["A"]),
                        sum(by_team["B"]) / len(by_team["B"]),
                        winner_team,
                        int(_row_value(game, "k_factor")),
                    )
                    for participant_id in user_ids:
                        team, role = participant_data[participant_id]
                        before = current_ratings[(participant_id, role)]
                        delta = delta_a if team == "A" else delta_b
                        after = before + delta
                        await conn.execute(
                            """UPDATE player_role_ratings
                               SET rating = $6, games_played = games_played + 1,
                                   updated_at = $7
                               WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                                 AND user_id = $4 AND role = $5""",
                            int(_row_value(row, "guild_id")), int(_row_value(row, "game_id")),
                            int(_row_value(row, "season_id")), participant_id,
                            role, after, current,
                        )
                        await conn.execute(
                            """INSERT INTO role_rating_history
                                   (match_id, user_id, role, rating_before,
                                    rating_delta, rating_after, created_at)
                               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                            match_id, participant_id, role, before, delta, after, current,
                        )
                elif bool(_row_value(game, "rating_enabled")):
                    default_rating = int(_row_value(game, "default_rating"))
                    user_ids = sorted(int(_row_value(item, "user_id")) for item in participants)
                    for user_id in user_ids:
                        await conn.execute(
                            """INSERT INTO player_ratings
                                   (guild_id, game_id, season_id, user_id, rating, games_played, updated_at)
                               VALUES ($1, $2, $3, $4, $5, 0, $6)
                               ON CONFLICT (guild_id, game_id, season_id, user_id) DO NOTHING""",
                            int(_row_value(row, "guild_id")),
                            int(_row_value(row, "game_id")),
                            int(_row_value(row, "season_id")),
                            user_id, default_rating, current,
                        )
                    rating_rows = await conn.fetch(
                        """SELECT user_id, rating FROM player_ratings
                           WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                             AND user_id = ANY($4::bigint[])
                           ORDER BY user_id FOR UPDATE""",
                        int(_row_value(row, "guild_id")),
                        int(_row_value(row, "game_id")),
                        int(_row_value(row, "season_id")),
                        user_ids,
                    )
                    current_ratings = {
                        int(_row_value(item, "user_id")): int(_row_value(item, "rating"))
                        for item in rating_rows
                    }
                    by_team: dict[str, list[int]] = {"A": [], "B": []}
                    participant_team: dict[int, str] = {}
                    for participant in participants:
                        user_id = int(_row_value(participant, "user_id"))
                        team = str(_row_value(participant, "team"))
                        participant_team[user_id] = team
                        snapshot = _row_value(participant, "rating_snapshot")
                        by_team[team].append(
                            current_ratings[user_id] if snapshot is None else int(snapshot)
                        )
                    if not by_team["A"] or len(by_team["A"]) != len(by_team["B"]):
                        raise InvalidMatchStateError("팀 인원이 올바르지 않습니다.")
                    delta_a, delta_b = calculate_rating_delta(
                        sum(by_team["A"]) / len(by_team["A"]),
                        sum(by_team["B"]) / len(by_team["B"]),
                        winner_team,
                        int(_row_value(game, "k_factor")),
                    )
                    for user_id in user_ids:
                        before = current_ratings[user_id]
                        delta = delta_a if participant_team[user_id] == "A" else delta_b
                        after = before + delta
                        await conn.execute(
                            """UPDATE player_ratings
                               SET rating = $5, games_played = games_played + 1, updated_at = $6
                               WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                                 AND user_id = $4""",
                            int(_row_value(row, "guild_id")),
                            int(_row_value(row, "game_id")),
                            int(_row_value(row, "season_id")),
                            user_id, after, current,
                        )
                        await conn.execute(
                            """INSERT INTO rating_history
                                   (match_id, user_id, rating_before, rating_delta,
                                    rating_after, created_at)
                               VALUES ($1, $2, $3, $4, $5, $6)""",
                            match_id, user_id, before, delta, after, current,
                        )
                await conn.execute(
                    """
                    UPDATE matches SET status = 'FINISHED', ended_at = $2,
                        ready_deadline_at = NULL,
                        voice_cleanup_at = CASE
                            WHEN team_a_voice_channel_id IS NOT NULL
                              OR team_b_voice_channel_id IS NOT NULL
                            THEN $2 + ($3 * interval '1 second')
                            ELSE NULL END
                    WHERE id = $1
                    """,
                    match_id, current, int(voice_cleanup_delay_seconds),
                )
                return await self._fetch_match(conn, match_id)

    async def stats(
        self,
        guild_id: int,
        user_id: int,
        *,
        game_key: str = "lol",
        season_id: int | None = None,
    ) -> MatchStats:
        async with self.pool.acquire() as conn:
            game = await conn.fetchrow(
                'SELECT id, role_rating_enabled FROM games WHERE "key" = $1', game_key
            )
            if game is None:
                raise GameNotFoundError()
            game_id = int(_row_value(game, "id"))
            effective_season_id = season_id
            if bool(_row_value(game, "role_rating_enabled")) and effective_season_id is None:
                effective_season_id = await conn.fetchval(
                    """SELECT id FROM seasons WHERE guild_id = $1 AND game_id = $2
                       AND ended_at IS NULL""",
                    guild_id, game_id,
                )
                if effective_season_id is None:
                    return MatchStats(
                        0, 0, 0, 0.0, 0,
                        tuple(RoleStats(role, 0, False, 0, 0, 0, 0.0) for role in ROLES),
                        0,
                    )
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
                  AND m.game_id = $3
                  AND ($4::bigint IS NULL OR m.season_id = $4)
                """,
                guild_id, user_id, game_id, effective_season_id,
            )
            games = int(_row_value(row, "games", 0) or 0)
            wins = int(_row_value(row, "wins", 0) or 0)
            losses = games - wins
            if not bool(_row_value(game, "role_rating_enabled")) or effective_season_id is None:
                return MatchStats(games, wins, losses, wins / games if games else 0.0)
            rating_rows = await conn.fetch(
                """SELECT role, rating, games_played FROM player_role_ratings
                   WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                     AND user_id = $4 ORDER BY role""",
                guild_id, game_id, int(effective_season_id), user_id,
            )
            ratings = {
                str(_row_value(item, "role")): (
                    int(_row_value(item, "rating")), int(_row_value(item, "games_played"))
                ) for item in rating_rows
            }
            result_rows = await conn.fetch(
                """SELECT p.assigned_role AS role, count(*) AS games,
                          count(*) FILTER (WHERE p.team = r.winner_team) AS wins
                   FROM matches m
                   JOIN match_participants p ON p.match_id = m.id
                   JOIN match_results r ON r.match_id = m.id
                   WHERE m.guild_id = $1 AND m.game_id = $2 AND m.season_id = $3
                     AND m.status = 'FINISHED' AND p.user_id = $4
                     AND p.membership = 'PARTICIPANT' AND p.assigned_role IS NOT NULL
                   GROUP BY p.assigned_role""",
                guild_id, game_id, int(effective_season_id), user_id,
            )
            results = {
                str(_row_value(item, "role")): (
                    int(_row_value(item, "games")), int(_row_value(item, "wins"))
                ) for item in result_rows
            }
            role_stats = []
            for role in ROLES:
                rating, _rating_games = ratings.get(role, (0, 0))
                role_games, role_wins = results.get(role, (0, 0))
                role_stats.append(RoleStats(
                    role, rating, role in ratings, role_games, role_wins,
                    role_games - role_wins,
                    role_wins / role_games if role_games else 0.0,
                ))
            highest = await conn.fetchval(
                """SELECT max(value) FROM (
                       SELECT rating AS value FROM player_role_ratings
                       WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                         AND user_id = $4
                       UNION ALL
                       SELECT greatest(rating_before, rating_after) FROM role_rating_history h
                       JOIN matches m ON m.id = h.match_id
                       WHERE m.guild_id = $1 AND m.game_id = $2 AND m.season_id = $3
                         AND h.user_id = $4
                   ) AS rating_values""",
                guild_id, game_id, int(effective_season_id), user_id,
            )
            return MatchStats(
                games, wins, losses, wins / games if games else 0.0,
                positive_rating_average(rating for rating, _ in ratings.values()),
                tuple(role_stats), int(highest or 0),
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
        if not 1 <= int(limit) <= 25:
            raise InvalidRankingLimitError()
        async with self.pool.acquire() as conn:
            game = await conn.fetchrow(
                'SELECT id, role_rating_enabled FROM games WHERE "key" = $1', game_key
            )
            if game is None:
                raise GameNotFoundError()
            game_id = int(_row_value(game, "id"))
            if season_id is None:
                season = await conn.fetchrow(
                    """SELECT id, guild_id, game_id, name, started_at, ended_at
                       FROM seasons WHERE guild_id = $1 AND game_id = $2
                         AND ended_at IS NULL""",
                    guild_id, game_id,
                )
            else:
                season = await conn.fetchrow(
                    """SELECT id, guild_id, game_id, name, started_at, ended_at
                       FROM seasons WHERE id = $1 AND guild_id = $2 AND game_id = $3""",
                    season_id, guild_id, game_id,
                )
            if season is None:
                raise SeasonNotFoundError()
            if bool(_row_value(game, "role_rating_enabled")):
                if role is not None:
                    try:
                        role = normalize_role(role)
                    except RoleAssignmentError as exc:
                        raise InvalidRolePreferencesError(str(exc)) from exc
                    rows = await conn.fetch(
                        """SELECT user_id, rating, games_played FROM player_role_ratings
                           WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                             AND role = $4
                           ORDER BY rating DESC, games_played DESC, user_id ASC LIMIT $5""",
                        guild_id, game_id, int(_row_value(season, "id")), role, int(limit),
                    )
                else:
                    rows = await conn.fetch(
                        """SELECT user_id, floor(avg(rating))::integer AS rating,
                                  sum(games_played)::integer AS games_played
                           FROM player_role_ratings
                           WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                             AND rating > 0
                           GROUP BY user_id
                           ORDER BY rating DESC, games_played DESC, user_id ASC LIMIT $4""",
                        guild_id, game_id, int(_row_value(season, "id")), int(limit),
                    )
            else:
                if role is not None:
                    raise InvalidRolePreferencesError("이 게임은 라인 랭킹을 사용하지 않습니다.")
                rows = await conn.fetch(
                    """SELECT user_id, rating, games_played FROM player_ratings
                       WHERE guild_id = $1 AND game_id = $2 AND season_id = $3
                       ORDER BY rating DESC, games_played DESC, user_id ASC LIMIT $4""",
                    guild_id, game_id, int(_row_value(season, "id")), int(limit),
                )
            return [
                RankingEntry(
                    int(_row_value(item, "user_id")),
                    int(_row_value(item, "rating")),
                    int(_row_value(item, "games_played")),
                    role,
                )
                for item in rows
            ]

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
                                    SET ready_at = NULL, team = NULL, assigned_role = NULL,
                                        rating_snapshot = NULL, role_rating_snapshot = NULL,
                                        draft_order = NULL
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
                            conn, match_id, int(_row_value(row, "guild_id")),
                            int(_row_value(row, "capacity")),
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

    async def set_voice_channel_id(
        self, match_id: int, team: str, channel_id: int, *, replace_missing: bool = False
    ) -> Match:
        if team not in ("A", "B"):
            raise InvalidMatchStateError("보이스 팀은 A 또는 B여야 합니다.")
        column = "team_a_voice_channel_id" if team == "A" else "team_b_voice_channel_id"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                self._require_state(row, PLAYING)
                current = _row_value(row, column)
                if current is not None and int(current) != int(channel_id) and not replace_missing:
                    raise InvalidMatchStateError(
                        f"{team}팀 보이스 채널이 이미 다른 ID로 저장되어 있습니다."
                    )
                await conn.execute(
                    f"UPDATE matches SET {column} = $2 WHERE id = $1",
                    match_id, channel_id,
                )
                return await self._fetch_match(conn, match_id)

    async def list_due_voice_cleanups(self, now: datetime | None = None) -> list[Match]:
        current = _now(now)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id FROM matches
                   WHERE voice_cleanup_at IS NOT NULL AND voice_cleanup_at <= $1
                   ORDER BY voice_cleanup_at, id""",
                current,
            )
            return [
                await self._fetch_match(conn, int(_row_value(row, "id")))
                for row in rows
            ]

    async def record_voice_cleanup(
        self,
        match_id: int,
        *,
        clear_team_a: bool,
        clear_team_b: bool,
        retry_at: datetime | None,
    ) -> Match:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                team_a = None if clear_team_a else _row_value(row, "team_a_voice_channel_id")
                team_b = None if clear_team_b else _row_value(row, "team_b_voice_channel_id")
                cleanup_at = retry_at if team_a is not None or team_b is not None else None
                await conn.execute(
                    """UPDATE matches
                       SET team_a_voice_channel_id = $2,
                           team_b_voice_channel_id = $3,
                           voice_cleanup_at = $4
                       WHERE id = $1""",
                    match_id, team_a, team_b, cleanup_at,
                )
                return await self._fetch_match(conn, match_id)

    async def update_message_id(self, match_id: int, message_id: int | None) -> Match:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._locked_match(conn, match_id)
                await conn.execute(
                    "UPDATE matches SET message_id = $2 WHERE id = $1", match_id, message_id
                )
                return await self._fetch_match(conn, match_id)

    async def cancel_missing_message(
        self, match_id: int, *, voice_cleanup_delay_seconds: int = 600
    ) -> Match:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await self._locked_match(conn, match_id)
                status = _row_value(row, "status")
                if _row_value(row, "ended_at") is None and status in ACTIVE_STATUSES:
                    await conn.execute(
                        """
                        UPDATE matches SET status = 'CANCELLED', ended_at = now(),
                            ready_deadline_at = NULL, cancel_reason = '모집 메시지를 찾을 수 없음',
                            voice_cleanup_at = CASE
                                WHEN team_a_voice_channel_id IS NOT NULL
                                  OR team_b_voice_channel_id IS NOT NULL
                                THEN now() + ($2 * interval '1 second')
                                ELSE NULL END
                        WHERE id = $1 AND ended_at IS NULL
                        """,
                        match_id, int(voice_cleanup_delay_seconds),
                    )
                return await self._fetch_match(conn, match_id)
