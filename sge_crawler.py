import argparse
import hashlib
import json
import os
import random
import re
import time
from typing import List, Optional
from urllib.parse import urljoin

from markdownify import markdownify as md
from playwright.sync_api import sync_playwright, Page, Browser


SOURCE = "https://www.sge.com.cn/"
LANGUAGE = "zh"
SCOPE = "commodity"
TIMEZONE = "Asia/Shanghai"
DOC_TYPE = "official_release"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
RAW_DIR = os.path.join(BASE_DIR, "crawl_result", "raw")
SHORT_TEXT_FILE = os.path.join(BASE_DIR, "crawl_result", "short_text.jsonl")
LONG_DOC_META_FILE = os.path.join(BASE_DIR, "crawl_result", "long_doc_meta.jsonl")


def get_site_dir(url: str) -> str:
    """从URL中提取站点目录名（域名部分，如 sge_com_cn）"""
    url = url.rstrip("/")
    if url.startswith("http://"):
        url = url[7:]
    elif url.startswith("https://"):
        url = url[8:]
    if url.startswith("www."):
        url = url[4:]
    # 取域名部分（到第一个 / 为止）
    domain = url.split("/")[0]
    return domain.replace(".", "_")


def compute_doc_id(url: str) -> str:
    """根据url计算doc_id"""
    url = url.rstrip("/")
    if url.endswith(".html"):
        url = url[:-5]
    if url.startswith("http://"):
        url = url[7:]
    elif url.startswith("https://"):
        url = url[8:]
    if url.startswith("www."):
        url = url[4:]
    doc_id = url.replace(".", "_").replace("/", "_")
    return doc_id


def compute_dedup_hash(text: str) -> str:
    """计算去重哈希"""
    normalized = "".join(text.split())
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_date(date_str: str) -> str:
    """规范化日期，将两位年份补全为四位年份，如 26-03-23 -> 2026-03-23"""
    date_str = date_str.strip()
    if not date_str:
        return date_str
    # 匹配以两位数字开头的日期格式，如 26-03-23、26/03/23
    m = re.match(r"^(\d{2})([-/])(\d{2})\2(\d{2})$", date_str)
    if m:
        yy, sep, mm, dd = m.group(1), m.group(2), m.group(3), m.group(4)
        # 简单判断：如果年份在合理范围(20-99)，补全为20xx
        year_int = int(yy)
        if 20 <= year_int <= 99:
            return f"20{yy}{sep}{mm}{sep}{dd}"
    return date_str


def append_result(data: dict, filepath: str):
    """追加结果到指定jsonl文件"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def download_file(page: Page, file_url: str, save_dir: str) -> Optional[str]:
    """使用Playwright浏览器上下文下载附件，返回相对路径"""
    try:
        os.makedirs(save_dir, exist_ok=True)
        filename = os.path.basename(file_url.split("?")[0])
        if not filename:
            filename = "attachment"
        filepath = os.path.join(save_dir, filename)

        # 复用浏览器会话下载，避免被反爬拦截
        response = page.context.request.get(file_url)
        if not response.ok:
            return None
        with open(filepath, "wb") as f:
            f.write(response.body())

        rel_path = os.path.relpath(filepath, BASE_DIR)
        return rel_path
    except Exception:
        return None


def extract_list_items(page: Page, list_url: str) -> List[dict]:
    """从列表页面提取所有条目的url、title、publish_date、附件地址"""
    items = []
    try:
        page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # 尝试两种可能的列表容器
        container = page.query_selector("div.notice_list_ul")
        if not container:
            container = page.query_selector("div.articleList.border_ea.mt30.mb30")
        if not container:
            return items

        lis = container.query_selector_all("ul > li")
        for li in lis:
            try:
                a_tag = li.query_selector("a")
                if not a_tag:
                    continue

                href = a_tag.get_attribute("href")
                if not href:
                    continue

                url = urljoin(list_url, href)

                spans = a_tag.query_selector_all("span")
                title = ""
                publish_date = ""
                for span in spans:
                    cls = span.get_attribute("class") or ""
                    if "txt" in cls and "fl" in cls:
                        title = span.inner_text().strip()
                    elif "fr" in cls:
                        publish_date = span.inner_text().strip()

                # 检查附件
                attachment_url = None
                sub_list = li.query_selector("div.subList")
                if sub_list:
                    media_a = sub_list.query_selector("a.media")
                    if media_a:
                        attach_href = media_a.get_attribute("href")
                        if attach_href:
                            attachment_url = urljoin(list_url, attach_href)

                items.append({
                    "url": url,
                    "title": title,
                    "publish_date": normalize_date(publish_date),
                    "attachment_url": attachment_url,
                })
            except Exception:
                continue
    except Exception:
        pass
    return items


def extract_article_content(page: Page, url: str) -> Optional[str]:
    """打开文章页面，获取div.content的HTML并转为markdown"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("div.jzk_newsCenter_meeting", timeout=30000)
        content_div = page.query_selector("div.jzk_newsCenter_meeting div.content")
        if not content_div:
            return None

        html = content_div.inner_html()

        # 将图片/链接的相对路径转为绝对路径
        base_url = urljoin(url, "/")
        html = re.sub(
            r'src=["\']([^"\']+)["\']',
            lambda m: f'src="{urljoin(base_url, m.group(1))}"',
            html,
        )
        html = re.sub(
            r'href=["\']([^"\']+)["\']',
            lambda m: f'href="{urljoin(base_url, m.group(1))}"',
            html,
        )

        markdown_text = md(
            html,
            strip=["script", "style"],
            heading_style="ATX",
        )
        return markdown_text.strip()
    except Exception:
        pass
    return None


