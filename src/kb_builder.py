"""
知识库构建模块 (Step 1)

组合 api_client + sequence_extractor + mutation_extractor，
构建蛋白关键词相关的专利知识库，提供缓存机制和 agent 查询接口。

职责：
- build_knowledge_base(): 根据蛋白关键词构建知识库
- query_protected_sites(): 查询某蛋白受保护的序列和位点
- 缓存机制：本地存储，相同关键词不重复查询API
- 增量更新：支持追加新数据
"""

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .api_client import CatalystClient
from .mutation_extractor import extract_mutations_from_patent
from .sequence_extractor import extract_sequences_from_patent
from .utils import (
    choose_abstract,
    choose_title,
    flatten_field,
    load_json,
    normalize_patent_status,
    save_json,
    short_text,
)


# ========== 知识库目录 ==========

DEFAULT_KB_DIR = Path(__file__).parent.parent / "knowledge_base"


# ========== 知识库构建 ==========

def build_knowledge_base(
    target: str,
    time_start: str = "1950-01-01",
    time_end: str | None = None,
    max_pages: int = 5,
    client: CatalystClient | None = None,
    kb_dir: Path | None = None,
    force_rebuild: bool = False,
    ipcs: list[str] | None = None,
    fulltext: bool = False,
) -> dict:
    """
    根据蛋白关键词构建知识库。

    流程：
    1. 检查本地缓存（相同关键词 + 未过期）
    2. 调用 API 搜索相关专利
    3. 获取专利详情
    4. 提取序列和突变位点
    5. 判断受保护状态
    6. 保存到本地知识库

    Args:
        target: 蛋白关键词（如 "EGFR"）
        time_start: 搜索时间范围起始
        time_end: 搜索时间范围结束（默认今天）
        max_pages: 最大分页数
        client: API 客户端（可选，不传则自动创建）
        kb_dir: 知识库存储目录
        force_rebuild: 是否强制重建（忽略缓存）
        ipcs: IPC 分类号过滤（如 ["C12N9/18"]），None 表示不过滤
        fulltext: 是否开启全文搜索（覆盖更广但准确性略低，默认关闭）

    Returns:
        知识库 dict，结构见 PLAN.md
    """
    if kb_dir is None:
        kb_dir = DEFAULT_KB_DIR
    kb_dir = Path(kb_dir)
    kb_dir.mkdir(parents=True, exist_ok=True)

    # 1. 检查缓存
    if not force_rebuild:
        cached = _load_cached_kb(target, kb_dir)
        if cached is not None:
            print(f"[KB] 使用缓存的知识库: {target} (构建时间: {cached.get('build_time', 'unknown')})")
            return cached

    # 2. 初始化 API 客户端
    if client is None:
        client = CatalystClient()

    if time_end is None:
        time_end = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

    print(f"[KB] 构建知识库: target={target}, time={time_start}~{time_end}, max_pages={max_pages}")

    # 3. 搜索专利
    print(f"[KB] Step 1/3: 搜索专利摘要...")
    summaries, details_map = client.search_and_get_details(
        keyword=target,
        time_start=time_start,
        time_end=time_end,
        max_pages=max_pages,
        ipcs=ipcs,
        fulltext=fulltext,
    )
    print(f"[KB] 找到 {len(summaries)} 个专利摘要, {len(details_map)} 个详情")

    # 4. 提取序列和突变
    print(f"[KB] Step 2/3: 提取序列和突变位点...")
    patents_data = []

    for summary_item in summaries:
        if not isinstance(summary_item, dict):
            continue
        patent_id = summary_item.get("patentId", "")
        detail = details_map.get(patent_id, summary_item)

        # 专利基本信息
        status_raw = detail.get("status", "")
        status = normalize_patent_status(status_raw)
        title = choose_title(detail)
        assignees = detail.get("assignees", []) or []
        pub_date = detail.get("publicationDate", "")

        # 提取序列
        seq_infos = extract_sequences_from_patent(detail)
        sequences = []
        for si in seq_infos:
            seq_dict = si.to_dict()
            seq_dict["protected"] = _is_protected(si.location, status)
            sequences.append(seq_dict)

        # 提取突变
        mut_infos = extract_mutations_from_patent(detail)
        mutations = []
        for mi in mut_infos:
            mut_dict = mi.to_dict()
            # 根据突变 location 和专利 status 判断是否受保护
            mut_dict["protected"] = _is_protected(mi.location, status)
            mutations.append(mut_dict)

        # 获取 claims 和 description 文本（用于后续查询）
        claims_text = flatten_field(detail.get("claims"))
        desc_text = flatten_field(detail.get("descriptions"))

        patent_entry = {
            "patent_id": patent_id,
            "title": title,
            "status": status,
            "status_raw": status_raw,
            "publication_date": pub_date,
            "assignees": assignees,
            "sequences": sequences,
            "mutations": mutations,
        }

        # 只有有序列或突变的专利才保留
        if sequences or mutations:
            patents_data.append(patent_entry)

    print(f"[KB] 提取完成: {len(patents_data)} 个专利包含序列/突变")

    # 5. 构建知识库
    kb = {
        "target": target,
        "build_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "query_params": {
            "time_start": time_start,
            "time_end": time_end,
            "max_pages": max_pages,
        },
        "total_patents_searched": len(summaries),
        "patents_with_data": len(patents_data),
        "patents": patents_data,
    }

    # 6. 保存缓存
    _save_kb_cache(target, kb, kb_dir)

    # 7. 打印统计
    total_seqs = sum(len(p["sequences"]) for p in patents_data)
    total_muts = sum(len(p["mutations"]) for p in patents_data)
    protected_muts = sum(
        1 for p in patents_data
        for m in p["mutations"]
        if m.get("protected")
    )
    print(f"[KB] 知识库构建完成: {total_seqs} 条序列, {total_muts} 个突变位点, {protected_muts} 个受保护突变")

    return kb


