"""
Meeting Log Bot v2
- 5분마다 노션 Meetinglog 데이터베이스 체크
- Claude API로 요약 (팀 컨텍스트 포함)
- Slack #meetinglog에 담당자 멘션 포함 포스팅
- 액션 아이템 확인 요청 → 확인 후 리마인더
"""

import os
import json
import time
import logging
import re
from datetime import datetime, timezone, timedelta
import requests
from apscheduler.schedulers.blocking import BlockingScheduler

# ── 환경변수 ──────────────────────────────────────────────
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ.get(
    "NOTION_DATABASE_ID", "3327f6a8812580b8bc7ec27ed8ea280a"
)
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C0B0CC7R9R8")
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "5"))
REMINDER_HOURS = int(os.environ.get("REMINDER_HOURS", "48"))  # 리마인더 간격 (시간)
POSTED_IDS_FILE = "/tmp/posted_ids.json"
ACTION_ITEMS_FILE = "/tmp/action_items.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── 팀 구성원 정보 ────────────────────────────────────────
TEAM_MEMBERS = {
    "김민수": {
        "slack_id": "U0APC5164SX",
        "role": "사업/전략/BM",
        "aliases": ["민수", "민수님", "Minsoo", "minsoo"],
    },
    "민혁": {
        "slack_id": "U0APF67VDEZ",
        "role": "개발/프로덕트",
        "aliases": ["민혁님", "Minhuek", "minhuek", "민혁이"],
    },
}


def name_to_slack_mention(name: str) -> str:
    """이름을 Slack 멘션으로 변환"""
    for member_name, info in TEAM_MEMBERS.items():
        if member_name in name or any(alias in name for alias in info["aliases"]):
            return f"<@{info['slack_id']}>"
    return name


def replace_names_with_mentions(text: str) -> str:
    """텍스트 내 모든 팀 멤버 이름을 Slack 멘션으로 변환"""
    for member_name, info in TEAM_MEMBERS.items():
        # 멤버 이름과 별칭 모두 치환
        all_names = [member_name] + info["aliases"]
        for name in all_names:
            # _이름:_ 패턴 (액션 아이템 담당자 라벨)
            text = text.replace(f"_{name}:_", f"_<@{info['slack_id']}>:_")
            text = text.replace(f"_{name}_", f"_<@{info['slack_id']}>_")
    return text


# ── 유틸: 이미 포스팅한 ID 관리 ───────────────────────────
def load_posted_ids() -> set:
    try:
        with open(POSTED_IDS_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_posted_ids(ids: set):
    with open(POSTED_IDS_FILE, "w") as f:
        json.dump(list(ids), f)


# ── 액션 아이템 저장/로드 ─────────────────────────────────
def load_action_items() -> list:
    try:
        with open(ACTION_ITEMS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_action_items(items: list):
    with open(ACTION_ITEMS_FILE, "w") as f:
        json.dump(items, f, ensure_ascii=False)


# ── 1단계: 노션 데이터베이스에서 최근 회의록 조회 ──────────
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def get_recent_meetings() -> list[dict]:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()

    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    resp = requests.post(
        url,
        headers=NOTION_HEADERS,
        json={
            "filter": {
                "timestamp": "created_time",
                "created_time": {"on_or_after": since},
            },
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
            "page_size": 10,
        },
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])

    pages = []
    for page in results:
        title = ""
        title_prop = page.get("properties", {}).get("Name", {})
        if title_prop.get("type") == "title":
            title_parts = title_prop.get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_parts)

        pages.append(
            {
                "id": page["id"],
                "title": title or "제목 없음",
                "created_time": page.get("created_time", ""),
                "url": page.get("url", ""),
            }
        )
    return pages


