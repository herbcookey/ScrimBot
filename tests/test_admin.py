from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import discord

from inhouse_bot.config import load_settings
from inhouse_bot.repositories.matches import (
    BotAdminAlreadyExistsError,
    BotAdminNotFoundError,
    PermissionDeniedError,
)


OWNER_ID = 9_876_543_210


def _set_required_env(monkeypatch, *, owner: str | None = str(OWNER_ID)) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")
    if owner is None:
        monkeypatch.delenv("BOT_OWNER_ID", raising=False)
    else:
        monkeypatch.setenv("BOT_OWNER_ID", owner)


def test_load_settings_requires_and_validates_bot_owner_id(monkeypatch):
    monkeypatch.setattr("inhouse_bot.config.load_dotenv", lambda: None)
    _set_required_env(monkeypatch, owner=None)
    with pytest.raises(RuntimeError, match="BOT_OWNER_ID"):
        load_settings()

    for value in ("not-an-integer", "0", "-1"):
        _set_required_env(monkeypatch, owner=value)
        with pytest.raises(RuntimeError, match="BOT_OWNER_ID"):
            load_settings()

    _set_required_env(monkeypatch)
    assert load_settings().bot_owner_id == OWNER_ID


@pytest.mark.asyncio
async def test_bot_owner_is_admin_without_a_database_row(service_and_scope):
    service, guild_id, _channel_id = service_and_scope

    assert await service.is_bot_owner(OWNER_ID)
    assert await service.is_bot_admin(guild_id, OWNER_ID)
    assert not await service.is_bot_owner(OWNER_ID + 1)
    async with service.repository.pool.acquire() as conn:
        assert await conn.fetchrow(
            "SELECT 1 FROM bot_admins WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            OWNER_ID,
        ) is None


@pytest.mark.asyncio
async def test_only_owner_can_add_or_remove_admin_and_owner_cannot_be_removed(
    service_and_scope,
):
    service, guild_id, _channel_id = service_and_scope
    admin_id = OWNER_ID + 1

    with pytest.raises(PermissionDeniedError):
        await service.add_bot_admin(guild_id, admin_id, OWNER_ID + 2)
    with pytest.raises(PermissionDeniedError):
        await service.add_bot_admin(guild_id, OWNER_ID, OWNER_ID)
    await service.add_bot_admin(guild_id, OWNER_ID, admin_id)
    assert await service.is_bot_admin(guild_id, admin_id)
    assert await service.list_bot_admins(guild_id) == [admin_id]
    with pytest.raises(BotAdminAlreadyExistsError, match="이미 봇 관리자"):
        await service.add_bot_admin(guild_id, OWNER_ID, admin_id)

    with pytest.raises(PermissionDeniedError):
        await service.remove_bot_admin(guild_id, admin_id, OWNER_ID)
    await service.remove_bot_admin(guild_id, OWNER_ID, admin_id)
    assert not await service.is_bot_admin(guild_id, admin_id)
    with pytest.raises(BotAdminNotFoundError, match="등록된 봇 관리자"):
        await service.remove_bot_admin(guild_id, OWNER_ID, admin_id)

    with pytest.raises(PermissionDeniedError):
        await service.remove_bot_admin(guild_id, OWNER_ID, OWNER_ID)
    assert await service.is_bot_admin(guild_id, OWNER_ID)


@pytest.mark.asyncio
async def test_bot_admin_rows_are_guild_scoped(service_and_scope):
    service, guild_id, _channel_id = service_and_scope
    other_guild_id = guild_id + 1
    admin_id = OWNER_ID + 1

    await service.add_bot_admin(guild_id, OWNER_ID, admin_id)
    assert await service.is_bot_admin(guild_id, admin_id)
    assert not await service.is_bot_admin(other_guild_id, admin_id)
    with pytest.raises(PermissionDeniedError):
        await service.add_bot_admin(other_guild_id, admin_id, OWNER_ID + 2)


