"""
序列提取模块

从专利详情 JSON 中提取完整蛋白/核酸序列，并判断 location（claims / description）。

职责：
- ST.26 序列表提取（三字母码转单字母码，100%准确）
- SEQ ID NO 引用提取（从文本中匹配 SEQ ID NO: X 后面的序列）
- 裸序列提取（上下文关键词过滤降低误报）
- 判断每条序列出现在 claims 还是 description 中
- 长文本优化（分段处理避免截断遗漏）

从 legacy/query_patent.py 重构而来。
"""

import re

from Bio.Seq import Seq

from .utils import (
    AA_ALPHABET,
    AA_THREE_TO_ONE,
    NT_ALPHABET,
    convert_three_letter_to_one,
    flatten_field,
)

# ========== 正则模式 ==========

# 核酸：5'-XXXXXX-3' 或 3'-XXXXXX-5' 格式
_NT_BODY = r"[ACGTURYMKSWHBVDN\s\-]{6,200}"
NT_PRIME_PATTERN = re.compile(
    r"[35][′']\s*[-]?\s*(" + _NT_BODY + r")\s*[-]?\s*[35][′']",
    re.IGNORECASE,
)

# SEQ ID NO: n 后面跟随的序列
SEQ_ID_PATTERN = re.compile(
    r"(?:SEQ|seq|Seq)\s+(?:ID|id)\s+(?:NO|no|No)[.:\s]*\d+[^a-zA-Z]{0,8}([A-Z]{5,200})"
)

# 英文常见词后缀——捕获到此类后缀结尾的全大写单词直接跳过
_EN_SUFFIX = re.compile(
    r"(?:TION|MENT|NESS|ENCE|ANCE|IVELY|INGLY|OUSLY|IVELY|EDLY|ERLY|IALLY|ALLY|ULLY|FULLY|WARD|WARDS|WISE|SHIP|HOOD|OLOGY|MENT)$"
)

# 氨基酸：连续大写字母，仅含标准20种氨基酸字符，长度>=12
_AA_STRICT = re.compile(r"\b([ACDEFGHIKLMNPQRSTVWY]{12,150})\b")

# 裸AA提取：上下文必须含序列相关关键词才提取（降低误报）
_BARE_AA_CTX_KW = re.compile(
    r"polypeptide|peptide|amino.acid|protein.sequen|融合蛋白|多肽|氨基酸序列|"
    r"enzyme|variant|mutant|cutinase|lipase|protease|amylase|cellulase|"
    r"酶|变体|突变体|序列",
    re.IGNORECASE,
)

# ST.26 / ST.25 序列表提取正则（支持两种格式）
# 格式1: 老版 ST.25 <400> 标签
# 同时捕获 <212> 类型字段（PRT/DNA/RNA）和 <400> 序列内容
ST25_SEQ_PATTERN = re.compile(
    r'<210>\s*(\d+).*?(?:<212>\s*([A-Z]+).*?)?<400>\s*\d+(.*?)(?=<210>|<110>|$)',
    re.DOTALL,
)

# 格式2: 新版 ST.26 <INSDSeq_sequence> 标签
ST26_INSD_PATTERN = re.compile(
    r'<SequenceData\s+sequenceIDNumber="(\d+)".*?'
    r'(?:<INSDSeq_moltype>([^<]*)</INSDSeq_moltype>)?.*?'
    r'<INSDSeq_sequence>([^<]+)</INSDSeq_sequence>',
    re.DOTALL,
)

# 格式2 补充：单独的 <INSDSeq_sequence> 标签（无 SequenceData 父标签）
ST26_SEQ_ONLY_PATTERN = re.compile(
    r'<INSDSeq_sequence>([ACDEFGHIKLMNPQRSTVWYACGTU\s]+?)</INSDSeq_sequence>',
    re.IGNORECASE | re.DOTALL,
)

# ST.26 特征描述标签
ST26_FEATURE_PATTERN = re.compile(
    r'<223>\s*(.*?)(?=<[0-9]|$)',
    re.DOTALL,
)

# 稀有氨基酸字母（在英文词中罕见，出现则更可能是真实序列）


# ========== 核酸序列翻译 ==========

def translate_nt_to_aa(nt_seq: str) -> str | None:
    """
    将核苷酸序列（DNA/RNA）翻译为氨基酸序列。

    策略：
    - 尝试三个读码框（+1, +2, +3），取最长的翻译结果
    - 遇到终止密码子截断
    - 翻译结果长度 < 10 aa 则认为无效

    Args:
        nt_seq: 核苷酸序列（仅含 ACGTU）

    Returns:
        氨基酸序列字符串，翻译失败返回 None
    """
    nt_seq = nt_seq.upper().replace('U', 'T')  # RNA → DNA
    best_aa = ""

    for frame in range(3):
        try:
            seq = Seq(nt_seq[frame:])
            # 翻译到第一个终止密码子
            aa = str(seq.translate(to_stop=True))
            if len(aa) > len(best_aa):
                best_aa = aa
        except Exception:
            continue

    return best_aa if len(best_aa) >= 10 else None
