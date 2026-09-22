# EeveeAgent 对话期间代码改动记录

更新时间：2026-09-19

本文只记录本次对话期间实际写入 EeveeAgent 源码的改动。工作区中原本存在的 session、skill、数据集和其他文件改动不属于本文范围。

## 改动概览

| 改动 | 主要位置 | 目的 |
| --- | --- | --- |
| 修复 OpenAI 多工具重复执行 | `agents/agent.py` 的 `_chat_openai()` | 保证一次模型响应里的每个 `tool_call_id` 只执行和写回一次 |
| 增加 context 与 reasoning 配置入口 | `agents/main.py`、`agents/agent.py`、`README.md` | 允许通过 CLI 或 `.env` 控制 EeveeAgent 压缩窗口，并向 OpenAI-compatible API 传递 reasoning effort |
| 修复中文 memory 召回门槛 | `agents/memory.py` 的 `start_memory_prefetch()` | 不再依赖空格判断，兼容通常不使用空格分词的中文请求 |
| 保留异步 memory prefetch 并改善结果消费 | `agents/agent.py` 的 `chat()`、`_consume_settled_memory_prefetch()`、两套 chat loop | 不阻塞首轮主模型；在后续轮次消费已完成的召回结果 |
| 增加回归测试 | `tests/test_openai_agent_loop.py`、`tests/test_memory_recall.py` | 固定上述行为，防止以后再次退化 |

## 1. 修复 OpenAI 多工具调用重复执行

### 原问题

原实现把 `oai_batches` 的构建和执行放在遍历 `tool_calls` 的循环内部。模型一次返回两个工具时，流程会变成：

1. 收到第一个工具，构建包含第一个工具的 batch 并执行。
2. 收到第二个工具，重新构建包含第一、第二个工具的 batch。
3. 第一个工具被再次执行，并可能重复写入相同的 `tool_call_id`。

这会造成有副作用的工具重复运行，也可能违反 OpenAI 的 tool message 协议。

### 修改位置

- `agents/agent.py:1660-1750`，`Agent._chat_openai()` 的 tool call 处理分支。
- `tests/test_openai_agent_loop.py:8-76`，多工具回归测试。

### 实现方式

- 第一阶段完整遍历模型返回的 `tool_calls`，解析参数、检查权限，并写入 `oai_checked`。
- 第二阶段在遍历结束后统一构建 `oai_batches`。
- `CONCURRENCY_SAFE_TOOLS` 中连续出现的安全工具放进并发 batch，通过 `asyncio.gather()` 执行。
- 非并发安全工具、被拒绝工具保持顺序处理。
- 每个工具结果只追加一条与原始 ID 对应的 `role="tool"` 消息。
- 保留 `_context_cleared` 中断当前 batch、刷新 system prompt 和上下文压缩的既有行为。

### 为什么这样修改

batch 的生命周期必须对应“一次完整模型响应”，不能对应“当前已经遍历到的前缀”。先收集、后执行能从结构上消除重复执行，而不是依靠 ID 去重掩盖控制流错误。

### 验证

`test_multiple_tool_calls_are_each_executed_and_recorded_once` 构造一次返回两个工具的响应，断言：

- `_execute_tool_call()` 恰好调用两次。
- 两个工具各执行一次且参数正确。
- 消息历史中只出现 `call-1`、`call-2` 各一条 tool result。

## 2. 增加 context window 与 thinking level 配置

### 修改位置

- `agents/main.py:54-57`：新增 CLI 参数。
- `agents/main.py:86-106`：新增配置解析和校验。
- `agents/main.py:412-434`：解析配置并传入根 Agent。
- `agents/agent.py:140-168`：Agent 构造参数、校验和压缩窗口计算。
- `agents/agent.py:1185-1190`、`agents/agent.py:1304-1309`：skill fork 和普通 sub-agent 继承配置。
- `agents/agent.py:1756-1764`：向 OpenAI-compatible API 转发 `reasoning_effort`。
- `README.md:117-132`：配置示例和 Ollama 边界说明。
- `tests/test_openai_agent_loop.py:78-120`：参数转发与环境变量测试。

### 新增配置

```env
EEVEE_CONTEXT_WINDOW=32768
OLLAMA_REASONING_EFFORT=low
```

