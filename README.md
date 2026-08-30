# Discord 내전 봇 MVP

Discord에서 게임 내전을 모집하고 팀을 배정하는 봇입니다. 기본 게임은 LoL 5:5입니다. 모집 상태와 참가자·팀·결과는 Supabase PostgreSQL에 저장하고, Discord 메시지는 그 상태를 보여 주는 UI로만 사용합니다.

현재 버전은 **1.0.1**입니다. 버전별 변경 사항은 [내전봇 패치 내역](CHANGELOG.md)에서 확인할 수 있습니다.

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

- `/내전 사용법`: 내전 진행 순서와 일반·관리자 명령을 비공개 안내로 확인합니다.
- `/내전 패치내역`: 현재 버전과 버전별 주요 변경 사항을 비공개 안내로 확인합니다.
- `/내전 생성 제목 1지망 2지망 [모집시간] [게임] [방식] [3지망]`: 현재 채널에 모집 패널을 만듭니다. 생성자의 1·2지망은 필수입니다.
- `/내전 생성`에는 선택적인 `모집시간`(분)을 지정할 수 있습니다. 기본값은 30분이며 5~1440분입니다.
- 패널의 `참가`, `나가기`, `팀 배정·시작`, `내전 취소` 버튼으로 모집을 관리합니다.
- `/내전 결과 승리팀 [메모]`: 진행 중인 내전 결과를 기록합니다.
- `/내전 강퇴 사용자:@사용자`: 현재 채널의 모집 중 내전에서 참가자 또는 대기자를 강퇴합니다.
- `/내전 전적 [사용자:@사용자] [게임] [시즌]`: 현재 서버의 종료 경기 전적과 승률을 조회합니다.
- `/내전 시즌시작 이름 [게임]`: 봇 소유자 또는 해당 서버의 DB 관리자만 새 시즌을 시작합니다. 기존 활성 시즌은 같이 종료됩니다. Discord `Manage Guild`만으로는 실행할 수 없습니다.
- `/내전 시즌종료 [게임]`: 현재 활성 시즌을 종료합니다.
- `/내전 라인변경 1지망 2지망 [3지망]`: 모집 또는 준비 확인 중 지망을 바꿉니다.
- `/내전 지명 사용자:@사용자`: Draft 현재 차례의 주장이 사용합니다.
- `/내전 등록 라인 티어 [게임]`: 서버 안에서 자신의 현재 시즌 라인 MMR을 최초 등록합니다. 일반 텍스트, 스레드, 음성 채널 내부 채팅에서도 됩니다. 예: `원딜 플래티넘2`.
- `/내전 mmr설정 사용자 라인 [티어] [점수] [게임]`: 봇 소유자 또는 해당 서버의 DB 관리자만 현재 시즌 라인 MMR을 설정합니다. 티어는 `플래티넘2`, `마스터중`, `챌린저`처럼 입력하며 점수가 있으면 점수를 우선합니다. Discord 명령 이름 제한 때문에 `MMR설정`이 아니라 소문자 `mmr설정`입니다.
- `/내전 랭킹 [게임] [시즌] [라인] [인원수]`: 라인별 또는 배치 라인 평균 MMR 순위를 조회합니다. 기본 10명, 최대 25명입니다.
- `/내전 관리자추가 사용자:@사용자` 및 `/내전 관리자삭제 사용자:@사용자`: 봇 소유자(`BOT_OWNER_ID`)가 해당 서버의 DB 관리자를 추가·삭제합니다. 봇 소유자는 DB 행이 없어도 모든 서버의 관리자이며 자기 자신은 삭제할 수 없습니다.
- `/내전 관리자목록`: 봇 소유자 또는 해당 서버의 DB 관리자만 현재 서버의 관리자 목록을 조회합니다. Discord `Manage Guild` 권한만으로는 봇 관리자가 되지 않습니다.

## 환경 변수

