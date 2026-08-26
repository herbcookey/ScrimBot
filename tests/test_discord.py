from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from inhouse_bot.discord.renderer import render_match
from inhouse_bot.discord.voice import move_match_participants


def _fields(embed):
    return {field.name: field.value for field in embed.fields}


def _participant(user_id, *, ready=False, team=None):
    return SimpleNamespace(
        user_id=user_id,
        ready_at=datetime.now(timezone.utc) if ready else None,
        team=team,
    )


def _role_participant(user_id, *, team=None, role=None, rating=None, preferences=("TOP", "MID")):
    return SimpleNamespace(
        user_id=user_id,
        ready_at=None,
        team=team,
        assigned_role=role,
        role_rating_snapshot=rating,
        preferred_role_1=preferences[0],
        preferred_role_2=preferences[1],
        preferred_role_3=None,
        preferences=preferences,
        average_role_rating=1500,
    )


def test_renderer_ready_waitlist_and_deadlines():
    deadline = datetime(2030, 1, 1, tzinfo=timezone.utc)
    match = SimpleNamespace(
        status="READY_CHECK",
        title="준비 테스트",
        capacity=10,
        creator_id=1,
        participants=tuple(_participant(user_id, ready=user_id <= 2) for user_id in range(1, 11)),
        waitlist=(_participant(11),),
        ready_count=2,
        ready_deadline_at=deadline,
        recruitment_deadline_at=None,
    )
    fields = _fields(render_match(match))
    assert fields["상태"] == "준비 확인"
    assert fields["준비"] == "2 / 10"
    assert "<@1>" in fields["준비 완료"] and "<@3>" in fields["미준비"]
    assert "<@11>" in fields["대기자"]
    assert "<t:" in fields["준비 마감"]

    recruiting = SimpleNamespace(
        status="RECRUITING",
        title="모집 테스트",
        capacity=10,
        creator_id=1,
        participants=(_participant(1),),
        waitlist=(_participant(2),),
        recruitment_deadline_at=deadline,
        ready_deadline_at=None,
    )
    recruiting_fields = _fields(render_match(recruiting))
    assert "<t:" in recruiting_fields["모집 마감"]
    assert "<@2>" in recruiting_fields["대기자"]


def test_match_view_ids_and_disabled_state():
    from inhouse_bot.discord.views import MatchView

    view = MatchView(object(), 42, status="READY_CHECK", disabled=True)
    ids = {item.custom_id for item in view.children}
    assert ids == {
        "match:42:join",
        "match:42:leave",
        "match:42:start",
        "match:42:ready",
        "match:42:cancel",
    }
    assert all(item.disabled for item in view.children)

    playing = MatchView(object(), 42, status="PLAYING")
    assert playing.join_button.disabled
    assert playing.leave_button.disabled
    assert playing.start_button.disabled
    assert playing.ready_button.disabled
    assert not playing.cancel_button.disabled

    drafting = MatchView(object(), 42, status="DRAFTING")
    assert drafting.cancel_button.disabled is False
    assert all(
        item.disabled for item in (
            drafting.join_button, drafting.leave_button,
            drafting.start_button, drafting.ready_button,
        )
    )


def test_renderer_drafting_and_assigned_roles():
    drafting = SimpleNamespace(
        status="DRAFTING", title="지명 테스트", capacity=10, creator_id=1,
        role_rating_enabled=True, captain_a_id=1, captain_b_id=2,
        draft_pick_index=0,
        participants=(
            _role_participant(1, team="A"),
            _role_participant(2, team="B"),
            _role_participant(3),
        ),
        waitlist=(),
    )
    fields = _fields(render_match(drafting))
    assert fields["상태"] == "주장 지명"
    assert "A팀 <@1>" == fields["현재 지명"]
    assert "탑/미드" in fields["미지명"] and "1500점" in fields["미지명"]

    playing = SimpleNamespace(
        status="PLAYING", title="라인 테스트", capacity=2, creator_id=1,
        role_rating_enabled=True,
        voice_category_id=10,
        team_a_voice_channel_id=20,
        team_b_voice_channel_id=21,
        participants=(
            _role_participant(1, team="A", role="TOP", rating=1680),
            _role_participant(2, team="B", role="TOP", rating=1922),
        ),
        waitlist=(),
    )
    fields = _fields(render_match(playing))
    assert "탑 1680점" in fields["A팀"]
    assert "탑 1922점" in fields["B팀"]
    assert fields["1팀 보이스"] == "<#20>"
    assert fields["2팀 보이스"] == "<#21>"

    playing.team_b_voice_channel_id = None
    fields = _fields(render_match(playing))
    assert fields["보이스"] == "보이스 채널 생성 실패"


@pytest.mark.asyncio
async def test_role_join_button_opens_modal_before_defer():
    from inhouse_bot.discord.views import JoinPreferencesModal, MatchView

    service = SimpleNamespace(
        get_match=AsyncMock(return_value=SimpleNamespace(role_rating_enabled=True))
    )
    view = MatchView(service, 42, status="RECRUITING")
    response = SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7), response=response, message=object()
    )
    await view._join(interaction)
    modal = response.send_modal.await_args.args[0]
    assert isinstance(modal, JoinPreferencesModal)
    assert modal.custom_id == "match:42:join_roles"
    assert modal.first.required and modal.second.required and not modal.third.required


