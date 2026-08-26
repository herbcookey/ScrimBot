from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import inhouse_bot.discord.voice as voice


class _Member:
    def __init__(self, user_id):
        self.id = user_id
        self.move_to = AsyncMock()


class _Voice:
    def __init__(self, channel_id, name, guild, category, members=()):
        self.id = channel_id
        self.name = name
        self.guild = guild
        self.category_id = category.id
        self.members = list(members)
        self.delete = AsyncMock()

    def permissions_for(self, _member):
        return SimpleNamespace(connect=True, move_members=True)


class _Category:
    def __init__(self, channel_id, guild):
        self.id = channel_id
        self.guild = guild
        self.voice_channels = []

    def permissions_for(self, _member):
        return SimpleNamespace(
            manage_channels=True, view_channel=True, connect=True, move_members=True
        )


class _Guild:
    def __init__(self):
        self.id = 1
        self.default_role = object()
        self.me = _Member(999)
        self.voice_channels = []
        self._channels = {}
        self._members = {user_id: _Member(user_id) for user_id in (1, 2)}
        self.created = []

    def add_category(self, channel_id=10):
        category = _Category(channel_id, self)
        self._channels[channel_id] = category
        return category

    def add_voice(self, channel_id, name, category):
        channel = _Voice(channel_id, name, self, category)
        self._channels[channel_id] = channel
        self.voice_channels.append(channel)
        category.voice_channels.append(channel)
        return channel

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_member(self, user_id):
        return self._members.get(user_id)

    async def fetch_member(self, user_id):
        return self._members[user_id]

    async def create_voice_channel(self, name, *, category, **kwargs):
        channel = self.add_voice(100 + len(self.created), name, category)
        self.created.append((channel, kwargs))
        return channel


class _Service:
    def __init__(self, match):
        self.match = match
        self.cleanup = None

    async def get_match(self, _match_id):
        return self.match

    async def set_voice_channel_id(self, _match_id, team, channel_id, **_kwargs):
        setattr(self.match, f"team_{team.lower()}_voice_channel_id", channel_id)
        return self.match

    async def claim_empty_voice_channel(self, _guild_id, channel_id, **_kwargs):
        for team in ("A", "B"):
            if (
                getattr(self.match, f"team_{team.lower()}_voice_channel_id") == channel_id
                and getattr(self.match, f"team_{team.lower()}_voice_closed_at") is None
            ):
                setattr(self.match, f"team_{team.lower()}_voice_closed_at", object())
                return self.match, team
        return None

    async def reopen_empty_voice_channel(self, _match_id, team, _channel_id):
        setattr(self.match, f"team_{team.lower()}_voice_closed_at", None)
        return self.match

    async def complete_empty_voice_channel(self, _match_id, team, _channel_id):
        setattr(self.match, f"team_{team.lower()}_voice_channel_id", None)
        return self.match

    async def record_voice_cleanup(self, _match_id, **kwargs):
        self.cleanup = kwargs
        if kwargs["clear_team_a"]:
            self.match.team_a_voice_channel_id = None
        if kwargs["clear_team_b"]:
            self.match.team_b_voice_channel_id = None
        return self.match


def _match(match_id=104):
    return SimpleNamespace(
        id=match_id,
        status="PLAYING",
        team_size=1,
        voice_category_id=10,
        team_a_voice_channel_id=None,
        team_b_voice_channel_id=None,
        team_a_voice_closed_at=None,
        team_b_voice_closed_at=None,
        voice_cleanup_at=None,
        participants=(
            SimpleNamespace(user_id=1, team="A"),
            SimpleNamespace(user_id=2, team="B"),
        ),
    )


@pytest.fixture(autouse=True)
def discord_channel_types(monkeypatch):
    monkeypatch.setattr(voice.discord, "VoiceChannel", _Voice)
    monkeypatch.setattr(voice.discord, "CategoryChannel", _Category)
    voice._VOICE_LOCKS.clear()


def test_dynamic_voice_names_and_team_mapping():
    assert voice.match_voice_channel_name(104, "A") == "104번째 내전 1팀"
    assert voice.match_voice_channel_name(104, "B") == "104번째 내전 2팀"