`.env.example`을 복사해 프로젝트 루트의 `.env`를 만들고 값을 채웁니다.

| 변수 | 설명 |
| --- | --- |
| `DISCORD_TOKEN` | Discord Bot 토큰. 저장소·로그·README에 남기지 않습니다. |
| `DISCORD_GUILD_ID` | 슬래시 명령을 동기화할 Discord 서버 ID(정수). |
| `BOT_OWNER_ID` | 봇 소유자의 Discord 사용자 ID(양의 정수). 관리자 추가·삭제와 전역 관리자 확인에 사용합니다. |
| `DATABASE_URL` | 앱이 사용할 PostgreSQL 연결 문자열. |
| `TEST_DATABASE_URL` | PostgreSQL 통합 테스트 전용 연결 문자열. 앱 실행에는 쓰지 않습니다. |
| `READY_TIMEOUT_SECONDS` | 준비 확인 제한 시간(초). 기본값 `120`. |
| `DEFAULT_RECRUITMENT_MINUTES` | `/내전 생성` 모집시간 기본값(분, 5~1440). 기본값 `30`. |
| `REMINDER_BEFORE_SECONDS` | 모집 마감 전 채널 알림 시점(초). 기본값 `300`(5분). |
| `TEAM_A_VOICE_CHANNEL_ID` | 기존 A팀 음성 채널 ID. B와 함께 설정할 때만 자동 배치 활성화. 기본값 빈 값(비활성). |
| `TEAM_B_VOICE_CHANNEL_ID` | 기존 B팀 음성 채널 ID. A와 함께 설정할 때만 자동 배치 활성화. 기본값 빈 값(비활성). |
| `INHOUSE_VOICE_CATEGORY_ID` | 내전 전용 보이스 채널을 만들 Discord 카테고리 ID. 설정하면 고정 A/B 채널보다 우선합니다. |
| `VOICE_CLEANUP_DELAY_SECONDS` | 종료 후 동적 보이스 채널 삭제 대기 시간. 기본값 `600`, 최소 `0`. |

실제 토큰이나 데이터베이스 비밀번호가 들어간 `.env`를 커밋하거나 채팅에 붙여넣지 마세요. 이미 노출했다면 Discord 토큰을 재발급하고 DB 비밀번호를 회전해야 합니다.

## 2차 내전 흐름

상태는 PostgreSQL이 관리하며 `RECRUITING → READY_CHECK → PLAYING → FINISHED` 순서로 진행합니다. 모집·준비·진행 중에는 생성자 또는 봇 관리자(소유자/서버별 DB 관리자)가 취소할 수 있고, 허용되지 않은 역방향 전이는 하지 않습니다. 준비 확인 중 참가자가 빠져 정원이 부족해진 경우에만 다시 `RECRUITING`으로 돌아갑니다.

- 기존 `팀 배정·시작` 버튼(현재 표시명 `준비 확인 시작`)은 생성자 또는 봇 관리자만 누를 수 있고, 참가자가 선택한 게임 정원과 정확히 같을 때 `READY_CHECK`를 시작합니다. 모든 준비 상태를 초기화하고 120초 제한을 저장합니다.
- 준비 카드에는 준비 완료/전체 인원, 준비·미준비 명단, 남은 시간, `준비` 토글(다시 누르면 준비 취소), `내전 취소`를 표시합니다. 실제 참가자만 준비할 수 있으며 중복 클릭은 한 번만 반영됩니다. 전원이 준비하면 한 트랜잭션에서 무작위 A/B 팀을 저장하고 `PLAYING`으로 전환한 뒤 카드와 음성 배치를 갱신합니다.
- 정원이 찬 상태에서 `참가`를 누르면 대기열에 등록됩니다. 참가자와 대기자는 중복될 수 없고, 등록 시각 FIFO 순서를 유지합니다. `나가기`는 둘 다 취소할 수 있으며, 참가자가 나가거나 강퇴되면 같은 트랜잭션에서 첫 대기자를 승격합니다. `READY_CHECK` 명단이 바뀌면 준비를 초기화하고, 정원이 다시 차면 새 제한 시간으로 준비 확인을 재시작합니다. `PLAYING` 중 이탈·강퇴·팀 재배정은 하지 않습니다.
- `/내전 강퇴 사용자:@사용자`는 현재 채널의 활성 내전에서 생성자 또는 봇 관리자만 사용할 수 있고, `RECRUITING`·`READY_CHECK`에서 참가자와 대기자를 모두 대상으로 합니다. 자기 자신 강퇴도 같은 규칙을 따르며, 대상이 아니면 ephemeral 오류를 반환합니다.
- `/내전 전적 [사용자:@사용자]`는 생략 시 실행자 기준으로 현재 서버의 `FINISHED` 경기만 SQL 집계합니다. 총 경기·승·패·승률을 표시하고, `CANCELLED` 경기와 다른 서버의 경기는 제외하며 0경기 승률은 0%입니다.

