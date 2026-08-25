"""봇 시작, 재시작 복구, 만료 내전 폴링 처리."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import discord
from discord.ext import commands

from .config import Settings, load_settings
from .db import close_pool, create_pool
from .discord.commands import add_match_commands
from .discord.renderer import render_match
from .discord.views import MatchView, SAFE_MENTIONS
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
                ready_timeout_seconds=self.settings.ready_timeout_seconds,
                default_recruitment_minutes=self.settings.default_recruitment_minutes,
                reminder_before_seconds=self.settings.reminder_before_seconds,
            )
        add_match_commands(
            self.tree,
            self.service,
            self.settings.discord_guild_id,
            team_a_voice_channel_id=self.settings.team_a_voice_channel_id,
            team_b_voice_channel_id=self.settings.team_b_voice_channel_id,
        )
        guild = discord.Object(id=self.settings.discord_guild_id)
        await self.tree.sync(guild=guild)
        logger.info("길드 명령 동기화 완료", extra={"guild_id": self.settings.discord_guild_id})

    async def on_ready(self) -> None:
        logger.info("Discord 로그인: %s", self.user)
        await self.recover_active_matches()
        self._ensure_poll_task()

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
        return events

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
            status = str(_get(latest, "status", ""))
            await _safe_edit(
                message,
                embed=render_match(latest),
                view=MatchView(
                    self.service,
                    match_id,
                    status=status,
                    disabled=status in {"FINISHED", "CANCELLED"},
                    team_a_voice_channel_id=self.settings.team_a_voice_channel_id,
                    team_b_voice_channel_id=self.settings.team_b_voice_channel_id,
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
        latest = await self._refresh_match(match)
        match = latest or match
        if latest is not None and str(_get(latest, "status")) != str(event_status):
            return
        channel = await self._channel_for(int(_get(match, "channel_id")))
        if channel is None:
            return
        kind = str(_get(event, "kind", ""))
        creator_id = _get(match, "creator_id")
        prefix = f"<@{int(creator_id)}> " if creator_id is not None else ""
        if kind == "recruitment_reminder":
            if str(_get(match, "status")) != "RECRUITING" or _get(match, "recruitment_reminded_at") != event_reminded_at:
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
        else:
            return
        await _safe_send(channel, text)

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
                    view = MatchView(
                        self.service,
                        match_id,
                        status=status,
                        team_a_voice_channel_id=self.settings.team_a_voice_channel_id,
                        team_b_voice_channel_id=self.settings.team_b_voice_channel_id,
                    )
                    message_id = _get(match, "message_id")
                    if message_id:
                        self.add_view(view, message_id=int(message_id))
                    else:
                        self.add_view(view)
                        logger.warning("활성 내전에 메시지 ID가 없음", extra={"match_id": match_id})
                        continue
                    channel = await self._channel_for(int(_get(match, "channel_id")))
                    if channel is None:
                        continue
                    try:
                        message = await channel.fetch_message(int(message_id))
                        latest = await self.service.get_match(match_id)
                        if latest is not None:
                            latest_status = str(_get(latest, "status", ""))
                            await _safe_edit(
                                message,
                                embed=render_match(latest),
                                view=MatchView(
                                    self.service,
                                    match_id,
                                    status=latest_status,
                                    disabled=latest_status in {"FINISHED", "CANCELLED"},
                                    team_a_voice_channel_id=self.settings.team_a_voice_channel_id,
                                    team_b_voice_channel_id=self.settings.team_b_voice_channel_id,
                                ),
                            )
                    except discord.NotFound:
                        logger.warning("활성 내전 메시지를 찾을 수 없음", extra={"match_id": match_id})
                        try:
                            await self.service.cancel_missing_message(match_id)
                        except Exception:
                            logger.exception("메시지가 없는 내전 취소 실패", extra={"match_id": match_id})
                    except (discord.Forbidden, discord.HTTPException, asyncio.TimeoutError):
                        logger.exception("내전 메시지 복구 실패", extra={"match_id": match_id})
                    except Exception:
                        logger.exception("활성 내전 복구 중 예상 못 한 오류", extra={"match_id": match_id})
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