def test_create_command_recruitment_minutes_is_optional():
    from inhouse_bot.discord.commands import MatchCommandGroup

    parameter = MatchCommandGroup.create._params["recruitment_minutes"]
    assert parameter.required is False and parameter.default is None
    assert parameter.min_value == 5 and parameter.max_value == 1440


def test_phase3a_command_options_are_optional_and_bounded():
    from inhouse_bot.discord.commands import MatchCommandGroup

    assert MatchCommandGroup.create._params["game"].required is False
    assert MatchCommandGroup.create._params["game"].autocomplete is not None
    assert MatchCommandGroup.stats._params["game"].required is False
    assert MatchCommandGroup.stats._params["season"].required is False
    assert MatchCommandGroup.season_start._params["name"].required is True
    assert MatchCommandGroup.season_end._params["game"].required is False
    limit = MatchCommandGroup.ranking._params["limit"]
    assert limit.default == 10 and limit.min_value == 1 and limit.max_value == 25


def test_phase3b_command_options_and_names():
    from inhouse_bot.discord.commands import MatchCommandGroup

    mode = MatchCommandGroup.create._params["assignment_mode"]
    assert mode.required is False and mode.default is None
    assert MatchCommandGroup.create._params["preferred_role_1"].required is False
    assert MatchCommandGroup.create._params["preferred_role_2"].required is False
    assert MatchCommandGroup.ranking._params["role"].required is False
    assert MatchCommandGroup.set_mmr.name == "mmr설정"
    assert MatchCommandGroup.set_mmr._params["game"].autocomplete is not None


def test_load_settings_rejects_duplicate_voice_channels(monkeypatch):
    from inhouse_bot.config import load_settings

    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")
    monkeypatch.setenv("TEAM_A_VOICE_CHANNEL_ID", "42")
    monkeypatch.setenv("TEAM_B_VOICE_CHANNEL_ID", "42")
    with pytest.raises(RuntimeError, match="달라야 합니다"):
        load_settings()


def test_voice_cleanup_delay_allows_zero_and_rejects_negative(monkeypatch):
    from inhouse_bot.config import load_settings

    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")
    monkeypatch.setenv("VOICE_CLEANUP_DELAY_SECONDS", "0")
    assert load_settings().voice_cleanup_delay_seconds == 0
    monkeypatch.setenv("VOICE_CLEANUP_DELAY_SECONDS", "-1")
    with pytest.raises(RuntimeError, match="0 이상의 정수"):
        load_settings()


class _FakeVoiceChannel:
    def __init__(self, channel_id, members=()):
        self.id = channel_id
        self.members = list(members)

    def permissions_for(self, _member):
        return SimpleNamespace(connect=True, move_members=True)


class _FakeGuild:
    def __init__(self, channels):
        self.voice_channels = list(channels)
        self.me = object()
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


@pytest.mark.asyncio
async def test_voice_no_config_and_disconnected_are_safe(monkeypatch):
    import inhouse_bot.discord.voice as voice

    monkeypatch.setattr(voice.discord, "VoiceChannel", _FakeVoiceChannel)
    member = SimpleNamespace(id=1, move_to=AsyncMock())
    channel_a = _FakeVoiceChannel(10, [member])
    channel_b = _FakeVoiceChannel(11)
    guild = _FakeGuild([channel_a, channel_b])
    match = SimpleNamespace(
        participants=(
            _participant(1, team="A"),
            _participant(2, team="B"),
        )
    )

    assert await move_match_participants(guild, match, None, None) == voice.VoiceMoveSummary()
    summary = await move_match_participants(guild, match, 10, 11)
    assert summary.success == 1 and summary.skipped == 1 and summary.failed == 0
    member.move_to.assert_awaited_once_with(channel_a)


@pytest.mark.asyncio
async def test_voice_team_channels_and_per_user_failure_isolated(monkeypatch):
    import inhouse_bot.discord.voice as voice

    monkeypatch.setattr(voice.discord, "VoiceChannel", _FakeVoiceChannel)
    failed = SimpleNamespace(id=1, move_to=AsyncMock(side_effect=RuntimeError("이동 실패")))
    moved = SimpleNamespace(id=2, move_to=AsyncMock())
    channel_a = _FakeVoiceChannel(20, [failed])
    channel_b = _FakeVoiceChannel(21, [moved])
    guild = _FakeGuild([channel_a, channel_b])
    match = SimpleNamespace(
        participants=(_participant(1, team="A"), _participant(2, team="B"))
    )

    summary = await move_match_participants(guild, match, 20, 21)
    assert summary.success == 1 and summary.failed == 1 and summary.skipped == 0
    failed.move_to.assert_awaited_once_with(channel_a)
    moved.move_to.assert_awaited_once_with(channel_b)
