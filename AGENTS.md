# Project instructions

- 사용자나 운영자에게 보이는 동작, 명령, 설정, migration 또는 실행·테스트 절차를 변경하면 같은 작업에서 `CHANGELOG.md`, `README.md`, `/내전 사용법`, `/내전 패치내역`의 관련 설명도 확인하고 실제 동작과 다르면 함께 수정한다.
- 릴리스 버전을 올릴 때는 `pyproject.toml`, `src/inhouse_bot/__init__.py`, `README.md`, `CHANGELOG.md`, `/내전 패치내역`과 관련 테스트의 버전을 모두 맞춘다.
- 내부 구현만 바뀌고 사용자·운영자 동작이 같으면 문서를 억지로 수정하지 않는다.
