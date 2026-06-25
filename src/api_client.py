"""
Catalyst+ 专利 API 客户端

从 legacy/query_patent.py 抽取并重构，职责：
- API 签名认证（generate_digester / signed_post）
- 关键词搜索专利摘要（支持分页 + 时间分片突破100条限制）
- 批量获取专利详情
- 凭证从环境变量读取，不再硬编码
"""

import hashlib
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import urllib3

from .utils import chunked

# 禁用 HTTPS 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== 默认配置 ==========
BASE_URL = "https://relay.catalystplus.cn:7443"
SUMMARY_API = "/patent/pass/advanced"
DETAIL_API = "/patent/pass/ids/detail"


class CatalystClient:
    """
    Catalyst+ 专利 API 的封装客户端。

    使用方式:
        client = CatalystClient()                       # 从环境变量读取凭证
        client = CatalystClient(key="xxx", secret="xxx") # 直接传入凭证

        # 搜索专利摘要
        summaries = client.search_by_keyword("EGFR", time_start="2020-01-01", time_end="2026-01-01")

        # 获取专利详情
        details = client.get_details(["US2020123456A1", "US2020789012A1"])
    """

    def __init__(
        self,
        key: str | None = None,
        secret: str | None = None,
        base_url: str | None = None,
        timeout: int = 30,
    ):
        self.access_key = key or os.getenv("CATALYST_ACCESS_KEY", "")
        self.access_secret = secret or os.getenv("CATALYST_ACCESS_SECRET", "")
        self.base_url = base_url or os.getenv("CATALYST_BASE_URL", BASE_URL)
        self.timeout = timeout

        if not self.access_key or not self.access_secret:
            raise ValueError(
                "缺少 Catalyst+ API 凭证。请设置环境变量 CATALYST_ACCESS_KEY 和 CATALYST_ACCESS_SECRET，"
                "或在初始化时传入 key/secret 参数。"
            )

    # ========== 签名认证 ==========

    def generate_digester(self) -> str:
        """
        动态摘要规则:
        data = access_key + access_secret[:10] + 当前上海时间(yyyyMMddHHmm)
        digester = sha512(data).hexdigest()
        """
        current_minutes = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d%H%M")
        data = self.access_key + self.access_secret[:10] + current_minutes
        return hashlib.sha512(data.encode("utf-8")).hexdigest()

    def signed_post(self, api_path: str, payload: dict) -> dict:
        """
        带签名的 POST 请求，自动重试 3 次（每次重新生成 digester 避免跨分钟过期）。
        """
        last_error = None
        for attempt in range(1, 4):
            try:
                body = {
                    "accessKey": self.access_key,
                    "digester": self.generate_digester(),
                    **payload,
                }
                response = requests.post(
                    self.base_url + api_path,
                    headers={"Content-Type": "application/json"},
                    json=body,
                    verify=False,
                    timeout=self.timeout,
                )
                response.encoding = "utf-8"
                try:
                    return response.json()
                except ValueError:
                    return {"http_status": response.status_code, "raw": response.text}
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(1.2 * attempt)
        return {"error": "request_failed", "api_path": api_path, "message": last_error}

    # ========== 时间分片 ==========

    @staticmethod
    def split_time_range(
        start_date: str, end_date: str, months_per_chunk: int = 6
    ) -> list[tuple[str, str]]:
        """
        将时间范围切分为多个小区间，用于突破 API 单次查询 100 条限制。

        Args:
            start_date: 开始日期 "YYYY-MM-DD"
            end_date: 结束日期 "YYYY-MM-DD"
            months_per_chunk: 每个时间片的月数（默认6个月）

        Returns:
            List of (start, end) tuples
        """
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        chunks = []
        current = start

        while current < end:
            next_date = current + timedelta(days=months_per_chunk * 30)
            if next_date > end:
                next_date = end
            chunks.append((
                current.strftime("%Y-%m-%d"),
                next_date.strftime("%Y-%m-%d"),
            ))
            current = next_date + timedelta(days=1)

        return chunks

    # ========== 关键词搜索 ==========

    def search_by_keyword(
        self,
        keyword: str,
        time_start: str = "1950-01-01",
        time_end: str | None = None,
        max_pages: int = 5,
        page_size: int = 100,
        ipcs: list[str] | None = None,
        fulltext: bool = False,
    ) -> list[dict]:
        """
        分页拉取全量摘要，支持时间分片突破 100 条限制。

        策略：
        1. 先尝试直接分页查询
        2. 如果返回结果刚好是 100 的整数倍（可能被截断），则切分时间范围重新查询
        3. 合并去重所有结果

        Args:
            keyword: 搜索关键词（如 "EGFR"）
            time_start: 时间范围起始 "YYYY-MM-DD"
            time_end: 时间范围结束 "YYYY-MM-DD"，默认今天
            max_pages: 最大分页数
            page_size: 每页条数
            ipcs: IPC 分类号过滤列表（如 ["C12N9/18", "C07K16/00"]），None 表示不过滤
            fulltext: 是否开启全文搜索（True=在claims/descriptions里搜，覆盖更广但准确性略低）

        Returns:
            去重后的专利摘要列表 [{patentId, ...}, ...]
        """
        if time_end is None:
            time_end = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

        if fulltext:
            print(f"      [全文搜索模式] 注意：覆盖更广，但部分结果可能仅偶尔提到关键词")

        def _build_payload(page: int, t_start: str, t_end: str) -> dict:
            """构建搜索 payload，统一处理所有参数。"""
            payload: dict = {
                "keywords": [keyword],
                "timeRange": {"start": t_start, "end": t_end},
                "pageSize": page_size,
                "pageNum": page,
            }
            if ipcs:
                payload["ipcs"] = ipcs
            if fulltext:
                payload["isFulltext"] = 1
            return payload

        all_items: list[dict] = []

        # 第一轮：尝试分页查询
        for page in range(1, max_pages + 1):
            payload = _build_payload(page, time_start, time_end)
            result = self.signed_post(SUMMARY_API, payload)
            items = result.get("data") or []
            if not isinstance(items, list):
                break
            all_items.extend(items)
            print(f"      Page {page}/{max_pages} -> {len(items)} items (total {len(all_items)} items)")
            if len(items) < page_size:
                break  # 已是最后一页
            time.sleep(0.5)

        # 如果拿到了 ≥100 条数据，可能存在更多数据被 API 截断，尝试时间分片
        # 注意：API 的 pageNum 参数不起作用，所有页返回的都是第 1 页数据
        # 因此只要结果 ≥100 条，就说明可能有遗漏，需要时间分片来突破限制
        if len(all_items) >= 100 and max_pages >= 5:
            print(f"      [WARNING] Detected possible data truncation, starting time-slicing query...")

            start_dt = datetime.strptime(time_start, "%Y-%m-%d")
            end_dt = datetime.strptime(time_end, "%Y-%m-%d")
            total_days = (end_dt - start_dt).days

            # 根据总天数动态调整时间片大小
            if total_days > 3650:
                months_per_chunk = 6
            elif total_days > 1825:
                months_per_chunk = 3
            else:
                months_per_chunk = 2

            time_chunks = self.split_time_range(time_start, time_end, months_per_chunk)
            print(f"      将时间范围切分为 {len(time_chunks)} 个片段（每片约{months_per_chunk}个月）")

            # 使用字典去重（按 patentId）
            all_items_by_id = {
                item.get("patentId"): item
                for item in all_items
                if isinstance(item, dict) and item.get("patentId")
            }

            for idx, (chunk_start, chunk_end) in enumerate(time_chunks, 1):
                payload = _build_payload(1, chunk_start, chunk_end)
                result = self.signed_post(SUMMARY_API, payload)
                items = result.get("data") or []

                if isinstance(items, list):
                    new_count = 0
                    for item in items:
                        if isinstance(item, dict):
                            patent_id = item.get("patentId")
                            if patent_id and patent_id not in all_items_by_id:
                                all_items_by_id[patent_id] = item
                                new_count += 1

                    print(f"        Chunk {idx}/{len(time_chunks)} ({chunk_start}~{chunk_end}) -> {len(items)} items, new: {new_count}")

                time.sleep(0.3)

            all_items = list(all_items_by_id.values())
            print(f"      [OK] Time-slicing query completed, total {len(all_items)} unique patents")

        return all_items

    # ========== 专利详情 ==========

    def get_details(
        self,
        patent_ids: list[str],
        detail_api: str | None = None,
        batch_size: int = 20,
    ) -> dict[str, dict]:
        """
        批量获取专利详情，返回 {patent_id: detail_dict} 映射。

        Args:
            patent_ids: 专利号列表
            detail_api: 详情接口路径（默认 /patent/pass/ids/detail）
            batch_size: 每批查询的专利数量

        Returns:
            {patent_id: 专利详情dict} 映射
        """
        if detail_api is None:
            detail_api = DETAIL_API

        patent_map: dict[str, dict] = {}

        for batch in chunked(patent_ids, batch_size):
            result = self.signed_post(detail_api, {"patentIds": batch})
            data = result.get("data") if isinstance(result, dict) else None
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("patentId"):
                        patent_map[item["patentId"]] = item
            time.sleep(0.3)

        return patent_map

    # ========== 一站式：搜索 + 获取详情 ==========

    def search_and_get_details(
        self,
        keyword: str,
        time_start: str = "1950-01-01",
        time_end: str | None = None,
        max_pages: int = 5,
        ipcs: list[str] | None = None,
        fulltext: bool = False,
    ) -> tuple[list[dict], dict[str, dict]]:
        """
        一次性完成搜索 + 获取详情，返回 (摘要列表, 详情映射)。

        Args:
            keyword: 搜索关键词
            time_start: 时间范围起始
            time_end: 时间范围结束
            max_pages: 最大分页数
            ipcs: IPC 分类号过滤列表
            fulltext: 是否开启全文搜索

        Returns:
            (summaries, details_map)
            summaries: [{patentId, ...}, ...]
            details_map: {patent_id: 专利详情dict}
        """
        summaries = self.search_by_keyword(
            keyword, time_start=time_start, time_end=time_end,
            max_pages=max_pages, ipcs=ipcs, fulltext=fulltext,
        )

        # 收集所有 patentId
        patent_ids = sorted({
            item.get("patentId")
            for item in summaries
            if isinstance(item, dict) and item.get("patentId")
        })

        details_map = {}
        if patent_ids:
            print(f"      获取 {len(patent_ids)} 个专利的详情...")
            details_map = self.get_details(patent_ids)

        return summaries, details_map
