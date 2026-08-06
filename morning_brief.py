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
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlsplit, urlunsplit

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
    region: str = "global"


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def china_now() -> datetime:
    return datetime.now(CHINA_TZ)


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def clean_headline(value: str) -> str:
    value = clean_text(value)
    return re.sub(
        r"\s+-\s+(?:Reuters|Bloomberg(?:\.com)?|AP News|The Associated Press)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )


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
            title = clean_headline(entry.get("title", ""))
            url = entry.get("link", "")
            if not title or not url:
                continue
            published = entry_date(entry)
            if published and published < cutoff:
                continue
            articles.append(Article(
                source["name"],
                title,
                canonical_url(url),
                published,
                clean_headline(entry.get("summary", "")),
                source.get("region", "global"),
            ))
    return deduplicate(articles), failures


def deduplicate(articles: list[Article]) -> list[Article]:
    """Remove exact URLs and near-identical headlines, retaining source diversity."""
    unique: list[Article] = []
    urls: set[str] = set()
    title_tokens: list[set[str]] = []
    for article in sorted(articles, key=lambda a: a.published or datetime.min, reverse=True):
        normalized = article.title.lower().replace("ukrainian capital", "kyiv")
        normalized = re.sub(r"\b(strikes?|attacks?|assaults?)\b", "attack", normalized)
        normalized = re.sub(r"\b(kills?|killed|dead)\b", "kill", normalized)
        tokens = {token.rstrip("s") for token in re.findall(r"[a-z0-9]+", normalized)}
        if article.url in urls or any(len(tokens & prior) / max(1, len(tokens | prior)) >= 0.50 for prior in title_tokens):
            continue
        unique.append(article)
        urls.add(article.url)
        title_tokens.append(tokens)
    return unique


def select_articles(articles: list[Article], maximum: int) -> list[Article]:
    """Return about seven global headlines plus three China headlines."""
    low_signal_terms = (
        "restaurant", "football", "fifa", "treehouse", "gangland", "podcast",
        "travel", "businessweek daily", "newsletter", "morning briefing",
        "wednesday briefing", "live updates", "sesame street", "opinion:",
        "vacation", "holiday hotspot", "travel destination",
    )
    china_pattern = re.compile(
        r"\b(?:china|chinese|beijing|shanghai|shenzhen|hong kong|taiwan|"
        r"xi jinping|yuan|renminbi|pla)\b",
        re.IGNORECASE,
    )

    def score(article: Article) -> int:
        material = f"{article.title} {article.description}".lower()
        source_weight = {
            "Reuters": 8,
            "Bloomberg": 7,
            "Financial Times": 6,
            "The New York Times": 5,
            "Associated Press": 5,
            "BBC News": 4,
            "South China Morning Post": 4,
            "The Guardian": 3,
            "Al Jazeera": 3,
        }
        value = source_weight.get(article.source, 2)
        groups = [
            (("war", "military", "attack", "missile", "ceasefire", "nuclear", "sanction"), 7),
            (("election", "president", "government", "parliament", "minister", "court"), 6),
            (("economy", "inflation", "trade", "tariff", "market", "central bank", "interest rate"), 6),
            (("climate", "wildfire", "flood", "earthquake", "hurricane", "disaster"), 6),
            (("health", "virus", "outbreak", "bird flu", "vaccine"), 6),
            (("united nations", "nato", "eu ", "diplomatic", "peace deal", "refugee", "migrant"), 5),
        ]
        for keywords, weight in groups:
            if any(keyword in material for keyword in keywords):
                value += weight
        if re.search(r"\b(?:killed|dead|wounded|missing)\b", material):
            value += 3
        if any(term in material for term in low_signal_terms):
            value -= 7
        return value

    def take(candidates: list[Article], count: int, required_sources: tuple[str, ...] = ()) -> list[Article]:
        chosen: list[Article] = []
        per_source: Counter[str] = Counter()
        headline_candidates = [
            article for article in candidates
            if not any(term in f"{article.title} {article.description}".lower() for term in low_signal_terms)
        ]
        ranked = sorted(headline_candidates or candidates, key=lambda a: (score(a), a.published or datetime.min), reverse=True)
        for source in required_sources:
            preferred = next((article for article in ranked if article.source == source and article not in chosen), None)
            if preferred and len(chosen) < count:
                chosen.append(preferred)
                per_source[preferred.source] += 1
        for article in ranked:
            if len(chosen) == count:
                break
            if article in chosen:
                continue
            if per_source[article.source] >= 2:
                continue
            chosen.append(article)
            per_source[article.source] += 1
        return chosen

    china_target = min(3, maximum)
    china_candidates = [
        article for article in articles
        if article.region == "china" or china_pattern.search(f"{article.title} {article.description}")
    ]
    china_items = take(
        china_candidates,
        china_target,
        ("Reuters", "Bloomberg"),
    )
    china_items = [replace(article, region="china") for article in china_items]
    china_urls = {article.url for article in china_items}
    global_items = take(
        [a for a in articles if a.url not in china_urls and a.region != "china"],
        maximum - len(china_items),
        ("Reuters", "Bloomberg"),
    )
    if len(global_items) + len(china_items) < maximum:
        used_urls = {a.url for a in global_items + china_items}
        global_items.extend(take([a for a in articles if a.url not in used_urls], maximum - len(global_items) - len(china_items)))
    return global_items + china_items


def translate_to_chinese(text: str) -> str:
    """Translate short source text to Chinese without relying on a hosted LLM."""
    text = clean_text(text)
    if not text:
        return ""
    url = (
        "https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl=auto&tl=zh-CN&dt=t&q={quote(text[:3500])}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "global-morning-brief/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
            translated = "".join(part[0] for part in result[0] if part and part[0])
            if clean_text(translated):
                return clean_text(translated)
        except Exception as exc:
            if attempt == 2:
                print(f"Primary translation unavailable: {exc}", file=sys.stderr)
    fallback_url = (
        "https://api.mymemory.translated.net/get"
        f"?langpair=en%7Czh-CN&q={quote(text[:450])}"
    )
    try:
        with urllib.request.urlopen(fallback_url, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        return clean_text(result["responseData"]["translatedText"]) or text
    except Exception as exc:
        print(f"Fallback translation unavailable: {exc}", file=sys.stderr)
        return text


def why_important(article: Article) -> str:
    material = f"{article.title} {article.description}".lower()
    groups = [
        (("war", "military", "attack", "ceasefire", "nuclear", "sanction"), "这可能影响地区安全、外交关系以及全球能源和市场预期。"),
        (("election", "president", "government", "parliament", "vote", "court"), "这可能改变政策方向、国内政治格局或对外关系。"),
        (("economy", "inflation", "trade", "tariff", "market", "bank", "rate"), "这与全球经济、贸易成本和金融市场走向直接相关。"),
        (("climate", "storm", "flood", "earthquake", "fire", "disaster"), "这关系到人员安全、救灾资源和后续经济损失。"),
        (("health", "virus", "disease", "vaccine"), "这可能影响公共卫生应对和跨境风险管理。"),
    ]
    for keywords, explanation in groups:
        if any(re.search(rf"\b{re.escape(keyword)}\b", material) for keyword in keywords):
            return explanation
    return "这件事可能对相关地区的政策、经济或国际关系产生后续影响。"


def chinese_summary(articles: list[Article]) -> str:
    """Create a readable Chinese digest from authoritative RSS facts."""
    translated: list[tuple[Article, str, str]] = []
    for article in articles[:10]:
        sentences = re.split(r"(?<=[.!?])\s+", clean_text(article.description))
        facts = " ".join(sentences[:2])[:700] or article.title
        translated.append((article, translate_to_chinese(article.title), translate_to_chinese(facts)))

    lines = ["今日要点"]
    for _, title, facts in translated[:3]:
        lines.append(f"• {title}：{facts}")

    def append_section(heading: str, items: list[tuple[Article, str, str]]) -> None:
        if not items:
            return
        lines.extend(["", heading])
        for index, (article, title, facts) in enumerate(items, 1):
            lines.extend([
                f"{index}. {title}",
                facts,
                f"为什么重要：{why_important(article)}",
                f"来源：{article.source}｜{article.url}",
                "",
            ])

    append_section("全球头条", [item for item in translated if item[0].region != "china"])
    append_section("中国头条", [item for item in translated if item[0].region == "china"])
    return "\n".join(lines).strip()


def render_html(articles: list[Article], summary: str | None, failures: list[str]) -> str:
    today = china_now().strftime("%Y-%m-%d")
    if summary:
        content_html = "".join(f"<p>{html.escape(line)}</p>" for line in summary.splitlines() if line.strip())
    else:
        rows = "".join(
            f'<li><a href="{html.escape(a.url, quote=True)}">{html.escape(a.title)}</a> <small>— {html.escape(a.source)}</small></li>'
            for a in articles
        )
        content_html = f"<p><em>以下为权威媒体原始头条。</em></p><ol>{rows}</ol>"
    failed_html = "" if not failures else f"<p><small>本次未能读取：{html.escape('、'.join(failures))}</small></p>"
    return f"""<!doctype html><html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;line-height:1.55;max-width:760px;margin:auto">
<h1>全球晨报 · {today}</h1>{content_html}{failed_html}
<hr><p><small>仅采集 sources.json 中的白名单媒体；中文摘要由原报道标题与摘要自动整理，每条链接均回到原报道。</small></p></body></html>"""


def render_text(articles: list[Article], summary: str | None, failures: list[str]) -> str:
    today = china_now().strftime("%Y-%m-%d")
    lines = [f"全球晨报 · {today}", ""]
    if summary:
        lines.extend(summary.splitlines())
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
    """Return an authorized Gmail API service for local or GitHub Actions use."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/gmail.send"]
    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")
    refresh_token = os.getenv("GMAIL_REFRESH_TOKEN")
    if client_id and client_secret and refresh_token:
        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
        )
        credentials.refresh(Request())
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)

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
    selected = select_articles(articles, int(os.getenv("MAX_ARTICLES", "10")))
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
        send_email(f"全球晨报 · {china_now():%Y-%m-%d}", body, text_body)
        print(f"Sent {len(selected)} source-linked headlines.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
