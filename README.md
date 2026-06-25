# 蛋白序列/突变位点 专利规避工作流

根据蛋白关键词匹配相关专利，提取专利中蛋白序列的突变位点，区分受保护与不受保护，提供风险筛查与规避建议。

---

## 核心功能

### Step 1：知识库构建

根据蛋白关键词（如 EGFR、TLR2）搜索相关专利，提取两类信息并构建本地知识库：

- **完整蛋白序列**：ST.26 / ST.25 序列表 / SEQ ID NO 引用 / 裸序列提取
- **突变位点**：从专利文本中提取突变描述（E484K、Glu484Lys 等多种格式）

序列提取支持两种序列表格式，逻辑完全统一：

| | ST.26（2022年后新专利） | ST.25（2022年前旧专利） |
|---|---|---|
| 类型判断 | 读 `<INSDSeq_moltype>` 字段 | 读 `<212>` 标签 |
| 无类型字段时 | 字符集推断 | 字符集推断 |
| DNA / RNA | 保存原序列 + 自动翻译为氨基酸 | 保存原序列 + 自动翻译为氨基酸 |
| 蛋白质 | 直接保存单字母序列 | 三字母码转单字母后保存 |

- 核酸序列自动翻译：尝试三个读码框（+1/+2/+3），取最长结果，翻译结果 ≥10aa 才保留
- 同时保留原始核酸序列（`source=ST.26`）和翻译后氨基酸序列（`source=ST.26_translated`）

每条序列和突变都标注了：
- `location`：出现在 claims 还是 description 中
- `protected`：是否受专利保护（granted + claims = 受保护）
- 风险等级：high / medium / low / safe

知识库自动缓存到本地，相同关键词不重复查询 API。

### Step 2：风险筛查

两种输入形式：

| 输入形式 | 流程 |
|---------|------|
| **输入完整蛋白序列** | 序列比对 → 确认同源 → 逐位点比较 → 检查突变是否命中受保护位点 → 输出风险报告 |
| **输入突变位点列表** | 直接在知识库中查找 → 检查是否受保护 → 输出风险报告 |

如果知识库中没有该蛋白的数据，自动触发 Step 1 建库。

### 风险判定矩阵

| 专利状态 | 序列/突变位置 | protected | 风险等级 | 含义 |
|---------|-------------|-----------|---------|------|
| granted | claims | ✅ true | ⛔ high | 已获批+权利要求保护，**必须规避** |
| granted | description | false | ⚠️ medium | 已获批但仅提及，可用需注意 |
| pending | claims | false | 🔶 medium | 审查中+权利要求保护，有风险 |
| pending | description | false | ✅ low | 审查中且仅提及，可用 |
| abandoned/expired/withdrawn | any | false | ✅ safe | 无风险 |

---

## 工作流程图

```
用户输入：蛋白名称（如 "LCC cutinase"）
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│                     STEP 1: 建知识库                         │
│                                                             │
│  api_client.py                                              │
│  ① 关键词搜索 → 拿到专利 ID 列表                             │
│     API: /patent/pass/advanced                              │
│     可选参数：IPC 过滤（如 C12N9/18）/ 全文搜索              │
│              │                                              │
│  ② 批量查详情 → 拿到每个专利的完整正文                       │
│     API: /patent/pass/ids/detail                            │
│     返回：claims + descriptions                             │
│              │                                              │
│  ③ sequence_extractor.py                                    │
│     从 descriptions + claims 提取序列                        │
│     • ST.26（新）：读 <INSDSeq_moltype> 判断 DNA/RNA/AA      │
│     • ST.25（旧）：读 <212> 标签判断 PRT/DNA/RNA             │
│     • 无类型字段：字符集推断                                  │
│     • DNA/RNA 自动翻译氨基酸（三读码框取最长）                │
│     • SEQ ID NO 后面跟的字符串                                │
│     • 裸序列（连续大写氨基酸字母）                            │
│              │                                              │
│  ④ mutation_extractor.py                                    │
│     从 claims + descriptions 提取突变位点                    │
│     • 格式 1：E484K（单字母码）                              │
│     • 格式 2：Glu484Lys（三字母码）                          │
│     • 格式 3：position 484 Glu→Lys（位置描述）               │
│     • 格式 4：484E→K（数字开头）                             │
│     • 格式 5：第484位谷氨酸替换为赖氨酸（中文）                │
│     • 格式 6：substitution of X at position N with Y（句式）  │
│              │                                              │
│  ⑤ kb_builder.py                                            │
│     判断每条序列/突变的：                                     │
│     • location：在 claims 还是 description 中                │
│     • protected：granted + claims = true                    │
│     • risk_level：high / medium / low / safe                │
│     保存为 JSON 知识库文件（自动缓存）                        │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│                     STEP 2: 风险筛查                         │
│                                                             │
│  输入方式 A：突变列表（如 ["E484K", "N501Y"]）               │
│      → 直接在知识库中查找这些突变                            │
│      → 返回每个突变的风险等级                                │
│                                                             │
│  输入方式 B：完整氨基酸序列                                  │
│      → alignment.py：与知识库里的专利序列做比对              │
│      → 找到相似度高的专利序列                                │
│      → 对比哪些位置不同（突变点）                            │
│      → 查这些突变点是否受保护                                │
│      → 生成风险报告                                         │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
        risk_screener.py
        输出风险报告 JSON
              │
    ┌─────────┴──────────┐
    ▼                    ▼
 skill.py              run.py
（供其他程序调用）    （命令行工具）
```