## 3A 게임·시즌·MMR

게임 설정은 `games` 테이블에 둡니다. 관리 명령은 따로 안 만들었습니다. 두 번째 게임은 SQL Editor나 migration으로 아래 정도만 추가하면 됩니다. `capacity`는 `team_size * 2`여야 하고 짝수여야 합니다. 시즌 시작과 라인 MMR 설정은 `BOT_OWNER_ID` 또는 서버별 DB 관리자만 실행할 수 있습니다.

```sql
insert into public.games
    ("key", name, team_size, capacity, default_rating, k_factor, rating_enabled)
values
    ('valorant', 'VALORANT', 5, 10, 1000, 32, true);
```

경기 생성 때 게임 정원과 활성 시즌을 경기 행에 저장합니다. 활성 시즌이 없으면 `시즌 1`을 자동 생성합니다. 시즌 변경은 `/내전 시즌시작`, `/내전 시즌종료`를 쓰면 되고 봇 소유자 또는 해당 서버의 DB 관리자로 등록돼 있어야 합니다. 서버·게임마다 활성 시즌은 하나만 허용됩니다.

MMR은 팀 평균 기준 Elo입니다.

```text
expected_a = 1 / (1 + 10 ** ((avg_b - avg_a) / 400))
delta_a = round(k_factor * (actual_a - expected_a))
delta_b = -delta_a
```

기본 점수는 1000, K 값은 32입니다. 게임별로 바꿀 수 있습니다. 팀 배정 시점 점수를 참가자에 스냅샷으로 남기고 결과 저장, 점수 갱신, 이력 저장, 경기 종료를 한 트랜잭션에서 처리합니다. 3A migration 전에 끝난 기존 경기는 `Legacy` 시즌에 연결만 합니다. 기존 결과로 MMR을 다시 계산하지 않습니다.

## 3B LoL 라인 MMR

LoL은 탑, 정글, 미드, 원딜, 서폿 점수를 따로 씁니다. 라인 row가 없으면 미배치고 실제 0점과는 다른 상태입니다. 참가할 때 낸 지망 라인은 전부 배치돼 있어야 합니다. 평균은 0보다 큰 배치 점수만 더해서 단순 평균을 냅니다.

처음 쓰는 서버는 아래 순서로 하면 됩니다.

1. `/내전 시즌시작 이름:시즌 1` 실행
2. 각 사용자가 `/내전 등록`으로 자신의 지망 라인 MMR을 등록하거나 관리자가 `/내전 mmr설정`으로 입력
3. `/내전 생성`에서 방식과 생성자 지망 입력

통합 티어 문자열은 아이언~다이아에 `1`~`4`, 마스터·그랜드마스터에 `하`·`중`·`상`을 붙입니다(예: `플래티넘2`, `마스터중`, `그랜드마스터상`). 챌린저는 `챌린저`만 입력합니다. 기존 점수 이전이 필요하면 관리자 명령에서 티어 대신 `점수`를 직접 넣을 수 있습니다. 승패나 기존 경기 기록은 이 명령에서 안 건드립니다.