@pytest.mark.asyncio
async def test_create_reuse_and_only_missing_channel():
    guild = _Guild()
    category = guild.add_category()
    match = _match()
    service = _Service(match)

    first = await voice.ensure_match_voice_channels(service, guild, match, 50, 51)
    assert [item[0].name for item in guild.created] == [
        "104번째 내전 1팀", "104번째 내전 2팀"
    ]
    assert len(first.created_channel_ids) == 2
    await voice.ensure_match_voice_channels(service, guild, match)
    assert len(guild.created) == 2

    match.team_b_voice_channel_id = None
    guild._channels.pop(first.team_b_channel_id)
    category.voice_channels = [
        channel for channel in category.voice_channels
        if channel.id != first.team_b_channel_id
    ]
    guild.voice_channels = [
        channel for channel in guild.voice_channels
        if channel.id != first.team_b_channel_id
    ]
    await voice.ensure_match_voice_channels(service, guild, match)
    assert len(guild.created) == 3


@pytest.mark.asyncio
async def test_orphan_adoption_and_duplicate_name_rejection():
    guild = _Guild()
    category = guild.add_category()
    match = _match()
    orphan = guild.add_voice(77, "104번째 내전 1팀", category)
    service = _Service(match)
    result = await voice.ensure_match_voice_channels(service, guild, match)
    assert result.team_a_channel_id == orphan.id
    assert len(result.created_channel_ids) == 1

    match = _match(105)
    service = _Service(match)
    guild.add_voice(78, "105번째 내전 1팀", category)
    guild.add_voice(79, "105번째 내전 1팀", category)
    result = await voice.ensure_match_voice_channels(service, guild, match)
    assert "여러 개" in result.error
    assert match.team_a_voice_channel_id is None


@pytest.mark.asyncio
async def test_new_channel_is_deleted_when_db_save_fails():
    guild = _Guild()
    guild.add_category()
    match = _match()

    class _FailingService(_Service):
        async def set_voice_channel_id(self, match_id, team, channel_id, **kwargs):
            if team == "B":
                raise RuntimeError("경기가 이미 종료됨")
            return await super().set_voice_channel_id(match_id, team, channel_id, **kwargs)

    result = await voice.ensure_match_voice_channels(_FailingService(match), guild, match)

    assert result.error == "경기가 이미 종료됨"
    assert match.team_a_voice_channel_id is not None
    assert match.team_b_voice_channel_id is None
    guild.created[1][0].delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_uses_only_saved_ids():
    guild = _Guild()
    category = guild.add_category()
    saved_a = guild.add_voice(20, "104번째 내전 1팀", category)
    saved_b = guild.add_voice(21, "104번째 내전 2팀", category)
    saved_a.members.append(guild.get_member(1))
    guild.add_voice(22, "104번째 내전 1팀", category)
    match = _match()
    match.team_a_voice_channel_id = 20
    match.team_b_voice_channel_id = 21
    match.status = "FINISHED"
    match.voice_cleanup_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    service = _Service(match)
    result = await voice.cleanup_match_voice_channels(service, guild, match)
    assert result.deleted == 2 and result.failed == 0
    saved_a.delete.assert_awaited_once()
    saved_b.delete.assert_awaited_once()
    assert service.cleanup["clear_team_a"] and service.cleanup["clear_team_b"]


@pytest.mark.asyncio
async def test_fixed_channel_fallback_does_not_create_or_delete():
    guild = _Guild()
    category = guild.add_category()
    guild.add_voice(50, "고정 1팀", category)
    guild.add_voice(51, "고정 2팀", category)
    match = _match()
    match.voice_category_id = None
    service = _Service(match)
    result = await voice.ensure_match_voice_channels(service, guild, match, 50, 51)
    assert result.error is None and guild.created == []
    assert match.team_a_voice_channel_id is None
    assert match.team_b_voice_channel_id is None


@pytest.mark.asyncio
async def test_partial_cleanup_keeps_failed_channel_id():
    guild = _Guild()
    category = guild.add_category()
    other_category = guild.add_category(11)
    saved_a = guild.add_voice(20, "104번째 내전 1팀", category)
    guild.add_voice(21, "104번째 내전 2팀", other_category)
    match = _match()
    match.team_a_voice_channel_id = 20
    match.team_b_voice_channel_id = 21
    match.status = "FINISHED"
    match.voice_cleanup_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    service = _Service(match)
    result = await voice.cleanup_match_voice_channels(service, guild, match)
    assert result.deleted == 1 and result.failed == 1
    saved_a.delete.assert_awaited_once()
    assert service.cleanup["clear_team_a"] is True
    assert service.cleanup["clear_team_b"] is False
    assert service.cleanup["retry_at"] is not None