_RARE_AA = set("WYHFQ")


# ========== 提取结果数据结构 ==========

class SequenceInfo:
    """
    提取的一条序列信息。

    Attributes:
        sequence: 序列字符串
        seq_type: "AA" 或 "NT"
        source: 提取来源 "ST.26" / "SEQ_ID_NO" / "bare"
        location: 出现位置 "claims" / "description" / "unknown"
        seq_id: SEQ ID NO 编号（如 "1"），仅 ST.26 和 SEQ_ID_NO 来源有值
        context: 原文上下文片段
        feature_desc: ST.26 的 <223> 标签描述（仅 ST.26 来源有值）
    """

    def __init__(
        self,
        sequence: str,
        seq_type: str,
        source: str,
        location: str = "unknown",
        seq_id: str | None = None,
        context: str = "",
        feature_desc: str | None = None,
    ):
        self.sequence = sequence
        self.seq_type = seq_type
        self.source = source
        self.location = location
        self.seq_id = seq_id
        self.context = context
        self.feature_desc = feature_desc

    def to_dict(self) -> dict:
        return {
            "seq_id": self.seq_id,
            "sequence": self.sequence,
            "seq_type": self.seq_type,
            "length": len(self.sequence),
            "source": self.source,
            "location": self.location,
            "context": self.context[:300] if self.context else "",
            "feature_desc": self.feature_desc,
        }

    def __repr__(self):
        preview = self.sequence[:30] + ("..." if len(self.sequence) > 30 else "")
        return f"SequenceInfo({self.seq_type}/{self.source}/{self.location}: {preview})"


# ========== ST.26 序列表提取 ==========

def extract_st26_feature_description(text: str, seq_id: str) -> str | None:
    """
    从 ST.26 格式中提取序列的特征描述（<223> 标签）。
    用于更准确地识别序列角色。
    """
    seq_block_pattern = re.compile(
        rf'<210>\s*{seq_id}(.*?)(?=<210>|$)',
        re.DOTALL,
    )
    match = seq_block_pattern.search(text)
    if not match:
        return None

    seq_block = match.group(1)
    feature_match = ST26_FEATURE_PATTERN.search(seq_block)
    if feature_match:
        description = feature_match.group(1).strip()
        description = re.sub(r'\s+', ' ', description)
        description = re.sub(r'<[^>]+>', '', description)
        return description[:200]

    return None


