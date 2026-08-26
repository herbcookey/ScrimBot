# Discord 내전 봇 MVP

Discord에서 게임 내전을 모집하고 팀을 배정하는 봇입니다. 기본 게임은 LoL 5:5입니다. 모집 상태와 참가자·팀·결과는 Supabase PostgreSQL에 저장하고, Discord 메시지는 그 상태를 보여 주는 UI로만 사용합니다.

## 요구 사항

- Python 3.11 이상
- PostgreSQL을 제공하는 Supabase 프로젝트
- Discord 애플리케이션과 대상 서버
- 로컬 개발: Windows PowerShell 또는 macOS 터미널

## Discord 애플리케이션 설정

Developer Portal에서 애플리케이션에 Bot을 추가하고 다음 OAuth2 scope로 대상 서버에 초대합니다.

- `bot`
- `applications.commands`

봇 권한은 사용할 채널에 `View Channel`, `Send Messages`, `Embed Links`, `Read Message History`를 부여합니다. 음성 자동 배치를 사용할 때는 기존 A/B 음성 채널에 `Connect`, `Move Members`도 부여합니다. 음성 채널을 자동으로 생성하거나 삭제하지 않으며, `Administrator`는 부여하지 마세요.

이 봇은 슬래시 명령과 버튼 interaction을 사용하므로 `MESSAGE_CONTENT` 또는 `GUILD_MEMBERS` privileged intent가 필요 없습니다. 음성 자동 배치에는 `voice_states` intent만 사용합니다. Developer Portal의 **Bot → Privileged Gateway Intents → Message Content Intent**와 **Server Members Intent**는 끄고, 토큰을 일반 메시지 수집용으로 사용하지 마세요. `DISCORD_GUILD_ID`가 가리키는 서버에 길드 명령을 동기화하므로 다른 서버를 시험할 때는 해당 ID와 초대 대상을 함께 바꿔야 합니다.

길드 전용 `/내전` 그룹을 사용합니다.

- `/내전 생성 제목 [게임]`: 현재 채널에 모집 패널을 만듭니다. 게임을 빼면 기존처럼 LoL입니다.
- `/내전 생성`에는 선택적인 `모집시간`(분)을 지정할 수 있습니다. 기본값은 30분이며 5~1440분입니다.
- 패널의 `참가`, `나가기`, `팀 배정·시작`, `내전 취소` 버튼으로 모집을 관리합니다.
- `/내전 결과 승리팀 [메모]`: 진행 중인 내전 결과를 기록합니다.
- `/내전 강퇴 사용자:@사용자`: 현재 채널의 모집 중 내전에서 참가자 또는 대기자를 강퇴합니다.
- `/내전 전적 [사용자:@사용자] [게임] [시즌]`: 현재 서버의 종료 경기 전적과 승률을 조회합니다.
- `/내전 시즌시작 이름 [게임]`: 서버 관리 권한으로 새 시즌을 시작합니다. 기존 활성 시즌은 같이 종료됩니다.
- `/내전 시즌종료 [게임]`: 현재 활성 시즌을 종료합니다.
- `/내전 랭킹 [게임] [시즌] [인원수]`: 시즌 MMR 순위를 조회합니다. 기본 10명, 최대 25명입니다.

## 환경 변수

`.env.example`을 복사해 프로젝트 루트의 `.env`를 만들고 값을 채웁니다.

