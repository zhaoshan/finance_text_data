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


SOURCE = "https://www.nea.gov.cn/"
LANGUAGE = "zh"
SCOPE = "commodity"
TIMEZONE = "Asia/Shanghai"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
OUTPUT_FILE = os.path.join(BASE_DIR, "crawl_result", "short_text.jsonl")


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


def get_doc_type(category: str) -> str:
    """根据category获取doc_type"""
    if category == "新闻发布":
        return "news"
    return "official_release"


def append_result(data: dict):
    """追加结果到jsonl文件"""
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def extract_list_items(page: Page, base_url: str) -> List[dict]:
    """从当前列表页面提取所有条目的url、title、publish_date（不导航，仅解析当前DOM）"""
    items = []
    try:
        page.wait_for_selector("ul#showData0", timeout=30000)
        page.wait_for_timeout(2000)

        lis = page.query_selector_all("ul#showData0 > li")
        for li in lis:
            try:
                a_tag = li.query_selector("a")
                if not a_tag:
                    continue

                href = a_tag.get_attribute("href")
                if not href:
                    continue

                url = urljoin(base_url, href)
                title = a_tag.inner_text().strip()

                date_span = li.query_selector("span.sj")
                publish_date = date_span.inner_text().strip() if date_span else ""

                items.append({
                    "url": url,
                    "title": title,
                    "publish_date": publish_date,
                })
            except Exception:
                continue
    except Exception:
        pass
    return items


def click_next_page(page: Page) -> bool:
    """点击下一页，返回是否成功"""
    try:
        nav = page.query_selector("div#page_navigation")
        if not nav:
            return False
        next_btn = nav.query_selector("span.nextClass.spanPagerStyle")
        if not next_btn:
            return False
        # disabled 是 CSS class，通过 class 判断是否不可用
        class_list = (next_btn.get_attribute("class") or "").split()
        if "disabled" in class_list:
            print(f"[分页] 已到最后一页")
            return False
        next_btn.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        return True
    except Exception:
        return False


def extract_article_content(page: Page, url: str) -> Optional[str]:
    """打开文章页面，获取文章内容的HTML并转为markdown。先尝试 span#detailContent，再尝试 div.article-content"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        content_elem = None
        # 先尝试 span#detailContent
        try:
            page.wait_for_selector("span#detailContent", timeout=10000)
            content_elem = page.query_selector("span#detailContent")
        except Exception:
            pass
        # 回退到 div.article-content
        if not content_elem:
            try:
                page.wait_for_selector("div.article-content", timeout=10000)
                content_elem = page.query_selector("div.article-content")
            except Exception:
                pass
        if not content_elem:
            return None

        html = content_elem.inner_html()

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
    category: str,
    raw_text: str,
) -> dict:
    """构建输出数据结构"""
    doc_id = compute_doc_id(url)
    dedup_hash = compute_dedup_hash(raw_text)
    doc_type = get_doc_type(category)

    return {
        "doc_id": doc_id,
        "source": SOURCE,
        "url": url,
        "language": LANGUAGE,
        "doc_type": doc_type,
        "scope": SCOPE,
        "publish_date": publish_date,
        "timezone": TIMEZONE,
        "dedup_hash": dedup_hash,
        "title": title,
        "raw_text": raw_text,
        "raw_path": "",
        "period_covered": "",
        "needs_ocr": False,
        "doc_cover": "",
    }


def random_sleep() -> float:
    """随机等待10-20秒，返回实际等待秒数"""
    wait = random.uniform(10, 20)
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


def load_crawled_urls() -> set:
    """从 short_text.jsonl 中加载已爬取的 url 集合"""
    crawled = set()
    if not os.path.exists(OUTPUT_FILE):
        return crawled
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("url"):
                        crawled.add(record["url"])
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return crawled


def crawl(start_url: str, category: str, max_pages: int, skip_pages: int = 0):
    """主爬取逻辑"""
    crawled_urls = load_crawled_urls()
    print(f"[去重] 已加载 {len(crawled_urls)} 条已爬取URL")

    playwright, browser = launch_browser()
    try:
        list_page = browser.new_page()
        article_page = browser.new_page()

        # 跳过指定页数：先翻到 skip_pages 后的那一页
        list_page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        list_page.wait_for_timeout(3000)
        if skip_pages > 0:
            print(f"[跳过] 跳过前 {skip_pages} 页...")
            for _ in range(skip_pages):
                if not click_next_page(list_page):
                    print(f"[跳过] 提前停止，实际跳过页数少于 {skip_pages}")
                    break

        for current_page in range(skip_pages + 1, skip_pages + max_pages + 1):
            print(f"[分页] 正在处理第 {current_page} 页...")

            if current_page > skip_pages + 1:
                success = click_next_page(list_page)
                if not success:
                    print(f"[分页] 无法点击下一页，停止翻页")
                    break

            items = extract_list_items(list_page, start_url)
            print(f"  [列表] 本页获取到 {len(items)} 条数据")

            for idx, item in enumerate(items, 1):
                print(f"  [条目 {idx}/{len(items)}] title={item['title']}, url={item['url']}")

                if item["url"] in crawled_urls:
                    print(f"  [去重跳过] URL已爬取: {item['url']}")
                    continue

                random_sleep()
                print(f"  [打开文章页] {item['url']}")
                raw_text = extract_article_content(article_page, item["url"])
                if raw_text is None:
                    print(f"  [跳过] 无法获取内容: {item['url']}")
                    continue

                result = build_result(
                    url=item["url"],
                    title=item["title"],
                    publish_date=item["publish_date"],
                    category=category,
                    raw_text=raw_text,
                )
                append_result(result)
                crawled_urls.add(item["url"])
                print(f"  [保存成功] {result['doc_id']}")

        list_page.close()
        article_page.close()
    finally:
        browser.close()
        playwright.stop()
    print("爬取完成！")


def main():
    parser = argparse.ArgumentParser(description="国家能源局数据爬取脚本")
    parser.add_argument("--url", required=True, help="爬取页面地址")
    parser.add_argument("--category", required=True, help="所属类目")
    parser.add_argument("--pages", type=int, default=1, help="爬取数据页数")
    parser.add_argument("--skip", type=int, default=0, help="跳过页数")
    args = parser.parse_args()

    crawl(args.url, args.category, args.pages, args.skip)


if __name__ == "__main__":
    main()