# ========== 受保护判定 ==========

def _is_protected(location: str, patent_status: str) -> bool:
    """
    根据序列/突变的 location 和专利 status 判断是否受保护。

    规则（见 PLAN.md 风险矩阵）：
    - granted + claims → protected=True
    - 其他情况 → protected=False
    """
    if patent_status == "granted" and location == "claims":
        return True
    return False


def get_risk_level(location: str, patent_status: str) -> str:
    """
    根据序列/突变的 location 和专利 status 判断风险等级。

    Returns:
        "high" / "medium" / "low" / "safe"
    """
    if patent_status in ("abandoned", "expired", "withdrawn"):
        return "safe"
    if patent_status == "granted" and location == "claims":
        return "high"
    if patent_status == "granted" and location == "description":
        return "medium"
    if patent_status == "pending" and location == "claims":
        return "medium"
    if patent_status == "pending" and location == "description":
        return "low"
    return "low"


# ========== Agent 查询接口 ==========

def query_protected_sites(
    target: str,
    kb_dir: Path | None = None,
    kb: dict | None = None,
) -> list[dict]:
    """
    查询某蛋白受保护的序列和位点（Step 1 的 agent 接口）。

    Args:
        target: 蛋白关键词
        kb_dir: 知识库目录（用于加载缓存）
        kb: 已有知识库（优先使用，不传则从缓存加载）

    Returns:
        受保护的序列和突变位点列表
    """
    if kb is None:
        kb = _load_cached_kb(target, kb_dir or DEFAULT_KB_DIR)

    if kb is None:
        print(f"[KB] 未找到 {target} 的知识库，请先调用 build_knowledge_base()")
        return []

    protected = []

    for patent in kb.get("patents", []):
        patent_id = patent.get("patent_id", "")
        patent_status = patent.get("status", "")

        # 受保护的序列
        for seq in patent.get("sequences", []):
            if seq.get("protected"):
                protected.append({
                    "type": "sequence",
                    "patent_id": patent_id,
                    "patent_status": patent_status,
                    "seq_id": seq.get("seq_id"),
                    "sequence": seq.get("sequence"),
                    "source": seq.get("source"),
                    "location": seq.get("location"),
                })

        # 受保护的突变
        for mut in patent.get("mutations", []):
            if mut.get("protected"):
                protected.append({
                    "type": "mutation",
                    "patent_id": patent_id,
                    "patent_status": patent_status,
                    "notation": mut.get("notation"),
                    "position": mut.get("position"),
                    "wild_type": mut.get("wild_type"),
                    "mutant": mut.get("mutant"),
                    "location": mut.get("location"),
                })

    # 非受保护但需关注的（medium 风险）
    medium_risk = []
    for patent in kb.get("patents", []):
        patent_id = patent.get("patent_id", "")
        patent_status = patent.get("status", "")

        for mut in patent.get("mutations", []):
            risk = get_risk_level(mut.get("location", ""), patent_status)
            if risk == "medium" and not mut.get("protected"):
                medium_risk.append({
                    "type": "mutation",
                    "patent_id": patent_id,
                    "patent_status": patent_status,
                    "notation": mut.get("notation"),
                    "position": mut.get("position"),
                    "wild_type": mut.get("wild_type"),
                    "mutant": mut.get("mutant"),
                    "location": mut.get("location"),
                    "risk_level": "medium",
                })

    return {
        "target": target,
        "protected_count": len(protected),
        "medium_risk_count": len(medium_risk),
        "protected": protected,
        "medium_risk": medium_risk,
    }


# ========== 缓存管理 ==========

def _kb_cache_path(target: str, kb_dir: Path) -> Path:
    """知识库缓存文件路径。"""
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in target)
    return kb_dir / f"{safe_name}_kb.json"


def _load_cached_kb(target: str, kb_dir: Path) -> dict | None:
    """加载缓存的知识库。"""
    cache_path = _kb_cache_path(target, kb_dir)
    return load_json(cache_path)


def _save_kb_cache(target: str, kb: dict, kb_dir: Path):
    """保存知识库到缓存。"""
    cache_path = _kb_cache_path(target, kb_dir)
    save_json(cache_path, kb)
    print(f"[KB] 知识库已缓存: {cache_path}")
