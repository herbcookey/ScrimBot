from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import discord

from inhouse_bot.discord.renderer import render_match
from inhouse_bot.discord.voice import move_match_participants
from inhouse_bot.repositories.matches import RoleRatingAlreadyExistsError


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
        draft_deadline_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
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
    assert "<t:" in fields["지명 마감"]

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
    assert MatchCommandGroup.create._params["preferred_role_1"].required is True
    assert MatchCommandGroup.create._params["preferred_role_2"].required is True
    assert MatchCommandGroup.create._params["preferred_role_3"].required is False
    assert MatchCommandGroup.ranking._params["role"].required is False
    assert MatchCommandGroup.set_mmr.name == "mmr설정"
    assert MatchCommandGroup.set_mmr._params["game"].autocomplete is not None
    assert "detail" not in MatchCommandGroup.set_mmr._params
    assert MatchCommandGroup.register._params["tier"].autocomplete is not None
    assert "user" not in MatchCommandGroup.register._params


@pytest.mark.asyncio
async def test_usage_command_sends_ephemeral_command_guide():
    from inhouse_bot.discord.commands import MatchCommandGroup

    group = MatchCommandGroup(SimpleNamespace())
    interaction = _command_interaction(channel_type=discord.ChannelType.text)

    await MatchCommandGroup.usage.callback(group, interaction)

    assert MatchCommandGroup.usage.name == "사용법"
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert embed.title == "내전 봇 사용법"
    guide = "\n".join(
        [embed.description or ""]
        + [f"{field.name}\n{field.value}" for field in embed.fields]
    )
    for command in (
        "/내전 생성",
        "/내전 라인변경",
        "/내전 지명",
        "/내전 결과",
        "/내전 등록",
        "/내전 전적",
        "/내전 랭킹",
        "/내전 시즌시작",
        "/내전 mmr설정",
        "/내전 관리자목록",
        "/내전 패치내역",
    ):
        assert command in guide
    assert "선택할 모든 지망" in guide
    assert "1·2지망을 반드시 선택" in guide
    assert "생성자는 자동으로 참가" in guide
    assert "모집/준비 확인 중" in guide
    assert "모집·준비 마감 이후" in guide
    assert "지명 마감까지 선택하지 않으면 내전이 자동 취소" in guide
    assert "패널과 실패한 모집 알림" in embed.footer.text
    assert all(len(field.value) <= 1024 for field in embed.fields)
    assert len(embed) <= 6000


@pytest.mark.asyncio
async def test_patch_notes_command_sends_ephemeral_version_history():
    from inhouse_bot import __version__
    from inhouse_bot.discord.commands import MatchCommandGroup

    group = MatchCommandGroup(SimpleNamespace())
    interaction = _command_interaction(channel_type=discord.ChannelType.text)

    await MatchCommandGroup.patch_notes.callback(group, interaction)

    assert MatchCommandGroup.patch_notes.name == "패치내역"
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert __version__ == "1.0.2"
    assert embed.title == "내전 봇 패치 내역 · v1.0.2"
    history = "\n".join(f"{field.name}\n{field.value}" for field in embed.fields)
    assert "v1.0.2" in history
    assert "v1.0.1" in history
    assert "v1.0.0" in history
    assert "정식 릴리즈" in history
    assert "패널 자동 복구" in history
    assert "시간 타입 오류 수정" in history


