"""내전 생성, 관리, 결과용 슬래시 명령."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import discord
from discord import app_commands

from .renderer import render_match
from .views import MatchView, SAFE_MENTIONS
from .voice import (
    close_empty_match_voice_channels,
    ensure_match_voice_channels,
    resolve_voice_category_id,
)
from inhouse_bot.repositories.matches import MatchError
from inhouse_bot.role_assignment import (
    ROLE_LABELS,
    RoleAssignmentError,
    compact_tier_label,
    parse_compact_tier,
)

logger = logging.getLogger(__name__)

ROLE_CHOICES = [
    app_commands.Choice(name=name, value=value)
    for name, value in (("탑", "TOP"), ("정글", "JUNGLE"), ("미드", "MID"), ("원딜", "ADC"), ("서폿", "SUPPORT"))
]
_COMPACT_TIER_CHOICES = tuple(
    f"{tier}{division}"
    for tier in ("아이언", "브론즈", "실버", "골드", "플래티넘", "에메랄드", "다이아")
    for division in (1, 2, 3, 4)
) + tuple(
    f"{tier}{division}"
    for tier in ("마스터", "그랜드마스터")
    for division in ("하", "중", "상")
) + ("챌린저",)


def _choice_value(value: Any, default: Any = None) -> Any:
    return getattr(value, "value", value) if value is not None else default


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


class MatchCommandGroup(app_commands.Group):
    """길드에 동기화하는 ``/내전`` 명령 그룹."""

    def __init__(
        self,
        service: Any,
        *,
        team_a_voice_channel_id: int | None = None,
        team_b_voice_channel_id: int | None = None,
        inhouse_voice_category_id: int | None = None,
    ) -> None:
        super().__init__(name="내전", description="게임 내전 관리")
        self.service = service
        self.team_a_voice_channel_id = team_a_voice_channel_id
        self.team_b_voice_channel_id = team_b_voice_channel_id
        self.inhouse_voice_category_id = inhouse_voice_category_id

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

    async def _send(self, interaction: discord.Interaction, content: str) -> None:
        if interaction.response.is_done():
            try:
                await interaction.followup.send(content, ephemeral=True, allowed_mentions=SAFE_MENTIONS)
            except TypeError:
                await interaction.followup.send(content, ephemeral=True)
        else:
            try:
                await interaction.response.send_message(
                    content, ephemeral=True, allowed_mentions=SAFE_MENTIONS
                )
            except TypeError:
                await interaction.response.send_message(content, ephemeral=True)

    async def _send_embed(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        if interaction.response.is_done():
            try:
                await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=SAFE_MENTIONS)
            except TypeError:
                await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            try:
                await interaction.response.send_message(
                    embed=embed, ephemeral=True, allowed_mentions=SAFE_MENTIONS
                )
            except TypeError:
                await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="생성", description="새 내전을 생성합니다.")
    @app_commands.describe(
        title="내전 제목", recruitment_minutes="모집시간(분)", game="게임",
        assignment_mode="팀 배정 방식", preferred_role_1="생성자 1지망",
        preferred_role_2="생성자 2지망", preferred_role_3="생성자 3지망",
    )
    @app_commands.rename(
        title="제목", recruitment_minutes="모집시간", game="게임",
        assignment_mode="방식", preferred_role_1="1지망",
        preferred_role_2="2지망", preferred_role_3="3지망",
    )
    @app_commands.choices(
        assignment_mode=[
            app_commands.Choice(name="Balanced", value="BALANCED"),
            app_commands.Choice(name="Draft", value="DRAFT"),
        ],
        preferred_role_1=ROLE_CHOICES,
        preferred_role_2=ROLE_CHOICES,
        preferred_role_3=ROLE_CHOICES,
    )
    async def create(
        self,
        interaction: discord.Interaction,
        title: str,
        recruitment_minutes: app_commands.Range[int, 5, 1440] | None = None,
        game: str | None = None,
        assignment_mode: app_commands.Choice[str] | None = None,
        preferred_role_1: app_commands.Choice[str] | None = None,
        preferred_role_2: app_commands.Choice[str] | None = None,
        preferred_role_3: app_commands.Choice[str] | None = None,
    ) -> None:
        if interaction.guild is None or interaction.channel is None:
            await self._send(interaction, "서버 채널에서만 사용할 수 있습니다.")
            return
        title = title.strip()
        if not title:
            await self._send(interaction, "제목을 입력해 주세요.")
            return
        await self._defer(interaction)
        try:
            voice_category_id, voice_category_error = resolve_voice_category_id(
                interaction.guild,
                interaction.channel,
                self.inhouse_voice_category_id,
                self.team_a_voice_channel_id,
                self.team_b_voice_channel_id,
            )
            if self.inhouse_voice_category_id is not None and voice_category_error:
                await self._send(interaction, voice_category_error)
                return
            created = await self.service.create_match(
                guild_id=int(interaction.guild.id),
                channel_id=int(interaction.channel.id),
                creator_id=int(interaction.user.id),
                title=title,
                game_key=game or "lol",
                assignment_mode=_choice_value(assignment_mode, "BALANCED"),
                preferred_role_1=_choice_value(preferred_role_1),
                preferred_role_2=_choice_value(preferred_role_2),
                preferred_role_3=_choice_value(preferred_role_3),
                voice_category_id=voice_category_id,
                **({} if recruitment_minutes is None else {"recruitment_minutes": int(recruitment_minutes)}),
            )
            match_id = int(_get(created, "id"))
            latest = await self.service.get_match(match_id)
            view = MatchView(
                self.service,
                match_id,
                status=str(_get(latest or created, "status", "RECRUITING")),
                team_a_voice_channel_id=self.team_a_voice_channel_id,
                team_b_voice_channel_id=self.team_b_voice_channel_id,
            )
            message = None
            try:
                message = await _safe_send(
                    interaction.channel,
                    "",
                    embed=render_match(latest or created),
                    view=view,
                )
                await self.service.update_message_id(match_id, int(message.id))
            except Exception:
                logger.exception("내전 메시지 전송 실패", extra={"match_id": match_id})
                try:
                    await self.service.cancel_missing_message(match_id)
                except Exception:
                    logger.exception("전송 못 한 내전 취소 실패", extra={"match_id": match_id})
                if message is not None:
                    try:
                        cancelled = await self.service.get_match(match_id)
                        await _safe_edit(
                            message,
                            embed=render_match(cancelled or created),
                            view=MatchView(self.service, match_id, status="CANCELLED", disabled=True),
                        )
                    except Exception:
                        logger.exception("전송된 내전 메시지 취소 표시 실패", extra={"match_id": match_id})
                await self._send(interaction, "내전 메시지 처리에 실패해 내전을 취소했습니다.")
                return
            await self._send(
                interaction,
                "내전을 생성했습니다."
                + (f" 보이스 자동 생성은 사용하지 않습니다: {voice_category_error}" if voice_category_error else ""),
            )
        except MatchError as exc:
            await self._send(interaction, str(exc) or "이 채널에 활성 내전이 이미 있습니다.")
        except Exception:
            logger.exception("내전 생성 실패")
            await self._send(interaction, "내전 생성 중 오류가 발생했습니다.")

    @app_commands.command(name="관리자추가", description="봇 관리자를 추가합니다.")
    @app_commands.describe(user="추가할 사용자")
    @app_commands.rename(user="사용자")
    async def admin_add(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None:
            await self._send(interaction, "서버에서만 사용할 수 있습니다.")
            return
        if bool(getattr(user, "bot", False)):
            await self._send(interaction, "봇 계정은 관리자로 추가할 수 없습니다.")
            return
        await self._defer(interaction)
        try:
            await self.service.add_bot_admin(
                int(interaction.guild.id), int(interaction.user.id), int(user.id)
            )
            await self._send(interaction, f"<@{int(user.id)}>님을 봇 관리자로 추가했습니다.")
        except MatchError as exc:
            await self._send(interaction, str(exc) or "봇 관리자 추가 권한이 없습니다.")
        except Exception:
            logger.exception("봇 관리자 추가 실패")
            await self._send(interaction, "봇 관리자 추가 중 오류가 발생했습니다.")

    @app_commands.command(name="관리자삭제", description="봇 관리자를 삭제합니다.")
    @app_commands.describe(user="삭제할 사용자")
    @app_commands.rename(user="사용자")
    async def admin_remove(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None:
            await self._send(interaction, "서버에서만 사용할 수 있습니다.")
            return
        await self._defer(interaction)
        try:
            await self.service.remove_bot_admin(
                int(interaction.guild.id), int(interaction.user.id), int(user.id)
            )
            await self._send(interaction, f"<@{int(user.id)}>님을 봇 관리자에서 삭제했습니다.")
        except MatchError as exc:
            await self._send(interaction, str(exc) or "봇 관리자 삭제 권한이 없습니다.")
        except Exception:
            logger.exception("봇 관리자 삭제 실패")
            await self._send(interaction, "봇 관리자 삭제 중 오류가 발생했습니다.")

    @app_commands.command(name="관리자목록", description="봇 관리자 목록을 확인합니다.")
    async def admin_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self._send(interaction, "서버에서만 사용할 수 있습니다.")
            return
        if not await self._is_bot_admin(interaction):
            await self._send(interaction, "봇 관리자 권한이 필요합니다.")
            return
        await self._defer(interaction)
        try:
            admins = tuple(await self.service.list_bot_admins(int(interaction.guild.id)) or ())
            user_ids = []
            owner_id = getattr(self.service, "bot_owner_id", None)
            if owner_id is not None and int(owner_id) > 0:
                user_ids.append(int(owner_id))
            for admin in admins:
                user_id = _get(admin, "user_id", _get(admin, "id", admin))
                if user_id is None or int(user_id) in user_ids:
                    continue
                user_ids.append(int(user_id))
            lines = [f"- <@{user_id}>" for user_id in user_ids]
            await self._send(
                interaction,
                "봇 관리자 목록:\n" + ("\n".join(lines) if lines else "등록된 관리자가 없습니다."),
            )
        except MatchError as exc:
            await self._send(interaction, str(exc) or "봇 관리자 목록을 조회할 수 없습니다.")
        except Exception:
            logger.exception("봇 관리자 목록 조회 실패")
            await self._send(interaction, "봇 관리자 목록 조회 중 오류가 발생했습니다.")

    @app_commands.command(name="강퇴", description="모집 중인 내전에서 사용자를 강퇴합니다.")
    @app_commands.describe(user="강퇴할 사용자")
    @app_commands.rename(user="사용자")
    async def kick(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or interaction.channel is None:
            await self._send(interaction, "서버 채널에서만 사용할 수 있습니다.")
            return
        await self._defer(interaction)
        try:
            manager_override = await self._is_bot_admin(interaction)
            active = await self._active_for_channel(int(interaction.guild.id), int(interaction.channel.id))
            if active is None:
                await self._send(interaction, "이 채널에 활성 내전이 없습니다.")
                return
            match_id = int(_get(active, "id"))
            result = await self.service.kick_match_member(
                match_id,
                int(user.id),
                int(interaction.user.id),
                manager_override=manager_override,
            )
            await self._refresh_message(interaction, match_id)
            await self._announce_member_change(interaction, result, int(user.id), "강퇴")
            await self._send(interaction, f"<@{int(user.id)}>님을 내전에서 강퇴했습니다.")
        except MatchError as exc:
            await self._send(interaction, str(exc) or "강퇴할 수 없습니다.")
        except Exception:
            logger.exception("내전 강퇴 실패")
            await self._send(interaction, "내전 강퇴 처리 중 오류가 발생했습니다.")

    @app_commands.command(name="전적", description="서버 내전 전적과 승률을 확인합니다.")
    @app_commands.describe(user="전적을 조회할 사용자", game="게임", season="시즌 ID")
    @app_commands.rename(user="사용자", game="게임", season="시즌")
    async def stats(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        game: str | None = None,
        season: int | None = None,
    ) -> None:
        if interaction.guild is None:
            await self._send(interaction, "서버에서만 사용할 수 있습니다.")
            return
        await self._defer(interaction)
        target = user or interaction.user
        try:
            stats = await self.service.stats(
                int(interaction.guild.id),
                int(target.id),
                game_key=game or "lol",
                season_id=season,
            )
            games = int(_get(stats, "games", 0) or 0)
            wins = int(_get(stats, "wins", 0) or 0)
            losses = int(_get(stats, "losses", games - wins) or 0)
            rate = float(_get(stats, "rate", _get(stats, "win_rate", 0.0)) or 0.0)
            embed = discord.Embed(title=f"{getattr(target, 'display_name', target)}님의 내전 전적", colour=0x57F287)
            embed.add_field(name="총 경기", value=f"{games}경기", inline=True)
            embed.add_field(name="승리", value=f"{wins}승", inline=True)
            embed.add_field(name="패배", value=f"{losses}패", inline=True)
            embed.add_field(name="승률", value=f"{rate * 100:.1f}%" if games else "0%", inline=False)
            role_stats = tuple(_get(stats, "role_stats", ()) or ())
            if role_stats:
                embed.add_field(
                    name="평균 라인 MMR",
                    value=f"{int(_get(stats, 'average_role_rating', 0))}점",
                    inline=False,
                )
                for role_stat in role_stats:
                    role = str(_get(role_stat, "role"))
                    placed = bool(_get(role_stat, "placed", False))
                    rating = int(_get(role_stat, "rating", 0))
                    role_games = int(_get(role_stat, "games", 0))
                    role_wins = int(_get(role_stat, "wins", 0))
                    role_losses = int(_get(role_stat, "losses", 0))
                    role_rate = float(_get(role_stat, "rate", 0.0))
                    rating_text = f"{rating}점" if placed else "0점 · 배치 전"
                    embed.add_field(
                        name=ROLE_LABELS.get(role, role),
                        value=(
                            f"{rating_text}\n{role_games}경기 {role_wins}승 "
                            f"{role_losses}패 · {role_rate * 100:.1f}%"
                        ),
                        inline=True,
                    )
                embed.add_field(
                    name="최고 라인 MMR",
                    value=f"{int(_get(stats, 'highest_role_rating', 0))}점",
                    inline=False,
                )
            await self._send_embed(interaction, embed)
        except MatchError as exc:
            await self._send(interaction, str(exc) or "전적을 조회할 수 없습니다.")
        except Exception:
            logger.exception("내전 전적 조회 실패")
            await self._send(interaction, "전적 조회 중 오류가 발생했습니다.")

    @app_commands.command(name="시즌시작", description="새 시즌을 시작합니다.")
    @app_commands.describe(game="게임", name="시즌 이름")
    @app_commands.rename(game="게임", name="이름")
    async def season_start(
        self,
        interaction: discord.Interaction,
        name: str,
        game: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await self._send(interaction, "서버에서만 사용할 수 있습니다.")
            return
        if not await self._is_bot_admin(interaction):
            await self._send(interaction, "봇 관리자 권한이 필요합니다.")
            return
        await self._defer(interaction)
        try:
            season = await self.service.start_season(
                int(interaction.guild.id),
                name,
                game_key=game or "lol",
                manager_override=True,
            )
            await self._send(interaction, f"{_get(season, 'name')} 시즌을 시작했습니다.")
        except MatchError as exc:
            await self._send(interaction, str(exc))
        except Exception:
            logger.exception("시즌 시작 실패")
            await self._send(interaction, "시즌 시작 중 오류가 발생했습니다.")

    @app_commands.command(name="시즌종료", description="현재 시즌을 종료합니다.")
    @app_commands.describe(game="게임")
    @app_commands.rename(game="게임")
    async def season_end(
        self,
        interaction: discord.Interaction,
        game: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await self._send(interaction, "서버에서만 사용할 수 있습니다.")
            return
        if not await self._is_bot_admin(interaction):
            await self._send(interaction, "봇 관리자 권한이 필요합니다.")
            return
        await self._defer(interaction)
        try:
            season = await self.service.end_season(
                int(interaction.guild.id),
                game_key=game or "lol",
                manager_override=True,
            )
            await self._send(interaction, f"{_get(season, 'name')} 시즌을 종료했습니다.")
        except MatchError as exc:
            await self._send(interaction, str(exc))
        except Exception:
            logger.exception("시즌 종료 실패")
            await self._send(interaction, "시즌 종료 중 오류가 발생했습니다.")

    @app_commands.command(name="랭킹", description="시즌 MMR 랭킹을 확인합니다.")
    @app_commands.describe(game="게임", season="시즌 ID", role="라인", limit="표시 인원수")
    @app_commands.rename(game="게임", season="시즌", role="라인", limit="인원수")
    @app_commands.choices(role=ROLE_CHOICES)
    async def ranking(
        self,
        interaction: discord.Interaction,
        game: str | None = None,
        season: int | None = None,
        role: app_commands.Choice[str] | None = None,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        if interaction.guild is None:
            await self._send(interaction, "서버에서만 사용할 수 있습니다.")
            return
        await self._defer(interaction)
        try:
            entries = await self.service.ranking(
                int(interaction.guild.id),
                game_key=game or "lol",
                season_id=season,
                role=_choice_value(role),
                limit=int(limit),
            )
            role_value = _choice_value(role)
            title = f"{ROLE_LABELS.get(role_value, role_value)} MMR 랭킹" if role_value else "평균 라인 MMR 랭킹"
            embed = discord.Embed(title=title, colour=0x5865F2)
            if entries:
                lines = [
                    f"{index}. <@{int(_get(entry, 'user_id'))}> · "
                    f"{int(_get(entry, 'rating'))}점 · {int(_get(entry, 'games_played'))}경기"
                    for index, entry in enumerate(entries, start=1)
                ]
                embed.description = "\n".join(lines)
            else:
                embed.description = "아직 랭킹 기록이 없습니다."
            await self._send_embed(interaction, embed)
        except MatchError as exc:
            await self._send(interaction, str(exc))
        except Exception:
            logger.exception("내전 랭킹 조회 실패")
            await self._send(interaction, "랭킹 조회 중 오류가 발생했습니다.")

    @app_commands.command(name="라인변경", description="참가 중인 내전의 지망 라인을 바꿉니다.")
    @app_commands.describe(first="1지망", second="2지망", third="3지망")
    @app_commands.rename(first="1지망", second="2지망", third="3지망")
    @app_commands.choices(first=ROLE_CHOICES, second=ROLE_CHOICES, third=ROLE_CHOICES)
    async def change_roles(
        self,
        interaction: discord.Interaction,
        first: app_commands.Choice[str],
        second: app_commands.Choice[str],
        third: app_commands.Choice[str] | None = None,
    ) -> None:
        if interaction.guild is None or interaction.channel is None:
            await self._send(interaction, "서버 채널에서만 사용할 수 있습니다.")
            return
        await self._defer(interaction)
        try:
            active = await self._active_for_channel(
                int(interaction.guild.id), int(interaction.channel.id)
            )
            if active is None:
                await self._send(interaction, "이 채널에 활성 내전이 없습니다.")
                return
            match_id = int(_get(active, "id"))
            await self.service.update_preferences(
                match_id, int(interaction.user.id),
                _choice_value(first), _choice_value(second), _choice_value(third),
            )
            await self._refresh_message(interaction, match_id)
            await self._send(interaction, "지망 라인을 변경했습니다.")
        except MatchError as exc:
            await self._send(interaction, str(exc))
        except Exception:
            logger.exception("라인 변경 실패")
            await self._send(interaction, "라인 변경 중 오류가 발생했습니다.")

    @app_commands.command(name="등록", description="현재 시즌의 내 라인 MMR을 등록합니다.")
    @app_commands.describe(role="라인", tier="티어", game="게임")
    @app_commands.rename(role="라인", tier="티어", game="게임")
    @app_commands.choices(role=ROLE_CHOICES)
    async def register(
        self,
        interaction: discord.Interaction,
        role: app_commands.Choice[str],
        tier: str,
        game: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await self._send(interaction, "서버에서만 사용할 수 있습니다.")
            return
        try:
            canonical_tier, division = parse_compact_tier(tier)
        except RoleAssignmentError as exc:
            await self._send(interaction, str(exc))
            return
        await self._defer(interaction)
        try:
            value = await self.service.register_role_rating(
                int(interaction.guild.id),
                int(interaction.user.id),
                _choice_value(role),
                tier,
                game_key=game or "lol",
            )
            await self._send(
                interaction,
                f"{ROLE_LABELS.get(_choice_value(role), _choice_value(role))} MMR 등록 완료: "
                f"{compact_tier_label(canonical_tier, division)} · {value}점",
            )
        except MatchError as exc:
            await self._send(interaction, str(exc))
        except Exception:
            logger.exception("라인 MMR 등록 실패")
            await self._send(interaction, "라인 MMR 등록 중 오류가 발생했습니다.")

    @app_commands.command(name="mmr설정", description="현재 시즌의 라인 MMR을 설정합니다.")
    @app_commands.describe(
        user="설정할 사용자", role="라인", tier="티어",
        rating="직접 지정할 점수", game="게임",
    )
    @app_commands.rename(
        user="사용자", role="라인", tier="티어",
        rating="점수", game="게임",
    )
    @app_commands.choices(role=ROLE_CHOICES)
    async def set_mmr(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        role: app_commands.Choice[str],
        tier: str | None = None,
        rating: app_commands.Range[int, 0, 10000] | None = None,
        game: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await self._send(interaction, "서버에서만 사용할 수 있습니다.")
            return
        if not await self._is_bot_admin(interaction):
            await self._send(interaction, "봇 관리자 권한이 필요합니다.")
            return
        if tier is None and rating is None:
            await self._send(interaction, "티어 또는 직접 지정 점수가 필요합니다.")
            return
        if rating is None:
            try:
                parse_compact_tier(tier or "")
            except RoleAssignmentError as exc:
                await self._send(interaction, str(exc))
                return
        await self._defer(interaction)
        try:
            value = await self.service.set_role_rating(
                int(interaction.guild.id), int(user.id), _choice_value(role),
                game_key=game or "lol", tier=tier,
                rating=int(rating) if rating is not None else None,
                manager_override=True,
            )
            await self._send(
                interaction,
                f"<@{int(user.id)}> {ROLE_LABELS.get(_choice_value(role), _choice_value(role))} MMR을 {value}점으로 설정했습니다.",
            )
        except MatchError as exc:
            await self._send(interaction, str(exc))
        except Exception:
            logger.exception("라인 MMR 설정 실패")
            await self._send(interaction, "라인 MMR 설정 중 오류가 발생했습니다.")

    @app_commands.command(name="지명", description="Draft에서 현재 차례의 사용자를 지명합니다.")
    @app_commands.describe(user="지명할 사용자")
    @app_commands.rename(user="사용자")
    async def draft_pick(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if interaction.guild is None or interaction.channel is None:
            await self._send(interaction, "서버 채널에서만 사용할 수 있습니다.")
            return
        await self._defer(interaction)
        try:
            active = await self._active_for_channel(
                int(interaction.guild.id), int(interaction.channel.id)
            )
            if active is None:
                await self._send(interaction, "이 채널에 활성 내전이 없습니다.")
                return
            match_id = int(_get(active, "id"))
            result = await self.service.draft_pick(
                match_id, int(interaction.user.id), int(user.id)
            )
            await self._refresh_message(interaction, match_id)
            if bool(_get(result, "started", False)):
                voice_summary = await ensure_match_voice_channels(
                    self.service, interaction.guild, result,
                    self.team_a_voice_channel_id, self.team_b_voice_channel_id,
                )
                await self._refresh_message(interaction, match_id)
                text = "지명이 끝나 내전을 시작했습니다."
                if voice_summary.error:
                    text += f" 보이스 처리 실패: {voice_summary.error} 경기와 팀 배정은 저장됐습니다."
                await self._send(interaction, text)
            else:
                await self._send(interaction, f"<@{int(user.id)}>님을 지명했습니다.")
        except MatchError as exc:
            await self._send(interaction, str(exc))
        except Exception:
            logger.exception("Draft 지명 실패")
            await self._send(interaction, "지명 처리 중 오류가 발생했습니다.")

    @app_commands.command(name="결과", description="진행 중인 내전의 결과를 기록합니다.")
    @app_commands.describe(winner_team="승리 팀", memo="선택 메모")
    @app_commands.rename(winner_team="승리팀", memo="메모")
    @app_commands.choices(
        winner_team=[
            app_commands.Choice(name="A팀", value="A"),
            app_commands.Choice(name="B팀", value="B"),
        ]
    )
    async def result(
        self,
        interaction: discord.Interaction,
        winner_team: app_commands.Choice[str],
        memo: str | None = None,
    ) -> None:
        if interaction.guild is None or interaction.channel is None:
            await self._send(interaction, "서버 채널에서만 사용할 수 있습니다.")
            return
        await self._defer(interaction)
        try:
            active = await self._active_for_channel(int(interaction.guild.id), int(interaction.channel.id))
            if active is None:
                await self._send(interaction, "이 채널에 활성 내전이 없습니다.")
                return
            match_id = int(_get(active, "id"))
            finished = await self.service.finish_match(
                match_id,
                int(interaction.user.id),
                getattr(winner_team, "value", winner_team),
                memo=memo,
                manager_override=await self._is_bot_admin(interaction),
            )
            await close_empty_match_voice_channels(
                self.service, interaction.guild, finished
            )
            finished = await self.service.get_match(match_id) or finished
            await self._refresh_message(interaction, match_id)
            text = "내전 결과를 기록했습니다."
            if _get(finished, "voice_cleanup_at") is not None:
                delay = int(getattr(self.service, "voice_cleanup_delay_seconds", 600))
                text += f" 보이스 채널은 {delay // 60}분 후 삭제됩니다."
            await self._send(interaction, text)
        except MatchError as exc:
            await self._send(interaction, str(exc) or "내전 결과를 기록할 수 없습니다.")
        except Exception:
            logger.exception("내전 결과 처리 실패")
            await self._send(interaction, "내전 결과 처리 중 오류가 발생했습니다.")

    @create.autocomplete("game")
    @stats.autocomplete("game")
    @season_start.autocomplete("game")
    @season_end.autocomplete("game")
    @ranking.autocomplete("game")
    @register.autocomplete("game")
    @set_mmr.autocomplete("game")
    async def game_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        try:
            games = await self.service.list_games()
        except Exception:
            logger.exception("게임 자동완성 조회 실패")
            return []
        query = current.casefold()
        return [
            app_commands.Choice(name=str(_get(game, "name")), value=str(_get(game, "key")))
            for game in games
            if query in str(_get(game, "name")).casefold()
            or query in str(_get(game, "key")).casefold()
        ][:25]

    @register.autocomplete("tier")
    @set_mmr.autocomplete("tier")
    async def tier_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        query = current.casefold()
        return [
            app_commands.Choice(name=value, value=value)
            for value in _COMPACT_TIER_CHOICES
            if query in value.casefold()
        ][:25]

    async def _active_for_channel(self, guild_id: int, channel_id: int) -> Any | None:
        return await self.service.get_active_match(guild_id, channel_id)

    async def _refresh_message(self, interaction: discord.Interaction, match_id: int) -> None:
        latest = await self.service.get_match(match_id)
        if latest is None or interaction.channel is None:
            return
        message_id = _get(latest, "message_id")
        if not message_id:
            return
        try:
            message = await interaction.channel.fetch_message(int(message_id))
            terminal = str(_get(latest, "status", "")) in {"FINISHED", "CANCELLED"}
            await _safe_edit(
                message,
                embed=render_match(latest),
                view=MatchView(
                    self.service,
                    match_id,
                    status=str(_get(latest, "status", "")),
                    disabled=terminal,
                    team_a_voice_channel_id=self.team_a_voice_channel_id,
                    team_b_voice_channel_id=self.team_b_voice_channel_id,
                ),
            )
        except discord.NotFound:
            logger.warning("내전 메시지를 찾을 수 없음", extra={"match_id": match_id})
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("내전 메시지 갱신 실패", extra={"match_id": match_id})
        except Exception:
            logger.exception("내전 메시지 갱신 중 예상 못 한 오류", extra={"match_id": match_id})

    async def _announce_member_change(
        self,
        interaction: discord.Interaction,
        result: Any,
        target_user_id: int,
        action: str,
    ) -> None:
        promoted = tuple(_get(result, "promoted_user_ids", ()) or ())
        text = f"<@{target_user_id}>님을 내전에서 {action}했습니다."
        if promoted:
            text += " 대기자 승격: " + ", ".join(f"<@{int(user_id)}>" for user_id in promoted)
        try:
            await _safe_send(interaction.channel, text)
        except Exception:
            logger.exception("참가자 변경 알림 전송 실패")


def add_match_commands(
    tree: app_commands.CommandTree,
    service: Any,
    guild_id: int,
    *,
    team_a_voice_channel_id: int | None = None,
    team_b_voice_channel_id: int | None = None,
    inhouse_voice_category_id: int | None = None,
) -> MatchCommandGroup:
    """길드 하나에 명령 그룹을 등록한다."""

    group = MatchCommandGroup(
        service,
        team_a_voice_channel_id=team_a_voice_channel_id,
        team_b_voice_channel_id=team_b_voice_channel_id,
        inhouse_voice_category_id=inhouse_voice_category_id,
    )
    tree.add_command(group, guild=discord.Object(id=int(guild_id)))
    return group


__all__ = ["MatchCommandGroup", "add_match_commands"]
