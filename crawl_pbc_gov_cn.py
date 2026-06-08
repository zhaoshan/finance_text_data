# -*- coding: utf-8 -*-
"""
中国人民银行货币政策执行报告爬虫脚本（Playwright + Chromium）
从 https://www.pbc.gov.cn/zhengcehuobisi/125207/125227/index.html 获取各年度货币政策执行报告数据。

数据结构（按《金融文本数据爬虫返回数据结构变更》规范）：
无附件时输出到 short_text.jsonl：
{
    "doc_id": "文档ID",
    "source": "来源站点url",
    "url": "文章url",
    "language": "zh",
    "doc_type": "official_release",
    "scope": "macro",
    "publish_date": "YYYY-MM-DD",
    "timezone": "Asia/Shanghai",
    "dedup_hash": "sha256:...",
    "title": "文章标题",
    "raw_text": "文章正文"
}

有附件时输出到 long_doc_meta.jsonl：
{
    "doc_id": "文档ID",
    "source": "来源站点url",
    "url": "文章url",
    "language": "zh",
    "doc_type": "official_release",
    "scope": "macro",
    "publish_date": "YYYY-MM-DD",
    "timezone": "Asia/Shanghai",
    "dedup_hash": "sha256:...",
    "raw_path": "./raw/pbc_gov_cn/xxx.pdf",
    "period_covered": "2001",
    "needs_ocr": false,
    "doc_cover": "文章摘要内容"
}

用法：
    python -m app.scripts.crawl_pbc_gov_cn                            # 默认CDP模式连接已运行的Chrome
    python -m app.scripts.crawl_pbc_gov_cn --cdp-port 9223            # 指定CDP端口
    python -m app.scripts.crawl_pbc_gov_cn --local-chromium            # 使用本地Playwright Chromium
    python -m app.scripts.crawl_pbc_gov_cn --local-chromium --visible # 本地Chromium + 显示浏览器窗口
"""

import asyncio
import calendar
import hashlib
import json
import logging
import random
import re
import sys
import io
import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# 确保项目根目录在 sys.path 中，支持 python 直接运行脚本和 python -m 两种方式
import os
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from app.utils.html_converter import HtmlConverter

# 解决 Windows 控制台 UTF-8 编码问题（reconfigure 不创建新 wrapper，避免 GC 关闭底层 buffer）
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ==================== 常量配置 ====================

# 站点标识（用于结果文件命名，区分不同站点）
SITE_NAME = "pbc_gov_cn"

# 结果存储根目录（项目根目录下的 crawl_result）
CRAWL_RESULT_DIR = Path(__file__).parent.parent.parent / "crawl_result"

# 爬取起始页
BASE_URL = "https://www.pbc.gov.cn/zhengcehuobisi/125207/125227/index.html"

# 站点域名（用于拼接相对链接）
SITE_DOMAIN = "https://www.pbc.gov.cn"

# 固定类目名称
CATEGORY = "货币执行报告"

# 新结构固定字段
DOC_TYPE = "official_release"
SCOPE = "macro"
LANGUAGE = "zh"
TIMEZONE = "Asia/Shanghai"

# 年份筛选范围
YEAR_MIN = 2001
YEAR_MAX = 2026

# 年份正则：匹配以年份开头的标题
YEAR_TITLE_PATTERN = re.compile(r"^(\d{4})年")

# 日期正则
DATE_PATTERN = re.compile(r"20\d{2}[-\.]\d{1,2}[-\.]\d{1,2}")

# 浏览器 User-Agent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# 请求头（用于 requests 下载附件）
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 请求超时（秒）
REQUEST_TIMEOUT = 30

# 页面间隔：10~20 秒随机
DELAY_MIN = 10
DELAY_MAX = 20

# CDP 默认调试端口
CDP_DEBUG_PORT = 9222

# Chromium 启动参数（参考 deep_crawl_nested_links.py）
CHROMIUM_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--disable-features=VizDisplayCompositor',
    '--disable-dev-shm-usage',
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-software-rasterizer',
    '--disable-gpu',
    '--disable-web-security',
    '--disable-features=IsolateOrigins,site-per-process',
]

# ==================== 日志配置 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _find_chrome_path() -> str:
    """
    查找本地 Chrome 浏览器可执行文件路径

    Returns:
        Chrome 可执行文件路径，未找到返回空字符串
    """
    import platform
    import os

    system = platform.system()
    if system == "Windows":
        paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    elif system == "Darwin":
        paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    else:  # Linux
        paths = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ]

    for path in paths:
        if os.path.exists(path):
            return path
    return ""


