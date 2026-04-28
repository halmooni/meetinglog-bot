# 🤖 Meeting Log Bot — 셋업 가이드

노션 회의록이 작성되면 자동으로 요약해서 Slack #meetinglog에 올려주는 봇.

## 구조

```
노션 _meeting-log 폴더
    ↓ (5분마다 체크)
Bot (Railway)
    ↓
Claude API로 요약
    ↓
Slack Bot → #meetinglog 채널
```

## 셋업 (10분이면 끝)

### 1. 노션 Integration 생성

1. https://www.notion.so/my-integrations 접속
2. **+ New integration** 클릭
3. 이름: `meetinglog-bot`
4. 권한: **Read content** 체크
5. Submit → **Internal Integration Secret** 복사 (= `NOTION_API_KEY`)
6. ⚠️ **중요**: 노션에서 `_meeting-log` 페이지 열기 → 우측 상단 `···` → **연결 추가** → `meetinglog-bot` 선택

### 2. Slack Bot 확인

이미 financial bot이 있으니, 같은 Slack App에서:
1. https://api.slack.com/apps 접속
2. 기존 앱 선택 (또는 새로 만들기)
3. **OAuth & Permissions** → Bot Token Scopes에 `chat:write` 추가
4. **Bot User OAuth Token** 복사 (= `SLACK_BOT_TOKEN`, `xoxb-`로 시작)
5. #meetinglog 채널에 봇 초대: 채널에서 `/invite @봇이름`

### 3. Anthropic API Key

1. https://console.anthropic.com/settings/keys 접속
2. **Create Key** → 복사 (= `ANTHROPIC_API_KEY`)

### 4. Railway 배포

```bash
# 프로젝트 디렉토리에서
git init
git add .
git commit -m "meetinglog bot"

# Railway CLI
railway login
railway init        # 또는 기존 프로젝트에 서비스 추가
railway up
```

또는 Railway 대시보드에서:
1. **New Service** → **GitHub Repo** 또는 **Deploy from local**
2. **Variables** 탭에서 환경변수 설정:

| 변수명 | 값 |
|---|---|
| `NOTION_API_KEY` | `ntn_...` |
| `NOTION_PARENT_PAGE_ID` | `32f7f6a88125802f87d4deb93baaf32a` |
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `SLACK_BOT_TOKEN` | `xoxb-...` |
| `SLACK_CHANNEL_ID` | `C0B0CC7R9R8` |
| `POLL_INTERVAL_MINUTES` | `5` |

3. **Settings** → Start Command가 `python bot.py`인지 확인 (Procfile이 있으면 자동)

### 5. 확인

Railway 로그에서 이런 메시지가 보이면 성공:
```
🚀 Meeting Log Bot 시작 (폴링 간격: 5분)
📂 노션 폴더: 32f7f6a88125802f87d4deb93baaf32a
💬 Slack 채널: C0B0CC7R9R8
🔍 노션 _meeting-log 폴더 체크 중...
```

## 비용

- **Railway**: Worker 서비스 → 월 $5 이하 (거의 idle 상태)
- **Claude API**: 회의록 1건 요약 ≈ $0.01~0.03 (Sonnet 기준)
- **노션/Slack API**: 무료

## FAQ

**Q: Railway 재배포하면 이미 올린 회의록 또 올라가나요?**
A: 24시간 이내 생성된 것만 체크하고, 로컬에 처리 기록을 저장해요. 재배포 직후 한 번 중복될 수 있는데, 자주 일어나진 않아요. 더 확실하게 하려면 노션 페이지에 "posted" 태그를 추가하는 방식으로 업그레이드할 수 있어요.

**Q: 봇이 죽었는지 어떻게 알아요?**
A: Railway 대시보드에서 로그를 확인하거나, Healthcheck를 추가할 수 있어요.

**Q: 요약 포맷을 바꾸고 싶어요**
A: `bot.py`의 `SUMMARY_SYSTEM_PROMPT` 변수를 수정하면 돼요.
