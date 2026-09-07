# Crosscrew (크로스크루)

**내가 쓰던 AI에서 계속 일하면서, 중요한 판단에 다른 모델의 시야를 빌립니다.**

설계·계획·구현 검증에서 다른 AI에게 반론이나 독립 검토를 요청하고,
결과를 반영한 뒤 같은 검토자에게 다시 물어보는 흐름을 돕습니다.
Claude Code·Codex·Grok·Antigravity(`agy`)의 실제 CLI를 호출합니다.

현재는 **macOS용 초기 알파**입니다. Python 3.11 이상과 사용할 CLI의 설치·로그인이
필요합니다. 첫 공개의 중심은 Claude ↔ Codex 검토와 후속 요청이며,
Grok·agy는 실험적 지원입니다. 모든 앱·조합·새 컴퓨터에서 검증했다는 뜻은 아닙니다.

- **Crosscrew**: 다른 AI 호출, 작업 상태·결과·세션 관리.
- **[Council](https://github.com/ktaey7/multi-agent-council)**: 토론·적대적 검증의 규칙.

둘은 독립적입니다. 한 번의 검토에 Council을 설치할 필요는 없습니다.

## 시작하기

사용할 AI의 공식 CLI에서 먼저 로그인하고 `python3 --version`이 3.11 이상인지 확인합니다.

```bash
git clone https://github.com/ktaey7/crosscrew.git "$HOME/.local/share/crosscrew"
cd "$HOME/.local/share/crosscrew"
python3 install.py --dry-run --host claude --host codex
python3 install.py --host claude --host codex
```

원하는 호스트만 선택합니다. 기존 파일과 충돌하면 덮어쓰지 않고 중단합니다.
기존 AI의 설정·인증·hook·MCP·Council 파일은 수정하지 않습니다.
`~/.local/bin`이 PATH에 있어야 `crosscrew` 명령을 찾을 수 있습니다.
호스트에서 새 진입점을 찾으려면 세션을 새로 열어야 할 수 있습니다.

설치한 호스트에서 다음처럼 요청해 봅니다.

> Crosscrew로 다른 모델에게 이 계획의 약점을 검토시켜 줘.
> 계획을 고친 다음 같은 검토자에게 다시 물어볼 수 있게 해 줘.

Codex → Claude처럼 호스트 sandbox 밖에서 실행해야 하는 경로에는 별도 broker가 필요합니다.
일반 설치는 서비스를 켜지 않습니다. 실행·권한·서비스 설치·제거와 CLI 예시는
[영문 README](README.md)를 확인하세요.

## 알고 사용할 범위

- 구독 인증은 각 CLI가 맡습니다. Crosscrew가 구독을 API처럼 중계하지 않습니다.
- CLI 실행만으로 구독 과금을 보장할 수는 없습니다. 알려진 API 인증 설정은 차단하지만,
  실제 로그인 방식과 추가 사용 과금은 각 CLI에서 확인해야 합니다.
- Claude의 `review`는 프롬프트로 쓰기를 제한합니다. OS 수준 읽기 전용 sandbox가 아닙니다.
- 백그라운드 완료 알림과 호스트 재개 경험은 사용하는 앱에 따라 다릅니다.
- 현재 웹 채팅 화면만으로는 이 로컬 CLI를 사용할 수 없습니다.
- 실행 기록은 `~/.local/state/crosscrew`에 보관되며, 제거 시 자동 삭제하지 않습니다.
  프롬프트·결과·세션 정보가 있으므로 공개 저장소에 올리지 마세요.

개인 환경에서 반복 사용한 구성을 다른 사람이 시험할 수 있게 정리한 첫 버전입니다.
외부 수요나 경쟁 도구보다 설치가 쉽다는 가설은 아직 검증되지 않았습니다.
설치 중 막힌 부분과 한 번의 실제 검토 경험이 가장 유용한 피드백입니다.
