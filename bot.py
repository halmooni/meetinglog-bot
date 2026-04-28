"""
Meeting Log Bot
- 5분마다 노션 _meeting-log 폴더를 체크
- 새 회의록 발견 시 Claude API로 요약
- Slack #meetinglog 채널에 자동 포스팅
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
import requests
from apscheduler.schedulers.blocking import BlockingScheduler

# ── 환경변수 ──────────────────────────────────────────────
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_PARENT_PAGE_ID = os.environ.get(
    "NOTION_PARENT_PAGE_ID", "32f7f6a88125802f87d4deb93baaf32a"
)  # _meeting-log 페이지 ID
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C0B0CC7R9R8")  # #meetinglog
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "5"))
POSTED_IDS_FILE = "/tmp/posted_ids.json"  # Railway는 재배포 시 리셋됨 (아래 Notion 방식으로 보완)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── 유틸: 이미 포스팅한 ID 관리 ───────────────────────────
def load_posted_ids() -> set:
    """로컬 파일에서 이미 처리한 페이지 ID 목록 로드"""
    try:
        with open(POSTED_IDS_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_posted_ids(ids: set):
    with open(POSTED_IDS_FILE, "w") as f:
        json.dump(list(ids), f)


# ── 1단계: 노션에서 _meeting-log 하위 페이지 조회 ──────────
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def get_child_pages() -> list[dict]:
    """_meeting-log 페이지의 하위 블록(child_page) 목록 조회"""
    url = f"https://api.notion.com/v1/blocks/{NOTION_PARENT_PAGE_ID}/children?page_size=20"
    resp = requests.get(url, headers=NOTION_HEADERS)
    resp.raise_for_status()
    results = resp.json().get("results", [])

    pages = []
    for block in results:
        if block.get("type") == "child_page":
            pages.append(
                {
                    "id": block["id"],
                    "title": block["child_page"]["title"],
                    "created_time": block.get("created_time", ""),
                    "last_edited_time": block.get("last_edited_time", ""),
                }
            )
    return pages


def get_page_content(page_id: str) -> str:
    """페이지의 전체 텍스트 블록을 재귀적으로 읽어오기"""
    blocks = _fetch_blocks(page_id)
    return _blocks_to_text(blocks)


def _fetch_blocks(block_id: str) -> list:
    url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
    resp = requests.get(url, headers=NOTION_HEADERS)
    resp.raise_for_status()
    return resp.json().get("results", [])


def _extract_text(rich_texts: list) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_texts)


def _blocks_to_text(blocks: list, depth: int = 0) -> str:
    lines = []
    for b in blocks:
        btype = b.get("type", "")
        prefix = "  " * depth

        # 텍스트 기반 블록 처리
        if btype in (
            "paragraph",
            "heading_1",
            "heading_2",
            "heading_3",
            "bulleted_list_item",
            "numbered_list_item",
            "to_do",
            "toggle",
            "quote",
            "callout",
        ):
            data = b.get(btype, {})
            text = _extract_text(data.get("rich_text", []))

            if btype == "heading_1":
                lines.append(f"\n# {text}")
            elif btype == "heading_2":
                lines.append(f"\n## {text}")
            elif btype == "heading_3":
                lines.append(f"\n### {text}")
            elif btype == "to_do":
                checked = "x" if data.get("checked") else " "
                lines.append(f"{prefix}- [{checked}] {text}")
            elif btype in ("bulleted_list_item", "numbered_list_item"):
                lines.append(f"{prefix}- {text}")
            else:
                if text.strip():
                    lines.append(f"{prefix}{text}")

        # 자식 블록 재귀
        if b.get("has_children"):
            children = _fetch_blocks(b["id"])
            lines.append(_blocks_to_text(children, depth + 1))

    return "\n".join(lines)


# ── 2단계: Claude API로 요약 ──────────────────────────────
SUMMARY_SYSTEM_PROMPT = """당신은 회의록 요약 전문가입니다. 
회의록 내용을 아래 포맷으로 요약하세요. Slack 메시지로 사용되므로 깔끔하게 정리하세요.