기본 방식 `Balanced`는 양 팀에 다섯 라인을 한 명씩 채웁니다. 먼저 전체 지망 비용을 줄이고, 다음으로 팀 MMR 차이, 가장 큰 맞라인 차이, 맞라인 차이 합계를 비교합니다. 같은 입력과 match ID면 결과도 같습니다.

`Draft`는 준비가 다 끝나면 `DRAFTING`으로 갑니다. 양수 라인 평균이 높은 두 명이 주장이고 A-B-B-A-A-B-B-A 순서로 지명합니다. 지명 때문에 마지막 라인 구성이 불가능해지면 그 지명은 저장하지 않습니다. 마지막 지명 뒤 팀 안에서 라인을 정하고 그때 점수를 스냅샷으로 남깁니다.

결과 저장 때는 실제 `assigned_role` 한 줄만 갱신합니다. 글로벌 `player_ratings`에는 같이 반영하지 않습니다. 라인 기능이 꺼진 다른 게임은 기존 3A 글로벌 MMR 흐름 그대로 갑니다.

## 모집 알림·만료와 재시작 복구

`/내전 생성` 시 모집 마감 시각을 저장하고 카드에 Discord timestamp로 표시합니다. 기본값은 마감 5분 전이며 `REMINDER_BEFORE_SECONDS`로 초 단위 설정을 바꿀 수 있습니다. 전체 모집 시간이 알림 시점보다 짧으면 사전 알림을 생략하고, DM은 보내지 않습니다. `recruitment_reminded_at`으로 중복 알림을 막습니다.

모집 마감 시 트랜잭션 안에서 상태와 시각을 다시 확인합니다. 정원이 차 있으면 `READY_CHECK`를 시작하고, 부족하면 `CANCELLED`(사유: 모집 시간 만료)로 종료하며 `ended_at`을 저장하고 버튼을 비활성화합니다. 준비 시간이 만료되면 미준비자를 제거하고 FIFO 대기자를 승격한 뒤, 정원이 차면 새 `READY_CHECK`, 부족하면 `RECRUITING`으로 돌립니다. 이 과정에서 제거·승격 결과를 채널에 알립니다.

예약 작업은 별도 큐 없이 단일 background task가 약 10~15초마다 한 번 폴링합니다. `process_due_matches(now)`처럼 현재 시각을 받는 함수로 모집 알림·모집 만료·준비 만료를 처리하며, 실행 전 DB 상태와 기한을 재검사하고 여러 번 실행해도 중복 전이가 없도록 멱등 처리합니다. DB 트랜잭션을 Discord API 호출보다 먼저 커밋하므로 API 실패가 상태 변경을 되돌리지 않습니다.

재시작 시 `RECRUITING`, `READY_CHECK`, `DRAFTING`, `PLAYING`을 조회해 Persistent View와 최신 카드를 다시 등록하고, 이미 지난 마감은 즉시 처리하며 남은 마감은 폴링이 이어서 처리합니다. 이미 `recruitment_reminded_at`이 있는 경기에는 알림을 다시 보내지 않습니다. `PLAYING`은 DB에 저장된 카테고리와 채널 ID를 다시 확인하고 빠진 채널만 복구합니다. 정리 시간이 지난 종료 경기 처리도 다시 시작합니다.

## 보이스 채널 자동 생성과 배치

카테고리 선택 순서는 `INHOUSE_VOICE_CATEGORY_ID` → 기존 고정 A/B 채널 → 내전 텍스트 채널의 상위 카테고리입니다. 환경 변수로 지정한 카테고리가 없거나 다른 서버 것이면 내전을 만들 때 오류를 표시합니다. 고정 A/B가 둘 다 있으면 예전 방식 그대로 쓰고 자동 삭제도 안 합니다.