@pytest.mark.asyncio
async def test_creator_can_manage_match_after_leaving(service_and_scope):
    service, guild_id, channel_id = service_and_scope
    match = await service.create_match(guild_id, channel_id, 1, "생성자 유지")
    match = await service.leave_match(match.id, 1)
    assert match.creator_id == 1
    cancelled = await service.cancel_match(match.id, 1)
    assert cancelled.status == "CANCELLED"


def _interaction(*, manage_guild: bool = False, guild_id: int = 123):
    response = SimpleNamespace(
        is_done=lambda: True,
        send_message=AsyncMock(),
        defer=AsyncMock(),
    )
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id),
        channel=SimpleNamespace(type=discord.ChannelType.text),
        user=SimpleNamespace(
            id=OWNER_ID + 1,
            guild_permissions=SimpleNamespace(manage_guild=manage_guild),
        ),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _command(group, name: str):
    return next(command for command in group.commands if command.name == name)


@pytest.mark.asyncio
async def test_manage_guild_alone_does_not_grant_admin_commands():
    from inhouse_bot.discord.commands import MatchCommandGroup

    service = SimpleNamespace(
        is_bot_admin=AsyncMock(return_value=False),
        start_season=AsyncMock(),
        set_role_rating=AsyncMock(),
    )
    group = MatchCommandGroup(service)
    interaction = _interaction(manage_guild=True)
    await _command(group, "시즌시작").callback(group, interaction, "시즌 1", None)
    service.start_season.assert_not_awaited()

    interaction = _interaction(manage_guild=True)
    await _command(group, "mmr설정").callback(
        group,
        interaction,
        SimpleNamespace(id=77, bot=False),
        discord.app_commands.Choice(name="탑", value="TOP"),
        None,
        1800,
        None,
    )
    service.set_role_rating.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_admin_can_start_season_and_set_mmr():
    from inhouse_bot.discord.commands import MatchCommandGroup

    service = SimpleNamespace(
        is_bot_admin=AsyncMock(return_value=True),
        start_season=AsyncMock(return_value=SimpleNamespace(name="시즌 1")),
        set_role_rating=AsyncMock(return_value=1800),
    )
    group = MatchCommandGroup(service)
    interaction = _interaction()
    await _command(group, "시즌시작").callback(group, interaction, "시즌 1", None)
    service.start_season.assert_awaited_once()
    assert service.start_season.await_args.kwargs["manager_override"] is True

    interaction = _interaction()
    await _command(group, "mmr설정").callback(
        group,
        interaction,
        SimpleNamespace(id=77, bot=False),
        discord.app_commands.Choice(name="탑", value="TOP"),
        None,
        1800,
        None,
    )
    service.set_role_rating.assert_awaited_once()
    assert service.set_role_rating.await_args.kwargs["manager_override"] is True


@pytest.mark.asyncio
async def test_admin_commands_use_database_admin_check_and_target_validation():
    from inhouse_bot.discord.commands import MatchCommandGroup

    service = SimpleNamespace(
        is_bot_admin=AsyncMock(return_value=True),
        add_bot_admin=AsyncMock(),
        remove_bot_admin=AsyncMock(),
        list_bot_admins=AsyncMock(return_value=[]),
    )
    group = MatchCommandGroup(service)
    target = SimpleNamespace(id=99, bot=False)
    interaction = _interaction()

    await _command(group, "관리자추가").callback(group, interaction, target)
    service.add_bot_admin.assert_awaited_once_with(123, OWNER_ID + 1, 99)

    await _command(group, "관리자삭제").callback(group, interaction, target)
    service.remove_bot_admin.assert_awaited_once_with(123, OWNER_ID + 1, 99)

    await _command(group, "관리자목록").callback(group, interaction)
    service.list_bot_admins.assert_awaited_once_with(123)


def test_admin_slash_commands_are_registered():
    from inhouse_bot.discord.commands import add_match_commands

    class _Tree:
        def __init__(self):
            self.commands = []

        def add_command(self, command, **_kwargs):
            self.commands.append(command)

    tree = _Tree()
    group = add_match_commands(tree, object(), 123)
    assert group in tree.commands
    assert {command.name for command in group.commands} >= {
        "관리자추가",
        "관리자삭제",
        "관리자목록",
    }