def build_result(
    url: str,
    title: str,
    publish_date: str,
    raw_text: str,
    raw_path: str,
) -> dict:
    """构建输出数据结构"""
    doc_id = compute_doc_id(url)
    dedup_hash = compute_dedup_hash(raw_text)

    # 如果 raw_path 不为空，设置对应字段
    period_covered = ""
    needs_ocr = False
    doc_cover = ""
    if raw_path:
        # 取 publish_date 的年份部分
        year_match = re.search(r"\d{4}", publish_date)
        period_covered = year_match.group(0) if year_match else ""
        needs_ocr = False
        doc_cover = title

    return {
        "doc_id": doc_id,
        "source": SOURCE,
        "url": url,
        "language": LANGUAGE,
        "doc_type": DOC_TYPE,
        "scope": SCOPE,
        "publish_date": publish_date,
        "timezone": TIMEZONE,
        "dedup_hash": dedup_hash,
        "title": title,
        "raw_text": raw_text,
        "raw_path": raw_path,
        "period_covered": period_covered,
        "needs_ocr": needs_ocr,
        "doc_cover": doc_cover,
    }


def random_sleep() -> float:
    """随机等待4-8秒，返回实际等待秒数"""
    wait = random.uniform(4, 8)
    print(f"  [等待] {wait:.1f}s 后继续...")
    time.sleep(wait)
    return wait


def launch_browser():
    """使用本地chromium启动浏览器"""
    playwright = sync_playwright().start()
    browser = None
    for channel in ("chrome", "msedge", "chromium"):
        try:
            browser = playwright.chromium.launch(
                channel=channel if channel != "chromium" else None,
                headless=False,
            )
            break
        except Exception:
            continue
    if browser is None:
        browser = playwright.chromium.launch(headless=False)
    return playwright, browser


def build_page_url(start_url: str, page_num: int) -> str:
    """构建带分页参数的URL"""
    if "?" in start_url:
        return f"{start_url}&p={page_num}"
    return f"{start_url}?p={page_num}"


def crawl(start_url: str, category: str, max_pages: int):
    """主爬取逻辑"""
    site_dir = get_site_dir(start_url)
    attachment_save_dir = os.path.join(RAW_DIR, site_dir)

    playwright, browser = launch_browser()
    try:
        list_page = browser.new_page()
        article_page = browser.new_page()

        for current_page in range(1, max_pages + 1):
            print(f"[分页] 正在处理第 {current_page}/{max_pages} 页...")

            page_url = build_page_url(start_url, current_page) if current_page > 1 else start_url
            print(f"[分页] 准备打开列表页: {page_url}")
            items = extract_list_items(list_page, page_url)
            print(f"  [列表] 本页获取到 {len(items)} 条数据")

            for idx, item in enumerate(items, 1):
                print(f"  [条目 {idx}/{len(items)}] title={item['title']}, url={item['url']}, attachment={item.get('attachment_url') or '无'}")
                random_sleep()

                raw_path = ""
                raw_text = ""

                # 处理附件下载
                if item.get("attachment_url"):
                    print(f"  [下载附件] {item['attachment_url']}")
                    downloaded = download_file(list_page, item["attachment_url"], attachment_save_dir)
                    if downloaded:
                        raw_path = downloaded
                        print(f"  [下载成功] {raw_path}")
                    else:
                        print(f"  [下载失败] {item['attachment_url']}")

                # 如果没有附件，需要获取正文
                if not raw_path:
                    print(f"  [打开文章页] {item['url']}")
                    content = extract_article_content(article_page, item["url"])
                    if content is None:
                        print(f"  [跳过] 无法获取内容: {item['url']}")
                        continue
                    raw_text = content

                result = build_result(
                    url=item["url"],
                    title=item["title"],
                    publish_date=item["publish_date"],
                    raw_text=raw_text,
                    raw_path=raw_path,
                )
                output_file = LONG_DOC_META_FILE if raw_path else SHORT_TEXT_FILE
                append_result(result, output_file)
                print(f"  [保存成功] {result['doc_id']} -> {'long_doc_meta' if raw_path else 'short_text'}")

        list_page.close()
        article_page.close()
    finally:
        browser.close()
        playwright.stop()
    print("爬取完成！")


def main():
    parser = argparse.ArgumentParser(description="上海黄金交易所数据爬取脚本")
    parser.add_argument("--url", required=True, help="爬取页面地址")
    parser.add_argument("--category", required=True, help="所属类目")
    parser.add_argument("--pages", type=int, default=1, help="爬取数据页数")
    args = parser.parse_args()

    crawl(args.url, args.category, args.pages)


if __name__ == "__main__":
    main()
