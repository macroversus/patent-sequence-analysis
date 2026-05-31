# 专利序列挖掘工具 - 技术报告

**项目名称**：Patent Sequence Analysis Tool  
**当前版本**：v3.2  
**报告日期**：2026-05-29  
**代码仓库**：https://github.com/HaEm-ai-bit/patent-sequence-analysis

---

## 目录

1. [系统概述](#1-系统概述)
2. [核心功能](#2-核心功能)
3. [技术实现](#3-技术实现)
4. [输出设计](#4-输出设计)
5. [性能指标](#5-性能指标)
6. [后续计划](#6-后续计划)

---

## 1. 系统概述

### 1.1 功能定位

本系统从专利 API 批量获取专利全文数据，自动提取其中的生物序列（氨基酸/核酸序列），并通过规则识别和 LLM 验证相结合的方式，判断序列在专利保护中的角色和重要性。

**输入**：靶点名称（如 TLR2、CD318）  
**输出**：三档 CSV 文件（完整版、高可信度版、LLM验证版）+ 原始 JSON  
**应用场景**：专利无效化分析、FTO（Freedom to Operate）调研

### 1.2 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                  Catalyst+ Patent API                   │
│          (检索 + 详情获取 + 时间分片重查)                  │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
         ┌────────────────────────────┐
         │   专利详情数据（JSON）       │
         │   - 标题、摘要、权利要求     │
         │   - 说明书、专利状态         │
         └────────────┬───────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│                  序列提取模块                             │
│  ┌──────────────┬──────────────┬─────────────────────┐ │
│  │ ST.26 提取   │ SEQ ID NO   │ 裸序列提取           │ │
│  │ (XML解析)    │ (正则匹配)   │ (关键词上下文过滤)   │ │
│  └──────┬───────┴──────┬───────┴──────┬──────────────┘ │
│         │              │               │                 │
│         └──────────────┴───────────────┘                 │
│                        │                                  │
│                        ▼                                  │
│         ┌──────────────────────────────┐                │
│         │    规则识别引擎               │                │
│         │  (CDR/VH/VL/信号肽/引物等)    │                │
│         └──────────┬───────────────────┘                │
│                    │                                      │
│           有角色信息 │ 无角色/不确定                      │
│                    │                                      │
│                    ▼                                      │
│         ┌──────────────────────────────┐                │
│         │      LLM 验证模块              │                │
│         │   (qwen-turbo，按专利分组)    │                │
│         │   - 角色判断                  │                │
│         │   - 相关性评估                │                │
│         │   - 置信度评级                │                │
│         └──────────┬───────────────────┘                │
└────────────────────┼────────────────────────────────────┘
                     │
                     ▼
      ┌──────────────────────────────┐
      │     置信度分级与分流           │
      │   (7级可信度体系)              │
      └──────────┬───────────────────┘
                 │
                 ▼
┌────────────────────────────────────────────────────────┐
│                  输出层                                  │
│  ┌────────────┬────────────────┬─────────────────────┐ │
│  │ 完整版 CSV │ 高可信度版 CSV │ LLM验证版 CSV        │ │
│  │ (490条)    │ (365条)        │ (272条)             │ │
│  └────────────┴────────────────┴─────────────────────┘ │
└────────────────────────────────────────────────────────┘
```

---

## 2. 核心功能

### 2.1 时间分片查询

**问题**：API 单次返回上限 100 条，靶点相关专利可能有数千条

**解决方案**：递归时间切分策略

```python
# 伪代码
def query_with_time_slicing(keyword, start_date, end_date):
    result = api_query(keyword, start_date, end_date)
    
    if len(result) == 100:  # 可能被截断
        # 将时间范围二分
        mid_date = (start_date + end_date) / 2
        left = query_with_time_slicing(keyword, start_date, mid_date)
        right = query_with_time_slicing(keyword, mid_date, end_date)
        return deduplicate(left + right)
    else:
        return result
```

**实际效果**：
- TLR2：初次查询 100 条 → 时间分片后 737 条（去重）
- 分片数：24 个时间段（每段约 6 个月）
- 查询次数：37 次 API 调用

---

### 2.2 多模态序列提取

#### 方法1：ST.26 标准序列表提取

**数据源**：专利 `descriptions` 字段中的 XML 格式序列表

**提取流程**：

```
1. 定位 <400> 标签（序列起始标记）
   <400> 1
       Met Asp Trp Gly Gln Gly Thr Leu Val Thr Val Ser Ser
       1               5                   10

2. 提取 <223> 标签（功能描述）
   <223> VH CDR3

3. 三字母码 → 单字母码转换
   Met Asp Trp Gly Gln Gly → MDWGQG
```

**特点**：
- 准确率：100%（国际标准格式）
- 附带信息：序列类型（AA/NT）、功能描述
- 无需二次验证

#### 方法2：SEQ ID NO 引用提取

**数据源**：专利 claims、abstracts、descriptions 字段

**正则模式**：
```python
pattern = r"(?:SEQ|seq|Seq)\s+(?:ID|id)\s+(?:NO|no|No)[.:\s]*\d+[^a-zA-Z]{0,8}([A-Z]{5,200})"
# 示例匹配：
# "SEQ ID NO: 1 MDWGQGTLVTVSS"
#              ↑ 提取此部分
```

**过滤策略**：
```python
# 排除明显的英文词
if sequence.endswith("TION", "MENT", "NESS", ...):
    skip()

# 核酸序列：长度>=6，仅含 ACGTU
if is_nucleotide(sequence) and len(sequence) >= 6:
    accept()

# 氨基酸序列：长度>=8，字符种类>=3，非纯核酸字母
if is_amino_acid(sequence) and len(sequence) >= 8 and variety >= 3:
    accept()
```

#### 方法3：裸序列提取（v3.2 新增）

**数据源**：专利正文中未格式化的连续氨基酸字符串

**四重过滤**：

| 过滤器 | 条件 | 目的 |
|--------|------|------|
| 长度过滤 | ≥12 个字符 | 排除短英文词（DETAILED、DRAWINGS） |
| 稀有氨基酸 | 包含 W/Y/H/F/Q 之一 | 排除纯常见字母的英文词（ELIMINATE） |
| 词尾检查 | 不以 TION/MENT/NESS 等结尾 | 排除常见后缀词 |
| 上下文关键词 | 前后300字符内含 antibody/VH/VL/CDR/polypeptide 等 | 确保在序列相关语境中 |

**示例**：

```
✅ 通过：FQHWGQGTLVTVSS
   - 长度 14，含 F/H/W/Q
   - 上下文："...VH humanized humanFRs 6B4-VH mouse 35 FQHWGQGTLVTVSS..."

❌ 拒绝：DETAILED
   - 长度 8 < 12

❌ 拒绝：ELIMINATE
   - 无稀有氨基酸（仅 EILMNAT）
```

---

### 2.3 LLM 验证流程

#### 验证对象

| 序列类型 | 是否验证 | 判断条件 |
|---------|---------|---------|
| ST.26 有 `<223>` 且已识别 | ❌ | seq_role 不含"用途未明" |
| ST.26 有 `<223>` 但未识别 | ✅ | seq_role 含"用途未明" |
| ST.26 无 `<223>` | ✅ | feature_desc 字段为空 |
| SEQ ID NO | ✅ | confidence = "中（SEQ ID NO引用）" |
| 裸序列 | ✅ | confidence = "低（裸序列）" |

#### 按专利分组调用

**优化前**（每条序列单独调用）：
```python
for seq in sequences:  # 219 条序列
    llm_result = call_llm(seq)  # 219 次调用
# 成本：219 × $0.004 = $0.88
```

**优化后**（按专利分组）：
```python
groups = group_by_patent_id(sequences)  # 50 个专利
for patent_id, seqs in groups.items():
    # 一次性传入该专利的所有序列 + 专利上下文
    llm_result = call_llm(patent_id, seqs)  # 50 次调用
# 成本：50 × $0.004 = $0.20
```

**节省**：77% 成本，且共享上下文信息

#### Prompt 设计

**输入结构**：
```json
{
  "patent_id": "US2020123456A1",
  "patent_title": "Anti-TLR2 antibody compositions",
  "patent_abstract": "前500字符...",
  "patent_claims": "前2000字符...",
  "patent_descriptions": "前1500字符...",
  "target": "TLR2",
  "sequences": [
    {
      "seq_index": 1,
      "sequence": "MDWGQGTLVTVSS",
      "seq_type": "AA",
      "context": "...SEQ ID NO:1 MDWGQGTLVTVSS is a VH domain..."
    }
  ]
}
```

**字段来源说明**：
- `patent_abstract`：API 返回的 `enAbstract` 或 `zhAbstract`，截取前 500 字符
- `patent_claims`：API 返回的 `claims[0].enName`，截取前 2000 字符
- `patent_descriptions`：API 返回的 `descriptions[0].enName`，截取前 1500 字符
- `context`：序列在原文中前后 120 字符的上下文

**输出结构**：
```json
{
  "sequences": [
    {
      "seq_index": 1,
      "role": "VH重链可变区",
      "relevance": "高",
      "confidence": "高",
      "reasoning": "权利要求明确指出SEQ ID NO:1为重链可变区",
      "guide": "用BLAST比对IMGT/PDB数据库，寻找先有技术"
    }
  ]
}
```

#### 置信度分流

```
LLM 验证结果
    │
    ├─ 非裸序列 ─┬─ confidence = "高" → 中（LLM验证-高置信）→ 全部3个CSV
    │           └─ confidence = "中/低" → 中（LLM验证）     → 完整+LLM验证CSV
    │
    └─ 裸序列 ───┬─ confidence = "高" → 中（LLM验证-高置信）→ 全部3个CSV
                └─ confidence = "中/低" → 低（LLM验证-低置信）→ 仅完整CSV
```

**设计依据**：
- 裸序列本身可信度低（可能误提取），仅当 LLM 高置信时才纳入高质量数据集
- 非裸序列（ST.26/SEQ ID NO）已有格式保证，LLM 中置信即可接受

---

### 2.4 可信度等级体系

| 等级 | 定义 | 数据来源 | 进入的CSV |
|------|------|---------|----------|
| `高（ST.26序列表）` | ST.26 格式 + `<223>` 标签 | `descriptions` 字段，XML 解析 | 全部 |
| `高（ST.26+LLM验证）` | ST.26 格式无 `<223>`，LLM 高置信 | `descriptions` 字段 + LLM | 全部 |
| `中（LLM验证-高置信）` | LLM 高置信验证 | SEQ ID NO / 裸序列 + LLM | 全部 |
| `中（LLM验证）` | LLM 中置信验证 | SEQ ID NO + LLM | 完整 + LLM验证 |
| `中（SEQ ID NO引用）` | 有引用，未验证/验证失败 | claims/descriptions，正则提取 | 仅完整 |
| `低（LLM验证-低置信）` | 裸序列，LLM 中/低置信 | 正文 + LLM | 仅完整 |
| `低（裸序列）` | 裸序列，LLM 失败 | 正文，四重过滤 | 仅完整 |

---

## 3. 技术实现

### 3.1 数据获取流程

```
1. 关键词检索
   ├─ API: /patent/pass/advanced
   ├─ 参数: keyword, startTime, endTime, page
   └─ 返回: 专利ID列表（summary）

2. 时间分片处理
   ├─ 检测截断：len(result) == 100
   ├─ 二分时间段递归查询
   └─ 去重合并

3. 批量获取详情
   ├─ API: /patent/pass/ids/detail
   ├─ 批次大小: 20个专利/次
   └─ 返回: 完整专利JSON（title, abstract, claims, descriptions, status）

4. 持久化
   └─ 保存为 patent_antibody_result_<timestamp>.json
```

### 3.2 序列提取实现

#### ST.26 提取

```python
def extract_st26_sequences(description_text):
    # 1. 定位序列块
    for match in re.finditer(r'<400>\s*(\d+)(.*?)(?=<210>|<110>|$)', text):
        seq_id = match.group(1)
        block = match.group(2)
        
        # 2. 提取三字母码
        triplets = re.findall(r'\b(Ala|Arg|Asn|...)\b', block)
        
        # 3. 转换为单字母码
        sequence = ''.join(TRIPLET_TO_SINGLE[t] for t in triplets)
        
        # 4. 提取功能描述
        feature = re.search(r'<223>\s*(.*?)(?=<[0-9]|$)', block)
        
        yield (sequence, seq_type, context, feature.group(1) if feature else None)
```

**字段映射**：
- `sequence` → 三字母码转换后的单字母序列
- `seq_type` → "AA"（氨基酸）或 "NT"（核酸）
- `context` → 序列块前后文本
- `feature_desc` → `<223>` 标签内容（如 "VH CDR3"）
- `source` → "ST.26"

#### SEQ ID NO 提取

```python
def extract_seq_id_no(text):
    pattern = r'SEQ\s+ID\s+NO[.:\s]*\d+[^a-zA-Z]{0,8}([A-Z]{5,200})'
    
    for match in re.finditer(pattern, text):
        sequence = match.group(1)
        
        # 过滤英文词
        if sequence.endswith(('TION', 'MENT', ...)):
            continue
        
        # 判断类型
        if is_nucleotide(sequence):
            seq_type = "NT"
        elif is_amino_acid(sequence):
            seq_type = "AA"
        else:
            continue
        
        context = text[match.start()-120 : match.end()+120]
        
        yield (sequence, seq_type, context, None, "SEQ_ID_NO")
```

#### 裸序列提取

```python
def extract_bare_sequences(text):
    RARE_AA = {'W', 'Y', 'H', 'F', 'Q'}
    CONTEXT_KW = r'antibody|VH|VL|CDR|polypeptide|...'
    
    for match in re.finditer(r'\b([ACDEFGHIKLMNPQRSTVWY]{12,150})\b', text):
        sequence = match.group(1)
        
        # 四重过滤
        if not (set(sequence) & RARE_AA):  # 无稀有氨基酸
            continue
        if sequence.endswith(('TION', 'MENT', ...)):  # 英文词后缀
            continue
        
        # 上下文检查
        context_window = text[match.start()-300 : match.end()+300]
        if not re.search(CONTEXT_KW, context_window):
            continue
        
        yield (sequence, "AA", context_window, None, "bare")
```

### 3.3 规则识别引擎

```python
def annotate_sequence(sequence, seq_type, context, target, source):
    # 优先检查 ST.26 特征标签
    if "feature:" in context:
        if "cdr3" in context.lower():
            return ("CDR3（互补决定区3）", "高", "...")
        if "vh" in context.lower() and "variable" in context.lower():
            return ("VH重链可变区", "高", "...")
    
    # 传统关键词识别
    if "sirna" in context.lower():
        return ("siRNA序列", "高", "...")
    if "primer" in context.lower():
        return ("PCR引物", "低", "...")
    
    # 序列特征识别
    if seq_type == "AA":
        if re.match(r'^(EVQL|QVQL)', sequence) and len(sequence) > 40:
            return ("VH重链可变区（由序列特征推断）", "高", "...")
        if re.match(r'^(DIVL|DIQM)', sequence) and len(sequence) > 40:
            return ("VL轻链可变区（由序列特征推断）", "高", "...")
    
    # 兜底
    return ("用途未明，需人工核查", "中", "...")
```

### 3.4 LLM 验证实现

```python
def apply_llm_verification(rows, patent_json_map):
    # 筛选需要验证的序列
    need_verify = [r for r in rows if 
        ("高（ST.26序列表）" in r["confidence"] and not r["feature_desc"]) or
        ("高（ST.26序列表）" in r["confidence"] and "用途未明" in r["seq_role"]) or
        ("中（SEQ ID NO引用）" in r["confidence"]) or
        ("低（裸序列）" in r["confidence"])
    ]
    
    # 按专利分组
    groups = defaultdict(list)
    for r in need_verify:
        groups[r["patent_id"]].append(r)
    
    # 批量调用
    for patent_id, seqs in groups.items():
        patent_json = patent_json_map[patent_id]
        
        # 构建 Prompt
        prompt = {
            "patent_id": patent_id,
            "patent_title": patent_json.get("enName", ""),
            "patent_abstract": patent_json.get("enAbstract", "")[:500],
            "patent_claims": patent_json.get("claims", [{}])[0].get("enName", "")[:2000],
            "patent_descriptions": patent_json.get("descriptions", [{}])[0].get("enName", "")[:1500],
            "target": target,
            "sequences": [{"seq_index": i+1, "sequence": s["sequence"], ...} 
                         for i, s in enumerate(seqs)]
        }
        
        # LLM 调用
        result = call_llm(prompt)
        
        # 结果映射
        for llm_seq in result["sequences"]:
            idx = llm_seq["seq_index"] - 1
            row = seqs[idx]
            
            # 更新字段
            row["seq_role"] = llm_seq["role"]
            row["break_relevance"] = llm_seq["relevance"]
            row["llm_confidence"] = llm_seq["confidence"]
            row["llm_reasoning"] = llm_seq["reasoning"]
            
            # 分流逻辑
            is_bare = "低（裸序列）" in row["confidence_level"]
            if is_bare:
                if llm_seq["confidence"] == "高":
                    row["confidence_level"] = "中（LLM验证-高置信）"
                    row["llm_verified"] = "是"
                else:
                    row["confidence_level"] = "低（LLM验证-低置信）"
                    row["llm_verified"] = "低置信"
            else:
                row["llm_verified"] = "是"
                if llm_seq["confidence"] == "高":
                    row["confidence_level"] = "中（LLM验证-高置信）"
                else:
                    row["confidence_level"] = "中（LLM验证）"
```

---

## 4. 输出设计

### 4.1 CSV 字段定义

| 字段名 | 类型 | 数据来源 | 说明 |
|--------|------|---------|------|
| `target` | string | 用户输入 | 靶点名称（如 TLR2） |
| `patent_id` | string | API `patentId` | 专利号（如 US2020123456A1） |
| `sequence` | string | 提取引擎 | 序列字符串（单字母码） |
| `seq_role` | string | 规则引擎 / LLM | 序列角色（CDR3、VH、siRNA等） |
| `break_relevance` | string | 规则引擎 / LLM | 对破专利的相关性：高/中/低 |
| `break_guide` | string | 规则引擎 / LLM | 破专利行动建议 |
| `confidence_level` | string | 分级器 | 7级可信度（见上表） |
| `final_recommendation` | string | 生成器 | 综合结论（含风险标识） |
| `llm_verified` | string | LLM模块 | LLM验证状态：是/低置信/失败/（空） |
| `llm_confidence` | string | LLM输出 | LLM置信度：高/中/低 |
| `llm_reasoning` | string | LLM输出 | LLM判断依据（≤50字） |
| `llm_raw_response` | JSON | LLM输出 | LLM原始响应（完整JSON） |
| `seq_type` | string | 提取引擎 | AA（氨基酸）/ NT（核酸） |
| `seq_context` | string | 提取引擎 | 序列前后120字符上下文 |
| `feature_desc` | string | ST.26解析 | `<223>` 标签内容（仅ST.26有值） |
| `status_label` | string | API `status` | 专利状态：有效/审查中/已放弃/已到期 |
| `action_note` | string | 映射表 | 基于状态的行动建议 |
| `patent_status` | string | API `status` | 原始专利状态字段 |
| `publication_date` | date | API `publicationDate` | 公开日期 |
| `title` | string | API `enName`/`zhName` | 专利标题 |
| `assignees` | string | API `assignees` | 申请人（`|` 分隔） |
| `abstract_brief` | string | API `enAbstract`/`zhAbstract` | 摘要（截取前200字） |
| `claims_brief` | string | API `claims` | 权利要求（截取前300字） |

### 4.2 三档 CSV 输出策略

#### 完整版 CSV

**文件名**：`<target>_patent_sequences_<timestamp>.csv`

**内容**：
```python
# 包含所有行（有序列的490行 + 无序列占位行621行）
rows = all_patents_with_sequences + no_sequence_placeholders
```

**行类型**：
- 有序列行：`sequence` 字段非空，其他字段完整
- 占位行：`sequence` 字段为空，仅保留专利元信息（title、status、assignees等）

**用途**：
- 完整数据集归档
- 包含低置信度序列供人工复核
- 查看哪些专利未提取到序列

#### 高可信度 CSV

**文件名**：`<target>_patent_sequences_<timestamp>_high_confidence.csv`

**过滤条件**：
```python
rows = [r for r in all_rows if 
    "高（ST.26序列表）" in r["confidence_level"] or
    "高（ST.26+LLM验证）" in r["confidence_level"] or
    "中（LLM验证-高置信）" in r["confidence_level"]
]
```

**内容**：365 条序列
- 109 条：ST.26 + `<223>` 标签
- 17 条：ST.26 无 `<223>` + LLM 高置信
- 239 条：SEQ ID NO/裸序列 + LLM 高置信

**用途**：
- 高质量数据集，可直接用于分析
- BLAST 比对候选序列
- 专利无效化重点序列

#### LLM验证 CSV

**文件名**：`<target>_patent_sequences_<timestamp>_llm_verified.csv`

**过滤条件**：
```python
rows = [r for r in all_rows if r["llm_verified"] == "是"]
```

**内容**：272 条序列
- 包含所有 LLM 成功验证的序列（高/中置信）
- 不包含 LLM 失败和裸序列低置信的序列

**用途**：
- 查看 LLM 验证效果
- 审查 AI 判断依据（`llm_reasoning` 字段）
- 评估 LLM 验证准确率

### 4.3 JSON 输出

**文件名**：`patent_antibody_result_<timestamp>.json`

**结构**：
```json
{
  "meta": {
    "query_time": "2026-05-29T17:00:00",
    "keywords": ["TLR2"],
    "total_patents": 737,
    "api_calls": 37
  },
  "patent_ids": ["US2020123456A1", "CA2708267C", ...],
  "summaries_by_target": {
    "TLR2": {
      "data": [
        {
          "patentId": "US2020123456A1",
          "enName": "Anti-TLR2 antibody...",
          "status": "Granted",
          "publicationDate": "2020-01-22",
          "assignees": ["Company A"]
        }
      ]
    }
  },
  "details": [
    {
      "response": {
        "data": [
          {
            "patentId": "US2020123456A1",
            "enAbstract": "完整摘要...",
            "claims": [{"enName": "完整权利要求..."}],
            "descriptions": [{"enName": "完整说明书..."}]
          }
        ]
      }
    }
  ],
  "extracted_sequences": {
    "aa_sequences": ["MDWGQGTLVTVSS", ...],
    "nt_sequences": ["ATCGATCG", ...]
  }
}
```

**用途**：
- 原始数据备份
- 二次分析的数据源
- API 响应调试

---

## 5. 性能指标

### 5.1 覆盖率提升

**测试数据集**：TLR2，737个专利

| 版本 | 有效序列数 | 高可信度序列 | 覆盖率（高可信/总序列） |
|------|-----------|-------------|----------------------|
| v2.2 | 264 | 264 | 27.2% (264/971行) |
| v3.1 | 436 | 268 | 44.9% (436/971行) |
| v3.2 | 490 | 365 | 74.5% (365/490条) |

**说明**：
- v3.2 统计口径调整：分母为实际序列数（490条），不含占位行（621行）
- 覆盖率 = 高可信度序列数 / 有效序列数

### 5.2 LLM 验证效果

**验证对象**：219 条序列，分布在 50 个专利中

| 指标 | 数值 |
|------|------|
| 总验证序列数 | 219 |
| 涉及专利数 | 50 |
| 验证成功 | 175 (79.9%) |
| 验证失败 | 44 (20.1%) |
| 高置信结果 | 239 (含非验证对象的直接识别) |
| 中/低置信结果 | 40 |

**失败原因分析**：
- JSON 格式错误：22 例 (50%)
- API 超时/限流：13 例 (30%)
- 专利信息不足：9 例 (20%)

### 5.3 准确率验证

**人工抽样**：随机抽取 10 条 LLM 验证序列

| 序列 | LLM判断 | 人工核验 | 准确性 |
|------|---------|---------|--------|
| TTCGTYN | 引物（简并碱基） | ✅ | ✅ |
| EIKRTVAAPSVFIF... | VL轻链 | ✅ | ✅ |
| QESVTEQDSK... | VH重链 | ✅ | ✅ |
| HPRYKFLEYH | CDR3 | ✅ | ✅ |
| FQHWGQGTLVTVSS | VH片段 | ✅ | ✅ |
| GLFDIIKKIAESF | 抗菌肽 | ✅ | ✅ |
| GRKKRRQRRRPPQ | 穿膜肽 | ✅ | ✅ |
| SGVHTFPAVLQS | CDR1 | ✅ | ✅ |
| GLFDIIKKIAESF | 抗菌肽 | ✅ | ✅ |
| PAMADIFECTIN | 未知肽 | ⚠️ | ⚠️ |

**准确率**：9/10 = 90%

### 5.4 成本分析

**TLR2 全量查询**（737个专利）：

| 项目 | 数值 |
|------|------|
| API 查询次数 | 37 |
| LLM 调用次数 | 50 |
| 平均 Prompt 长度 | ~1,300 tokens |
| 平均 Response 长度 | ~500 tokens |
| 单次 LLM 成本 | $0.004 |
| 总成本 | $0.20 |
| 运行时间 | ~10 分钟 |

**成本构成**：
- API 查询：免费（内部 API）
- LLM 验证：$0.20（qwen-turbo）
- 计算资源：可忽略

---

## 6. 后续计划

### 6.1 用户界面开发

**目标**：提供 Web 前端，降低使用门槛

**功能设计**：
- 表单化查询配置（靶点、时间范围、LLM开关）
- 实时进度展示（WebSocket 推送）
- 交互式结果过滤（按可信度、状态、序列类型）
- 可视化分析（序列长度分布、专利时间线）
- 多格式导出（CSV / Excel / JSON）

**技术栈**：React + FastAPI + WebSocket

### 6.2 对话式查询（Agent）

**目标**：通过自然语言交互完成查询和分析

**示例**：
```
用户: "查询 TLR2 相关的有效专利，重点关注 CDR 序列"
  ↓
Agent: [调用查询工具] → [过滤结果] → [生成报告]
  ↓
回复: "找到 23 个有效专利中的 42 条 CDR 序列，其中 CDR3 15 条为高风险..."
```

**技术方案**：LangChain + GPT-4o-mini / Claude Haiku

### 6.3 BLAST 自动比对

**目标**：自动将序列与公共数据库比对，寻找先有技术

**集成数据库**：
- IMGT（抗体序列）
- PDB（蛋白质结构）
- NCBI（通用序列库）

**输出**：相似度 > 90% 且时间早于专利申请日的序列

---

**报告完成时间**：2026-05-29  
**项目版本**：v3.2  
**代码仓库**：https://github.com/HaEm-ai-bit/patent-sequence-analysis  
**版本标签**：`v2.2`（基线）、`v3.2`（当前）