동적 채널 이름은 `{match.id}번째 내전 1팀`, `{match.id}번째 내전 2팀`으로 고정입니다. 예를 들면 `104번째 내전 1팀`입니다. 서로 다른 텍스트 채널에서는 내전을 여러 개 열 수 있고 각 경기의 DB 채널 ID만 사용합니다. 같은 사용자는 같은 서버의 활성 내전 하나에만 참가할 수 있습니다. 참가자와 대기열 둘 다 포함입니다.

보이스 채널은 팀과 라인이 DB에서 확정되고 `PLAYING` 커밋이 끝난 다음 만듭니다. 팀 사용자별 overwrite를 넣고 현재 음성에 접속한 사람만 옮깁니다. 미접속은 실패가 아닙니다. 한 명 이동이 실패해도 나머지는 계속 처리합니다. Discord 실패로 경기 시작이나 결과가 롤백되지는 않습니다.

봇 권한은 `Manage Channels`, `Move Members`, `View Channel`, `Connect`가 필요합니다. 사용자별 permission overwrite도 설정할 수 있어야 합니다. `Administrator`는 필요 없습니다. Gateway intent는 기존 `voice_states`만 쓰며 `GUILD_MEMBERS`, `MESSAGE_CONTENT`는 추가하지 않습니다.

동적 팀 보이스는 마지막 사용자가 연결을 끊거나 다른 채널로 옮겨 채널이 비면 바로 삭제합니다. 빈 채널로 닫힌 팀 보이스는 같은 내전에서 다시 만들지 않습니다. DB에 저장된 동적 채널 ID만 대상으로 하므로 고정 A/B 채널과 이름만 비슷한 사용자 채널은 건드리지 않습니다.

종료나 취소 뒤 사람이 남은 채널은 기본 10분이 지나면 삭제합니다. `VOICE_CLEANUP_DELAY_SECONDS`는 사람이 남아 있거나 음성 상태 이벤트가 누락된 경우의 보완 처리입니다. 한쪽만 삭제되면 다른 팀에는 영향이 없고, 삭제 실패 ID는 유지한 채 약 10~15초 폴링에서 다시 확인합니다. 재시도 전에 사용자가 들어오면 빈 채널 종료 표시를 되돌립니다.

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

`supabase db push`는 연결한 원격 프로젝트에 아직 적용되지 않은 migration을 적용합니다. 이미 데이터가 있는 프로젝트에서는 먼저 백업과 대상 project-ref를 확인하세요. CLI를 사용할 수 없을 때만 대시보드 SQL Editor에서 migration 파일을 순서대로 검토해 수동 실행하고, migration 이력을 정리하기 전에는 `db push`와 섞어 쓰지 않습니다.

1차부터 3A까지는 `20260826012146_schema.sql`, 3B는 `20260826025650_phase_3b_role_mmr.sql`, 동적 보이스는 `20260826034441_phase_3b_dynamic_voice_channels.sql`, 빈 보이스 종료 상태는 `20260826060624_phase_3b_empty_voice_channels.sql`, 봇 관리자 테이블은 `20260826063825_bot_admins.sql`입니다. 초기화한 DB에서는 프로젝트 루트에서 `supabase db push` 한 번이면 순서대로 들어갑니다. 3B는 LoL의 라인 기능만 켜고 기존 글로벌 점수를 다섯 라인에 복사하지 않습니다. 끝난 경기도 다시 계산 안 합니다. migration 전에 만들어진 경기는 경기별 라인 기능 값이 `false`라서 진행 중인 기존 경기도 3A 방식으로 마무리됩니다.

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

개발 의존성을 설치한 가상 환경에서 실행합니다. Discord 쪽은 오프라인 테스트이고, `tests/test_matches.py`, `tests/test_role_matches.py`, `tests/test_dynamic_voice_matches.py`, `tests/test_admin.py`의 DB 관련 테스트는 실제 PostgreSQL 통합 테스트입니다.

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
