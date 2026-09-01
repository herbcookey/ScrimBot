"""내전 메시지용 영구 버튼."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Awaitable, Callable

import discord

from .renderer import render_match
from .voice import (
    VoiceMoveSummary,
    close_empty_match_voice_channels,
    ensure_match_voice_channels,
)
from inhouse_bot.repositories.matches import MatchError

logger = logging.getLogger(__name__)

SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)
_ACTIVE = {"RECRUITING", "READY_CHECK", "DRAFTING", "PLAYING"}
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
        guild_id: int | None = None,
        role_rating_enabled: bool | None = None,
        team_a_voice_channel_id: int | None = None,
        team_b_voice_channel_id: int | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.match_id = int(match_id)
        status = status or state
        self.status = status
        # Persist the guild and role-mode metadata on every newly rendered
        # view.  The values are also supplied when the bot reconstructs a
        # view after a restart; ``None`` is retained as a compatibility mode
        # for older callers that do not yet provide the persisted fields.
        self.guild_id = int(guild_id) if guild_id is not None else None
        self.role_rating_enabled = (
            bool(role_rating_enabled) if role_rating_enabled is not None else None
        )
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

    async def _is_bot_admin(self, interaction: discord.Interaction) -> bool:
        guild = getattr(interaction, "guild", None)
        user = getattr(interaction, "user", None)
        if guild is None or user is None:
            return False
        return bool(await self.service.is_bot_admin(int(guild.id), int(user.id)))

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

    @staticmethod
    def _interaction_guild_id(interaction: discord.Interaction) -> int | None:
        guild = getattr(interaction, "guild", None)
        guild_id = getattr(guild, "id", None)
        return int(guild_id) if guild_id is not None else None

    async def _match_for_interaction(self, interaction: discord.Interaction) -> Any:
        """Load and validate the match's guild before any mutation or render.

        Button interactions can be delivered from a different guild while an
        old persistent view is still present.  Always compare the persisted
        match guild with the interaction guild after the interaction has been
        acknowledged.  A view created by an older process may not carry
        ``guild_id``; in that case the loaded row supplies it and is cached on
        the view for subsequent callbacks.
        """

        match = await self.service.get_match(self.match_id)
        if match is None:
            raise MatchError("내전을 찾을 수 없습니다.")
        interaction_guild_id = self._interaction_guild_id(interaction)
        match_guild_id = _get(match, "guild_id", self.guild_id)
        if match_guild_id is not None:
            match_guild_id = int(match_guild_id)
            if self.guild_id is not None and self.guild_id != match_guild_id:
                raise MatchError("내전 서버 정보가 일치하지 않습니다.")
            self.guild_id = match_guild_id
            if interaction_guild_id != match_guild_id:
                raise MatchError("이 내전은 현재 서버에서 사용할 수 없습니다.")
        elif interaction_guild_id is None:
            # A guild-less interaction can never be safe for a persistent
            # match.  Legacy test doubles without guild metadata are handled
            # only when they do provide a guild context.
            raise MatchError("서버에서만 내전 버튼을 사용할 수 있습니다.")
        return match

    def _known_guild_matches(self, interaction: discord.Interaction) -> bool:
        """Synchronously check guild metadata already carried by the view."""

        if self.guild_id is None:
            return True
        return self._interaction_guild_id(interaction) == self.guild_id

    async def _refresh(self, interaction: discord.Interaction) -> Any | None:
        latest = await self.service.get_match(self.match_id)
        if latest is None:
            return None
        # Rendering is guarded by the same guild check as mutation.  This is
        # especially important for stale persistent views whose interaction
        # arrives from another server.
        await self._match_for_interaction_from_loaded(interaction, latest)
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
                        guild_id=_get(latest, "guild_id", self.guild_id),
                        role_rating_enabled=_get(
                            latest, "role_rating_enabled", self.role_rating_enabled
                        ),
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

    async def _match_for_interaction_from_loaded(
        self, interaction: discord.Interaction, match: Any
    ) -> Any:
        """Validate a match row that has already been fetched.

        ``_refresh`` has to avoid a second remote read, while callbacks still
        need the persisted guild comparison immediately before rendering.
        """

        interaction_guild_id = self._interaction_guild_id(interaction)
        match_guild_id = _get(match, "guild_id", self.guild_id)
        if match_guild_id is not None:
            match_guild_id = int(match_guild_id)
            if self.guild_id is not None and self.guild_id != match_guild_id:
                raise MatchError("내전 서버 정보가 일치하지 않습니다.")
            self.guild_id = match_guild_id
            if interaction_guild_id != match_guild_id:
                raise MatchError("이 내전은 현재 서버에서 사용할 수 없습니다.")
        elif interaction_guild_id is None:
            raise MatchError("서버에서만 내전 버튼을 사용할 수 있습니다.")
        return match

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
        try:
            summary = await ensure_match_voice_channels(
                self.service, guild, match,
                self.team_a_voice_channel_id,
                self.team_b_voice_channel_id,
            )
        except Exception as exc:
            logger.exception("내전 보이스 생성 실패", extra={"match_id": self.match_id})
            summary = VoiceMoveSummary(error=str(exc) or "보이스 채널을 준비하지 못했습니다.")
        retry = getattr(
            self.service,
            "retry_voice_for" if summary.error else "clear_voice_retry",
            None,
        )
        if callable(retry):
            retry(self.match_id)
        await self._refresh(interaction)
        creator_id = _get(match, "creator_id")
        channel = getattr(interaction, "channel", None)
        if channel is not None and creator_id is not None:
            try:
                created = (
                    f" 생성 {len(summary.created_channel_ids)}개," if summary.created_channel_ids else ""
                )
                error = f" 오류: {summary.error}" if summary.error else ""
                await _safe_send(
                    channel,
                    f"<@{int(creator_id)}> 보이스:{created} 이동 {summary.success}명, "
                    f"미접속 {summary.skipped}명, 실패 {summary.failed}명.{error}",
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
            # ``get_match`` is remote, but the interaction is acknowledged
            # above.  Never mutate before confirming this button belongs to
            # the guild that owns the persisted match.
            await self._match_for_interaction(interaction)
            result = await operation()
            try:
                await self._refresh(interaction)
            except Exception:
                # The DB mutation already committed.  A failed read/edit of
                # the panel is a Discord refresh problem, not a failed join or
                # leave operation; log it and still acknowledge success.
                logger.exception("DB 반영 후 내전 메시지 갱신 실패", extra={"match_id": self.match_id})
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
        # Sending a modal must be the initial response.  The role-mode bit is
        # carried by the persistent view, so opening it never waits on the
        # database.  ``None`` is a legacy-view fallback until recovery passes
        # the persisted flag; collecting preferences is harmless for games
        # whose role-rating mode is disabled because the repository ignores
        # them.
        if self.role_rating_enabled is not False:
            if not self._known_guild_matches(interaction):
                try:
                    await interaction.response.send_message(
                        "이 내전은 현재 서버에서 사용할 수 없습니다.", ephemeral=True
                    )
                except TypeError:
                    await interaction.response.send_message(
                        "이 내전은 현재 서버에서 사용할 수 없습니다.", ephemeral=True
                    )
                return
            try:
                await interaction.response.send_modal(
                    JoinPreferencesModal(self, getattr(interaction, "message", None))
                )
            except Exception:
                logger.exception("참가 지망 입력창 열기 실패", extra={"match_id": self.match_id})
                if not getattr(interaction.response, "is_done", lambda: False)():
                    await interaction.response.send_message(
                        "참가 처리 중 오류가 발생했습니다.", ephemeral=True
                    )
            return
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

        async def start() -> Any:
            return await self.service.start_match(
                self.match_id,
                actor_id,
                manager_override=await self._is_bot_admin(interaction),
            )

        await self._run(
            interaction,
            start,
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
                "모든 참가자가 준비했습니다. 주장 지명을 시작합니다."
                if str(_get(result, "status", "")) == "DRAFTING"
                else
                "모든 참가자가 준비해 내전을 시작했습니다."
                if bool(_get(result, "started", False)) or str(_get(result, "status", "")) == "PLAYING"
                else ("준비 완료했습니다." if _get(result, "ready", False) else "준비를 취소했습니다.")
            ),
            after=after,
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        actor_id = int(interaction.user.id)

        async def cancel() -> Any:
            result = await self.service.cancel_match(
                self.match_id,
                actor_id,
                manager_override=await self._is_bot_admin(interaction),
            )
            try:
                await close_empty_match_voice_channels(
                    self.service, getattr(interaction, "guild", None), result
                )
            except Exception:
                logger.exception("취소된 내전 보이스 정리 예약 실패", extra={"match_id": self.match_id})
            return await self.service.get_match(self.match_id) or result

        await self._run(
            interaction,
            cancel,
            lambda result: (
                f"내전을 취소했습니다. 보이스 채널은 "
                f"{int(getattr(self.service, 'voice_cleanup_delay_seconds', 600)) // 60}분 후 삭제됩니다."
                if _get(result, "voice_cleanup_at") is not None
                else "내전을 취소했습니다."
            ),
        )


class JoinPreferencesModal(discord.ui.Modal, title="내전 참가 라인 입력"):
    first = discord.ui.TextInput(label="1지망", placeholder="탑 / 정글 / 미드 / 원딜 / 서폿")
    second = discord.ui.TextInput(
        label="2지망", placeholder="선택 입력 (탑 / 정글 / 미드 / 원딜 / 서폿)", required=False
    )
    third = discord.ui.TextInput(
        label="3지망", placeholder="선택 입력", required=False
    )

    def __init__(self, view: MatchView, message: Any | None) -> None:
        super().__init__(custom_id=f"match:{view.match_id}:join_roles")
        self.match_view = view
        self.source_message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        view = self.match_view
        try:
            # Modal submissions are the first place where we can afford a DB
            # read.  Validate the persisted guild before joining so a modal
            # opened from a stale/foreign server can never mutate the match.
            await view._match_for_interaction(interaction)
            first = str(self.first).strip() or None
            second = str(self.second).strip() or None
            third = str(self.third).strip() or None
            if third is not None and second is None:
                await view._followup(
                    interaction, "3지망을 입력하려면 2지망도 입력해야 합니다."
                )
                return
            result = await view.service.join_match(
                view.match_id,
                int(interaction.user.id),
                preferred_role_1=first,
                preferred_role_2=second,
                preferred_role_3=third,
            )
            text = "대기열에 등록했습니다." if _get(result, "waitlisted", False) else "내전에 참가했습니다."
        except MatchError as exc:
            text = _error_text(exc)
            await view._followup(interaction, text)
            return
        except Exception:
            logger.exception("지망 라인 참가 처리 실패", extra={"match_id": view.match_id})
            await view._followup(interaction, "참가 처리 중 오류가 발생했습니다.")
            return

        # The DB mutation has succeeded.  Refreshing the source panel is a
        # best-effort Discord side effect; an API failure must not turn a
        # successful join into an error response.
        try:
            latest = await view.service.get_match(view.match_id)
            if latest is not None and self.source_message is not None:
                await view._match_for_interaction_from_loaded(interaction, latest)
                await _safe_edit(
                    self.source_message,
                    embed=render_match(latest),
                    view=MatchView(
                        view.service,
                        view.match_id,
                        status=str(_get(latest, "status", "")),
                        guild_id=_get(latest, "guild_id", view.guild_id),
                        role_rating_enabled=_get(
                            latest, "role_rating_enabled", view.role_rating_enabled
                        ),
                        team_a_voice_channel_id=view.team_a_voice_channel_id,
                        team_b_voice_channel_id=view.team_b_voice_channel_id,
                    ),
                )
        except Exception:
            logger.exception("참가 후 내전 메시지 갱신 실패", extra={"match_id": view.match_id})
        await view._followup(interaction, text)


__all__ = ["JoinPreferencesModal", "MatchView", "SAFE_MENTIONS"]