def extract_st26_sequences(text: str) -> list[SequenceInfo]:
    """
    从序列表中提取序列，支持两种格式：
    - ST.25 格式: <400> N ... <210> 标签 (旧版)
    - ST.26 格式: <SequenceData sequenceIDNumber="N"><INSDSeq_sequence>... (新版 XML)

    Returns:
        List of SequenceInfo objects
    """
    results = []
    seen_ids: set[str] = set()

    # ---------- 格式1: 新版 ST.26 XML（<SequenceData> + <INSDSeq_sequence>）----------
    for m in ST26_INSD_PATTERN.finditer(text):
        seq_id = m.group(1)
        moltype = (m.group(2) or "").strip().upper()   # DNA / RNA / AA，可能为空
        raw_seq = re.sub(r'\s+', '', m.group(3)).upper()

        if not raw_seq or len(raw_seq) < 5:
            continue

        # 优先用 XML 里的 moltype 字段判断类型，其次靠字符集推断
        if moltype in ("AA", "PRT"):
            seq_type = "AA"
        elif moltype in ("DNA", "RNA"):
            seq_type = "NT"
        else:
            # 字符集推断：氨基酸特有字母（DEFHIKLMNPQRSVWY）出现则为 AA
            is_nt = not bool(set(raw_seq) & set("DEFHIKLMNPQRSVWY"))
            seq_type = "NT" if is_nt else "AA"

        key = f"{seq_id}_{raw_seq[:20]}"
        if key in seen_ids:
            continue
        seen_ids.add(key)

        # 核酸序列：尝试翻译为氨基酸
        aa_seq = None
        if seq_type == "NT":
            aa_seq = translate_nt_to_aa(raw_seq)

        context = f"SEQ ID NO: {seq_id} (ST.26 XML) | moltype={moltype or '未知'} | {raw_seq[:50]}"

        # 保存原始核酸序列
        results.append(SequenceInfo(
            sequence=raw_seq,
            seq_type="NT",
            source="ST.26",
            seq_id=seq_id,
            context=context,
        )) if seq_type == "NT" else results.append(SequenceInfo(
            sequence=raw_seq,
            seq_type="AA",
            source="ST.26",
            seq_id=seq_id,
            context=context,
        ))

        # 如果翻译成功，额外保存一条氨基酸序列
        if aa_seq:
            results.append(SequenceInfo(
                sequence=aa_seq,
                seq_type="AA",
                source="ST.26_translated",
                seq_id=f"{seq_id}_translated",
                context=f"SEQ ID NO: {seq_id} (ST.26 XML 翻译) | NT长度={len(raw_seq)} | AA长度={len(aa_seq)} | {aa_seq[:50]}",
            ))

    # ---------- 格式2: 老版 ST.25（<400> 标签）----------
    seq_listing_match = re.search(r'[Ss]equence\s+[Ll]isting', text)
    if seq_listing_match:
        seq_section = text[seq_listing_match.start():]
        matches = ST25_SEQ_PATTERN.findall(seq_section)

        for seq_id, moltype_raw, seq_text in matches:
            moltype = moltype_raw.strip().upper()  # PRT / DNA / RNA / ""

            # 先判断类型，再决定如何处理序列内容
            if moltype == "PRT":
                seq_type = "AA"
            elif moltype in ("DNA", "RNA"):
                seq_type = "NT"
            else:
                # 没有 moltype，先尝试三字母转换，转出来的判断
                seq_type = None

            if seq_type == "NT":
                # DNA/RNA：直接清理空白取纯序列
                one_letter = re.sub(r'\s+', '', seq_text).upper()
            else:
                # 蛋白质或未知：尝试三字母码转单字母
                one_letter = convert_three_letter_to_one(seq_text)
                if not one_letter:
                    # 三字母转换失败，可能本身就是单字母裸序列
                    one_letter = re.sub(r'\s+', '', seq_text).upper()
                if seq_type is None:
                    seq_type = "AA" if set(one_letter) <= AA_ALPHABET else "NT"

            feature_desc = extract_st26_feature_description(seq_section, seq_id)
            context_parts = [f"SEQ ID NO: {seq_id} (ST.25)"]
            if moltype:
                context_parts.append(f"moltype={moltype}")
            if feature_desc:
                context_parts.append(f"Feature: {feature_desc}")
            context_parts.append(f"Seq: {one_letter[:50]}")

            results.append(SequenceInfo(
                sequence=one_letter,
                seq_type=seq_type,
                source="ST.25",
                seq_id=seq_id,
                context=" | ".join(context_parts),
                feature_desc=feature_desc,
            ))

            # 核酸序列：尝试翻译为氨基酸
            if seq_type == "NT":
                aa_seq = translate_nt_to_aa(one_letter)
                if aa_seq:
                    results.append(SequenceInfo(
                        sequence=aa_seq,
                        seq_type="AA",
                        source="ST.25_translated",
                        seq_id=f"{seq_id}_translated",
                        context=f"SEQ ID NO: {seq_id} (ST.25 翻译) | NT长度={len(one_letter)} | AA长度={len(aa_seq)} | {aa_seq[:50]}",
                    ))

    return results


# ========== 文本序列提取 ==========

def _extract_context(text: str, start: int, end: int, span: int = 120) -> str:
    """提取序列前后 span 字符的上下文。"""
    snippet = text[max(0, start - span): min(len(text), end + span)]
    return re.sub(r"\s+", " ", snippet).strip()