# ==================== 工具函数 ====================

async def random_delay(min_s: float = DELAY_MIN, max_s: float = DELAY_MAX):
    """
    随机等待 min_s ~ max_s 秒，模拟人类浏览间隔

    Args:
        min_s: 最小等待秒数
        max_s: 最大等待秒数
    """
    delay = random.uniform(min_s, max_s)
    logger.info(f"  等待 {delay:.1f} 秒...")
    await asyncio.sleep(delay)


async def fetch_page(page: Page, url: str) -> Optional[BeautifulSoup]:
    """
    使用 Playwright 打开页面并返回 BeautifulSoup 对象

    Args:
        page: Playwright Page 对象
        url: 页面 URL

    Returns:
        BeautifulSoup 对象，请求失败返回 None
    """
    try:
        await page.goto(url, timeout=REQUEST_TIMEOUT * 1000, wait_until="domcontentloaded")
        html = await page.content()
        return BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.error(f"请求页面失败: {url}, 错误: {e}")
        return None


def resolve_url(href: str) -> str:
    """
    将相对链接转换为绝对链接

    Args:
        href: 可能是相对路径的链接

    Returns:
        绝对 URL
    """
    if href.startswith("http"):
        return href
    return urljoin(SITE_DOMAIN, href)


def extract_publish_date(soup: BeautifulSoup) -> Optional[str]:
    """
    从页面中提取发布日期

    优先级：meta[PubDate] > meta[createDate]

    Args:
        soup: BeautifulSoup 对象

    Returns:
        日期字符串（如 "2026-02-10"），未找到返回 None
    """
    for meta in soup.find_all("meta", attrs={"name": "PubDate"}):
        content = meta.get("content", "")
        match = DATE_PATTERN.search(content)
        if match:
            return match.group()

    for meta in soup.find_all("meta", attrs={"name": "createDate"}):
        content = meta.get("content", "")
        match = DATE_PATTERN.search(content)
        if match:
            return match.group()

    return None


def extract_article_content(soup: BeautifulSoup) -> str:
    """
    从 td.content 中提取文章正文内容

    Args:
        soup: BeautifulSoup 对象

    Returns:
        文章正文文本，未找到返回空字符串
    """
    td_content = soup.find("td", class_="content")
    if not td_content:
        return ""

    paragraphs = td_content.find_all(["p", "br"])
    if paragraphs:
        parts = []
        for p in paragraphs:
            text = p.get_text(strip=True)
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)

    text = td_content.get_text(strip=True)
    return text


