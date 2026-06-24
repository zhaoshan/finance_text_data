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


SOURCE = "https://www.chinania.org.cn/"
LANGUAGE = "zh"
SCOPE = "commodity"
TIMEZONE = "Asia/Shanghai"
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "crawl_result", "short_text.jsonl")


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
    if category == "行业新闻":
        return "news"
    return "official_release"


def append_result(data: dict):
    """追加结果到jsonl文件"""
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def extract_list_items(page: Page, list_url: str) -> List[dict]:
    """从列表页面提取所有条目的url、title、publish_date"""
    items = []
    try:
        page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("ul.notice_list_ul", timeout=30000)

        lis = page.query_selector_all("ul.notice_list_ul > li")
        for li in lis:
            try:
                a_tag = li.query_selector("a")
                p_tags = li.query_selector_all("p")
                if not a_tag or len(p_tags) < 2:
                    continue

                href = a_tag.get_attribute("href")
                if not href:
                    continue

                url = urljoin(list_url, href)
                title = p_tags[0].inner_text().strip()
                publish_date = p_tags[1].inner_text().strip()

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


def get_total_pages(page: Page) -> int:
    """获取总页数"""
    try:
        pages_div = page.query_selector("div.pages")
        if not pages_div:
            return 1
        links = pages_div.query_selector_all("a")
        page_numbers = []
        for link in links:
            text = link.inner_text().strip()
            if text.isdigit():
                page_numbers.append(int(text))
        if page_numbers:
            return max(page_numbers)
    except Exception:
        pass
    return 1


def goto_page(page: Page, page_num: int):
    """点击分页跳转到指定页码"""
    try:
        pages_div = page.query_selector("div.pages")
        if not pages_div:
            return False
        links = pages_div.query_selector_all("a")
        for link in links:
            text = link.inner_text().strip()
            if text == str(page_num):
                link.click()
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                return True
    except Exception:
        pass
    return False


def extract_article_content(page: Page, url: str) -> Optional[str]:
    """打开文章页面，获取article_content的HTML并转为markdown"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("div.article_content", timeout=30000)
        content_div = page.query_selector("div.article_content")
        if not content_div:
            return None

        # 获取原始HTML
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

        # 转为markdown
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
    # 优先尝试连接本地chrome，否则尝试chrome-canary/chromium等channel
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
        # 最后尝试无channel的默认chromium
        browser = playwright.chromium.launch(headless=False)
    return playwright, browser


def crawl(start_url: str, category: str, max_pages: int):
    """主爬取逻辑"""
    playwright, browser = launch_browser()
    try:
        list_page = browser.new_page()
        article_page = browser.new_page()

        list_page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        list_page.wait_for_timeout(3000)

        total_pages = get_total_pages(list_page)
        pages_to_crawl = min(max_pages, total_pages)
        print(f"总页数: {total_pages}, 计划爬取: {pages_to_crawl} 页")

        for current_page in range(1, pages_to_crawl + 1):
            print(f"[分页] 正在处理第 {current_page}/{pages_to_crawl} 页...")

            if current_page > 1:
                success = goto_page(list_page, current_page)
                if not success:
                    print(f"[分页] 无法点击跳转到第 {current_page} 页，尝试构建翻页URL")
                    # 尝试通过修改URL参数来翻页（常见模式）
                    if start_url.endswith(".html"):
                        base = start_url[:-5]
                        if base.endswith("index"):
                            next_url = f"{base[:-5]}index_{current_page}.html"
                        else:
                            next_url = f"{base}_{current_page}.html"
                    else:
                        next_url = f"{start_url}index_{current_page}.html"
                    print(f"[分页] 构建翻页URL: {next_url}")
                    list_page.goto(next_url, wait_until="domcontentloaded", timeout=60000)
                    list_page.wait_for_timeout(3000)

            items = extract_list_items(list_page, list_page.url)
            print(f"  [列表] 本页获取到 {len(items)} 条数据")

            for idx, item in enumerate(items, 1):
                print(f"  [条目 {idx}/{len(items)}] title={item['title']}, url={item['url']}")
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
                print(f"  已保存: {result['doc_id']}")

        list_page.close()
        article_page.close()
    finally:
        browser.close()
        playwright.stop()
    print("爬取完成！")


def main():
    parser = argparse.ArgumentParser(description="有色金属网数据爬取脚本")
    parser.add_argument("--url", required=True, help="爬取页面地址")
    parser.add_argument("--category", required=True, help="所属类目")
    parser.add_argument("--pages", type=int, default=1, help="爬取数据页数")
    args = parser.parse_args()

    crawl(args.url, args.category, args.pages)


if __name__ == "__main__":
    main()
