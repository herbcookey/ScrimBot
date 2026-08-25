"""내전 메시지용 영구 버튼."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Awaitable, Callable

import discord

from .renderer import render_match
from .voice import VoiceMoveSummary, move_match_participants
from inhouse_bot.repositories.matches import MatchError

logger = logging.getLogger(__name__)

SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)
_ACTIVE = {"RECRUITING", "READY_CHECK", "PLAYING"}
_TERMINAL = {"FINISHED", "CANCELLED"}


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _error_text(error: Exception) -> str:
    return str(error) or "내전 처리에 실패했습니다."


async def _safe_send(channel: Any, content: str, **kwargs: Any) -> Any:
    try:
        return await channel.send(content, allowed_mentions=SAFE_MENTIONS, **kwargs)
    except TypeError:
        # 간단한 테스트 객체에는 멘션 설정이 없을 수 있다.
        return await channel.send(content, **kwargs)


async def _safe_edit(message: Any, **kwargs: Any) -> Any:
    try:
        return await message.edit(allowed_mentions=SAFE_MENTIONS, **kwargs)
    except TypeError:
        return await message.edit(**kwargs)


class MatchView(discord.ui.View):
    """재시작 후에도 쓰는 버튼 뷰. 버튼 식별값에는 내전 ID만 넣는다."""

    def __init__(
        self,
        service: Any,
        match_id: int,
        *,
        disabled: bool = False,
        status: str | None = None,
        state: str | None = None,
        team_a_voice_channel_id: int | None = None,
        team_b_voice_channel_id: int | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.match_id = int(match_id)
        status = status or state
        self.status = status
        self.team_a_voice_channel_id = team_a_voice_channel_id
        self.team_b_voice_channel_id = team_b_voice_channel_id
        self.join_button = discord.ui.Button(
            label="참가", style=discord.ButtonStyle.success,
            custom_id=f"match:{self.match_id}:join",
            disabled=disabled or (status not in (None, "RECRUITING", "READY_CHECK")),
        )
        self.leave_button = discord.ui.Button(
            label="나가기", style=discord.ButtonStyle.secondary,
            custom_id=f"match:{self.match_id}:leave",
            disabled=disabled or (status not in (None, "RECRUITING", "READY_CHECK")),
        )
        self.start_button = discord.ui.Button(
            label="준비 확인 시작", style=discord.ButtonStyle.primary,
            custom_id=f"match:{self.match_id}:start",
            disabled=disabled or status not in (None, "RECRUITING"),
        )
        self.ready_button = discord.ui.Button(
            label="준비", style=discord.ButtonStyle.success,
            custom_id=f"match:{self.match_id}:ready",
            disabled=disabled or status not in (None, "READY_CHECK"),
        )
        self.cancel_button = discord.ui.Button(
            label="내전 취소", style=discord.ButtonStyle.danger,
            custom_id=f"match:{self.match_id}:cancel",
            disabled=disabled or (status not in (None, *_ACTIVE)),
        )
        self.join_button.callback = self._join
        self.leave_button.callback = self._leave
        self.start_button.callback = self._start
        self.ready_button.callback = self._ready
        self.cancel_button.callback = self._cancel
        for button in (
            self.join_button,
            self.leave_button,
            self.start_button,
            self.ready_button,
            self.cancel_button,
        ):
            self.add_item(button)

    @staticmethod
    def _manage_guild(interaction: discord.Interaction) -> bool:
        permissions = getattr(getattr(interaction, "user", None), "guild_permissions", None)
        return bool(getattr(permissions, "manage_guild", False))

    async def _defer(self, interaction: discord.Interaction) -> None:
        response = getattr(interaction, "response", None)
        is_done = getattr(response, "is_done", lambda: False)
        if response is not None and not is_done():
            await response.defer(ephemeral=True)

    async def _followup(self, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.followup.send(content, ephemeral=True, allowed_mentions=SAFE_MENTIONS)
        except TypeError:
            await interaction.followup.send(content, ephemeral=True)

    async def _refresh(self, interaction: discord.Interaction) -> Any | None:
        latest = await self.service.get_match(self.match_id)
        if latest is None:
            return None
        message = getattr(interaction, "message", None)
        if message is not None:
            terminal = str(_get(latest, "status", "")) in _TERMINAL
            try:
                await _safe_edit(
                    message,
                    embed=render_match(latest),
                    view=MatchView(
                        self.service,
                        self.match_id,
                        status=str(_get(latest, "status", "")),
                        team_a_voice_channel_id=self.team_a_voice_channel_id,
                        team_b_voice_channel_id=self.team_b_voice_channel_id,
                        disabled=terminal,
                    ),
                )
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                logger.exception("내전 메시지 갱신 실패", extra={"match_id": self.match_id})
            except Exception:
                # DB 변경은 이미 끝났다. 화면 수정 실패를 처리 실패로 보이면 안 된다.
                logger.exception("내전 메시지 갱신 중 예상 못 한 오류", extra={"match_id": self.match_id})
        return latest

    async def _announce_mutation(self, interaction: discord.Interaction, result: Any) -> None:
        removed = _get(result, "removed_user_id")
        promoted = tuple(_get(result, "promoted_user_ids", ()) or ())
        if removed is None and not promoted:
            return
        parts = []
        if removed is not None:
            parts.append(f"<@{int(removed)}>님이 내전에서 나갔습니다.")
        if promoted:
            parts.append("대기자 승격: " + ", ".join(f"<@{int(user_id)}>" for user_id in promoted))
        channel = getattr(interaction, "channel", None)
        if channel is not None:
            try:
                await _safe_send(channel, " ".join(parts))
            except Exception:
                logger.exception("참가자 변경 알림 전송 실패", extra={"match_id": self.match_id})

    async def _move_voice(self, interaction: discord.Interaction, match: Any) -> VoiceMoveSummary:
        guild = getattr(interaction, "guild", None)
        summary = await move_match_participants(
            guild,
            match,
            self.team_a_voice_channel_id,
            self.team_b_voice_channel_id,
        )
        creator_id = _get(match, "creator_id")
        channel = getattr(interaction, "channel", None)
        if channel is not None and creator_id is not None and (
            self.team_a_voice_channel_id is not None and self.team_b_voice_channel_id is not None
        ):
            try:
                await _safe_send(
                    channel,
                    f"<@{int(creator_id)}> 음성 배치: 성공 {summary.success}명, "
                    f"건너뜀 {summary.skipped}명, 실패 {summary.failed}명.",
                )
            except Exception:
                logger.exception("음성 채널 배치 알림 전송 실패", extra={"match_id": self.match_id})
        return summary

    async def _run(
        self,
        interaction: discord.Interaction,
        operation: Callable[[], Awaitable[Any]],
        success: str | Callable[[Any], str],
        *,
        after: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        await self._defer(interaction)
        try:
            result = await operation()
            await self._refresh(interaction)
            if after is not None:
                try:
                    await after(result)
                except Exception:
                    logger.exception("DB 반영 후 Discord 후처리 실패", extra={"match_id": self.match_id})
            message = success(result) if callable(success) else success
        except MatchError as exc:
            await self._followup(interaction, _error_text(exc))
            return
        except Exception:
            logger.exception("내전 버튼 처리 실패", extra={"match_id": self.match_id})
            await self._followup(interaction, "내전 처리 중 오류가 발생했습니다.")
            return
        await self._followup(interaction, message)

    async def _join(self, interaction: discord.Interaction) -> None:
        user_id = int(interaction.user.id)
        await self._run(
            interaction,
            lambda: self.service.join_match(self.match_id, user_id),
            lambda result: "대기열에 등록했습니다." if _get(result, "waitlisted", False) else "내전에 참가했습니다.",
        )

    async def _leave(self, interaction: discord.Interaction) -> None:
        user_id = int(interaction.user.id)
        await self._run(
            interaction,
            lambda: self.service.leave_match(self.match_id, user_id),
            "내전에서 나갔습니다.",
            after=lambda result: self._announce_mutation(interaction, result),
        )

    async def _start(self, interaction: discord.Interaction) -> None:
        actor_id = int(interaction.user.id)
        await self._run(
            interaction,
            lambda: self.service.start_match(
                self.match_id, actor_id, manage_guild=self._manage_guild(interaction)
            ),
            "준비 확인을 시작했습니다.",
        )

    async def _ready(self, interaction: discord.Interaction) -> None:
        user_id = int(interaction.user.id)

        async def after(result: Any) -> None:
            if bool(_get(result, "started", False)) or str(_get(result, "status", "")) == "PLAYING":
                await self._move_voice(interaction, result)

        await self._run(
            interaction,
            lambda: self.service.toggle_ready(self.match_id, user_id),
            lambda result: (
                "모든 참가자가 준비해 내전을 시작했습니다."
                if bool(_get(result, "started", False)) or str(_get(result, "status", "")) == "PLAYING"
                else ("준비 완료했습니다." if _get(result, "ready", False) else "준비를 취소했습니다.")
            ),
            after=after,
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        actor_id = int(interaction.user.id)
        await self._run(
            interaction,
            lambda: self.service.cancel_match(
                self.match_id, actor_id, manage_guild=self._manage_guild(interaction)
            ),
            "내전을 취소했습니다.",
        )


__all__ = ["MatchView", "SAFE_MENTIONS"]