def extract_sequences_from_text(text: str) -> list[tuple[str, str, str]]:
    """
    从专利文本中提取序列（非 ST.26 格式）。

    Returns:
        List of (sequence, seq_type, context_snippet)
    """
    text = text or ""
    if len(text) > 40000:
        # 长文本优化：保留前 24000 字符和后 16000 字符
        text = text[:24000] + "\n" + text[-16000:]

    results: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    # 1. 5'/3' 核酸序列
    for m in NT_PRIME_PATTERN.finditer(text):
        normalized = re.sub(r"[\s\-]", "", m.group(1)).upper()
        if len(normalized) >= 6 and normalized not in seen:
            seen.add(normalized)
            results.append((normalized, "NT", _extract_context(text, m.start(), m.end())))

    # 2. SEQ ID NO 后的序列（优先级最高）
    for m in SEQ_ID_PATTERN.finditer(text):
        normalized = m.group(1)
        chars = set(normalized)
        if normalized in seen:
            continue
        if _EN_SUFFIX.search(normalized):
            continue
        if chars.issubset(NT_ALPHABET) and len(normalized) >= 6:
            seen.add(normalized)
            results.append((normalized, "NT", _extract_context(text, m.start(), m.end())))
        elif chars.issubset(AA_ALPHABET) and len(normalized) >= 8 and len(chars) >= 3:
            if not chars.issubset(NT_ALPHABET):
                seen.add(normalized)
                results.append((normalized, "AA", _extract_context(text, m.start(), m.end())))

    # 3. 裸AA片段（上下文关键词过滤）
    for m in _AA_STRICT.finditer(text):
        normalized = m.group(1)
        if normalized in seen:
            continue
        if _EN_SUFFIX.search(normalized):
            continue
        chars = set(normalized)
        if chars.issubset(NT_ALPHABET):
            continue
        if len(chars) < 4:
            continue
        if not (chars & _RARE_AA):
            continue
        # 检查上下文
        ctx_start = max(0, m.start() - 300)
        ctx_end = min(len(text), m.end() + 300)
        context_window = text[ctx_start:ctx_end]
        if not _BARE_AA_CTX_KW.search(context_window):
            continue
        seen.add(normalized)
        results.append((normalized, "AA", _extract_context(text, m.start(), m.end())))

    return results


# ========== 判断序列出现的 location ==========

def determine_sequence_location(sequence: str, claims_text: str, desc_text: str) -> str:
    """
    判断序列出现在 claims 还是 description 中。

    优先级：claims > description > unknown

    Args:
        sequence: 序列字符串
        claims_text: 权利要求文本
        desc_text: 说明书文本

    Returns:
        "claims" / "description" / "unknown"
    """
    # 短序列在长文本中可能多次出现，优先判定为 claims
    if sequence in claims_text:
        return "claims"
    if sequence in desc_text:
        return "description"
    return "unknown"


# ========== 主入口：从专利记录提取所有序列 ==========

def extract_sequences_from_patent(record: dict) -> list[SequenceInfo]:
    """
    从单个专利详情中提取所有序列，并判断每条序列的 location。

    提取顺序：
    1. ST.26 序列表（优先级最高，100%准确）
    2. claims 中的文本序列
    3. 摘要中的文本序列
    4. descriptions 中的文本序列

    每条序列都会标注 location（claims / description / unknown）。

    Args:
        record: 专利详情 dict（从 API 获取的完整记录）

    Returns:
        List of SequenceInfo objects
    """
    if not isinstance(record, dict):
        return []

    seen_sequences: set[str] = set()
    results: list[SequenceInfo] = []

    # 获取各部分文本
    claims_text = flatten_field(record.get("claims"))
    desc_text = flatten_field(record.get("descriptions"))
    en_abstract = record.get("enAbstract") or ""
    zh_abstract = record.get("zhAbstract") or ""

    # 1. ST.26 序列表提取（主要在 descriptions，也扫 claims 以防万一）
    for seq_info in extract_st26_sequences(desc_text + "\n" + claims_text):
        if seq_info.sequence not in seen_sequences:
            seen_sequences.add(seq_info.sequence)
            # ST.26 序列表也需要判断 location
            seq_info.location = determine_sequence_location(
                seq_info.sequence, claims_text, desc_text
            )
            results.append(seq_info)

    # 2. 从 claims 中提取
    for seq, stype, ctx in extract_sequences_from_text(claims_text):
        if seq not in seen_sequences:
            seen_sequences.add(seq)
            source = "SEQ_ID_NO" if "SEQ ID NO" in ctx else "bare"
            results.append(SequenceInfo(
                sequence=seq,
                seq_type=stype,
                source=source,
                location="claims",
                context=ctx,
            ))

    # 3. 从摘要中提取
    for text in [en_abstract, zh_abstract]:
        for seq, stype, ctx in extract_sequences_from_text(text):
            if seq not in seen_sequences:
                seen_sequences.add(seq)
                source = "SEQ_ID_NO" if "SEQ ID NO" in ctx else "bare"
                location = determine_sequence_location(seq, claims_text, desc_text)
                results.append(SequenceInfo(
                    sequence=seq,
                    seq_type=stype,
                    source=source,
                    location=location,
                    context=ctx,
                ))

    # 4. 从 descriptions 中提取
    for seq, stype, ctx in extract_sequences_from_text(desc_text):
        if seq not in seen_sequences:
            seen_sequences.add(seq)
            source = "SEQ_ID_NO" if "SEQ ID NO" in ctx else "bare"
            location = determine_sequence_location(seq, claims_text, desc_text)
            results.append(SequenceInfo(
                sequence=seq,
                seq_type=stype,
                source=source,
                location=location,
                context=ctx,
            ))

    return results
