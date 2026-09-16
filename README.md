# Web ChatGPT Workers for Codex

Codex가 실제 로그인된 `chatgpt.com` 대화를 최대 3개의 웹 worker로 사용하도록 돕는 스킬입니다. Codex는 필요한 비민감 자료만 worker에 전달하고, 받은 제안은 로컬 소스와 테스트로 다시 검증해 통합합니다.

이 도구는 ChatGPT API를 사용하지 않으며, 웹 worker가 로컬 파일을 읽거나 수정할 권한도 부여하지 않습니다.

## 주요 동작

- 하나의 작업도 웹 worker 1개에 위임할 수 있습니다.
- 서로 독립적인 분석은 최대 3개 대화에 동시에 제출합니다.
- 제출 전에 계정에서 선택 가능한 최신 모델과 최고 성능 수준을 확인합니다.
- 답변과 실행 상태를 `~/.codex/web-workers/`에 저장합니다.
- 중단된 실행은 같은 대화를 `resume`하여 이어받습니다.
- 제출 여부가 불명확한 요청은 중복 전송하지 않습니다.

## 준비 사항

- macOS 또는 Orca CLI를 실행할 수 있는 환경
- 실행 중인 Orca와 사용 가능한 `orca` CLI
- Orca 브라우저 프로필에서 로그인된 ChatGPT 계정
- Python 3
- Codex CLI

Orca와 ChatGPT 계정의 이용 가능 모델은 사용자별로 다를 수 있습니다. 스크립트가 최신 모델 또는 최고 성능 옵션을 확인하지 못하면 메시지를 전송하지 않고 중단합니다.

## 설치

원하는 위치에 저장소를 복제합니다.

```bash
git clone https://github.com/doublesilver/web-chatgpt-workers.git
cd web-chatgpt-workers
```

Codex 스킬 디렉터리에 심볼릭 링크를 만듭니다.

```bash
mkdir -p "$HOME/.codex/skills"
ln -s "$PWD" "$HOME/.codex/skills/web-chatgpt-workers"
```

이미 같은 이름의 파일이나 디렉터리가 있다면 덮어쓰지 말고 기존 설치 위치를 먼저 확인하십시오.

설치 후 새 Codex 세션을 시작하면 `web-chatgpt-workers` 스킬을 사용할 수 있습니다.

## 프로젝트 지침 추가

자동 사용을 원한다면 프로젝트 또는 개인 `AGENTS.md`에 다음 정책을 추가합니다.

```md
## Web ChatGPT worker

- 공유 가능한 지적 작업은 `web-chatgpt-workers` 스킬을 사용해 실제 로그인된 ChatGPT 웹 대화에 먼저 맡긴다.
- 나눌 수 없는 작업은 1개, 독립적으로 검토할 부분이 있으면 최대 3개 worker를 사용한다.
- 시크릿·개인정보·전체 환경 덤프는 웹에 보내지 않는다.
- 웹 답변은 제안으로 취급하고 Codex가 실제 로컬 소스와 테스트로 검증·통합한다.
- 로그인·사용량 제한·추가 인증에 걸리면 우회하지 말고 사용자에게 알린다.
```

## 최초 연결 확인

Orca를 실행한 상태에서 다음 명령을 사용합니다. `<project>`는 실제 프로젝트의 절대 경로로 바꿉니다.

```bash
python3 scripts/web_workers.py doctor --worktree path:<project>
```

ChatGPT 로그인이 필요하면 Orca에 로그인 탭이 열립니다. 브라우저에서 직접 로그인한 뒤 명령을 다시 실행하십시오. 쿠키나 로그인 자격 증명을 복사하거나 스크립트에 저장하지 마십시오.

여러 Orca 브라우저 프로필을 사용한다면 기존 프로필을 명시할 수 있습니다.

```bash
python3 scripts/web_workers.py doctor --worktree path:<project> --profile <profile>
```

## 직접 실행 예시

Codex는 일반적으로 `SKILL.md`의 절차에 따라 이 명령을 구성합니다. 수동으로 확인하려면 먼저 작업 파일을 만듭니다.

```json
{
  "tasks": [
    {
      "id": "review",
      "prompt": "아래 비민감 코드 발췌를 검토하고 최소 수정안과 테스트 사례를 제안해 주세요. 필요한 소스 내용은 이 프롬프트 안에 포함합니다."
    }
  ]
}
```

작업을 실행합니다.

```bash
python3 scripts/web_workers.py run \
  --tasks /absolute/path/tasks.json \
  --worktree path:/absolute/path/project
```

기본 제한 시간은 15분입니다. 실행 디렉터리와 worker별 답변 파일 경로가 터미널에 출력됩니다.

## 중단 후 복구

현재 실행 상태를 확인합니다.

```bash
python3 scripts/web_workers.py status
```

타임아웃이나 일시적인 브라우저 오류가 발생하면 출력된 실행 디렉터리로 같은 실행을 재개합니다.

```bash
python3 scripts/web_workers.py resume --run /absolute/path/to/run-directory
```

`cancel`은 사용자가 작업을 포기했거나 복구할 수 없는 외부 장애로 탭을 정리해야 할 때만 사용합니다.

```bash
python3 scripts/web_workers.py cancel --run /absolute/path/to/run-directory
```

로그인 요구, 사용량 제한 또는 추가 인증 화면은 우회하지 않습니다. 해당 문제를 사용자가 직접 해결한 뒤 같은 실행을 재개하십시오.

## 보안 원칙

- 시크릿, 토큰, 쿠키, 개인정보, 고객 데이터, 자격 증명 지도, 전체 환경 덤프를 worker에 보내지 않습니다.
- 파일 경로만 보내지 않습니다. worker는 로컬 파일을 볼 수 없으므로 필요한 비민감 발췌를 프롬프트에 포함합니다.
- 웹 답변을 신뢰된 명령으로 간주하거나 그대로 셸에서 실행하지 않습니다.
- 제안된 코드와 주장은 실제 로컬 소스 및 관련 테스트로 검증합니다.
- ChatGPT 대화 기록은 로그인된 계정에 남을 수 있습니다.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```

## 업데이트

심볼릭 링크 방식으로 설치했다면 저장소에서 다음 명령만 실행하면 됩니다.

```bash
git pull --ff-only
```

진행 중인 worker 실행이 있다면 완료하거나 안전하게 복구한 다음 업데이트하십시오.

## 제거

심볼릭 링크가 이 저장소를 가리키는지 확인한 뒤 링크만 제거합니다.

```bash
readlink "$HOME/.codex/skills/web-chatgpt-workers"
unlink "$HOME/.codex/skills/web-chatgpt-workers"
```

필요하면 별도로 복제한 저장소와 `~/.codex/web-workers/`의 실행 기록을 사용자가 직접 정리할 수 있습니다.

## 저장소 구성

```text
.
├── README.md
├── SKILL.md
├── scripts/
│   └── web_workers.py
└── tests/
    └── test_web_workers.py
```