对应 CLI：

```bash
python -m agents.main --context-window 32768 --thinking-level low
```

配置优先级为：

1. CLI 显式参数。
2. `.env` 或进程环境变量。
3. 未配置时使用 EeveeAgent 现有的模型名推断逻辑；reasoning effort 则不发送。

### 参数约束

- context window 必须是正整数。
- thinking level 只能是 `none`、`low`、`medium` 或 `high`。
- 非法值在入口处打印错误并以状态码 2 退出，避免带着错误配置运行。

### EeveeAgent 内部窗口计算

原实现固定使用：

```python
effective_window = inferred_context_window - 20000
```

现在保存显式的 `self.context_window`，并按以下方式计算压缩阈值：

```python
context_reserve = min(20000, max(1, context_window // 10))
effective_window = max(1, context_window - context_reserve)
```

这样小 context 不会因为固定减去 20,000 而得到零或负阈值，大窗口仍保留最多 20,000 token 的安全空间。

### 为什么这样修改

此前 `.env` 和 CLI 都无法显式控制这两个量：

- EeveeAgent 只能根据模型名猜测 context，可能与实际部署不一致，进而过早或过晚压缩。
- OpenAI-compatible 模型的 reasoning effort 没有被放进 API 请求。
- sub-agent 没有明确继承根 Agent 的运行参数。

现在根 Agent 和 sub-agent 使用一致的 context/reasoning 配置，且只有用户显式配置 reasoning effort 时才向服务端发送该字段。

### 重要边界

`EEVEE_CONTEXT_WINDOW` 只控制 EeveeAgent 何时压缩消息历史，不会改变 Ollama 实际分配的 context/KV cache。Ollama 仍需通过启动环境的 `OLLAMA_CONTEXT_LENGTH`，或 Modelfile 的 `PARAMETER num_ctx` 配置真实窗口，并应让两边数值保持一致。

`--thinking` 仍是原有的 Anthropic extended thinking 开关；`--thinking-level` 是新增的 OpenAI-compatible `reasoning_effort` 参数，两者不是同一个选项。

## 3. 修复 memory 对中文请求的召回门槛

### 原问题

`start_memory_prefetch()` 原先要求查询中存在空格：

```python
if not re.search(r"\s", query.strip()):
    return None
```

这近似假定有效查询一定包含多个以空格分隔的词。中文通常连续书写，因此“请记住我的偏好”一类请求会被直接跳过，根本不会进入 memory 选择阶段。

### 修改位置

- `agents/memory.py:169-172`：新增 `MIN_MEMORY_QUERY_CHARS = 4`。
- `agents/memory.py:347-379`：替换召回启动条件并更新任务说明。
- `tests/test_memory_recall.py:11-31`：中文、短输入和纯标点测试。

### 实现方式

现在统计 Unicode 字母和数字字符：

```python
meaningful_chars = sum(1 for char in query.strip() if char.isalnum())
if meaningful_chars < MIN_MEMORY_QUERY_CHARS:
    return None
```

`str.isalnum()` 能把中文汉字按单个字符计数，同时排除空格和标点。当前阈值取 4：

- `好`、`继续`、`？！`：跳过召回。
- `我的偏好`、`你记得吗`：允许召回。

### 为什么选择 4

阈值过低会让“好”“嗯”“继续”等高频短回复频繁触发 side LLM，增加 token、API 调用和并发负载；阈值过高又会漏掉简短的中文记忆请求。4 个有效字符是初始折中，并以常量形式集中定义，后续可以根据真实召回命中率调整为 3 或 5。

长度门槛不是唯一条件。以下情况仍会跳过召回：

- 当前是 sub-agent。
- 无可用 side query。
- session memory 注入预算已满。
- memory 目录没有实际 memory 文件。
- 查询少于 4 个 Unicode 字母/数字字符。

这些跳过条件有保留必要，能避免空输入、误触输入和没有可召回内容时浪费一次模型调用。

## 4. 保留异步 memory prefetch，并改善结果消费

### 讨论中发现的问题

原实现启动异步召回后立即请求主模型，只在 agent loop 每轮开始时用 `task.done()` 检查结果。如果主模型首轮直接返回普通文本，loop 随即结束，较晚完成的召回没有机会注入。因此它有明确取舍：

