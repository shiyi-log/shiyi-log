#!/usr/bin/env python3
"""Build Chinese profile statistics from GitHub's public GraphQL data."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PALETTES = {
    "light": {"bg": "#ffffff", "border": "#d1d9e0", "text": "#1f2328", "muted": "#59636e", "accent": "#0969da", "levels": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"]},
    "dark": {"bg": "#0d1117", "border": "#30363d", "text": "#e6edf3", "muted": "#8b949e", "accent": "#58a6ff", "levels": ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"]},
}
SHANGHAI = timezone(timedelta(hours=8))
USERNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
QUERY = """
query ProfileStats($login: String!, $from: DateTime!, $to: DateTime!, $after: String) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
    repositories(first: 100, after: $after, privacy: PUBLIC, ownerAffiliations: OWNER) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { isFork primaryLanguage { name color } }
    }
  }
}
"""


def graphql(token: str, variables: dict) -> dict:
    """Retry transient HTTP failures without logging credentials or response bodies."""
    payload = json.dumps({"query": QUERY, "variables": variables}).encode()
    for attempt in range(3):
        request = Request("https://api.github.com/graphql", data=payload, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "User-Agent": "shiyi-log-profile-stats", "Accept": "application/vnd.github+json",
        })
        try:
            with urlopen(request, timeout=40) as response:
                result = json.load(response)
            if result.get("errors"):
                rate_limited = any(error.get("type") == "RATE_LIMITED" for error in result["errors"])
                if rate_limited and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError("GitHub GraphQL 返回错误；未更新统计图片。")
            if not isinstance(result.get("data"), dict):
                raise RuntimeError("GitHub 响应缺少数据。")
            return result["data"]
        except HTTPError as error:
            if error.code not in (403, 429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"GitHub HTTP {error.code}；未更新统计图片。") from None
            retry_after = error.headers.get("Retry-After", "")
            delay = min(int(retry_after), 30) if retry_after.isdigit() else 5 * (attempt + 1)
            time.sleep(delay)
        except (URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError("GitHub 网络请求失败；未更新统计图片。") from None
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("GitHub 请求失败。")


def validate_calendar(calendar: dict, start: date, end: date) -> list[tuple[date, int]]:
    days = {}
    for week in calendar["weeks"]:
        for item in week["contributionDays"]:
            day = date.fromisoformat(item["date"])
            count = item["contributionCount"]
            if type(count) is not int or count < 0 or day in days:
                raise ValueError("贡献日历包含重复日期或无效数量。")
            days[day] = count
    expected = [start + timedelta(days=index) for index in range((end - start).days + 1)]
    if sorted(days) != expected:
        raise ValueError("贡献日历日期不连续或与请求窗口不一致。")
    if sum(days.values()) != calendar["totalContributions"]:
        raise ValueError("贡献日历合计与 GitHub 返回值不一致。")
    return [(day, days[day]) for day in expected]


def fetch_stats(username: str, token: str, end: date) -> dict:
    start = end - timedelta(days=364)
    variables = {"login": username, "from": f"{start.isoformat()}T00:00:00Z", "to": f"{end.isoformat()}T23:59:59Z", "after": None}
    repositories = []
    calendar = None
    total_repositories = None
    seen_cursors = set()
    while True:
        user = graphql(token, variables).get("user")
        if not user:
            raise ValueError("GitHub 用户不存在或不可读取。")
        if calendar is None:
            calendar = user["contributionsCollection"]["contributionCalendar"]
        connection = user["repositories"]
        if total_repositories is None:
            total_repositories = connection["totalCount"]
        elif connection["totalCount"] != total_repositories:
            raise ValueError("读取过程中公开仓库数量发生变化，请重试。")
        repositories.extend(connection["nodes"])
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
        if not cursor or cursor in seen_cursors:
            raise ValueError("仓库分页游标无效。")
        seen_cursors.add(cursor)
        variables["after"] = cursor
    if len(repositories) != total_repositories:
        raise ValueError("公开仓库分页数量不一致。")
    days = validate_calendar(calendar, start, end)
    languages = Counter()
    colors = {}
    for repository in repositories:
        language = repository["primaryLanguage"]
        if language:
            languages[language["name"]] += 1
            color = language.get("color") or ""
            colors[language["name"]] = color if COLOR.fullmatch(color) else "#8b949e"
    streak = longest = 0
    for _, count in days:
        streak = streak + 1 if count else 0
        longest = max(longest, streak)
    return {"username": username, "start": start, "end": end, "days": days,
            "contributions": sum(count for _, count in days), "repositories": total_repositories,
            "longest_streak": longest, "languages": dict(languages), "language_colors": colors}


def render_svg(stats: dict, theme: str, updated_at: datetime) -> str:
    """Render deterministically for a given data set, palette, and timestamp."""
    palette = PALETTES[theme]
    languages = sorted(stats["languages"].items(), key=lambda item: (-item[1], item[0]))
    language_total = sum(count for _, count in languages)
    visible = languages[:5]
    if len(languages) > 5:
        visible.append(("其他", sum(count for _, count in languages[5:])))
    title = f"{stats['username']} 的 GitHub 统计"
    description = (f"过去一年贡献 {stats['contributions']} 次，公开仓库 {stats['repositories']} 个，"
                   f"统计期内最长连续贡献 {stats['longest_streak']} 天。"
                   f"语言分布按 {language_total} 个有主要语言的公开仓库计数，包含派生仓库。")
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="535" viewBox="0 0 1000 535" role="img" aria-labelledby="title desc">',
           f'<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>',
           f'<style>text{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans CJK SC","Microsoft YaHei",sans-serif;fill:{palette["text"]}}}.muted{{fill:{palette["muted"]}}}.accent{{fill:{palette["accent"]}}}</style>',
           f'<rect x="0.5" y="0.5" width="999" height="534" rx="14" fill="{palette["bg"]}" stroke="{palette["border"]}"/>']

    def text(x, y, content, size=14, css="", weight=None, anchor=None):
        attrs = f' class="{css}"' if css else ""
        if weight:
            attrs += f' font-weight="{weight}"'
        if anchor:
            attrs += f' text-anchor="{anchor}"'
        out.append(f'<text x="{x}" y="{y}" font-size="{size}"{attrs}>{escape(str(content))}</text>')

    text(28, 36, "GitHub · 开发足迹", 23, weight=650)
    text(972, 36, stats["username"], 14, "muted", anchor="end")
    metrics = [("过去一年贡献", f"{stats['contributions']:,}", "次"), ("公开仓库", stats["repositories"], "个"), ("期内最长连续贡献", stats["longest_streak"], "天")]
    for index, (label, value, unit) in enumerate(metrics):
        x = 28 + index * 320
        out.append(f'<rect x="{x}" y="58" width="304" height="93" rx="9" fill="{palette["bg"]}" stroke="{palette["border"]}"/>')
        text(x + 17, 84, label, 13, "muted")
        text(x + 17, 130, value, 34, "accent", weight=650)
        text(x + 282, 128, unit, 13, "muted", anchor="end")

    text(28, 186, "贡献日历", 17, weight=650)
    text(972, 186, "按 GitHub 日期 · 周日为每周起点", 12, "muted", anchor="end")
    first = stats["start"] - timedelta(days=(stats["start"].weekday() + 1) % 7)
    columns = ((stats["end"] - first).days // 7) + 1
    gap, cell = 4, 12
    origin_x, origin_y = 82, 222
    pitch = cell + gap
    previous_month = None
    for day, count in stats["days"]:
        column = (day - first).days // 7
        row = (day.weekday() + 1) % 7
        if day.month != previous_month:
            text(origin_x + column * pitch, 211, f"{day.month}月", 11, "muted")
            previous_month = day.month
        level = 0 if count == 0 else 1 if count < 4 else 2 if count < 8 else 3 if count < 12 else 4
        out.append(f'<rect x="{origin_x + column * pitch}" y="{origin_y + row * pitch}" width="{cell}" height="{cell}" rx="2" fill="{palette["levels"][level]}"><title>{day.isoformat()}：{count} 次贡献</title></rect>')
    for row, label in [(1, "周一"), (3, "周三"), (5, "周五")]:
        text(60, origin_y + row * pitch + 10, label, 11, "muted", anchor="end")
    legend_x = origin_x + columns * pitch - 133
    text(legend_x - 9, 354, "少", 11, "muted", anchor="end")
    for index, color in enumerate(palette["levels"]):
        out.append(f'<rect x="{legend_x + index * 17}" y="343" width="12" height="12" rx="2" fill="{color}"/>')
    text(legend_x + 90, 354, "多", 11, "muted")

    text(28, 390, "仓库主要语言", 17, weight=650)
    text(972, 390, f"按 {language_total} 个有主要语言的公开仓库计数（含派生仓库）", 12, "muted", anchor="end")
    bar_x = 28.0
    for language, count in visible:
        color = stats["language_colors"].get(language, "#8b949e")
        width = 944 * count / language_total
        out.append(f'<rect x="{bar_x:.2f}" y="408" width="{width:.2f}" height="13" fill="{color}"><title>{escape(language)}：{count} 个仓库</title></rect>')
        bar_x += width
    if not language_total:
        text(28, 440, "暂无带主要语言的公开仓库", 13, "muted")
    for index, (language, count) in enumerate(visible):
        x = 28 + (index % 3) * 320
        y = 442 + (index // 3) * 23
        color = stats["language_colors"].get(language, "#8b949e")
        out.append(f'<circle cx="{x + 5}" cy="{y - 4}" r="4.5" fill="{color}"/>')
        text(x + 17, y, f"{language} · {count} 个（{count / language_total:.0%}）", 12, "muted")
    out.append(f'<path d="M28 484H972" stroke="{palette["border"]}"/>')
    text(28, 509, f"统计窗口：{stats['start'].isoformat()} — {stats['end'].isoformat()}", 11, "muted")
    text(972, 509, f"更新于 {updated_at.astimezone(SHANGHAI):%Y-%m-%d %H:%M}（北京时间）", 11, "muted", anchor="end")
    out.append("</svg>\n")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="shiyi-log")
    parser.add_argument("--output-dir", type=Path, default=Path("assets"))
    args = parser.parse_args()
    if not USERNAME.fullmatch(args.username):
        parser.error("无效的 GitHub 用户名。")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("请通过 GH_TOKEN 或 GITHUB_TOKEN 提供 GitHub 令牌。")
    updated_at = datetime.now(SHANGHAI)
    stats = fetch_stats(args.username, token, updated_at.date())
    images = {f"github-stats-{theme}.svg": render_svg(stats, theme, updated_at) for theme in PALETTES}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    pending = []
    try:
        for filename, content in images.items():
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=args.output_dir, prefix=".stats-", delete=False) as temporary:
                temporary.write(content)
                pending.append((Path(temporary.name), args.output_dir / filename))
        for temporary, destination in pending:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in pending:
            temporary.unlink(missing_ok=True)
    print("已更新中文 GitHub 统计图片（浅色 / 深色）。")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError) as error:
        raise SystemExit(str(error)) from None