---

## 快速开始

### 安装依赖

```bash
pip install requests biopython
```

### 配置 API 凭证

设置环境变量：

```bash
export CATALYST_ACCESS_KEY="your_access_key"
export CATALYST_ACCESS_SECRET="your_access_secret"
```

### 命令行使用

```bash
# Step 1: 构建知识库
python run.py build-kb --target EGFR

# Step 1: 查询受保护位点
python run.py query --target EGFR

# Step 2: 风险筛查（输入序列）
python run.py screen --sequence "MTEYKLVVLGAVGVGKSALT..." --target EGFR

# Step 2: 风险筛查（输入突变位点）
python run.py screen --mutations E484K,N501Y --target EGFR

# 强制重建知识库（忽略缓存）
python run.py build-kb --target EGFR --force

# 保存结果到文件
python run.py screen --mutations E484K --target EGFR -o report.json
```

### Python 直接调用

```python
from src.kb_builder import build_knowledge_base, query_protected_sites
from src.risk_screener import screen_risk

# Step 1: 构建知识库（有缓存，相同关键词不重复查）
kb = build_knowledge_base(target="EGFR")

# Step 1 的 agent 接口：查询某蛋白受保护的位点
protected = query_protected_sites(target="EGFR")

# Step 2: 风险筛查（输入序列）
report = screen_risk(query_sequence="MTEYKLVVLGAVGVGKSALT...", target="EGFR")

# Step 2: 风险筛查（输入突变位点）
report = screen_risk(mutations=["E484K", "N501Y"], target="EGFR")
```

### Skill 调用（供 macroflow 集成）

```python
from skill import run_skill

# 构建知识库
result = run_skill({"action": "build_kb", "target": "EGFR"})

# 查询受保护位点
result = run_skill({"action": "query_protected", "target": "EGFR"})

# 风险筛查
result = run_skill({"action": "screen_risk", "query_sequence": "MTEY...", "target": "EGFR"})
result = run_skill({"action": "screen_risk", "mutations": ["E484K", "N501Y"], "target": "EGFR"})
```

---

## 项目结构

```
patent-sequence-analysis-main/
├── README.md                   # 本文档
├── PLAN.md                     # 开发计划
├── CLAUDE.md                   # 项目配置
│
├── src/                        # 核心模块
│   ├── __init__.py             # 公共 API 导出
│   ├── api_client.py           # Catalyst+ API 客户端（签名、搜索、详情）
│   ├── sequence_extractor.py   # 序列提取（ST.26/SEQ ID NO/裸序列 + location 判断）
│   ├── mutation_extractor.py   # 突变位点提取（6种格式 + location 判断）
│   ├── kb_builder.py           # Step1: 知识库构建 + 缓存 + agent 查询接口
│   ├── risk_screener.py        # Step2: 风险筛查（序列/突变两种输入）
│   ├── alignment.py            # 序列比对（Biopython pairwise alignment）
│   └── utils.py                # 通用工具函数
│
├── skill.py                    # Skill 接口，供 macroflow 调用
├── run.py                      # 命令行入口
│
├── knowledge_base/             # 知识库 JSON 缓存目录
└── archive/                    # 旧版 v3.2 代码与历史数据（保留参考）
```