def _command_interaction(*, channel_type=None, manage_guild=False, guild=True):
    response = SimpleNamespace(is_done=lambda: True, send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    channel = SimpleNamespace(type=channel_type) if channel_type is not None else SimpleNamespace()
    return SimpleNamespace(
        guild=SimpleNamespace(id=123) if guild else None,
        channel=channel,
        user=SimpleNamespace(id=77, guild_permissions=SimpleNamespace(manage_guild=manage_guild)),
        response=response,
        followup=followup,
    )


@pytest.mark.asyncio
async def test_register_command_uses_interaction_user_and_normalized_tier():
    from inhouse_bot.discord.commands import MatchCommandGroup

    service = SimpleNamespace(register_role_rating=AsyncMock(return_value=1850))
    group = MatchCommandGroup(service)
    interaction = _command_interaction(channel_type=discord.ChannelType.text)
    await MatchCommandGroup.register.callback(
        group, interaction, discord.app_commands.Choice(name="원딜", value="ADC"), " 플래티넘 2 ", None
    )
    service.register_role_rating.assert_awaited_once_with(
        123, 77, "ADC", " 플래티넘 2 ", game_key="lol"
    )
    sent = interaction.followup.send.await_args.args[0]
    assert sent == "원딜 MMR 등록 완료: 플래티넘2 · 1850점"


@pytest.mark.asyncio
async def test_register_command_preserves_duplicate():
    from inhouse_bot.discord.commands import MatchCommandGroup

    service = SimpleNamespace(
        register_role_rating=AsyncMock(side_effect=RoleRatingAlreadyExistsError("ADC"))
    )
    group = MatchCommandGroup(service)
    interaction = _command_interaction(channel_type=discord.ChannelType.text)
    await MatchCommandGroup.register.callback(
        group, interaction, discord.app_commands.Choice(name="원딜", value="ADC"), "플래티넘2", None
    )
    assert "이미 이번 시즌 원딜 MMR이 등록되어 있습니다" in interaction.followup.send.await_args.args[0]



@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel_type", (discord.ChannelType.voice, discord.ChannelType.public_thread)
)
async def test_register_command_allows_voice_chat_and_thread(channel_type):
    from inhouse_bot.discord.commands import MatchCommandGroup

    service = SimpleNamespace(register_role_rating=AsyncMock(return_value=1850))
    group = MatchCommandGroup(service)
    interaction = _command_interaction(channel_type=channel_type)
    await MatchCommandGroup.register.callback(
        group, interaction, discord.app_commands.Choice(name="원딜", value="ADC"), "플래티넘2", None
    )
    service.register_role_rating.assert_awaited_once_with(
        123, 77, "ADC", "플래티넘2", game_key="lol"
    )


@pytest.mark.asyncio
async def test_register_command_rejects_dm():
    from inhouse_bot.discord.commands import MatchCommandGroup

    service = SimpleNamespace(register_role_rating=AsyncMock(return_value=1850))
    group = MatchCommandGroup(service)
    interaction = _command_interaction(guild=False)
    await MatchCommandGroup.register.callback(
        group, interaction, discord.app_commands.Choice(name="원딜", value="ADC"), "플래티넘2", None
    )
    service.register_role_rating.assert_not_awaited()
    assert interaction.followup.send.await_args.args[0] == "서버에서만 사용할 수 있습니다."


@pytest.mark.asyncio
async def test_voice_state_event_handles_disconnect_and_move_only_when_empty(monkeypatch):
    import inhouse_bot.main as main_module

    close = AsyncMock()
    monkeypatch.setattr(main_module, "close_empty_match_voice_channel", close)
    bot = SimpleNamespace(service=object())
    member = SimpleNamespace(id=77)
    channel = SimpleNamespace(id=20, members=[])

    await main_module.InhouseBot.on_voice_state_update(
        bot, member, SimpleNamespace(channel=channel), SimpleNamespace(channel=None)
    )
    close.assert_awaited_once_with(bot.service, channel)

    close.reset_mock()
    await main_module.InhouseBot.on_voice_state_update(
        bot,
        member,
        SimpleNamespace(channel=channel),
        SimpleNamespace(channel=SimpleNamespace(id=21)),
    )
    close.assert_awaited_once_with(bot.service, channel)

    close.reset_mock()
    channel.members.append(SimpleNamespace(id=88))
    await main_module.InhouseBot.on_voice_state_update(
        bot, member, SimpleNamespace(channel=channel), SimpleNamespace(channel=None)
    )
    close.assert_not_awaited()


