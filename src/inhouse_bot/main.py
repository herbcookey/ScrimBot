"""봇 시작, 재시작 복구, 만료 내전 폴링 처리."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import commands

from .config import Settings, load_settings
from .db import close_pool, create_pool
from .discord.commands import add_match_commands
from .discord.renderer import render_match
from .discord.views import MatchView, SAFE_MENTIONS
from .discord.voice import (
    cleanup_match_voice_channels,
    close_empty_match_voice_channel,
    close_empty_match_voice_channels,
    ensure_match_voice_channels,
)
from .services.matches import MatchService

logger = logging.getLogger(__name__)


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


async def _safe_send(channel: Any, content: str, **kwargs: Any) -> Any:
    try:
        return await channel.send(content, allowed_mentions=SAFE_MENTIONS, **kwargs)
    except TypeError:
        return await channel.send(content, **kwargs)


async def _safe_edit(message: Any, **kwargs: Any) -> Any:
    try:
        return await message.edit(allowed_mentions=SAFE_MENTIONS, **kwargs)
    except TypeError:
        return await message.edit(**kwargs)


class InhouseBot(commands.Bot):
    """길드와 음성 상태 인텐트만 쓰는 Discord 봇."""

    POLL_SECONDS = 12

    def __init__(
        self,
        settings: Settings,
        *,
        pool: Any | None = None,
        service: MatchService | None = None,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.pool = pool
        self.service = service
        self._recovery_lock = asyncio.Lock()
        self._recovered = False
        self._poll_task: asyncio.Task[Any] | None = None

    async def setup_hook(self) -> None:
        if self.service is None:
            if self.pool is None:
                self.pool = await create_pool(self.settings.database_url)
                logger.info("DB 연결 풀 생성 완료")
            self.service = MatchService(
                self.pool,
                bot_owner_id=self.settings.bot_owner_id,
                ready_timeout_seconds=self.settings.ready_timeout_seconds,
                draft_timeout_seconds=self.settings.draft_timeout_seconds,
                default_recruitment_minutes=self.settings.default_recruitment_minutes,
                reminder_before_seconds=self.settings.reminder_before_seconds,
                voice_cleanup_delay_seconds=self.settings.voice_cleanup_delay_seconds,
            )
        add_match_commands(
            self.tree,
            self.service,
            self.settings.discord_guild_id,
            team_a_voice_channel_id=self.settings.team_a_voice_channel_id,
            team_b_voice_channel_id=self.settings.team_b_voice_channel_id,
            inhouse_voice_category_id=self.settings.inhouse_voice_category_id,
        )
        guild = discord.Object(id=self.settings.discord_guild_id)
        await self.tree.sync(guild=guild)
        logger.info("길드 명령 동기화 완료", extra={"guild_id": self.settings.discord_guild_id})

    async def on_ready(self) -> None:
        logger.info("디스코드 로그인: %s", self.user)
        await self.recover_active_matches()
        self._ensure_poll_task()

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        before_channel = before.channel
        if (
            self.service is None
            or before_channel is None
            or getattr(before_channel, "id", None) == getattr(after.channel, "id", None)
            or len(getattr(before_channel, "members", ()) or ()) != 0
        ):
            return
        try:
            await close_empty_match_voice_channel(self.service, before_channel)
        except Exception:
            logger.exception(
                "빈 내전 보이스 채널 처리 실패",
                extra={
                    "channel_id": getattr(before_channel, "id", None),
                    "user_id": getattr(member, "id", None),
                },
            )

    def _ensure_poll_task(self) -> None:
        task = self._poll_task
        if task is None or task.done():
            self._poll_task = asyncio.create_task(self._poll_loop(), name="inhouse-match-poller")

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.process_due_matches()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 잠깐 난 DB나 Discord 오류 때문에 폴링을 끝내면 안 된다.
                logger.exception("만료 내전 폴링 실패")
            try:
                await asyncio.sleep(self.POLL_SECONDS)
            except asyncio.CancelledError:
                raise

    async def process_due_matches(self) -> list[Any]:
        """만료 건을 DB에 반영한 다음 Discord 작업을 한다."""

        if self.service is None:
            return []
        events = await self.service.process_due_matches()
        for event in events:
            try:
                await self._handle_due_event(event)
            except Exception:
                logger.exception("만료 내전 알림 전송 실패", extra={"match_id": _get(event, "match_id")})
        await self._repair_panel_less_matches()
        await self._retry_match_voice_channels()
        await self.process_voice_cleanups()
        return events

    def _match_view(self, match: Any, *, disabled: bool = False) -> MatchView:
        """Construct a view with persisted guild/mode metadata.

        Recovery can run after a process restart, so the view must not infer
        authorization from the channel or from stale constructor defaults.
        """

        return MatchView(
            self.service,
            int(_get(match, "id")),
            status=str(_get(match, "status", "")),
            disabled=disabled,
            guild_id=_get(match, "guild_id"),
            role_rating_enabled=_get(match, "role_rating_enabled"),
            team_a_voice_channel_id=self.settings.team_a_voice_channel_id,
            team_b_voice_channel_id=self.settings.team_b_voice_channel_id,
        )

    def _is_match_panel(self, message: Any, match_id: int) -> bool:
        """Recognize a previously posted panel without relying on its text."""

        author_id = _get(_get(message, "author"), "id")
        application_id = _get(message, "application_id")
        bot_user_id = _get(self.user, "id")
        bot_application_id = getattr(self, "application_id", None)
        is_bot_author = bool(_get(_get(message, "author"), "bot", False))
        own_bot = is_bot_author and bot_user_id is not None and author_id == int(bot_user_id)
        own_application = (
            is_bot_author
            and bot_application_id is not None
            and application_id == int(bot_application_id)
        )
        if not own_bot and not own_application:
            return False
        target = f"match:{int(match_id)}:join"
        for row in getattr(message, "components", ()) or ():
            children = getattr(row, "children", None)
            for component in children if children is not None else (row,):
                if getattr(component, "custom_id", None) == target:
                    return True
        return False

    async def _find_existing_panel(self, channel: Any, match_id: int) -> Any | None:
        history = getattr(channel, "history", None)
        if not callable(history):
            return None
        try:
            stream = history(limit=50)
            if hasattr(stream, "__await__"):
                stream = await stream
            if hasattr(stream, "__aiter__"):
                async for message in stream:
                    if self._is_match_panel(message, match_id):
                        return message
            else:
                for message in stream or ():
                    if self._is_match_panel(message, match_id):
                        return message
        except (TypeError, AttributeError):
            # Minimal test doubles and channels without history support are
            # handled by the normal send path.
            return None
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("내전 패널 기록 조회 실패", extra={"match_id": match_id})
        return None

    async def _attach_panel_id(
        self, match_id: int, message_id: int, *, expected_old: int | None = None
    ) -> Any:
        attach = getattr(self.service, "attach_message_id", None)
        if callable(attach):
            return await attach(match_id, message_id, expected_old=expected_old)
        # Compatibility for a service object created before the recovery API.
        return await self.service.update_message_id(match_id, message_id)

    async def _ensure_match_panel(
        self, match: Any, *, refresh: bool = True
    ) -> Any | None:
        """Find, repair, or post the panel for one active DB match.

        The database remains the source of truth.  A failed Discord call is
        deliberately allowed to bubble to the caller so the active row stays
        panel-less and can be retried later rather than being cancelled.
        """

        if self.service is None:
            return None
        match_id = int(_get(match, "id", _get(match, "match_id", 0)))
        latest = await self.service.get_match(match_id)
        if latest is None:
            return None
        channel_id = _get(latest, "channel_id")
        if channel_id is None:
            return latest
        channel = await self._channel_for(int(channel_id))
        if channel is None:
            return latest
        message_id = _get(latest, "message_id")
        message = None
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                message = None
            except (discord.Forbidden, discord.HTTPException):
                raise
            # A stale/non-NULL ID can point to an unrelated message after a
            # channel cleanup or manual edit.  Never overwrite that message;
            # search for this match's fingerprint and publish a replacement.
            if message is not None and not self._is_match_panel(message, match_id):
                message = None
        if message is None:
            # A crash can happen after Discord accepted a send but before the
            # ID was committed. Adopt an existing component-bearing panel
            # before posting another one.
            message = await self._find_existing_panel(channel, match_id)
            if message is not None:
                await self._attach_panel_id(
                    match_id,
                    int(_get(message, "id")),
                    expected_old=int(message_id) if message_id else None,
                )
                latest = await self.service.get_match(match_id) or latest
        if message is None:
            latest = await self.service.get_match(match_id) or latest
            message = await _safe_send(
                channel,
                "",
                embed=render_match(latest),
                view=self._match_view(
                    latest,
                    disabled=str(_get(latest, "status", "")) in {"FINISHED", "CANCELLED"},
                ),
            )
            await self._attach_panel_id(
                match_id,
                int(_get(message, "id")),
                expected_old=int(message_id) if message_id else None,
            )
            latest = await self.service.get_match(match_id) or latest
        elif refresh:
            latest = await self.service.get_match(match_id) or latest
            await _safe_edit(
                message,
                embed=render_match(latest),
                view=self._match_view(
                    latest,
                    disabled=str(_get(latest, "status", "")) in {"FINISHED", "CANCELLED"},
                ),
            )
        return latest

    async def _repair_panel_less_matches(self) -> None:
        """Retry active rows whose panel is missing or points at stale data."""

        if self.service is None:
            return
        for match in await self.service.list_active():
            try:
                # Verify known IDs too: Discord can delete a panel while the
                # database still contains its ID.  A valid panel needs no
                # edit here; ordinary state transitions refresh it elsewhere.
                await self._ensure_match_panel(match, refresh=False)
            except Exception:
                logger.exception("패널 없는 내전 복구 실패", extra={"match_id": _get(match, "id")})

    async def process_voice_cleanups(self) -> None:
        if self.service is None:
            return
        for match in await self.service.list_due_voice_cleanups():
            guild = self.get_guild(int(_get(match, "guild_id")))
            try:
                await cleanup_match_voice_channels(self.service, guild, match)
            except Exception:
                logger.exception("내전 보이스 정리 처리 실패", extra={"match_id": _get(match, "id")})

    async def _retry_match_voice_channels(self) -> None:
        if self.service is None:
            return
        pending = getattr(self.service, "voice_retry_ids", None)
        clear = getattr(self.service, "clear_voice_retry", None)
        if not callable(pending) or not callable(clear):
            return
        for match_id in pending():
            try:
                match = await self.service.get_match(match_id)
                if match is None or str(_get(match, "status", "")) != "PLAYING":
                    clear(match_id)
                    continue
                guild = self.get_guild(int(_get(match, "guild_id")))
                if guild is None:
                    continue
                summary = await ensure_match_voice_channels(
                    self.service,
                    guild,
                    match,
                    self.settings.team_a_voice_channel_id,
                    self.settings.team_b_voice_channel_id,
                )
                if summary.error:
                    logger.warning(
                        "진행 중 내전 보이스 복구 재시도 실패: %s",
                        summary.error,
                        extra={"match_id": match_id},
                    )
                else:
                    clear(match_id)
                    await self._refresh_match(match)
            except Exception:
                logger.exception("진행 중 내전 보이스 복구 재시도 오류", extra={"match_id": match_id})

    def _set_voice_retry(self, match_id: int, *, pending: bool) -> None:
        if self.service is None:
            return
        update = getattr(
            self.service,
            "retry_voice_for" if pending else "clear_voice_retry",
            None,
        )
        if callable(update):
            update(match_id)

    async def _channel_for(self, channel_id: int) -> Any | None:
        channel = self.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.exception("내전 채널 조회 실패", extra={"channel_id": channel_id})
                return None
        return channel

    async def _refresh_match(self, match: Any) -> Any | None:
        if self.service is None:
            return None
        match_id = int(_get(match, "id", _get(match, "match_id", 0)))
        latest = await self.service.get_match(match_id)
        if latest is None:
            return None
        message_id = _get(latest, "message_id")
        if not message_id:
            return latest
        channel = await self._channel_for(int(_get(latest, "channel_id")))
        if channel is None:
            return latest
        try:
            message = await channel.fetch_message(int(message_id))
            if not self._is_match_panel(message, match_id):
                # Do not let a stale ID overwrite an unrelated message.  The
                # recovery path adopts/reposts a panel with the right custom ID.
                await self._ensure_match_panel(latest)
                return latest
            await _safe_edit(
                message,
                embed=render_match(latest),
                view=self._match_view(
                    latest,
                    disabled=str(_get(latest, "status", "")) in {"FINISHED", "CANCELLED"},
                ),
            )
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            logger.exception("내전 메시지 갱신 실패", extra={"match_id": match_id})
        except Exception:
            logger.exception("내전 메시지 갱신 중 예상 못 한 오류", extra={"match_id": match_id})
        return latest

    async def _handle_due_event(self, event: Any) -> None:
        match = _get(event, "match", event)
        event_status = _get(match, "status")
        event_reminded_at = _get(match, "recruitment_reminded_at")
        delivery_token = _get(event, "delivery_token")
        latest = await self._refresh_match(match)
        match = latest or match
        if latest is not None and str(_get(latest, "status")) != str(event_status):
            return
        if str(_get(match, "status", "")) in {"FINISHED", "CANCELLED"}:
            guild = self.get_guild(int(_get(match, "guild_id")))
            await close_empty_match_voice_channels(self.service, guild, match)
            match = await self.service.get_match(int(_get(match, "id"))) or match
        channel = await self._channel_for(int(_get(match, "channel_id")))
        kind = str(_get(event, "kind", ""))
        creator_id = _get(match, "creator_id")
        prefix = f"<@{int(creator_id)}> " if creator_id is not None else ""
        if kind == "recruitment_reminder":
            if str(_get(match, "status")) != "RECRUITING" or _get(match, "recruitment_reminded_at") != event_reminded_at:
                return
            current_token = _get(match, "recruitment_reminder_token")
            if delivery_token is not None and current_token is not None:
                if str(current_token) != str(delivery_token):
                    return
            deadline = _get(match, "recruitment_deadline_at")
            text = f"{prefix}{_reminder_text(self.settings.reminder_before_seconds)}입니다. 마감: {_timestamp(deadline)}"
        elif kind == "recruitment_expired":
            if str(_get(match, "status")) == "READY_CHECK":
                text = f"{prefix}모집 시간이 끝나 준비 확인을 시작했습니다."
            else:
                text = f"{prefix}모집 시간 만료로 내전이 취소되었습니다."
        elif kind == "ready_expired":
            removed = tuple(_get(event, "removed_user_ids", ()) or ())
            promoted = tuple(_get(event, "promoted_user_ids", ()) or ())
            text = f"{prefix}준비 시간이 만료되었습니다."
            if removed:
                text += " 제외: " + ", ".join(f"<@{int(user_id)}>" for user_id in removed)
            if promoted:
                text += " 승격: " + ", ".join(f"<@{int(user_id)}>" for user_id in promoted)
        elif kind == "draft_expired":
            text = f"{prefix}지명 시간이 만료되어 내전이 취소되었습니다."
        else:
            return
        if channel is None:
            if kind == "recruitment_reminder" and delivery_token is not None:
                await self._retry_recruitment_reminder(int(_get(match, "id")), str(delivery_token))
            return
        if kind == "recruitment_reminder" and delivery_token is not None:
            try:
                await _safe_send(channel, text)
            except Exception:
                await self._retry_recruitment_reminder(int(_get(match, "id")), str(delivery_token))
                raise
            try:
                acknowledge = getattr(self.service, "acknowledge_recruitment_reminder", None)
                if not callable(acknowledge):
                    # Compatibility with a test/embedded service from before
                    # durable reminder delivery was introduced.  Production
                    # MatchService always provides this method.
                    logger.warning(
                        "모집 알림 ACK API가 없어 전달만 완료함",
                        extra={"match_id": _get(match, "id")},
                    )
                    return
                acknowledged = await acknowledge(
                    int(_get(match, "id")), str(delivery_token)
                )
            except Exception:
                await self._retry_recruitment_reminder(int(_get(match, "id")), str(delivery_token))
                raise
            if not acknowledged:
                logger.info(
                    "오래된 모집 알림 전달 토큰을 무시함",
                    extra={"match_id": _get(match, "id")},
                )
            return
        await _safe_send(channel, text)

    async def _retry_recruitment_reminder(self, match_id: int, token: str) -> None:
        retry = getattr(self.service, "retry_recruitment_reminder", None)
        if not callable(retry):
            return
        retry_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(1, int(getattr(self.service, "reminder_retry_seconds", 60)))
        )
        try:
            await retry(match_id, token, retry_at)
        except Exception:
            logger.exception("모집 알림 재시도 예약 실패", extra={"match_id": match_id})

    async def recover_active_matches(self) -> None:
        if self.service is None:
            return
        async with self._recovery_lock:
            if self._recovered:
                return
            try:
                # 버튼 뷰 등록 전에 이미 기한이 지난 건부터 처리한다.
                try:
                    await self.process_due_matches()
                except Exception:
                    logger.exception("복구 중 만료 내전 처리 실패")
                matches = await self.service.list_active()
                for match in matches:
                    match_id = int(_get(match, "id"))
                    status = str(_get(match, "status", ""))
                    if status == "PLAYING":
                        guild = self.get_guild(int(_get(match, "guild_id")))
                        if guild is None:
                            self._set_voice_retry(match_id, pending=True)
                        else:
                            try:
                                summary = await ensure_match_voice_channels(
                                    self.service,
                                    guild,
                                    match,
                                    self.settings.team_a_voice_channel_id,
                                    self.settings.team_b_voice_channel_id,
                                )
                            except Exception:
                                self._set_voice_retry(match_id, pending=True)
                                logger.exception(
                                    "진행 중 내전 보이스 복구 오류", extra={"match_id": match_id}
                                )
                                summary = None
                            if summary is not None and summary.error:
                                self._set_voice_retry(match_id, pending=True)
                                logger.error(
                                    "진행 중 내전 보이스 복구 실패: %s",
                                    summary.error,
                                    extra={"match_id": match_id},
                                )
                                text_channel = await self._channel_for(int(_get(match, "channel_id")))
                                if text_channel is not None:
                                    try:
                                        await _safe_send(
                                            text_channel,
                                            f"<@{int(_get(match, 'creator_id'))}> 보이스 복구 실패: "
                                            f"{summary.error} 경기와 팀 배정은 DB에 저장되어 있습니다.",
                                        )
                                    except Exception:
                                        logger.exception(
                                            "보이스 복구 실패 알림 전송 오류",
                                            extra={"match_id": match_id},
                                        )
                            elif summary is not None:
                                self._set_voice_retry(match_id, pending=False)
                            match = await self.service.get_match(match_id) or match
                    # Panel IDs can be NULL after a crash, or can point to a
                    # deleted Discord message.  Re-adopt/repost the panel while
                    # retaining the active DB row; transient Discord failures
                    # are logged and retried by the next poll/startup pass.
                    try:
                        repaired = await self._ensure_match_panel(match)
                    except (discord.Forbidden, discord.HTTPException, asyncio.TimeoutError):
                        logger.exception("내전 메시지 복구 실패", extra={"match_id": match_id})
                        continue
                    except Exception:
                        logger.exception("활성 내전 복구 중 예상 못 한 오류", extra={"match_id": match_id})
                        continue
                    if repaired is None:
                        continue
                    message_id = _get(repaired, "message_id")
                    if not message_id:
                        logger.warning("활성 내전에 메시지 ID가 없음", extra={"match_id": match_id})
                        continue
                    try:
                        self.add_view(self._match_view(repaired), message_id=int(message_id))
                    except Exception:
                        logger.exception("내전 버튼 뷰 등록 실패", extra={"match_id": match_id})
                self._recovered = True
            except Exception:
                logger.exception("복구할 활성 내전 조회 실패")

    async def close(self) -> None:
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await super().close()
        await close_pool(self.pool)
        self.pool = None


def _timestamp(value: Any) -> str:
    if value is None:
        return "-"
    try:
        from datetime import date, datetime, timezone

        if isinstance(value, datetime):
            parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            value = int(parsed.timestamp())
        elif isinstance(value, date):
            value = int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp())
        return f"<t:{int(value)}:F>"
    except (TypeError, ValueError):
        return "-"


def _reminder_text(seconds: int) -> str:
    minutes, remainder = divmod(max(0, int(seconds)), 60)
    if minutes and remainder:
        return f"모집 마감 {minutes}분 {remainder}초 전"
    if minutes:
        return f"모집 마감 {minutes}분 전"
    return f"모집 마감 {remainder}초 전"


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    bot = InhouseBot(settings)
    try:
        bot.run(settings.discord_token)
    except Exception:
        logger.exception("Discord 봇이 예상치 못하게 종료됨")
        raise


if __name__ == "__main__":
    main()