def extract_attachments(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """
    从 td.content 内部的 a 标签中提取附件列表

    Args:
        soup: BeautifulSoup 对象

    Returns:
        附件列表，每项包含 name 和 url
    """
    attachments = []
    td_content = soup.find("td", class_="content")
    if not td_content:
        return attachments

    for link in td_content.find_all("a", href=True):
        href = link["href"].strip()
        name = link.get_text(strip=True)
        if href and (href.lower().endswith(".pdf") or "/attachDir/" in href or "/attach/" in href):
            attachments.append({
                "name": name,
                "url": resolve_url(href),
            })

    return attachments


def compute_doc_id(url: str) -> str:
    """
    根据文章 URL 生成 doc_id

    规则：去掉协议头 http(s)://，去掉开头的 www.，去掉末尾的 .html，
    将剩余路径中的 . 和 / 替换为 _

    Args:
        url: 文章 URL

    Returns:
        doc_id 字符串
    """
    s = re.sub(r"^https?://", "", url)
    if s.startswith("www."):
        s = s[4:]
    if s.lower().endswith(".html"):
        s = s[:-5]
    s = s.rstrip("/")
    s = s.replace(".", "_").replace("/", "_")
    return s


def compute_dedup_hash(text: str) -> str:
    """
    根据正文内容计算去重哈希

    Args:
        text: 文章正文

    Returns:
        去重哈希字符串，格式 sha256:xxxxx
    """
    normalized = "".join(text.split())  # 去空白/换行
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_publish_date(date_str: str) -> str:
    """
    规范化发布日期，确保格式为 YYYY-MM-DD

    如果只有年份和月份（如 2001-05 或 2001.05），
    则补全为当月最后一天。

    Args:
        date_str: 日期字符串

    Returns:
        规范化后的日期字符串
    """
    if not date_str:
        return ""

    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str

    if re.match(r"^\d{4}$", date_str):
        return f"{date_str}-12-31"

    m = re.match(r"^(\d{4})[-\.](\d{1,2})$", date_str)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        last_day = calendar.monthrange(year, month)[1]
        return f"{year:04d}-{month:02d}-{last_day:02d}"

    return date_str


def download_attachment(att_url: str, referer: str = "",
                        ctx: BrowserContext = None,
                        site_name: str = SITE_NAME) -> Optional[str]:
    """
    下载附件到 crawl_result/raw/{site_name}/ 目录

    通过浏览器 context 获取 cookie 携带下载，解决防盗链问题。

    Args:
        att_url: 附件 URL
        referer: 来源页面 URL，用于防盗链校验
        ctx: Playwright BrowserContext 对象，用于提取 cookie
        site_name: 站点标识，用于子目录命名

    Returns:
        下载成功返回相对路径（以 crawl_result 为主目录），失败返回 None
    """
    raw_dir = CRAWL_RESULT_DIR / "raw" / site_name
    raw_dir.mkdir(parents=True, exist_ok=True)

    filename = att_url.rsplit("/", 1)[-1]
    if not filename:
        filename = f"attachment_{int(time.time())}.pdf"

    filepath = raw_dir / filename

    dl_headers = dict(HEADERS)
    if referer:
        dl_headers["Referer"] = referer

    # 从浏览器 context 获取 cookie（同步调用，download_attachment 在同步上下文中）
    cookies = {}
    if ctx:
        # BrowserContext.cookies() 是协程，需要事件循环来获取
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果已经在异步事件循环中，创建 task
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    cookie_list = pool.submit(
                        asyncio.run, ctx.cookies()
                    ).result(timeout=10)
            else:
                cookie_list = loop.run_until_complete(ctx.cookies())
            for c in cookie_list:
                cookies[c["name"]] = c["value"]
        except Exception as e:
            logger.warning(f"  获取浏览器 cookie 失败: {e}")

    try:
        resp = requests.get(att_url, headers=dl_headers, cookies=cookies, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(resp.content)
        logger.info(f"  附件已下载: {filename} ({len(resp.content)} bytes)")
        return f"./raw/{site_name}/{filename}"
    except requests.RequestException as e:
        logger.error(f"  附件下载失败: {att_url}, 错误: {e}")
        return None


def append_result(item: Dict, site_name: str = SITE_NAME) -> Path:
    """
    将单条爬取结果追加写入对应的 JSONL 文件：
    - 无附件 → short_text.jsonl
    - 有附件 → long_doc_meta.jsonl

    Args:
        item: 单条爬取结果
        site_name: 站点标识

    Returns:
        写入的文件路径
    """
    CRAWL_RESULT_DIR.mkdir(parents=True, exist_ok=True)

    is_long = item.pop("_has_attachments", False)

    if is_long:
        filepath = CRAWL_RESULT_DIR / "long_doc_meta.jsonl"
    else:
        filepath = CRAWL_RESULT_DIR / "short_text.jsonl"

    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

    return filepath


# ==================== 核心爬取逻辑 ====================

async def crawl_year_links(page: Page) -> List[Dict]:
    """
    第一步：从起始页获取各年度链接

    Args:
        page: Playwright Page 对象

    Returns:
        年度页面信息列表，每项包含: year, url, title
    """
    logger.info(f"正在请求起始页: {BASE_URL}")
    soup = await fetch_page(page, BASE_URL)
    if not soup:
        logger.error("无法获取起始页内容")
        return []

    table = soup.find("table", class_="left_menu_table_style")
    if not table:
        logger.error("未找到 table.left_menu_table_style")
        return []

    year_links = []
    links = table.find_all("a", href=True)
    logger.info(f"左侧菜单共找到 {len(links)} 个链接")

    for link in links:
        href = link["href"].strip()
        title = link.get_text(strip=True)
        if not href or not title:
            continue

        match = YEAR_TITLE_PATTERN.match(title)
        if not match:
            continue

        year = int(match.group(1))
        if year < YEAR_MIN or year > YEAR_MAX:
            continue

        full_url = resolve_url(href)
        year_links.append({
            "year": year,
            "url": full_url,
            "title": title,
        })

    year_links.sort(key=lambda x: x["year"])

    logger.info(f"筛选出 {len(year_links)} 个年度链接: "
                f"{year_links[0]['year']}-{year_links[-1]['year']}")
    return year_links


async def crawl_article_links_from_year(page: Page, year_info: Dict) -> List[Dict]:
    """
    第二步：打开年度页面，从目标表格中获取文章链接

    Args:
        page: Playwright Page 对象
        year_info: 年度信息字典，包含 year, url, title

    Returns:
        文章信息列表，每项包含: title, article_url, year
    """
    year = year_info["year"]
    url = year_info["url"]
    logger.info(f"正在请求 {year} 年页面: {url}")

    soup = await fetch_page(page, url)
    if not soup:
        logger.warning(f"无法获取 {year} 年页面")
        return []

    target_style = "border-top:1px #4A6078 solid; margin-bottom:20px;"
    table = soup.find("table", style=target_style)
    if not table:
        logger.info(f"{year} 年页面未找到目标表格，跳过")
        return []

    links = table.find_all("a", href=True)
    if not links:
        logger.info(f"{year} 年页面目标表格为空，跳过")
        return []

    article_links = []
    for link in links:
        href = link["href"].strip()
        title = link.get_text(strip=True)
        if not href or not title:
            continue

        full_url = resolve_url(href)
        article_links.append({
            "title": title,
            "article_url": full_url,
            "year": year,
        })

    logger.info(f"{year} 年页面找到 {len(article_links)} 篇文章链接")
    return article_links


async def fetch_article_detail(page: Page, article: Dict, ctx: BrowserContext) -> Dict:
    """
    第三步：打开文章详情页，提取发布日期、正文内容和附件列表，
    并按新数据结构规范组装结果

    Args:
        page: Playwright Page 对象
        article: 文章信息字典，包含 title, article_url, year
        ctx: Playwright BrowserContext 对象，用于附件下载

    Returns:
        按新规范组装的文章数据字典
    """
    url = article["article_url"]
    title = article["title"]
    logger.info(f"正在爬取文章: {title}")

    soup = await fetch_page(page, url)
    if not soup:
        logger.warning(f"无法获取文章页面: {url}")
        publish_date = ""
        content = ""
        attachments = []
    else:
        publish_date = extract_publish_date(soup)
        if not publish_date:
            match = YEAR_TITLE_PATTERN.match(title)
            if match:
                publish_date = match.group(1)

        # 使用 HtmlConverter 将 HTML 正文转为 Markdown，保留标题、列表等格式
        td_content = soup.find("td", class_="content")
        content = HtmlConverter.html_to_markdown(td_content, base_url=url) if td_content else ""
        attachments = extract_attachments(soup)

    publish_date = normalize_publish_date(publish_date or "")
    doc_id = compute_doc_id(url)
    dedup_hash = compute_dedup_hash(content) if content else compute_dedup_hash("")
    has_attachments = bool(attachments)

    result = {
        "doc_id": doc_id,
        "source": BASE_URL,
        "url": url,
        "language": LANGUAGE,
        "doc_type": DOC_TYPE,
        "scope": SCOPE,
        "publish_date": publish_date,
        "timezone": TIMEZONE,
        "dedup_hash": dedup_hash,
    }

    if has_attachments:
        # 有附件：不输出 title 和 raw_text，输出 raw_path/period_covered/needs_ocr/doc_cover
        raw_paths = []
        for att in attachments:
            raw_path = download_attachment(att["url"], referer=url, ctx=ctx)
            if raw_path:
                raw_paths.append(raw_path)

        result["raw_path"] = ", ".join(raw_paths) if raw_paths else ""
        result["period_covered"] = publish_date[:4] if publish_date else ""
        result["needs_ocr"] = False
        result["doc_cover"] = content
        result["_has_attachments"] = True
    else:
        result["title"] = title
        result["raw_text"] = content

    logger.info(f"  日期: {publish_date}, 内容长度: {len(content)} 字, "
                f"附件: {len(attachments)} 个, 类型: {'long_doc' if has_attachments else 'short_text'}")
    return result


async def crawl_pbc_gov_cn(use_cdp: bool = True,
                            cdp_port: int = CDP_DEBUG_PORT,
                            headless: bool = True) -> tuple:
    """
    主爬取流程（Playwright + Chromium）：
    1. 启动浏览器（默认CDP连接已运行的Chrome，可切换为本地Chromium）
    2. 从起始页获取年度链接
    3. 逐个打开年度页面获取文章链接
    4. 逐个打开文章页面提取详情，每完成一条立即写入 JSONL
    5. 每个页面之间随机等待 5~10 秒

    Args:
        use_cdp: 是否使用CDP连接已运行的Chrome（默认True）
        cdp_port: CDP调试端口（默认9222）
        headless: 是否无头模式运行（仅本地Chromium模式生效）

    Returns:
        (short_count, long_count, total_attachments) 统计元组
    """
    async with async_playwright() as p:
        browser = None
        cdp_browser = None
        ctx = None

        try:
            if use_cdp:
                # 默认模式：通过CDP连接已运行的Chrome
                cdp_url = f"http://127.0.0.1:{cdp_port}"
                logger.info(f"正在通过CDP连接已运行的Chrome: {cdp_url}")
                cdp_browser = await p.chromium.connect_over_cdp(
                    endpoint_url=cdp_url,
                    timeout=30000,
                )
                contexts = cdp_browser.contexts
                if contexts:
                    ctx = contexts[0]
                    logger.info(f"已连接Chrome，复用现有context（共{len(contexts)}个）")
                else:
                    ctx = await cdp_browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        user_agent=USER_AGENT,
                    )
                    logger.info("已连接Chrome，创建新context")
            else:
                # 备选模式：使用本地Playwright Chromium
                logger.info(f"使用本地Playwright Chromium（headless={headless}）")
                browser = await p.chromium.launch(
                    headless=headless,
                    args=CHROMIUM_ARGS,
                )
                ctx = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=USER_AGENT,
                    ignore_https_errors=True,
                )

            page = await ctx.new_page()

            # 第一步：获取年度链接
            year_links = await crawl_year_links(page)
            if not year_links:
                logger.warning("未获取到任何年度链接")
                return (0, 0, 0)

            # 第二步：逐个打开年度页面，获取文章链接
            all_article_links = []
            for i, year_info in enumerate(year_links):
                article_links = await crawl_article_links_from_year(page, year_info)
                all_article_links.extend(article_links)
                if i < len(year_links) - 1:
                    await random_delay()

            if not all_article_links:
                logger.warning("未获取到任何文章链接")
                return (0, 0, 0)

            logger.info(f"共获取 {len(all_article_links)} 篇文章链接，开始逐个爬取详情...")

            # 第三步：逐个爬取并即时写入
            short_count = 0
            long_count = 0
            total_attachments = 0

            for i, article in enumerate(all_article_links):
                detail = await fetch_article_detail(page, article, ctx)

                is_long = detail.get("_has_attachments", False)
                if is_long:
                    long_count += 1
                    total_attachments += len(detail.get("raw_path", "").split(", ")) if detail.get("raw_path") else 0
                else:
                    short_count += 1

                append_result(detail)
                logger.info(f"  [{i + 1}/{len(all_article_links)}] 已写入 {'long_doc_meta' if is_long else 'short_text'}")

                if i < len(all_article_links) - 1:
                    await random_delay()

            logger.info(f"爬取完成！短文本 {short_count} 篇, 长文档 {long_count} 篇，共 {total_attachments} 个附件下载")

            return (short_count, long_count, total_attachments)

        finally:
            # 清理资源
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if cdp_browser:
                try:
                    await cdp_browser.close()
                except Exception:
                    pass


# ==================== 入口 ====================

def main():
    """脚本入口"""
    global REQUEST_TIMEOUT

    parser = argparse.ArgumentParser(description="中国人民银行货币政策执行报告爬虫（Playwright）")
    parser.add_argument(
        "--local-chromium",
        action="store_true",
        help="使用本地Playwright Chromium（默认通过CDP连接已运行的Chrome）",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=CDP_DEBUG_PORT,
        help=f"CDP调试端口（默认 {CDP_DEBUG_PORT}）",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="显示浏览器窗口（仅--local-chromium模式生效，调试用）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=REQUEST_TIMEOUT,
        help=f"请求超时秒数（默认 {REQUEST_TIMEOUT}）",
    )

    args = parser.parse_args()

    REQUEST_TIMEOUT = args.timeout
    use_cdp = not args.local_chromium
    headless = not args.visible

    result = asyncio.run(crawl_pbc_gov_cn(
        use_cdp=use_cdp,
        cdp_port=args.cdp_port,
        headless=headless,
    ))

    if result and (result[0] + result[1]) > 0:
        logger.info(f"结果已保存到 crawl_result/short_text.jsonl 和 crawl_result/long_doc_meta.jsonl")


if __name__ == "__main__":
    main()
