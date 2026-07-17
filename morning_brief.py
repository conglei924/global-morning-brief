"""Generate and send a source-transparent global morning news briefing."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import smtplib
import urllib.error
import urllib.request
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

import feedparser
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Article:
    source: str
    title: str
    url: str
    published: datetime | None
    description: str


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def entry_date(entry: object) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    return datetime(*parsed[:6])


def load_sources() -> list[dict[str, str]]:
    with (ROOT / "sources.json").open(encoding="utf-8") as handle:
        sources = json.load(handle)
    if not isinstance(sources, list) or not all({"name", "url"} <= set(x) for x in sources):
        raise ValueError("sources.json must be a list of objects with name and url")
    return sources


def fetch_articles(sources: Iterable[dict[str, str]]) -> tuple[list[Article], list[str]]:
    articles: list[Article] = []
    failures: list[str] = []
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=30)
    for source in sources:
        feed = feedparser.parse(source["url"])
        if feed.bozo and not feed.entries:
            failures.append(source["name"])
            continue
        for entry in feed.entries[:25]:
            title = clean_text(entry.get("title", ""))
            url = entry.get("link", "")
            if not title or not url:
                continue
            published = entry_date(entry)
            if published and published < cutoff:
                continue
            articles.append(Article(source["name"], title, canonical_url(url), published, clean_text(entry.get("summary", ""))))
    return deduplicate(articles), failures


def deduplicate(articles: list[Article]) -> list[Article]:
    """Remove exact URLs and near-identical headlines, retaining source diversity."""
    unique: list[Article] = []
    urls: set[str] = set()
    title_tokens: list[set[str]] = []
    for article in sorted(articles, key=lambda a: a.published or datetime.min, reverse=True):
        tokens = set(re.findall(r"[a-z0-9]+", article.title.lower()))
        if article.url in urls or any(len(tokens & prior) / max(1, len(tokens | prior)) >= 0.78 for prior in title_tokens):
            continue
        unique.append(article)
        urls.add(article.url)
        title_tokens.append(tokens)
    return unique


def select_articles(articles: list[Article], maximum: int) -> list[Article]:
    """Prefer recent stories while preventing one outlet from dominating the digest."""
    selected: list[Article] = []
    per_source: Counter[str] = Counter()
    for article in articles:
        if len(selected) == maximum:
            break
        if per_source[article.source] >= 4:
            continue
        selected.append(article)
        per_source[article.source] += 1
    return selected


def chinese_summary(articles: list[Article]) -> str | None:
    key, model = os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_MODEL")
    material = "\n".join(
        f"[{index}] 来源：{a.source}\n标题：{a.title}\n摘要：{a.description}\n链接：{a.url}"
        for index, a in enumerate(articles, 1)
    )
    prompt = """你是严谨的国际新闻编辑。仅根据下列新闻标题和摘要，写一份中文全球晨报。
要求：不补充材料中没有的事实；将相近报道合并为一个议题；只选最重要的 6–10 个议题；每条用 1–2 句中文解释“发生了什么、为什么重要”，不要复述英文标题；若报道相互矛盾要明确写出。开头给出不超过 3 句的“今日要点”。每条末尾标出支撑它的 [编号]，但正文不要放 URL。使用纯文本，不用 Markdown 标记。"""
    if key and model:
        try:
            from openai import OpenAI

            response = OpenAI(api_key=key).responses.create(model=model, input=f"{prompt}\n\n素材：\n{material}")
            return response.output_text.strip()
        except Exception as exc:
            print(f"OpenAI summary unavailable: {exc}", file=sys.stderr)

    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        return None
    payload = json.dumps({
        "model": os.getenv("GITHUB_MODEL", "openai/gpt-4o"),
        "messages": [{"role": "user", "content": f"{prompt}\n\n素材：\n{material}"}],
        "temperature": 0.2,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://models.github.ai/inference/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {github_token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "User-Agent": "global-morning-brief/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        print(f"GitHub Models summary unavailable: {exc}", file=sys.stderr)
        return None


def render_html(articles: list[Article], summary: str | None, failures: list[str]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    rows = "".join(
        f'<li><a href="{html.escape(a.url, quote=True)}">{html.escape(a.title)}</a> <small>— {html.escape(a.source)}</small></li>'
        for a in articles
    )
    summary_html = "".join(f"<p>{html.escape(line)}</p>" for line in summary.splitlines() if line.strip()) if summary else "<p><em>未配置 AI 摘要，以下为原始头条。</em></p>"
    failed_html = "" if not failures else f"<p><small>本次未能读取：{html.escape('、'.join(failures))}</small></p>"
    return f"""<!doctype html><html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;line-height:1.55;max-width:760px;margin:auto">