@pytest.mark.asyncio
async def test_result_command_checks_empty_voice_without_waiting_cleanup_delay(monkeypatch):
    import inhouse_bot.discord.commands as commands_module

    finished = SimpleNamespace(id=42, voice_cleanup_at=None)
    service = SimpleNamespace(
        get_active_match=AsyncMock(return_value=SimpleNamespace(id=42)),
        finish_match=AsyncMock(return_value=finished),
        get_match=AsyncMock(return_value=finished),
        is_bot_admin=AsyncMock(return_value=True),
        voice_cleanup_delay_seconds=600,
    )
    close = AsyncMock()
    monkeypatch.setattr(commands_module, "close_empty_match_voice_channels", close)
    group = commands_module.MatchCommandGroup(service)
    group._refresh_message = AsyncMock()
    interaction = _command_interaction(channel_type=discord.ChannelType.text)
    interaction.channel.id = 456

    await commands_module.MatchCommandGroup.result.callback(
        group, interaction, discord.app_commands.Choice(name="A팀", value="A"), None
    )

    close.assert_awaited_once_with(service, interaction.guild, finished)


@pytest.mark.asyncio
async def test_tier_autocomplete_filters_compact_values_to_25():
    from inhouse_bot.discord.commands import MatchCommandGroup

    group = MatchCommandGroup(object())
    choices = await group.tier_autocomplete(_command_interaction(), "플래")
    assert choices and all("플래" in choice.name for choice in choices)
    assert len(await group.tier_autocomplete(_command_interaction(), "")) <= 25
    master = await group.tier_autocomplete(_command_interaction(), "마스터")
    assert {choice.name for choice in master} == {"마스터하", "마스터중", "마스터상", "그랜드마스터하", "그랜드마스터중", "그랜드마스터상"}