- 优点：memory selection 可以与主模型请求并行，不把一次 side LLM 延迟串行加到首 token 前。
- 缺点：无工具、单轮结束的回复可能用不到本轮 memory。
- 有工具调用并进入后续轮次时，已经完成的 memory 可以在下一轮模型请求前注入。

对话中曾短暂改成“主模型首轮请求前强制等待 memory”，以保证无工具回复也能获得记忆。随后考虑 EeveeAgent 原有 latency 设计，本改动已回退；最终代码继续使用非阻塞 prefetch。

### 最终修改位置

- `agents/agent.py:420-436`：保留原始用户查询，与 skill 增强后的消息分离。
- `agents/agent.py:534-553`：新增非阻塞的 `_consume_settled_memory_prefetch()`，集中处理完成状态、异常和预算统计。
- `agents/agent.py:1326-1360`：Anthropic 每轮请求前只消费已经完成的结果。
- `agents/agent.py:1601-1627`：OpenAI-compatible 每轮请求前只消费已经完成的结果。
- `agents/memory.py:339-385`：保留并明确 `settled` 非阻塞轮询语义。
- `tests/test_memory_recall.py:34-111`：覆盖未完成任务不阻塞，以及已完成结果可以注入。

### 最终流程

```text
原始用户输入
  -> 满足门槛时创建 memory task
  -> 进入 agent loop
  -> 非阻塞检查 task.done()
       ├─ 未完成：立即请求主模型
       └─ 已完成：注入 memory，再请求主模型
  -> 有工具调用时进入下一轮并再次检查
```

### 保留下来的改善

- memory 选择使用 `original_user_message`，不会把自动追加的 `<retrieved_skills>` 内容当作用户查询，减少 skill 文本对相关性判断的污染。
- `_consume_settled_memory_prefetch()` 在任务仍运行时立即返回，不执行 `await`。
- 命中的 memory 路径写入 `_already_surfaced_memories`，同一 session 不重复注入。
- 注入内容大小累计到 `_session_memory_bytes`，继续遵守 session budget。
- 已取消的召回任务被安全忽略；其他任务异常记录 warning，不再用宽泛的 `except: pass` 静默吞掉。
- OpenAI 和 Anthropic 使用相同的结果消费规则。

### 为什么回退同步等待

memory selection 自身需要一次 side LLM 调用。若在主模型前强制等待，首 token 延迟会近似串行增加 `T_memory`；保留异步方式时，它可以和 `T_model` 重叠。当前决策是 latency 优先，接受“首轮无工具回复可能错过 memory”这一已知限制。

需要注意：当前 `start_memory_prefetch()` 是在 MCP 初始化和 skill augmentation 之后启动的，所以它主要与主模型请求重叠，并没有与这些更早的准备步骤重叠。若未来既要首轮稳定命中又要降低延迟，需要把召回启动点前移或引入缓存，而不是在主模型前无条件等待。

### 验证

- `test_memory_prefetch_does_not_block_first_no_tool_model_response` 用一个被 Event 阻塞的召回任务验证主模型仍立即执行。
- `test_already_settled_memory_is_injected_before_model_request` 验证已完成的召回会在当前轮模型请求前注入。

## 5. 优化 _recent_dialog_messages() 方法逻辑
    原本每次需要遍历整个session记录，现倒叙遍历8个

## 6. 待处理tools优化
    Eevee Agent 已经实现文件引用式 Tool Result Offloading，但当前主要是「完整结果落盘 + 前 200 行预览 + read_file 重新读取」。不根本解决文件太大读写问题。要实现更完整的按需加载，还需要增加分页或分块读取能力。