<h1>全球晨报 · {today}</h1>{summary_html}<h2>原始报道与来源</h2><ol>{rows}</ol>{failed_html}
<hr><p><small>仅采集 sources.json 中的白名单媒体；每条链接均回到原报道。AI 摘要仅基于所列素材生成。</small></p></body></html>"""


def render_text(articles: list[Article], summary: str | None, failures: list[str]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"全球晨报 · {today}", ""]
    if summary:
        lines.extend(summary.splitlines())
        lines.extend(["", "核验来源（可选阅读）："])
        for index, article in enumerate(articles, 1):
            lines.append(f"[{index}] {article.source}：{article.url}")
    else:
        lines.extend(["以下为权威媒体原始头条与链接。", ""])
    for index, article in enumerate(articles, 1):
        lines.extend([f"{index}. {article.title}", f"   来源：{article.source}", f"   链接：{article.url}"])
    if failures:
        lines.extend(["", f"本次未能读取：{'、'.join(failures)}"])
    lines.extend(["", "仅采集白名单媒体；请通过原文链接核验报道。"])
    return "\n".join(lines)


def send_email(subject: str, body: str, text_body: str) -> None:
    if os.getenv("MAIL_TRANSPORT", "smtp").lower() == "resend":
        send_via_resend(subject, body, text_body)
        return
    if os.getenv("MAIL_TRANSPORT", "smtp").lower() == "gmail_oauth":
        send_via_gmail_oauth(subject, body, text_body)
        return
    required = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing configuration: {', '.join(missing)}")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ["EMAIL_FROM"]
    message["To"] = os.environ["EMAIL_TO"]
    message.set_content(text_body)
    if os.getenv("MAIL_FORMAT", "plain").lower() == "html":
        message.add_alternative(body, subtype="html")
    host, port = os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30) as smtp:
        if port != 465:
            smtp.starttls()
        smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)


def send_via_resend(subject: str, body: str, text_body: str) -> None:
    """Send through Resend's HTTPS API; intended for GitHub Actions secrets."""
    key = os.getenv("RESEND_API_KEY")
    required = ["EMAIL_FROM", "EMAIL_TO"]
    missing = [name for name in required if not os.getenv(name)]
    if not key:
        missing.append("RESEND_API_KEY")
    if missing:
        raise RuntimeError(f"Missing configuration: {', '.join(missing)}")
    payload_dict = {
        "from": os.environ["EMAIL_FROM"], "to": [os.environ["EMAIL_TO"]],
        "subject": subject, "text": text_body,
    }
    if os.getenv("MAIL_FORMAT", "plain").lower() == "html":
        payload_dict["html"] = body
    payload = json.dumps(payload_dict).encode("utf-8")
    request = urllib.request.Request("https://api.resend.com/emails", data=payload, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "global-morning-brief/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"Resend accepted email: {json.loads(response.read())['id']}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Resend rejected email ({exc.code}): {exc.read().decode('utf-8', 'replace')}") from exc


def gmail_service():
    """Return an authorized Gmail API service, opening a browser only on first use."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/gmail.send"]
    credentials_file = ROOT / os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = ROOT / os.getenv("GMAIL_TOKEN_FILE", "data/gmail-token.json")
    if not credentials_file.is_file():
        raise RuntimeError(
            f"Gmail OAuth client file not found: {credentials_file}. "
            "Download a Desktop app OAuth client JSON from Google Cloud and save it here."
        )
    credentials = Credentials.from_authorized_user_file(token_file, scopes) if token_file.is_file() else None
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            credentials = InstalledAppFlow.from_client_secrets_file(credentials_file, scopes).run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(credentials.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def send_via_gmail_oauth(subject: str, body: str, text_body: str) -> None:
    required = ["EMAIL_FROM", "EMAIL_TO"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing configuration: {', '.join(missing)}")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ["EMAIL_FROM"]
    message["To"] = os.environ["EMAIL_TO"]
    message.set_content(text_body)
    if os.getenv("MAIL_FORMAT", "plain").lower() == "html":
        message.add_alternative(body, subtype="html")
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    gmail_service().users().messages().send(userId="me", body={"raw": encoded}).execute()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Generate preview.html without sending email")
    parser.add_argument("--authorize", action="store_true", help="Complete the one-time Gmail OAuth browser authorization")
    args = parser.parse_args()
    if args.authorize:
        gmail_service()
        print("Gmail OAuth authorization completed.")
        return 0
    articles, failures = fetch_articles(load_sources())
    selected = select_articles(articles, int(os.getenv("MAX_ARTICLES", "18")))
    if not selected:
        raise RuntimeError("No recent articles were collected; email was not sent.")
    summary = chinese_summary(selected)
    if os.getenv("REQUIRE_AI_SUMMARY", "false").lower() == "true" and not summary:
        raise RuntimeError("AI summary is required but unavailable; email was not sent.")
    body = render_html(selected, summary, failures)
    text_body = render_text(selected, summary, failures)
    if args.dry_run:
        output = ROOT / "preview.html"
        output.write_text(body, encoding="utf-8")
        print(f"Preview written to {output}")
    else:
        send_email(f"全球晨报 · {datetime.now():%Y-%m-%d}", body, text_body)
        print(f"Sent {len(selected)} source-linked headlines.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