### 模块职责

| 模块 | 职责 |
|------|------|
| `api_client.py` | 封装 Catalyst+ API（签名、分页、时间分片、详情获取） |
| `sequence_extractor.py` | 从专利详情 JSON 中提取完整序列，判断 location（claims/description） |
| `mutation_extractor.py` | 从专利文本中提取突变位点描述（E484K 等多种格式），判断 location |
| `kb_builder.py` | 组合 api_client + sequence_extractor + mutation_extractor，构建知识库，提供查询接口 |
| `risk_screener.py` | 在知识库上做风险筛查（序列比对 / 突变查询），知识库无数据时自动触发建库 |
| `alignment.py` | 序列比对，返回 identity、对齐结果、差异位点 |
| `skill.py` | 统一对外接口：`build_kb` / `query_protected` / `screen_risk` |

---

## 突变位点提取格式

支持从专利文本中提取以下格式的突变描述：

| 格式 | 示例 | 匹配正则 |
|------|------|---------|
| 标准单字母 | `E484K` | `[A-Z]\d+[A-Z]` |
| 三字母码 | `Glu484Lys` | 三字母+数字+三字母 |
| position 描述 | `position 484 Glu→Lys` | position + 数字 + AA + 箭头 + AA |
| 数字开头 | `484E→K` / `484E/K` | 数字+AA+分隔符+AA |
| 中文格式 | `第484位谷氨酸替换为赖氨酸` | 中文数字+位+AA+替换为+AA |
| substitution 句式 | `substitution of Glu at position 484 with Lys` | substitution...of...at...with |

---

## 知识库 JSON 结构

```json
{
  "target": "EGFR",
  "build_time": "2026-06-22T10:00:00",
  "total_patents_searched": 200,
  "patents_with_data": 15,
  "patents": [
    {
      "patent_id": "US2020123456A1",
      "title": "Anti-EGFR antibody...",
      "status": "granted",
      "publication_date": "2020-03-15",
      "assignees": ["Company A"],
      "sequences": [
        {
          "seq_id": "1",
          "sequence": "MTEYKLVVVGAVGVGKSALT...",
          "seq_type": "AA",
          "length": 170,
          "source": "ST.26",
          "location": "claims",
          "protected": true,
          "role": "目标蛋白变体"
        }
      ],
      "mutations": [
        {
          "position": 484,
          "wild_type": "E",
          "mutant": "K",
          "notation": "E484K",
          "location": "claims",
          "protected": true,
          "context": "substitution of Glu at position 484 with Lys"
        }
      ]
    }
  ]
}
```

---

## 风险报告结构

```json
{
  "query_type": "mutations",
  "target": "EGFR",
  "screening_time": "2026-06-22T11:00:00",
  "hits": [
    {
      "patent_id": "US2020123456A1",
      "patent_status": "granted",
      "overall_risk": "high",
      "mutation_hits": [
        {
          "notation": "E484K",
          "risk_level": "high",
          "reason": "E484K命中已授权专利的claims，必须规避"
        }
      ]
    }
  ],
  "summary": {
    "high_risk_mutations": ["E484K"],
    "medium_risk_mutations": [],
    "low_risk_mutations": ["N501Y"],
    "safe_mutations": [],
    "conclusion": "E484K命中已授权专利的claims保护，必须规避"
  }
}
```

---

## 依赖

- `requests` — API 调用
- `biopython` — 序列比对（BLOSUM62 替换矩阵 + pairwise alignment）

---

## 版本历史

| 版本 | 说明 |
|------|------|
| v1.0 | 初始版本：关键词搜索 + 序列提取 |
| v2.0 | 时间分片、ST.26 提取、增强识别 |
| v3.2 | LLM 验证、裸序列提取、三档 CSV 输出 |
| **v4.0** | **重构为专利规避工作流：知识库 + 突变提取 + 风险筛查 + Skill 接口** |
| **v4.1** | **序列提取增强：ST.25/ST.26 统一处理、DNA 自动翻译氨基酸、Active 状态修复、裸序列上下文关键词针对酶工程优化** |