def get_page_content(page_id: str) -> str:
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

        if btype in (
            "paragraph", "heading_1", "heading_2", "heading_3",
            "bulleted_list_item", "numbered_list_item", "to_do",
            "toggle", "quote", "callout",
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

        if b.get("has_children"):
            try:
                children = _fetch_blocks(b["id"])
                lines.append(_blocks_to_text(children, depth + 1))
            except Exception as e:
                log.warning(f"자식 블록 읽기 실패: {e}")

    return "\n".join(lines)


# ── 2단계: Claude API로 요약 (팀 컨텍스트 포함) ──────────
SUMMARY_SYSTEM_PROMPT = """당신은 회의록 요약 전문가입니다.

## 팀 구성원
- 김민수 (사업/전략/BM 담당)
- 민혁 (개발/프로덕트 담당)

## 요약 포맷
회의록 내용을 아래 포맷으로 요약하세요. Slack 메시지로 사용됩니다.

📋 **{회의 제목}** ({날짜})

---

**🎯 핵심 결론**
• (3~5개 핵심 결론)

---

**📌 액션 아이템**
담당자별로 그룹핑. 담당자는 반드시 "김민수" 또는 "민혁" 중 하나(또는 공동)로 명확히 지정.

_김민수:_
- [ ] 할일 내용

_민혁:_
- [ ] 할일 내용

_공동:_
- [ ] 할일 내용

---

**💡 주요 논의 사항**
(주요 토픽별로 2~3줄씩 간결하게 요약)

---

## 중요 규칙
- 한국어로 작성
- 액션 아이템이 가장 중요 - 빠뜨리지 말 것
- 담당자 배정 기준:
  - "제가 해볼게요", "제가 리서치할게요" 등은 발화자에게 할당
  - 사업/전략/BM 관련은 김민수에게 할당
  - 개발/프로덕트/기능 관련은 민혁에게 할당
  - 둘 다 관련되면 공동으로 할당
- 불필요한 잡담은 제외하고 비즈니스 관련 내용만 추출
- 회의록 내용이 너무 짧거나 비어있으면 "회의록 내용이 아직 작성되지 않았습니다." 라고만 작성
"""


def summarize_with_claude(title: str, content: str) -> str:
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


# ── 3단계: Slack에 메시지 전송 (멘션 포함) ────────────────
def post_to_slack(message: str, notion_url: str) -> dict:
    """Slack에 메시지 전송. 멘션이 포함된 메시지 전송 후 메시지 정보 반환."""
    # 이름을 Slack 멘션으로 변환
    message_with_mentions = replace_names_with_mentions(message)

    full_message = (
        f"{message_with_mentions}\n\n"
        f"🔗 <{notion_url}|노션 회의록 원문>\n\n"
        f"_🤖 meetinglog bot에 의해 자동 생성됨_"
    )

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
    return result


def post_confirmation_request(parent_ts: str, action_items_text: str):
    """액션 아이템 확인 요청을 스레드로 전송"""
    # 모든 팀 멤버 멘션
    mentions = " ".join(
        f"<@{info['slack_id']}>" for info in TEAM_MEMBERS.values()
    )

    confirmation_msg = (
        f"📋 *액션 아이템 확인 요청* {mentions}\n\n"
        f"위 액션 아이템이 맞는지 확인해주세요!\n\n"
        f"✅ 맞으면 → 이 메시지에 ✅ 이모지를 달아주세요\n"
        f"❌ 수정이 필요하면 → 이 스레드에 수정사항을 남겨주세요\n"
        f"  예시: `추가: 민혁 - 로그인 페이지 디자인 수정`\n"
        f"  예시: `삭제: 김민수 - 사무실 시세 조사`\n"
        f"  예시: `변경: 김민수 → 민혁 - 플래너 리서치`\n\n"
        f"⏰ *{REMINDER_HOURS}시간 후* 확인된 액션 아이템 기준으로 리마인더가 갑니다!"
    )

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "channel": SLACK_CHANNEL_ID,
            "text": confirmation_msg,
            "thread_ts": parent_ts,
            "unfurl_links": False,
        },
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise Exception(f"Slack confirmation error: {result.get('error')}")
    log.info("📋 액션 아이템 확인 요청 전송 완료")