| 변수 | 설명 |
| --- | --- |
| `DISCORD_TOKEN` | Discord Bot 토큰. 저장소·로그·README에 남기지 않습니다. |
| `DISCORD_GUILD_ID` | 슬래시 명령을 동기화할 Discord 서버 ID(정수). |
| `DATABASE_URL` | 앱이 사용할 PostgreSQL 연결 문자열. |
| `TEST_DATABASE_URL` | PostgreSQL 통합 테스트 전용 연결 문자열. 앱 실행에는 쓰지 않습니다. |
| `READY_TIMEOUT_SECONDS` | 준비 확인 제한 시간(초). 기본값 `120`. |
| `DEFAULT_RECRUITMENT_MINUTES` | `/내전 생성` 모집시간 기본값(분, 5~1440). 기본값 `30`. |
| `REMINDER_BEFORE_SECONDS` | 모집 마감 전 채널 알림 시점(초). 기본값 `300`(5분). |
| `TEAM_A_VOICE_CHANNEL_ID` | 기존 A팀 음성 채널 ID. B와 함께 설정할 때만 자동 배치 활성화. 기본값 빈 값(비활성). |
| `TEAM_B_VOICE_CHANNEL_ID` | 기존 B팀 음성 채널 ID. A와 함께 설정할 때만 자동 배치 활성화. 기본값 빈 값(비활성). |

실제 토큰이나 데이터베이스 비밀번호가 들어간 `.env`를 커밋하거나 채팅에 붙여넣지 마세요. 이미 노출했다면 Discord 토큰을 재발급하고 DB 비밀번호를 회전해야 합니다.

## 2차 내전 흐름

상태는 PostgreSQL이 관리하며 `RECRUITING → READY_CHECK → PLAYING → FINISHED` 순서로 진행합니다. 모집·준비·진행 중에는 생성자 또는 `Manage Guild` 권한 사용자가 취소할 수 있고, 허용되지 않은 역방향 전이는 하지 않습니다. 준비 확인 중 참가자가 빠져 정원이 부족해진 경우에만 다시 `RECRUITING`으로 돌아갑니다.

- 기존 `팀 배정·시작` 버튼(현재 표시명 `준비 확인 시작`)은 생성자 또는 `Manage Guild` 사용자만 누를 수 있고, 참가자가 선택한 게임 정원과 정확히 같을 때 `READY_CHECK`를 시작합니다. 모든 준비 상태를 초기화하고 120초 제한을 저장합니다.
- 준비 카드에는 준비 완료/전체 인원, 준비·미준비 명단, 남은 시간, `준비` 토글(다시 누르면 준비 취소), `내전 취소`를 표시합니다. 실제 참가자만 준비할 수 있으며 중복 클릭은 한 번만 반영됩니다. 전원이 준비하면 한 트랜잭션에서 무작위 A/B 팀을 저장하고 `PLAYING`으로 전환한 뒤 카드와 음성 배치를 갱신합니다.
- 정원이 찬 상태에서 `참가`를 누르면 대기열에 등록됩니다. 참가자와 대기자는 중복될 수 없고, 등록 시각 FIFO 순서를 유지합니다. `나가기`는 둘 다 취소할 수 있으며, 참가자가 나가거나 강퇴되면 같은 트랜잭션에서 첫 대기자를 승격합니다. `READY_CHECK` 명단이 바뀌면 준비를 초기화하고, 정원이 다시 차면 새 제한 시간으로 준비 확인을 재시작합니다. `PLAYING` 중 이탈·강퇴·팀 재배정은 하지 않습니다.
- `/내전 강퇴 사용자:@사용자`는 현재 채널의 활성 내전에서 생성자 또는 `Manage Guild` 사용자만 사용할 수 있고, `RECRUITING`·`READY_CHECK`에서 참가자와 대기자를 모두 대상으로 합니다. 자기 자신 강퇴도 같은 규칙을 따르며, 대상이 아니면 ephemeral 오류를 반환합니다.
- `/내전 전적 [사용자:@사용자]`는 생략 시 실행자 기준으로 현재 서버의 `FINISHED` 경기만 SQL 집계합니다. 총 경기·승·패·승률을 표시하고, `CANCELLED` 경기와 다른 서버의 경기는 제외하며 0경기 승률은 0%입니다.

## 3A 게임·시즌·MMR

게임 설정은 `games` 테이블에 둡니다. 관리 명령은 따로 안 만들었습니다. 두 번째 게임은 SQL Editor나 migration으로 아래 정도만 추가하면 됩니다. `capacity`는 `team_size * 2`여야 하고 짝수여야 합니다.