---

## 未来改进计划

### Phase 1：核心功能补全（高优先级）

#### 1. PDF 序列表解析 ⭐⭐⭐

**问题**：目前 API 返回的 `descriptions` 文本中，只有少数专利嵌入了 ST.26 序列表 XML（测试数据显示 153 个专利中只有 1 个包含）。大部分专利的序列表存在于单独的 PDF 附件中，导致序列覆盖率极低。

**改进方案**：
- 利用第三个 API 接口 `/patent/pass/url` 获取专利 PDF 下载链接
- 使用 PyMuPDF 或 pdfplumber 解析 PDF 文本
- 从 PDF 中提取 ST.26/ST.25 序列表
- 自动缓存已解析的 PDF，避免重复下载

**预期效果**：序列覆盖率从 <1% 提升到 60-80%

**技术难点**：
- PDF 格式不统一（扫描版 vs 文本版）
- 序列表可能分散在多页
- 需要较大存储空间缓存 PDF

---

#### 2. 突变位点编号对齐 ⭐⭐⭐

**问题**：不同来源的同一酶，由于序列长度差异（插入/缺失），导致功能相同的氨基酸位点编号不同。例如：
- 用户的酶：第 150 位 D→K
- 专利的酶：第 138 位 D→K
- 实际上两者对应同一个活性位点，但现在的精确匹配会判定为不同突变

**改进方案**：
- 在 Step 2 风险筛查中，先对用户序列和专利序列做全局比对（`alignment.py` 已有）
- 建立编号映射表：`{用户位点: 专利对应位点}`
- 突变匹配时使用映射后的编号，而非绝对编号

**预期效果**：解决同源蛋白编号偏移导致的漏判/误判问题

**前提条件**：知识库中必须有专利的完整序列（依赖 Phase 1 的 PDF 解析）

---

### Phase 2：数据质量优化（中优先级）

#### 3. 专利去重 ⭐⭐

**问题**：同一专利的不同申请号格式（如 `US2025179448A1` 和 `US20250179448A1`）被识别为不同专利，导致知识库冗余。

**改进方案**：
- 标准化专利号格式（去除多余零、统一分隔符）
- 在 `api_client.py` 的搜索结果去重逻辑中加入模糊匹配
- 合并相同专利的数据（保留最新版本）

---

#### 4. Excel 报告导出 ⭐⭐

**问题**：当前风险筛查只输出 JSON，不便于非技术人员查看。

**改进方案**：
- 在 `risk_screener.py` 中增加 Excel 导出功能（使用 `openpyxl`）
- 表格包含：突变位点 | 风险等级 | 专利号 | 专利状态 | 权利要求链接
- 高风险行标红，低风险行标绿

---

### Phase 3：增强功能（低优先级）

#### 5. GenBank/UniProt 编号查询 ⭐

专利中如果引用公共数据库编号（如 `NP_004439.2`），自动调用 NCBI/UniProt API 获取实际序列。

#### 6. IPC 分类号自动推荐 ⭐

根据蛋白名称（如 "cutinase"），自动推荐相关 IPC（如 `C12N9/18`），减少用户输入错误。

#### 7. 批量筛查 ⭐

支持一次输入多个蛋白/突变组合，批量生成风险报告。

#### 8. 可视化 ⭐

生成突变位点热图，直观显示哪些位点是专利高频保护区域。

---

### 当前版本限制说明

| 限制 | 影响 | 计划解决版本 |
|------|------|-------------|
| 序列覆盖率低（<1%） | 大部分专利无法做序列比对 | Phase 1.1（PDF 解析） |
| 编号精确匹配 | 同源蛋白编号偏移导致误判 | Phase 1.2（编号对齐） |
| 专利号冗余 | 知识库膨胀 | Phase 2.1（去重） |
| 只支持 JSON 输出 | 不便于查看 | Phase 2.2（Excel） |

---

## License

Internal use only.