def check_thread_for_updates(channel_id: str, thread_ts: str) -> list[str]:
    """스레드에서 수정 요청 확인"""
    resp = requests.get(
        "https://slack.com/api/conversations.replies",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        params={
            "channel": channel_id,
            "ts": thread_ts,
            "limit": 50,
        },
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        return []

    updates = []
    for msg in result.get("messages", []):
        # 봇 메시지는 스킵
        if msg.get("bot_id"):
            continue
        text = msg.get("text", "")
        # 추가/삭제/변경 키워드가 있는 메시지만 수집
        if any(keyword in text for keyword in ["추가:", "삭제:", "변경:", "수정:"]):
            updates.append(text)
    return updates


def send_reminder(channel_id: str, thread_ts: str, meeting_title: str, action_items: list[dict]):
    """리마인더 전송"""
    # 스레드에서 수정사항 확인
    updates = check_thread_for_updates(channel_id, thread_ts)

    update_note = ""
    if updates:
        update_note = "\n\n📝 *스레드에서 수정된 사항:*\n" + "\n".join(f"• {u}" for u in updates)

    # 담당자별 그룹핑
    by_owner = {}
    for item in action_items:
        owner = item.get("owner", "공동")
        if owner not in by_owner:
            by_owner[owner] = []
        by_owner[owner].append(item.get("task", ""))

    items_text = ""
    for owner, tasks in by_owner.items():
        mention = name_to_slack_mention(owner)
        items_text += f"\n{mention}:\n"
        for task in tasks:
            items_text += f"  • {task}\n"

    reminder_msg = (
        f"⏰ *리마인더: {meeting_title}*\n\n"
        f"아래 액션 아이템의 진행 상황을 확인해주세요!\n"
        f"{items_text}"
        f"{update_note}\n\n"
        f"완료된 항목은 ✅로 표시해주세요!"
    )

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "channel": channel_id,
            "text": reminder_msg,
            "thread_ts": thread_ts,
            "unfurl_links": False,
        },
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("ok"):
        log.info(f"⏰ 리마인더 전송 완료: {meeting_title}")
    else:
        log.error(f"리마인더 전송 실패: {result.get('error')}")


# ── 액션 아이템 파싱 ──────────────────────────────────────
def parse_action_items(summary: str) -> list[dict]:
    """요약에서 액션 아이템 추출"""
    items = []
    current_owner = "공동"

    in_action_section = False
    for line in summary.split("\n"):
        # 액션 아이템 섹션 감지
        if "액션 아이템" in line:
            in_action_section = True
            continue
        # 다른 섹션 시작하면 종료
        if in_action_section and (line.startswith("**💡") or line.startswith("---")):
            if items:  # 이미 아이템이 있으면 종료
                break
            continue

        if not in_action_section:
            continue

        # 담당자 라벨 감지: _김민수:_ or _민혁:_ or _공동:_
        owner_match = re.match(r"_(.+?):_", line.strip())
        if owner_match:
            current_owner = owner_match.group(1)
            continue

        # 체크박스 아이템 감지
        task_match = re.match(r"\s*-\s*\[[ x]\]\s*(.+)", line)
        if task_match:
            task = task_match.group(1).strip()
            # "담당자: 내용" 패턴 제거 (이미 섹션으로 분류됨)
            for member_name in TEAM_MEMBERS:
                task = re.sub(rf"^{member_name}:\s*", "", task)
                for alias in TEAM_MEMBERS[member_name]["aliases"]:
                    task = re.sub(rf"^{alias}:\s*", "", task)
            if task:
                items.append({"owner": current_owner, "task": task})

    return items