```sql
insert into public.games
    ("key", name, team_size, capacity, default_rating, k_factor, rating_enabled)
values
    ('valorant', 'VALORANT', 5, 10, 1000, 32, true);
```

경기 생성 때 게임 정원과 활성 시즌을 경기 행에 저장합니다. 활성 시즌이 없으면 `시즌 1`을 자동 생성합니다. 시즌 변경은 `/내전 시즌시작`, `/내전 시즌종료`를 쓰면 되고 서버 관리 권한이 있어야 합니다. 서버·게임마다 활성 시즌은 하나만 허용됩니다.

MMR은 팀 평균 기준 Elo입니다.

```text
expected_a = 1 / (1 + 10 ** ((avg_b - avg_a) / 400))
delta_a = round(k_factor * (actual_a - expected_a))
delta_b = -delta_a
```

기본 점수는 1000, K 값은 32입니다. 게임별로 바꿀 수 있습니다. 팀 배정 시점 점수를 참가자에 스냅샷으로 남기고 결과 저장, 점수 갱신, 이력 저장, 경기 종료를 한 트랜잭션에서 처리합니다. 3A migration 전에 끝난 기존 경기는 `Legacy` 시즌에 연결만 합니다. 기존 결과로 MMR을 다시 계산하지 않습니다.

## 모집 알림·만료와 재시작 복구

`/내전 생성` 시 모집 마감 시각을 저장하고 카드에 Discord timestamp로 표시합니다. 기본값은 마감 5분 전이며 `REMINDER_BEFORE_SECONDS`로 초 단위 설정을 바꿀 수 있습니다. 전체 모집 시간이 알림 시점보다 짧으면 사전 알림을 생략하고, DM은 보내지 않습니다. `recruitment_reminded_at`으로 중복 알림을 막습니다.

모집 마감 시 트랜잭션 안에서 상태와 시각을 다시 확인합니다. 정원이 차 있으면 `READY_CHECK`를 시작하고, 부족하면 `CANCELLED`(사유: 모집 시간 만료)로 종료하며 `ended_at`을 저장하고 버튼을 비활성화합니다. 준비 시간이 만료되면 미준비자를 제거하고 FIFO 대기자를 승격한 뒤, 정원이 차면 새 `READY_CHECK`, 부족하면 `RECRUITING`으로 돌립니다. 이 과정에서 제거·승격 결과를 채널에 알립니다.

예약 작업은 별도 큐 없이 단일 background task가 약 10~15초마다 한 번 폴링합니다. `process_due_matches(now)`처럼 현재 시각을 받는 함수로 모집 알림·모집 만료·준비 만료를 처리하며, 실행 전 DB 상태와 기한을 재검사하고 여러 번 실행해도 중복 전이가 없도록 멱등 처리합니다. DB 트랜잭션을 Discord API 호출보다 먼저 커밋하므로 API 실패가 상태 변경을 되돌리지 않습니다.

재시작 시 `RECRUITING`, `READY_CHECK`, `PLAYING`을 조회해 Persistent View와 최신 카드를 다시 등록하고, 이미 지난 마감은 즉시 처리하며 남은 마감은 폴링이 이어서 처리합니다. 이미 `recruitment_reminded_at`이 있는 경기에는 알림을 다시 보내지 않습니다.

## 음성 채널 자동 배치

`TEAM_A_VOICE_CHANNEL_ID`와 `TEAM_B_VOICE_CHANNEL_ID`를 모두 설정할 때만 기능을 켭니다. 두 채널은 봇이 속한 서버의 **기존** 음성 채널이어야 하며 자동 생성·삭제하지 않습니다. 봇에 해당 채널의 `Connect`와 `Move Members` 권한을 부여하세요.

경기가 DB에서 `PLAYING`으로 커밋된 뒤 현재 음성 채널에 접속한 참가자만 A/B 채널로 이동합니다. 음성 채널에 없는 사용자는 건너뛰고, 성공·미접속·실패 인원을 생성자에게 요약합니다. 일부 이동 실패나 Discord API 오류는 DB 경기 시작을 롤백하지 않으며 로그와 알림으로 확인합니다. 필요한 Gateway intent는 `voice_states`뿐이고 `GUILD_MEMBERS`와 `MESSAGE_CONTENT` privileged intent는 사용하지 않습니다.

