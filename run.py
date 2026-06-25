"""
命令行入口

用法:
    python run.py build-kb --target EGFR
    python run.py query --target EGFR
    python run.py screen --sequence "MTEYKLVVLGAVGVGKSALT..." --target EGFR
    python run.py screen --mutations E484K,N501Y --target EGFR
"""

import argparse
import json
import os
import sys
from pathlib import Path as _Path

# 自动加载 .env 文件（如果存在）
_env_file = _Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from src.kb_builder import build_knowledge_base, query_protected_sites
from src.risk_screener import screen_risk
from src.utils import save_json, Path


def cmd_build_kb(args):
    """构建知识库。"""
    kb = build_knowledge_base(
        target=args.target,
        time_start=args.start,
        time_end=args.end,
        max_pages=args.max_pages,
        force_rebuild=args.force,
    )
    print(f"\n知识库构建完成:")
    print(f"  靶点: {kb.get('target')}")
    print(f"  搜索专利数: {kb.get('total_patents_searched', 0)}")
    print(f"  包含数据专利数: {kb.get('patents_with_data', 0)}")

    # 统计
    total_seqs = sum(len(p["sequences"]) for p in kb.get("patents", []))
    total_muts = sum(len(p["mutations"]) for p in kb.get("patents", []))
    protected_muts = sum(1 for p in kb.get("patents", []) for m in p.get("mutations", []) if m.get("protected"))
    print(f"  序列数: {total_seqs}")
    print(f"  突变位点数: {total_muts}")
    print(f"  受保护突变: {protected_muts}")

    if args.output:
        save_json(Path(args.output), kb)
        print(f"  已保存到: {args.output}")


def cmd_query(args):
    """查询受保护位点。"""
    result = query_protected_sites(target=args.target)

    if not result:
        print(f"未找到 {args.target} 的知识库，请先运行 build-kb")
        return

    print(f"\n=== {args.target} 受保护位点 ===")
    print(f"受保护项数: {result.get('protected_count', 0)}")
    print(f"中等风险项数: {result.get('medium_risk_count', 0)}")

    for item in result.get("protected", []):
        if item["type"] == "mutation":
            print(f"  ⛔ 突变 {item['notation']} (专利 {item['patent_id']}, {item['patent_status']}, {item['location']})")
        else:
            print(f"  ⛔ 序列 {item.get('seq_id', '?')} (专利 {item['patent_id']}, {item['location']}, {item.get('role', '')})")

    for item in result.get("medium_risk", []):
        if item["type"] == "mutation":
            print(f"  ⚠️ 突变 {item['notation']} (专利 {item['patent_id']}, {item['patent_status']}, {item['location']})")

    if args.output:
        save_json(Path(args.output), result)
        print(f"\n已保存到: {args.output}")


def cmd_screen(args):
    """风险筛查。"""
    if not args.sequence and not args.mutations:
        print("错误: 必须提供 --sequence 或 --mutations 参数")
        sys.exit(1)

    mutations = args.mutations.split(",") if args.mutations else None

    report = screen_risk(
        target=args.target,
        query_sequence=args.sequence,
        mutations=mutations,
    )

    print(f"\n=== 风险筛查报告 ===")
    print(f"靶点: {report.get('target')}")
    print(f"查询类型: {report.get('query_type')}")
    print(f"命中专利数: {len(report.get('hits', []))}")

    for hit in report.get("hits", []):
        print(f"\n  专利: {hit['patent_id']} ({hit['patent_status']}) - 总体风险: {hit['overall_risk']}")
        if "identity" in hit:
            print(f"  序列一致性: {hit['identity']:.1%}")
        for mh in hit.get("mutation_hits", []):
            icon = {"high": "⛔", "medium": "⚠️", "low": "🔶", "safe": "✅"}.get(mh["risk_level"], "?")
            print(f"    {icon} {mh['notation']}: {mh['risk_level']} - {mh['reason']}")
        for nm in hit.get("novel_mutations", []):
            print(f"    ✅ {nm['notation']}: 安全 - {nm['reason']}")

    summary = report.get("summary", {})
    print(f"\n--- 总结 ---")
    print(f"检查专利总数: {summary.get('total_patents_checked', 0)}")
    print(f"高风险突变: {summary.get('high_risk_mutations', [])}")
    print(f"中风险突变: {summary.get('medium_risk_mutations', [])}")
    print(f"低风险突变: {summary.get('low_risk_mutations', [])}")
    print(f"安全突变: {summary.get('safe_mutations', [])}")
    print(f"结论: {summary.get('conclusion', '')}")

    if args.output:
        save_json(Path(args.output), report)
        print(f"\n已保存到: {args.output}")


def main():
    parser = argparse.ArgumentParser(
        description="蛋白序列/突变位点专利规避工作流",
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # build-kb 子命令
    build_parser = subparsers.add_parser("build-kb", help="构建知识库")
    build_parser.add_argument("--target", required=True, help="蛋白关键词（如 EGFR）")
    build_parser.add_argument("--start", default="1950-01-01", help="时间范围起始")
    build_parser.add_argument("--end", default=None, help="时间范围结束")
    build_parser.add_argument("--max-pages", type=int, default=5, help="最大分页数")
    build_parser.add_argument("--force", action="store_true", help="强制重建（忽略缓存）")
    build_parser.add_argument("--output", "-o", help="输出文件路径")

    # query 子命令
    query_parser = subparsers.add_parser("query", help="查询受保护位点")
    query_parser.add_argument("--target", required=True, help="蛋白关键词")
    query_parser.add_argument("--output", "-o", help="输出文件路径")

    # screen 子命令
    screen_parser = subparsers.add_parser("screen", help="风险筛查")
    screen_parser.add_argument("--target", required=True, help="蛋白关键词")
    screen_parser.add_argument("--sequence", "-s", help="用户蛋白序列")
    screen_parser.add_argument("--mutations", "-m", help="突变位点列表（逗号分隔，如 E484K,N501Y）")
    screen_parser.add_argument("--output", "-o", help="输出文件路径")

    args = parser.parse_args()

    if args.command == "build-kb":
        cmd_build_kb(args)
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "screen":
        cmd_screen(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