포맷:
📋 **{회의 제목}** ({날짜})

---

**🎯 핵심 결론**
• (3~5개 핵심 결론)

---

**📌 액션 아이템**
담당자별로 그룹핑:

_담당자명:_
- [ ] 할일 내용

---

**💡 주요 논의 사항**
(주요 토픽별로 2~3줄씩 간결하게 요약)

---

규칙:
- 한국어로 작성
- 전체 길이는 Slack 메시지로 읽기 좋은 수준으로 (너무 길지 않게)
- 액션 아이템이 가장 중요 - 빠뜨리지 말 것
- 불필요한 잡담은 제외하고 비즈니스 관련 내용만 추출
"""


def summarize_with_claude(title: str, content: str) -> str:
    """Claude API로 회의록 요약 생성"""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 2000,
            "system": SUMMARY_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": f"회의 제목: {title}\n\n회의록 내용:\n{content[:15000]}",
                }
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["content"][0]["text"]


# ── 3단계: Slack에 메시지 전송 ────────────────────────────
def post_to_slack(message: str, notion_url: str):
    """Slack Bot으로 #meetinglog 채널에 메시지 전송"""
    full_message = f"{message}\n\n🔗 <{notion_url}|노션 회의록 원문>"

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "channel": SLACK_CHANNEL_ID,
            "text": full_message,
            "unfurl_links": False,
        },
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise Exception(f"Slack API error: {result.get('error')}")
    log.info(f"✅ Slack 전송 완료: {result['ts']}")


# ── 메인 폴링 로직 ───────────────────────────────────────
def check_and_post():
    """새 회의록 체크 → 요약 → Slack 전송"""
    log.info("🔍 노션 _meeting-log 폴더 체크 중...")

    try:
        posted_ids = load_posted_ids()
        pages = get_child_pages()

        # 최근 24시간 내 생성된 페이지만 대상
        now = datetime.now(timezone.utc)
        new_pages = []
        for p in pages:
            if p["id"].replace("-", "") in posted_ids or p["id"] in posted_ids:
                continue
            # created_time 파싱
            try:
                created = datetime.fromisoformat(
                    p["created_time"].replace("Z", "+00:00")
                )
                hours_ago = (now - created).total_seconds() / 3600
                if hours_ago <= 24:
                    new_pages.append(p)
            except (ValueError, KeyError):
                continue

        if not new_pages:
            log.info("📭 새 회의록 없음")
            return

        for page in new_pages:
            log.info(f"📝 새 회의록 발견: {page['title']}")

            # 내용 가져오기
            content = get_page_content(page["id"])
            if len(content.strip()) < 50:
                log.info(f"⏭️ 내용이 너무 짧아 스킵: {page['title']}")
                continue

            # Claude로 요약
            log.info("🤖 Claude API로 요약 중...")
            summary = summarize_with_claude(page["title"], content)

            # Slack에 전송
            page_id_clean = page["id"].replace("-", "")
            notion_url = f"https://www.notion.so/{page_id_clean}"
            post_to_slack(summary, notion_url)

            # 처리 완료 기록
            posted_ids.add(page["id"])
            posted_ids.add(page_id_clean)
            save_posted_ids(posted_ids)
            log.info(f"✅ 완료: {page['title']}")

            # Rate limit 방지
            time.sleep(2)

    except Exception as e:
        log.error(f"❌ 에러 발생: {e}", exc_info=True)


# ── 스케줄러 실행 ─────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"🚀 Meeting Log Bot 시작 (폴링 간격: {POLL_INTERVAL_MINUTES}분)")
    log.info(f"📂 노션 폴더: {NOTION_PARENT_PAGE_ID}")
    log.info(f"💬 Slack 채널: {SLACK_CHANNEL_ID}")

    # 시작 시 한 번 즉시 실행
    check_and_post()

    # 이후 주기적 실행
    scheduler = BlockingScheduler()
    scheduler.add_job(check_and_post, "interval", minutes=POLL_INTERVAL_MINUTES)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("👋 Bot 종료")