## Windows에서 설치·실행

PowerShell에서 프로젝트 루트로 이동한 뒤 실행합니다.

```powershell
cd "C:\Users\<사용자>\path\lol-inhouse-bot"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
# .env 편집 후 실제 값 입력
python -m inhouse_bot.main
```

스크립트 실행 정책 때문에 활성화가 막히면 현재 PowerShell 세션에서만 다음을 실행한 후 다시 활성화합니다.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

## macOS Mac mini에서 실행

Mac mini에서도 저장소를 받고 동일하게 가상 환경을 만든 뒤 절대 경로로 실행합니다.

```bash
cd ~/path/to/lol-inhouse-bot
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cp .env.example .env
# .env 편집 후 실제 값 입력
python -m inhouse_bot.main
```

터미널을 닫아도 계속 실행하려면 macOS `launchd`(또는 관리 중인 `tmux`/`screen`)를 사용합니다. `launchd`의 `WorkingDirectory`는 프로젝트 루트, `ProgramArguments`는 `.venv/bin/python -m inhouse_bot.main`의 절대 경로로 지정하고, 표준 출력·오류 로그 경로를 별도로 둡니다. `.env`는 프로세스가 읽을 수 있는 프로젝트 루트에 두되 로그에는 비밀값을 출력하지 않습니다.

## Supabase migration

스키마의 기준은 `supabase/migrations/`의 타임스탬프 SQL입니다. 처음 연결하는 경우 Supabase CLI를 설치하고 프로젝트 루트에서 한 번 초기화·연결합니다.

```bash
supabase init                 # supabase/config.toml이 아직 없을 때만
supabase login
supabase link --project-ref <PROJECT_REF>
supabase db push
```

`supabase db push`는 연결한 원격 프로젝트에 아직 적용되지 않은 migration을 적용합니다. 이미 데이터가 있는 프로젝트에서는 먼저 백업과 대상 project-ref를 확인하세요. CLI를 사용할 수 없을 때만 대시보드 SQL Editor에서 `supabase/migrations/20260826012146_schema.sql`을 검토해 수동 실행하고, migration 이력을 정리하기 전에는 `db push`와 섞어 쓰지 않습니다.

1차부터 3A까지는 `20260826012146_schema.sql` 하나에 실행 순서대로 들어 있습니다. 초기화한 DB에서는 프로젝트 루트에서 `supabase db push` 한 번이면 됩니다. 3A 부분은 기존 경기를 `Legacy` 시즌에 연결하고 경기 ID나 결과는 건드리지 않습니다. 수동 SQL과 `db push`를 같은 변경에 섞지 마세요.

롤백할 때 migration 파일만 지우면 안 됩니다. 3A 이후 기록된 `player_ratings`, `rating_history`, `matches.season_id`가 있어서 컬럼이나 테이블부터 내리면 점수 이력과 시즌 연결이 없어집니다. 먼저 백업하고 봇을 중지한 다음, 어떤 데이터를 보존할지 정해서 별도 하향 migration을 작성해야 합니다.

연결 문자열은 용도를 구분합니다.

- migration·DDL은 Supabase CLI 또는 직접 데이터베이스 연결(`db.<project-ref>.supabase.co:5432`)을 사용합니다. 앱 실행용 pooler URL을 migration 명령에 그대로 넣지 마세요.
- 앱의 `DATABASE_URL`은 네트워크가 IPv6를 지원하면 direct URL, IPv4 환경이면 Supabase **session pooler**(보통 `:5432`)를 우선 사용합니다.
- Supabase 연결은 테이블 owner인 `postgres` DB role의 direct URL 또는 session pooler를 사용합니다. RLS 정책은 Data API에 의도적으로 열어 두지 않았습니다.
- **transaction pooler**(`:6543`)는 연결 간 prepared statement를 보장하지 않습니다. 이 프로젝트는 `asyncpg` pool을 사용하므로 transaction pooler는 검증되지 않았으며 오류가 나면 direct/session pooler로 바꾸세요. 실제로 사용하려면 asyncpg statement cache 설정과 Supabase 동작을 별도로 검증해야 합니다.