@pytest.mark.asyncio
async def test_admin_mmr_command_uses_compact_tier_and_prioritizes_score():
    from inhouse_bot.discord.commands import MatchCommandGroup

    service = SimpleNamespace(
        set_role_rating=AsyncMock(return_value=1850),
        is_bot_admin=AsyncMock(return_value=True),
    )
    group = MatchCommandGroup(service)
    interaction = _command_interaction(channel_type=discord.ChannelType.text, manage_guild=True)
    target = SimpleNamespace(id=88)
    role = discord.app_commands.Choice(name="원딜", value="ADC")
    await MatchCommandGroup.set_mmr.callback(group, interaction, target, role, "플래티넘2", None, "lol")
    service.set_role_rating.assert_awaited_once_with(
        123, 88, "ADC", game_key="lol", tier="플래티넘2", rating=None,
        manager_override=True,
    )

    service.set_role_rating.reset_mock()
    interaction = _command_interaction(channel_type=discord.ChannelType.text, manage_guild=True)
    await MatchCommandGroup.set_mmr.callback(group, interaction, target, role, "잘못된티어", 1900, "lol")
    service.set_role_rating.assert_awaited_once_with(
        123, 88, "ADC", game_key="lol", tier="잘못된티어", rating=1900,
        manager_override=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", ("admin_list", "season_start", "season_end", "set_mmr"))
async def test_admin_commands_defer_before_remote_permission_check(command_name):
    from inhouse_bot.discord.commands import MatchCommandGroup

    events = []

    async def is_admin(*_args):
        events.append("permission")
        return False

    service = SimpleNamespace(is_bot_admin=is_admin)
    group = MatchCommandGroup(service)
    interaction = _command_interaction(channel_type=discord.ChannelType.text)
    interaction.response.is_done = lambda: False

    async def defer(*_args, **_kwargs):
        events.append("defer")

    interaction.response.defer.side_effect = defer
    if command_name == "admin_list":
        await MatchCommandGroup.admin_list.callback(group, interaction)
    elif command_name == "season_start":
        await MatchCommandGroup.season_start.callback(group, interaction, "시즌", None)
    elif command_name == "season_end":
        await MatchCommandGroup.season_end.callback(group, interaction, None)
    else:
        target = SimpleNamespace(id=88)
        role = discord.app_commands.Choice(name="원딜", value="ADC")
        await MatchCommandGroup.set_mmr.callback(group, interaction, target, role, "플래티넘2", None, "lol")

    assert events[:2] == ["defer", "permission"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", ("admin_list", "season_start", "season_end", "set_mmr"))
async def test_admin_commands_reply_when_permission_check_fails(command_name):
    from inhouse_bot.discord.commands import MatchCommandGroup

    service = SimpleNamespace(is_bot_admin=AsyncMock(side_effect=RuntimeError("db down")))
    group = MatchCommandGroup(service)
    interaction = _command_interaction(channel_type=discord.ChannelType.text)
    if command_name == "admin_list":
        await MatchCommandGroup.admin_list.callback(group, interaction)
    elif command_name == "season_start":
        await MatchCommandGroup.season_start.callback(group, interaction, "시즌", None)
    elif command_name == "season_end":
        await MatchCommandGroup.season_end.callback(group, interaction, None)
    else:
        role = discord.app_commands.Choice(name="원딜", value="ADC")
        await MatchCommandGroup.set_mmr.callback(
            group, interaction, SimpleNamespace(id=88), role, "플래티넘2", None, "lol"
        )
    assert "오류" in interaction.followup.send.await_args.args[0]


def test_match_panel_requires_own_bot_and_exact_component():
    from inhouse_bot.main import InhouseBot

    bot = SimpleNamespace(user=SimpleNamespace(id=7), application_id=7)

    def message(author_id, custom_id=None, content=""):
        children = () if custom_id is None else (SimpleNamespace(custom_id=custom_id),)
        return SimpleNamespace(
            author=SimpleNamespace(id=author_id, bot=author_id == 7),
            application_id=None,
            components=(SimpleNamespace(children=children),),
            content=content,
        )

    assert not InhouseBot._is_match_panel(bot, message(8, content="hello match:42"), 42)
    assert not InhouseBot._is_match_panel(bot, message(7, "match:10:join"), 1)
    assert not InhouseBot._is_match_panel(bot, message(7, content="match:42"), 42)
    assert InhouseBot._is_match_panel(bot, message(7, "match:42:join"), 42)


def test_renderer_caps_large_waitlist():
    waiting = tuple(_participant(user_id) for user_id in range(1000, 1100))
    match = SimpleNamespace(
        status="RECRUITING", title="대기열", capacity=10, participants=(),
        waitlist=waiting, recruitment_deadline_at=None, role_rating_enabled=False,
    )
    value = _fields(render_match(match))["대기자"]
    assert len(value) <= 1024
    assert "<@1024>" in value and "<@1025>" not in value
    assert "외 75명" in value


@pytest.mark.asyncio
async def test_result_cleanup_failure_still_reports_success(monkeypatch):
    import inhouse_bot.discord.commands as commands_module

    finished = SimpleNamespace(id=42, voice_cleanup_at=None)
    service = SimpleNamespace(
        get_active_match=AsyncMock(return_value=finished),
        finish_match=AsyncMock(return_value=finished),
        get_match=AsyncMock(return_value=finished),
        is_bot_admin=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        commands_module,
        "close_empty_match_voice_channels",
        AsyncMock(side_effect=RuntimeError("cleanup")),
    )
    group = commands_module.MatchCommandGroup(service)
    group._refresh_message = AsyncMock()
    interaction = _command_interaction(channel_type=discord.ChannelType.text)
    interaction.channel.id = 456
    await commands_module.MatchCommandGroup.result.callback(
        group, interaction, discord.app_commands.Choice(name="A팀", value="A"), None
    )
    service.finish_match.assert_awaited_once()
    assert interaction.followup.send.await_args.args[0] == "내전 결과를 기록했습니다."


@pytest.mark.asyncio
async def test_role_join_modal_does_not_wait_for_match_when_role_state_is_known():
    from inhouse_bot.discord.views import MatchView

    service = SimpleNamespace(get_match=AsyncMock(side_effect=AssertionError("modal opened before DB read")))
    view = MatchView(service, 42, status="RECRUITING", guild_id=123, role_rating_enabled=True)
    response = SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock())
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        user=SimpleNamespace(id=7),
        response=response,
        message=object(),
    )

    await view._join(interaction)

    response.send_modal.assert_awaited_once()
    service.get_match.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_join_success_survives_source_message_edit_failure():
    from inhouse_bot.discord.views import JoinPreferencesModal, MatchView

    latest = SimpleNamespace(
        id=42,
        guild_id=123,
        status="RECRUITING",
        title="참가 테스트",
        capacity=10,
        creator_id=1,
        participants=(),
        waitlist=(),
        recruitment_deadline_at=None,
    )
    source_message = SimpleNamespace(edit=AsyncMock(side_effect=RuntimeError("Discord down")))
    service = SimpleNamespace(
        get_match=AsyncMock(return_value=latest),
        join_match=AsyncMock(return_value=SimpleNamespace(waitlisted=False)),
    )
    view = MatchView(service, 42, guild_id=123, role_rating_enabled=True)
    modal = JoinPreferencesModal(view, source_message)
    response = SimpleNamespace(defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        user=SimpleNamespace(id=7),
        response=response,
        followup=followup,
    )

    await modal.on_submit(interaction)

    service.join_match.assert_awaited_once()
    source_message.edit.assert_awaited_once()
    assert "참가했습니다" in followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_foreign_guild_button_is_rejected_before_mutation():
    from inhouse_bot.discord.views import MatchView

    service = SimpleNamespace(
        get_match=AsyncMock(return_value=SimpleNamespace(id=42, guild_id=123)),
        leave_match=AsyncMock(),
    )
    view = MatchView(service, 42, status="RECRUITING", guild_id=123, role_rating_enabled=False)
    response = SimpleNamespace(is_done=lambda: False, defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=999),
        user=SimpleNamespace(id=7),
        response=response,
        followup=followup,
        message=None,
    )

    await view._leave(interaction)

    service.leave_match.assert_not_awaited()
    assert "현재 서버" in followup.send.await_args.args[0]


