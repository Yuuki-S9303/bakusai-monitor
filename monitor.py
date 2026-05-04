"""
爆サイ監視スクリプト
- Google Sheetsから監視リストを読み込み
- source_urlのスレッドを直接取得
- キーワード検知でDiscord通知
"""

import os
import json
import time
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
NOTIFIED_IDS_FILE = "notified_ids.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ── Google Sheets 読み込み ────────────────────────────
def load_targets_from_sheet():
    """
    スプシの構成（1行目はヘッダー）:
    A: thread_title_keyword
    B: acode
    C: ctgid
    D: bid
    E: detect_keyword  （カンマ区切り or * で全マッチ）
    F: detect_condition（OR / AND）
    G: active          （TRUE/FALSE）
    H: source_url      （スレッドの直接URL）
    """
    import json as _json
    creds_info = _json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    service = build("sheets", "v4", credentials=creds)

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="監視KW管理!A2:H100"
    ).execute()

    rows = result.get("values", [])
    targets = []
    for row in rows:
        if len(row) < 8:
            continue
        title, acode, ctgid, bid, detect_keyword, detect_condition, active, source_url = row[:8]
        if active.strip().upper() != "TRUE":
            continue
        targets.append({
            "title": title.strip(),
            "source_url": source_url.strip(),
            "detect_keyword": detect_keyword.strip(),
            "detect_condition": detect_condition.strip().upper(),
        })
    return targets

# ── スレッド書き込み取得 ──────────────────────────────
def get_posts(thread_url: str) -> list[dict]:
    try:
        resp = requests.get(thread_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] スレッド取得失敗: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []

    for item in soup.select("div.res_list_article"):
        post_id = item.get("id", "")  # 例: "res1", "res286"
        body = item.select_one(".res_body")
        text = body.get_text(separator=" ", strip=True) if body else item.get_text(separator=" ", strip=True)
        post_url = f"{thread_url}#{post_id}" if post_id else thread_url

        if post_id and text:
            posts.append({
                "id": post_id,
                "text": text,
                "url": post_url,
            })

    return posts

# ── キーワードマッチ ──────────────────────────────────
def matches_keyword(text: str, detect_keyword: str, detect_condition: str) -> bool:
    if detect_keyword == "*":
        return True
    keywords = [k.strip() for k in detect_keyword.split(",") if k.strip()]
    if detect_condition == "AND":
        return all(k in text for k in keywords)
    return any(k in text for k in keywords)  # OR（デフォルト）

# ── 通知済みID管理 ────────────────────────────────────
def load_notified_ids() -> dict:
    if os.path.exists(NOTIFIED_IDS_FILE):
        with open(NOTIFIED_IDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_notified_ids(data: dict):
    with open(NOTIFIED_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── Discord通知 ───────────────────────────────────────
def notify_discord(keyword: str, post: dict, thread_url: str) -> bool:
    message = (
        f"🔔 **キーワード検知: `{keyword}`**\n"
        f"🧵 スレッド: {thread_url}\n"
        f"🔗 投稿URL: {post['url']}\n"
        f"📝 内容（抜粋）: {post['text'][:200]}"
    )
    for attempt in range(3):
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
            if resp.status_code == 429:
                retry_after = float(resp.json().get("retry_after", 1))
                print(f"[WARN] Discord rate limit。{retry_after}秒待機")
                time.sleep(retry_after + 0.2)
                continue
            resp.raise_for_status()
            print(f"[OK] Discord通知送信: post_id={post['id']}")
            return True
        except Exception as e:
            print(f"[ERROR] Discord通知失敗: {e}")
            return False
    print(f"[ERROR] Discord通知失敗（3回リトライ超過）: post_id={post['id']}")
    return False

# ── メイン処理 ────────────────────────────────────────
def main():
    print("=== 爆サイ監視スタート ===")

    notified_ids = load_notified_ids()
    updated = False

    try:
        targets = load_targets_from_sheet()
    except Exception as e:
        print(f"[ERROR] スプシ読み込み失敗: {e}")
        return

    print(f"監視対象: {len(targets)}件")

    for target in targets:
        title = target["title"]
        source_url = target["source_url"]
        detect_keyword = target["detect_keyword"]
        detect_condition = target["detect_condition"]

        print(f"\n--- [{title}] 処理中 ---")
        print(f"URL: {source_url}")

        posts = get_posts(source_url)
        print(f"取得した書き込み数: {len(posts)}")

        notified_key = source_url
        if notified_key not in notified_ids:
            notified_ids[notified_key] = []

        for post in posts:
            post_id = post["id"]

            if post_id in notified_ids[notified_key]:
                continue

            if matches_keyword(post["text"], detect_keyword, detect_condition):
                if notify_discord(detect_keyword, post, source_url):
                    notified_ids[notified_key].append(post_id)
                    updated = True

        time.sleep(2)

    if updated:
        save_notified_ids(notified_ids)
        print("\n通知済みIDを更新しました")

    print("\n=== 処理完了 ===")

if __name__ == "__main__":
    main()