## 데이터 일관성과 복구

- `matches`, `match_participants`, `match_results`가 source of truth입니다. Discord 임베드·버튼·메시지 ID는 캐시된 표시 계층이며, 재시작·중복 interaction 때 DB를 다시 읽어야 합니다.
- Discord 메시지가 삭제되거나 오래된 경우 메시지 내용을 수동으로 진실로 취급하지 말고 DB 행을 확인한 뒤 패널을 다시 게시·갱신합니다. migration은 스키마만 바꾸며 참가자/결과를 복구하지 않습니다.
- 운영 DB에 migration 또는 수동 SQL을 실행하기 전 Supabase 백업/`pg_dump`를 확보합니다. 장애 시 봇을 중지하고 백업과 DB 상태를 확인한 다음 필요한 행을 복구하고 재시작합니다.
- `RECRUITING` 중에는 생성자도 `나가기`를 누를 수 있습니다. 이때 참가자 행만 나가고 `matches.creator_id`는 유지하므로 생성자의 관리 권한(시작·취소·결과 기록)은 사라지지 않습니다. `PLAYING`에서는 나갈 수 없습니다.
- 이 봇은 단일 프로세스 폴링 작업이며 Outbox나 분산 작업 큐를 사용하지 않습니다. 프로세스 중단·재시작 또는 Discord 장애 시 모집 reminder가 유실될 수 있으므로 로그와 카드의 마감 시각을 운영자가 확인해야 합니다. DB 상태는 Discord 메시지 편집·음성 이동 실패로 롤백되지 않으며, API 실패는 로그에 남긴 뒤 재시작 복구나 수동 카드 갱신으로 대응합니다.

## 테스트

개발 의존성을 설치한 가상 환경에서 실행합니다. Discord 쪽은 오프라인 테스트이고, `tests/test_matches.py`는 실제 PostgreSQL 통합 테스트입니다.

```powershell
# TEST_DATABASE_URL이 없으면 통합 테스트 모듈은 안전하게 skip됩니다.
python -m pytest
```

실제 통합 테스트는 별도 PostgreSQL DB를 만들고 `TEST_DATABASE_URL`만 설정해 실행합니다. fixture가 해당 DB에 migration을 적용하고 테스트 데이터를 만들고 지우므로 운영 DB를 절대 지정하지 마세요. 시간 만료 테스트는 고정된 `now`를 `process_due_matches(now)`에 전달하며 실제 `sleep`/대기를 사용하지 않습니다.

```powershell
$env:TEST_DATABASE_URL = "postgresql://<test-user>:<password>@<host>:5432/<test-db>"
python -m pytest tests/test_matches.py
```

macOS/Linux에서는 `export TEST_DATABASE_URL='...'`를 사용합니다. `TEST_DATABASE_URL`이 없으면 통합 테스트를 건너뛰며, 운영 `DATABASE_URL`을 테스트에 사용하지 않습니다. 테스트는 실제 PostgreSQL과 migration을 대상으로 하되 실제 Discord 토큰 검증과 실제 Supabase 연결은 네트워크·데이터 변경·명령 등록을 발생시키므로 pytest에 넣지 말고, 격리된 테스트 서버와 DB에서 별도 수동 검증만 수행하세요.

## 코드 구조

```text
src/inhouse_bot/
├── config.py                 # .env 로드 및 필수 설정 검증
├── db.py                     # asyncpg pool 생성/종료
├── services/                 # DB transaction과 내전 규칙
└── discord/                  # embed/버튼 interaction 표현 계층
supabase/migrations/          # PostgreSQL 스키마 migration
```

실행 진입점은 `python -m inhouse_bot.main`입니다.