def test_embed_limits_are_exposed_in_command_metadata():
    from inhouse_bot.discord.commands import MatchCommandGroup

    title = MatchCommandGroup.create._params["title"]
    memo = MatchCommandGroup.result._params["memo"]
    assert title.min_value == 1 and title.max_value == 4096
    assert memo.min_value == 0 and memo.max_value == 1024


@pytest.mark.asyncio
async def test_overlong_title_and_result_memo_are_rejected_before_service_calls():
    from inhouse_bot.discord.commands import MatchCommandGroup

    service = SimpleNamespace(
        create_match=AsyncMock(),
        get_active_match=AsyncMock(),
        finish_match=AsyncMock(),
    )
    group = MatchCommandGroup(service)
    interaction = _command_interaction(channel_type=discord.ChannelType.text)
    await MatchCommandGroup.create.callback(
        group,
        interaction,
        "x" * 4097,
        discord.app_commands.Choice(name="탑", value="TOP"),
        discord.app_commands.Choice(name="미드", value="MID"),
    )
    service.create_match.assert_not_awaited()

    interaction = _command_interaction(channel_type=discord.ChannelType.text)
    await MatchCommandGroup.result.callback(
        group,
        interaction,
        discord.app_commands.Choice(name="A팀", value="A"),
        "x" * 1025,
    )
    service.get_active_match.assert_not_awaited()
    service.finish_match.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_embed_text_limits_guard_repository_mutations():
    from inhouse_bot.services.matches import MatchService

    repository = SimpleNamespace(
        create_match=AsyncMock(return_value=object()),
        finish_match=AsyncMock(return_value=object()),
    )
    service = object.__new__(MatchService)
    service.repository = repository
    service.default_recruitment_minutes = 30
    service.voice_cleanup_delay_seconds = 600

    await service.create_match(1, 2, 3, "x" * 4096)
    repository.create_match.assert_awaited_once()
    with pytest.raises(ValueError):
        await service.create_match(1, 2, 3, "x" * 4097)
    with pytest.raises(ValueError):
        await service.create_match(1, 2, 3, "")
    assert repository.create_match.await_count == 1

    await service.finish_match(42, 7, "A", "")
    await service.finish_match(42, 7, "A", "x" * 1024)
    assert repository.finish_match.await_count == 2
    with pytest.raises(ValueError):
        await service.finish_match(42, 7, "A", "x" * 1025)
    assert repository.finish_match.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "voice_error",
    (
        "설정한 1팀 고정 보이스 채널을 이 서버에서 찾을 수 없습니다.",
        "설정한 1팀 고정 보이스 채널이 이 서버의 보이스 채널이 아닙니다.",
        "설정한 2팀 고정 보이스 채널이 이 서버의 보이스 채널이 아닙니다.",
        "고정 보이스 채널에 Connect 및 Move Members 권한이 필요합니다.",
    ),
)
async def test_create_rejects_invalid_configured_fixed_voice_before_persist(
    monkeypatch, voice_error
):
    import inhouse_bot.discord.commands as commands_module

    monkeypatch.setattr(
        commands_module,
        "resolve_voice_category_id",
        lambda *_args: (None, voice_error),
    )
    service = SimpleNamespace(create_match=AsyncMock())
    group = commands_module.MatchCommandGroup(
        service, team_a_voice_channel_id=50, team_b_voice_channel_id=51
    )
    interaction = _command_interaction(channel_type=discord.ChannelType.text)

    await commands_module.MatchCommandGroup.create.callback(
        group,
        interaction,
        "고정 보이스 테스트",
        discord.app_commands.Choice(name="탑", value="TOP"),
        discord.app_commands.Choice(name="미드", value="MID"),
    )

    service.create_match.assert_not_awaited()
    assert voice_error in interaction.followup.send.await_args.args[0]