## 7. Memory待优化
    你的方案：增加一个 memory_wait Tool

    完全可以实现。

    例如新增：

    {
        "name": "memory_wait",
        "description": (
            "Wait for the ongoing memory retrieval "
            "when historical context is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    }

    主模型如果认为自己缺少历史信息，就调用：

    tool_call:
    memory_wait()

    Runtime 执行：

    async def _execute_memory_wait(self):
        memories = await self._memory_prefetch.task
        return format_memories_for_injection(memories)

    此时 await 的含义是：

    当前主 Agent 等待 Memory 检索结果。

    Memory 检索继续运行。

    Event Loop 中的其他任务仍然可以执行。

    完成后把结果作为 Tool Result 返回。

    方案 A：由 Agent 主动等待

    User Query

    ↓

    Memory Prefetch 启动

    Main LLM #1

    判断是否需要历史记忆

    需要

    memory_wait()

    ↓

    等待并返回 Memory

    ↓

    Main LLM #2

    不需要

    直接回答或调用其他工具

    这种设计的优点是：Agent 有了明确的控制能力，不再只能被动等待 Runtime 注入。

    但我不建议只实现这个工具。

    原因有三个。

    问题 1：第一次推理仍然缺少 Memory

    假设用户问：

    按照我之前确定的架构修改数据库连接模块。

    主模型没有看到 Memory，就需要先意识到：

    我缺少架构信息
    → 应该调用 memory_wait

    但它也可能直接调用：

    edit_file(...)

    或者根据当前代码自行推断架构。

    Agent 不一定能意识到自己不知道什么。

    这就是把全部决策权交给 LLM 的一个风险。

    问题 2：增加一次模型调用

    如果 Memory 是必要条件：

    Main LLM #1
        ↓
    memory_wait
        ↓
    Main LLM #2

    这会增加一次完整的模型请求。

    而如果 Runtime 在第一次模型请求前直接等待 Memory：

    Memory
        ↓
    Main LLM #1

    只需要一次主模型调用。

    对于明确依赖历史记忆的请求，后者通常更直接。

    问题 3：当前 Prefetch 可能根本没有启动

    Eevee 有：

    if not re.search(r"\s", query.strip()):
        return None

    这样的启动条件。

    因此可能出现：

    用户：
    “记得吗”

    没有空格
        ↓
    memory_prefetch = None

    此时：

    await self._memory_prefetch.task

    根本没有可等待的 Task。

    所以如果实现工具，还必须处理：

    任务尚未启动
    任务正在运行
    任务已经完成
    任务失败
    任务结果已经注入

    这些状态。

    三、我更建议的方案：Hybrid Memory Retrieval

    把 Memory 按照对当前请求的重要程度分成三种处理路径。

    推荐架构：三层 Memory Retrieval

    Layer 1：Pinned Memory

    稳定、优先级高的用户偏好和长期约束。

    在首轮模型调用前准备好

    Layer 2：Automatic Prefetch

    根据当前用户问题自动查找相关记忆。

    普通问题异步，必要时同步等待

    Layer 3：On-demand Retrieval

    Agent 在任务过程中根据新发现主动查询。

    Tool-driven

    分别说一下。

    Layer 1：高优先级 Memory 常驻

    例如：

    用户偏好：
    - 回答代码问题时先讲架构再讲细节。
    - 当前项目不得修改数据库 Schema。

    这种稳定且简短的信息，可以在构建 System Prompt 时直接加载。

    不必每轮都：

    Header
    → Side LLM
    → 等待
    → 注入

    当前 Eevee 已经把 MEMORY.md 的索引放进 System Prompt，但是索引只是名称和描述，并不保证包含完整的约束正文。

    可以扩展为：

    System Prompt
    ├── Memory Policy
    ├── Memory Index
    └── Pinned Memories
        ├── 用户稳定偏好
        └── 当前项目关键约束

    注意必须设置 Token Budget，例如几百 Token，并提供修改和撤销机制。

    并不是让所有长期 Memory 都常驻。

    Layer 2：普通问题异步，明确依赖历史的问题同步

    这是我认为你应该优先修改的部分。

    在用户请求进入 Agent Loop 时先判断：

    是否明确依赖历史信息？

    例如：

    “上次我们决定了什么？”
    “按照之前确定的方案继续。”
    “我之前说过希望怎么处理？”

    这些请求可以先完成 Memory Recall，再进行第一次主模型调用。

    而普通请求：

    “解释一下这段 Python 代码。”

    可以继续异步预取。

    具体逻辑：

    memory_prefetch = start_memory_prefetch(...)

    if requires_memory(user_message):

        memories = await memory_prefetch.task

        inject_memories(memories)

    # 开始第一次主模型调用
    response = await call_main_llm()

    这里的 requires_memory() 可以先用简单规则判断，后续再结合小模型或索引匹配。

    这不是把所有 Memory 改成同步，而是只对有明确历史依赖的任务建立同步屏障。

    四、进一步优化：设置有限等待时间

    如果不想让 Memory Selector 阻塞太久，可以采用 Bounded Wait。

    比如允许等待 500 ms。

    import asyncio

    async def wait_memory_if_ready(prefetch, timeout=0.5):

        if prefetch is None:
            return None

        try:
            return await asyncio.wait_for(
                asyncio.shield(prefetch.task),
                timeout=timeout,
            )

        except asyncio.TimeoutError:
            return None

    最关键的是：

    asyncio.shield(prefetch.task)

    因为 asyncio.wait_for() 超时后通常会取消被等待的对象。

    如果直接写：

    await asyncio.wait_for(
        prefetch.task,
        timeout=0.5
    )

    超时可能导致 Memory Prefetch 被取消。

    但我们的目标不是取消它，而是：

    最多等 500 ms；如果还没完成，就让它在后台继续，后续 Agent Loop 再检查。

    所以需要：

    asyncio.shield(...)

    这样就能实现：

    User
    ↓
    Start Prefetch
    ↓
    最多等待 500ms
    │
    ├── 已完成
    │     ↓
    │   注入 Memory
    │     ↓
    │   Main LLM #1
    │
    └── 未完成
            ↓
        Main LLM #1
            +
        Memory 继续后台执行

    不过这里要非常明确：

    500 ms 是延迟优化，不是正确性保证。

    如果用户明确要求回忆此前的决定，超时后不能假装已经获得历史信息。此时应采用严格等待，或者在检索失败时明确告知 Agent 相关历史尚未获得，避免它自行补全事实。

    五、Layer 3：新增 memory_search，而不只是 memory_wait

    这是我认为你的想法里最有扩展价值的部分。

    为什么？

    因为当前 Eevee 的 Memory Query 主要来自本轮最初的用户请求。Side LLM 基于这个 Query 和 Memory headers 选择最多 5 条，随后 Agent Loop 只检查同一个 Task，并不会利用后续工具结果重新检索。

    例如：

    User:
    “帮我修这个 Bug”
        ↓
    Memory Prefetch
    Query = "帮我修这个 Bug"
        ↓
    没有选中相关 Memory
        ↓
    Main Agent read_file()
        ↓
    发现：
    这是 PostgreSQL 连接池的问题

    到这里，Agent 获得了新的检索关键词。

    但当前 Memory Prefetch 不会重新执行。

    因此，我建议提供一个真正带 Query 的工具：

    memory_search(
        query="PostgreSQL connection pool previous decisions"
    )

    这比：

    memory_wait()

    更有用。

    因为：

    Tool

        

    能做什么




    memory_wait()

        

    等待最初那次检索




    memory_search(query)

        

    根据新信息发起新的检索




    memory_get(path)

        

    精确读取某条已知 Memory

    三者职责不同。

    可以进一步把它们简化成两个公开工具：

    memory_search(query: str)
    memory_get(memory_id: str)

    而原始 Prefetch 仍然作为 Runtime 内部任务。

    如果 Agent 需要等待正在执行的同 Query 预取，Runtime 可以直接复用那个 Task，而不是重新搜索。

    六、我会怎样具体改 Eevee 的代码？

    我会优先修改四个地方。

    文件

        

    修改内容




    memory.py

        

    支持显式 Query 检索，提供状态和超时控制




    agent.py

        

    增加首轮 Memory Barrier；统一注入函数




    tools.py

        

    注册 memory_search / memory_get




    prompt.py

        

    告诉模型什么时候必须查 Memory，以及什么时候不能自行猜测

    其中最重要的不是多加一个工具，而是让自动检索与工具检索复用同一套 Memory Manager。

    理想状态：

                        Memory Manager
                                │
                    ┌─────────┴─────────┐
                    │                   │
                Prefetch          Memory Tool
                    │                   │
                    └─────────┬─────────┘
                                │
                        Query Cache
                                │
                    Memory Retrieval
                                │
                        RelevantMemory
                                │
                    ┌─────────┴─────────┐
                    │                   │
                自动注入             Tool Result

    这里必须解决一个细节：

    同一次检索结果只能算一次使用，不能因为 Prefetch 和 Tool 同时消费它，就把相同 Memory 注入两次。

    你之前看到的：

    self._already_surfaced_memories

    可以用来辅助去重，但还需要区分“已检索”“已返回给模型”和“仍在当前 Context 中”这几种状态。

    尤其是 Eevee 的 Full Folding 会替换原始消息历史，而当前 _compact_openai() 和 _compact_anthropic() 没有同步清除 _already_surfaced_memories。因此，某条此前已经注入、但折叠时没有保留的 Memory，后续仍可能被排除在召回候选之外。

    这是重构时应该一起处理的 Context 生命周期问题。

### 当前已实现：主 Agent 按需调用 `memory_search`

本阶段只落地只读的 `memory_search(query)`，尚未实现 `memory_wait`、`memory_get`、首轮 Memory Barrier 或完整 Memory Manager 重构。

- 工具复用 `select_relevant_memories()`，根据显式 query 发起新的检索，而不是等待最初的 prefetch。
- 自动 prefetch 和显式工具共用最终的去重与 session budget 登记逻辑，避免并发召回把同一文件展示两次。
- 工具结果使用独立的结构化 JSON，不伪装成自动注入使用的 `<system-reminder>`。
- 工具属于 `READ_TOOLS`，无需额外确认，但仍尊重用户配置的 deny rule。
- `memory_search` 只向主 Agent 暴露；general/custom/skill-fork 子 Agent 都会过滤该工具，并有 `is_sub_agent` 运行时兜底。
- selector prompt 将 query 和 manifest 作为数据处理，并要求只返回 manifest 中的精确文件名。
- system prompt 说明了何时按需检索、避免重复检索，以及代码相关结论必须回到当前仓库核实。

本阶段仍保留的后续问题包括：prefetch task 复用与 query cache、folding 后 surfaced 状态的生命周期、无结果与 selector 失败的状态区分，以及 pinned memory 和首轮同步屏障。

### 修复绝对路径的新文件被误写到项目目录

`_resolve_tool_path()` 过去会对不存在的绝对写入目标逐段删除路径前缀，直到在当前工作目录找到一个已存在的父目录。这会把本应写入项目 memory 目录的新文件错误地写到仓库根目录，同时 `_write_file()` 仍在结果中显示原始目标路径。

现在新建文件时会原样保留绝对路径，并且 `write_file` 的成功结果报告实际解析后的写入路径。已经误写到仓库根目录的 `user_jacky_profile.md` 也恢复到了该 session 对应的 memory 目录并重新生成索引。

## 8. Skill evaluation 缺陷待修正
    不应该只用一个 used 指标来衡量所有事情。

    可以分成三个维度：

    A. Selection：选择是否正确

    检查相关 Skill 是否被检索，以及在尚未加载时 Agent 是否选择调用它。

    B. Exposure：Skill 是否对 Agent 可用

    通过工具调用记录、当前上下文、版本号和折叠状态，判断 Agent 是否接触过适用的完整 instructions。
    这里目前缺失 - 上下文中是否有？ 之前没调用过一定没有，如果没有触发折叠之前调用过就自动有，如果触发折叠需要再判断

    C. Adherence：规则是否得到执行

    检查最终回复与工具执行轨迹，判断 Agent 是否遵循了该 Skill 的具体要求。
    C是否需要先有B才能有

## 检验

执行命令：

```bash
python -m py_compile agents/memory.py agents/agent.py tests/test_memory_recall.py tests/test_openai_agent_loop.py
python -m unittest discover -s tests -v
git diff --check
```

结果：9 项测试全部通过，Python 编译检查和 diff whitespace 检查通过。

覆盖范围包括：

- OpenAI 单次返回多个工具时各执行一次。
- reasoning effort 正确转发。
- context window 和 reasoning effort 从环境变量读取。
- 非法配置被拒绝。
- 四字符中文请求启动 memory 召回。
- 极短输入和纯标点跳过召回。
- 未完成的 memory prefetch 不会阻塞首轮无工具回复。
- 已经完成的 memory prefetch 会在当前轮模型请求前注入。

## 9. 其余尚未形成代码改动的事项

- CLI 中文输入有字符无法删除：提出过终端输入层问题，但尚未修改 REPL 的行编辑实现。