@pytest.mark.asyncio
async def test_empty_dynamic_channel_deletes_only_claimed_team():
    guild = _Guild()
    category = guild.add_category()
    channel_a = guild.add_voice(20, "104번째 내전 1팀", category)
    channel_b = guild.add_voice(21, "104번째 내전 2팀", category)
    match = _match()
    match.team_a_voice_channel_id = 20
    match.team_b_voice_channel_id = 21
    service = _Service(match)

    result = await voice.close_empty_match_voice_channel(service, channel_a)

    assert result.deleted == 1
    channel_a.delete.assert_awaited_once()
    channel_b.delete.assert_not_awaited()
    assert match.team_a_voice_channel_id is None
    assert match.team_a_voice_closed_at is not None
    assert match.team_b_voice_channel_id == 21


@pytest.mark.asyncio
async def test_nonempty_untracked_and_fixed_channels_are_not_deleted():
    guild = _Guild()
    category = guild.add_category()
    member = guild.get_member(1)
    nonempty = guild.add_voice(20, "104번째 내전 1팀", category)
    nonempty.members.append(member)
    untracked = guild.add_voice(22, "104번째 내전 1팀", category)
    match = _match()
    match.team_a_voice_channel_id = 20
    service = _Service(match)

    assert await voice.close_empty_match_voice_channel(service, nonempty) == voice.VoiceCleanupSummary()
    assert await voice.close_empty_match_voice_channel(service, untracked) == voice.VoiceCleanupSummary()
    nonempty.delete.assert_not_awaited()
    untracked.delete.assert_not_awaited()

    match.voice_category_id = None
    match.team_a_voice_channel_id = None
    fixed = guild.add_voice(50, "고정 1팀", category)
    assert await voice.close_empty_match_voice_channel(service, fixed) == voice.VoiceCleanupSummary()
    fixed.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_empty_events_delete_once():
    guild = _Guild()
    category = guild.add_category()
    channel = guild.add_voice(20, "104번째 내전 1팀", category)
    match = _match()
    match.team_a_voice_channel_id = 20
    service = _Service(match)

    results = await asyncio.gather(
        voice.close_empty_match_voice_channel(service, channel),
        voice.close_empty_match_voice_channel(service, channel),
    )

    assert sum(result.deleted for result in results) == 1
    channel.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_empty_delete_keeps_id_and_poll_reopens_when_member_returns(monkeypatch):
    class _Forbidden(Exception):
        pass

    monkeypatch.setattr(voice.discord, "Forbidden", _Forbidden)
    guild = _Guild()
    category = guild.add_category()
    channel = guild.add_voice(20, "104번째 내전 1팀", category)
    channel.delete.side_effect = _Forbidden()
    match = _match()
    match.team_a_voice_channel_id = 20
    service = _Service(match)

    result = await voice.close_empty_match_voice_channel(service, channel)
    assert result.failed == 1
    assert match.team_a_voice_channel_id == 20
    assert match.team_a_voice_closed_at is not None

    channel.delete.side_effect = None
    channel.members.append(guild.get_member(1))
    await voice.cleanup_match_voice_channels(service, guild, match)
    assert match.team_a_voice_closed_at is None
    assert match.team_a_voice_channel_id == 20
    assert channel.delete.await_count == 1


@pytest.mark.asyncio
async def test_not_found_is_success_and_closed_channel_is_not_recreated(monkeypatch):
    class _NotFound(Exception):
        pass

    monkeypatch.setattr(voice.discord, "NotFound", _NotFound)
    guild = _Guild()
    category = guild.add_category()
    channel_a = guild.add_voice(20, "104번째 내전 1팀", category)
    channel_b = guild.add_voice(21, "104번째 내전 2팀", category)
    channel_a.delete.side_effect = _NotFound()
    match = _match()
    match.team_a_voice_channel_id = 20
    match.team_b_voice_channel_id = 21
    service = _Service(match)

    result = await voice.close_empty_match_voice_channel(service, channel_a)
    assert result.deleted == 1
    assert match.team_a_voice_channel_id is None

    await voice.ensure_match_voice_channels(service, guild, match)
    assert all(created[0].name != "104번째 내전 1팀" for created in guild.created)
    assert match.team_b_voice_channel_id == channel_b.id