# ── 메인 폴링 로직 ───────────────────────────────────────
def check_and_post():
    """새 회의록 체크 → 요약 → Slack 전송 → 확인 요청"""
    log.info("🔍 노션 Meetinglog 데이터베이스 체크 중...")

    try:
        posted_ids = load_posted_ids()
        pages = get_recent_meetings()

        new_pages = []
        for p in pages:
            pid_clean = p["id"].replace("-", "")
            if p["id"] in posted_ids or pid_clean in posted_ids:
                continue
            new_pages.append(p)

        if not new_pages:
            log.info("📭 새 회의록 없음")
            return

        for page in new_pages:
            log.info(f"📝 새 회의록 발견: {page['title']}")

            # 내용 가져오기
            content = get_page_content(page["id"])
            if len(content.strip()) < 50:
                log.info(f"⏭️ 내용이 너무 짧아 스킵: {page['title']}")
                posted_ids.add(page["id"])
                posted_ids.add(page["id"].replace("-", ""))
                save_posted_ids(posted_ids)
                continue

            # Claude로 요약
            log.info("🤖 Claude API로 요약 중...")
            summary = summarize_with_claude(page["title"], content)

            # 액션 아이템 파싱
            action_items = parse_action_items(summary)
            log.info(f"📌 액션 아이템 {len(action_items)}개 추출")

            # Slack에 전송 (멘션 포함)
            notion_url = page.get(
                "url", f"https://www.notion.so/{page['id'].replace('-', '')}"
            )
            result = post_to_slack(summary, notion_url)
            parent_ts = result.get("ts", "")

            # 액션 아이템 확인 요청 (스레드)
            if action_items and parent_ts:
                post_confirmation_request(parent_ts, summary)

                # 리마인더용 데이터 저장
                all_items = load_action_items()
                all_items.append({
                    "meeting_title": page["title"],
                    "channel_id": SLACK_CHANNEL_ID,
                    "thread_ts": parent_ts,
                    "action_items": action_items,
                    "posted_at": datetime.now(timezone.utc).isoformat(),
                    "reminder_sent": False,
                })
                save_action_items(all_items)

            # 처리 완료 기록
            posted_ids.add(page["id"])
            posted_ids.add(page["id"].replace("-", ""))
            save_posted_ids(posted_ids)
            log.info(f"✅ 완료: {page['title']}")

            time.sleep(2)

    except Exception as e:
        log.error(f"❌ 에러 발생: {e}", exc_info=True)


def check_and_send_reminders():
    """리마인더 시간이 된 액션 아이템 체크 & 리마인더 전송"""
    log.info("⏰ 리마인더 체크 중...")
    
    try:
        all_items = load_action_items()
        now = datetime.now(timezone.utc)
        updated = False

        for item in all_items:
            if item.get("reminder_sent"):
                continue

            posted_at = datetime.fromisoformat(item["posted_at"])
            hours_since = (now - posted_at).total_seconds() / 3600

            if hours_since >= REMINDER_HOURS:
                log.info(f"⏰ 리마인더 발송: {item['meeting_title']}")
                send_reminder(
                    item["channel_id"],
                    item["thread_ts"],
                    item["meeting_title"],
                    item["action_items"],
                )
                item["reminder_sent"] = True
                updated = True

        if updated:
            # 오래된 항목 정리 (7일 이상)
            all_items = [
                i for i in all_items
                if (now - datetime.fromisoformat(i["posted_at"])).days < 7
            ]
            save_action_items(all_items)

    except Exception as e:
        log.error(f"❌ 리마인더 에러: {e}", exc_info=True)


# ── 스케줄러 실행 ─────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"🚀 Meeting Log Bot v2 시작 (폴링 간격: {POLL_INTERVAL_MINUTES}분)")
    log.info(f"📂 노션 DB: {NOTION_DATABASE_ID}")
    log.info(f"💬 Slack 채널: {SLACK_CHANNEL_ID}")
    log.info(f"⏰ 리마인더 간격: {REMINDER_HOURS}시간")

    # 시작 시 한 번 즉시 실행
    check_and_post()

    # 스케줄러 설정
    scheduler = BlockingScheduler()
    # 5분마다 새 회의록 체크
    scheduler.add_job(check_and_post, "interval", minutes=POLL_INTERVAL_MINUTES)
    # 30분마다 리마인더 체크
    scheduler.add_job(check_and_send_reminders, "interval", minutes=30)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("👋 Bot 종료")