def test_load_settings_rejects_duplicate_voice_channels(monkeypatch):
    from inhouse_bot.config import load_settings

    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("BOT_OWNER_ID", "99")
    monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")
    monkeypatch.setenv("TEAM_A_VOICE_CHANNEL_ID", "42")
    monkeypatch.setenv("TEAM_B_VOICE_CHANNEL_ID", "42")
    with pytest.raises(RuntimeError, match="달라야 합니다"):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    (("DISCORD_TOKEN", "   "), ("DATABASE_URL", "\t"), ("DISCORD_GUILD_ID", "0"),
     ("DISCORD_GUILD_ID", "-1")),
)
def test_load_settings_rejects_blank_required_and_nonpositive_guild(monkeypatch, name, value):
    from inhouse_bot.config import load_settings

    monkeypatch.setattr("inhouse_bot.config.load_dotenv", lambda: None)
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("BOT_OWNER_ID", "99")
    monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError):
        load_settings()


def test_load_settings_rejects_only_one_fixed_voice_channel(monkeypatch):
    from inhouse_bot.config import load_settings

    monkeypatch.setattr("inhouse_bot.config.load_dotenv", lambda: None)
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("BOT_OWNER_ID", "99")
    monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")
    monkeypatch.setenv("TEAM_A_VOICE_CHANNEL_ID", "42")
    monkeypatch.delenv("TEAM_B_VOICE_CHANNEL_ID", raising=False)
    with pytest.raises(RuntimeError, match="둘 다"):
        load_settings()


def test_voice_cleanup_delay_allows_zero_and_rejects_negative(monkeypatch):
    from inhouse_bot.config import load_settings

    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("BOT_OWNER_ID", "99")
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
