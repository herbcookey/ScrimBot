"""환경변수 기반 애플리케이션 설정."""

from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    discord_guild_id: int
    bot_owner_id: int
    database_url: str
    ready_timeout_seconds: int = 120
    default_recruitment_minutes: int = 30
    reminder_before_seconds: int = 300
    voice_cleanup_delay_seconds: int = 600
    inhouse_voice_category_id: int | None = None
    team_a_voice_channel_id: int | None = None
    team_b_voice_channel_id: int | None = None


def load_settings() -> Settings:
    """필수 설정을 ``.env``와 프로세스 환경변수에서 읽는다."""

    load_dotenv()
    missing = [
        name
        for name in ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "BOT_OWNER_ID", "DATABASE_URL")
        if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError(f"필수 환경변수가 없습니다: {', '.join(missing)}")

    try:
        guild_id = int(os.environ["DISCORD_GUILD_ID"])
    except ValueError as exc:
        raise RuntimeError("DISCORD_GUILD_ID는 정수여야 합니다") from exc

    bot_owner_id = _positive_int("BOT_OWNER_ID", 0)

    ready_timeout_seconds = _positive_int("READY_TIMEOUT_SECONDS", 120)
    default_recruitment_minutes = _bounded_int(
        "DEFAULT_RECRUITMENT_MINUTES", 30, minimum=5, maximum=1440
    )
    reminder_before_seconds = _positive_int("REMINDER_BEFORE_SECONDS", 300)
    voice_cleanup_delay_seconds = _non_negative_int("VOICE_CLEANUP_DELAY_SECONDS", 600)
    inhouse_voice_category_id = _optional_int("INHOUSE_VOICE_CATEGORY_ID")
    team_a_voice_channel_id = _optional_int("TEAM_A_VOICE_CHANNEL_ID")
    team_b_voice_channel_id = _optional_int("TEAM_B_VOICE_CHANNEL_ID")
    if team_a_voice_channel_id is not None and team_a_voice_channel_id == team_b_voice_channel_id:
        raise RuntimeError("A팀과 B팀 음성 채널 ID는 달라야 합니다")
    if team_a_voice_channel_id is None or team_b_voice_channel_id is None:
        team_a_voice_channel_id = team_b_voice_channel_id = None

    return Settings(
        discord_token=os.environ["DISCORD_TOKEN"],
        discord_guild_id=guild_id,
        bot_owner_id=bot_owner_id,
        database_url=os.environ["DATABASE_URL"],
        ready_timeout_seconds=ready_timeout_seconds,
        default_recruitment_minutes=default_recruitment_minutes,
        reminder_before_seconds=reminder_before_seconds,
        voice_cleanup_delay_seconds=voice_cleanup_delay_seconds,
        inhouse_voice_category_id=inhouse_voice_category_id,
        team_a_voice_channel_id=team_a_voice_channel_id,
        team_b_voice_channel_id=team_b_voice_channel_id,
    )


def _positive_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name}은 양의 정수여야 합니다") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name}은 양의 정수여야 합니다")
    return parsed


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    parsed = _positive_int(name, default)
    if not minimum <= parsed <= maximum:
        raise RuntimeError(f"{name}은 {minimum}에서 {maximum} 사이여야 합니다")
    return parsed


def _non_negative_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name}은 0 이상의 정수여야 합니다") from exc
    if parsed < 0:
        raise RuntimeError(f"{name}은 0 이상의 정수여야 합니다")
    return parsed


def _optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name}은 양의 정수여야 합니다") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name}은 양의 정수여야 합니다")
    return parsed
