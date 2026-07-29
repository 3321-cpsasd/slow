"""Import a source-backed, two-hour quick book into the local Slow bookshelf.

This is a curated import, not a provider-model generation. The persisted
GenerationRun and source verification records make that provenance explicit.
"""

import asyncio
import hashlib
import json
import sys

from sqlalchemy import select

from app.ai.contracts import Source
from app.ai.local_adapter import LocalDemoAdapter
from app.application.service import SlowService
from app.core.config import settings
from app.infrastructure.database import build_database
from app.infrastructure.tables import (
    Base,
    Book,
    BookCapstone,
    Chapter,
    ChapterPractice,
    ContentVersion,
    GenerationRun,
    LearningNote,
    LearningPlan,
    PlanCreationRequest,
    QuizSet,
    Section,
    Series,
    SourceVerification,
    now,
)
from app.services.source_verifier import AcceptingSourceVerifier, HttpSourceVerifier


SERIES_ID = "series_llm_training_stability_quickbook_v1"
BOOK_ID = "book_llm_training_stability_quickbook_v1"
PLAN_ID = "plan_llm_training_stability_quickbook_v1"
REQUEST_KEY = "curated-llm-training-stability-quickbook-v1"
MODEL = "codex-curated-v1"


SOURCES = {
    "sre_monitoring": {
        "title": "Google SRE：Monitoring Distributed Systems",
        "url": "https://sre.google/sre-book/monitoring-distributed-systems/",
        "kind": "official",
        "version": "accessed-2026-07-26",
    },
    "sre_postmortem": {
        "title": "Google SRE Workbook：Postmortem Culture",
        "url": "https://sre.google/workbook/postmortem-culture/",
        "kind": "official",
        "version": "accessed-2026-07-26",
    },
    "nccl_troubleshooting": {
        "title": "NVIDIA NCCL 2.30.7：Troubleshooting",
        "url": "https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting.html",
        "kind": "official",
        "version": "2.30.7",
    },
    "nccl_ras": {
        "title": "NVIDIA NCCL 2.30.7：RAS",
        "url": "https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting/ras.html",
        "kind": "official",
        "version": "2.30.7",
    },
    "dcgm": {
        "title": "NVIDIA DCGM：Health and Diagnostics",
        "url": "https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/feature-overview.html",
        "kind": "official",
        "version": "latest-2026-06-17",
    },
    "roce": {
        "title": "NVIDIA NVUE：RoCE QoS、PFC 与 ECN",
        "url": "https://docs.nvidia.com/networking-ethernet-software/nvue-reference/Set-and-Unset-Commands/QoS/",
        "kind": "official",
        "version": "5.x-accessed-2026-07-26",
    },
    "dcp": {
        "title": "PyTorch 2.13：Distributed Checkpoint",
        "url": "https://docs.pytorch.org/docs/stable/distributed.checkpoint.html",
        "kind": "official",
        "version": "2.13",
    },
    "elastic": {
        "title": "PyTorch 2.13：Elastic Agent",
        "url": "https://docs.pytorch.org/docs/stable/elastic/agent.html",
        "kind": "official",
        "version": "2.13",
    },
    "gang": {
        "title": "Kubernetes：PodGroup Scheduling Policies",
        "url": "https://kubernetes.io/docs/concepts/workloads/workload-api/policies/",
        "kind": "official",
        "version": "accessed-2026-07-26",
    },
    "megatron": {
        "title": "NVIDIA Megatron Core：Parallelism Strategies Guide",
        "url": "https://docs.nvidia.com/megatron-core/developer-guide/latest/user-guide/parallelism-guide.html",
        "kind": "official",
        "version": "latest-accessed-2026-07-26",
    },
    "zero": {
        "title": "DeepSpeed：Zero Redundancy Optimizer",
        "url": "https://www.deepspeed.ai/tutorials/zero/",
        "kind": "official",
        "version": "accessed-2026-07-26",
    },
    "nemo_perf": {
        "title": "NVIDIA NeMo：Performance Tuning Guide",
        "url": "https://docs.nvidia.com/nemo-framework/user-guide/25.07/performance/performance-guide.html",
        "kind": "official",
        "version": "25.07",
    },
    "nsight": {
        "title": "NVIDIA Nsight Systems：User Guide",
        "url": "https://docs.nvidia.com/nsight-systems/UserGuide/index.html",
        "kind": "official",
        "version": "accessed-2026-07-26",
    },
}


def question(prompt, options, correct, objective, explanation, *, core=False):
    return {
        "prompt": prompt,
        "options": options,
        "correct": correct,
        "core": core,
        "objective": objective,
        "explanation": explanation,
        "difficulty": "standard",
    }


LESSONS = [
    {
        "chapter": 1,
        "position": 1,
        "title": "稳定性不是“不报错”：先建立岗位目标",
        "question": "训练稳定性工程师究竟对什么结果负责？",
        "objectives": ["区分稳定性、恢复效率与训练效率", "用 SLO、ETTR/MTTR 和 MFU 描述结果"],
        "source_keys": ["sre_monitoring", "sre_postmortem", "nemo_perf"],
        "blocks": [
            ("conclusion", "一句话答案", "岗位目标不是让训练“永不失败”，而是让有效训练进度可预测：故障能尽早暴露、能定位、能恢复，同时不过度牺牲吞吐。用稳定性 SLO 约束中断，用 ETTR/MTTR 约束恢复，用 MFU 或 tokens/s 观察效率，三者必须一起看。", [0, 1, 2]),
            ("mechanism", "一棵够用的指标树", "顶层看单位日历时间内完成的有效训练 token。向下拆成：成功运行时间、故障频率、检测时间、定位时间、恢复时间、回滚损失，以及稳定运行阶段的 step time、tokens/s、MFU。ETTR 可作为事件到恢复训练的端到端时间；MTTR 口径必须在团队内写清，是修复、恢复还是确认健康。", [0, 1]),
            ("example", "1024 卡任务的数字化复盘", "一次 hang 持续 40 分钟：10 分钟后报警，15 分钟定位到单节点 GPU 异常，10 分钟换节点并重启，5 分钟确认 loss 与吞吐恢复。此时优先改进点不是“把 all-reduce 再快 2%”，而是缩短检测和自动隔离，让 40 分钟变成可重复的 10 分钟闭环。", [0, 1]),
            ("boundary", "常见误区", "GPU 利用率高不等于 MFU 高；MFU 高也不代表系统稳定。重试次数少可能是故障少，也可能是任务失败后没人自动拉起。任何指标都要带分母、时间窗和运行阶段；初始化、checkpoint 和稳态 step 不能混成一个平均数。", [0, 2]),
            ("practice", "两分钟岗位表述", "试着说出：我负责把大规模训练变成可观测、可恢复、可持续优化的生产系统；先用 SLO/ETTR 保护有效进度，再用 MFU/tokens/s 优化稳态效率。然后补一个你会追踪的领先指标，例如 straggler 比例或 checkpoint 成功率。", [0, 1, 2]),
        ],
        "questions": [
            question("哪组指标最完整地描述训练稳定性岗位结果？", ["只有 GPU 利用率", "失败次数与告警数量", "稳定性 SLO、ETTR/MTTR、有效吞吐与 MFU", "只看最终 loss"], [2], "区分稳定性、恢复效率与训练效率", "稳定性、恢复速度和稳态效率必须同时受控。", core=True),
            question("一次 hang 用时 40 分钟，其中报警等待 10 分钟。最直接的工程改进是什么？", ["先换更快 GPU", "缩短检测并自动触发证据采集", "提高 batch size", "关闭告警避免噪声"], [1], "用 SLO、ETTR/MTTR 和 MFU 描述结果", "报警等待属于可直接压缩的检测阶段。"),
            question("为什么不能只用 GPU utilization 判断训练效率？", ["它无法区分有效计算、等待与低效 kernel", "它总是等于 0", "它只适用于 CPU", "它已经包含 MTTR"], [0], "区分稳定性、恢复效率与训练效率", "设备忙并不保证在执行模型的有效 FLOPs。"),
            question("定义 MTTR 时最重要的动作是什么？", ["采用行业唯一口径", "明确起点、终点、分母和时间窗", "只统计成功任务", "把初始化时间删除"], [1], "用 SLO、ETTR/MTTR 和 MFU 描述结果", "团队口径清晰才能比较和改进。"),
            question("下面哪个更接近领先指标？", ["季度训练产出", "事故后的总损失", "straggler 比例持续上升", "最终项目是否延期"], [2], "用 SLO、ETTR/MTTR 和 MFU 描述结果", "straggler 上升可在整体失败前暴露退化。"),
        ],
    },
    {
        "chapter": 1,
        "position": 2,
        "title": "六层故障地图：先找第一处异常",
        "question": "GPU、通信、网络、存储、调度和框架告警一起出现时，先查哪一层？",
        "objectives": ["建立跨层故障地图", "用时间线和第一处异常避免误判"],
        "source_keys": ["nccl_troubleshooting", "dcgm", "sre_postmortem"],
        "blocks": [
            ("conclusion", "不要从最后一条错误开始", "大规模同步训练会放大单点异常：一个 rank 先 OOM、GPU Xid 或数据读取卡住，其他 rank 最后都可能表现为 NCCL timeout。正确入口是对齐所有 rank、节点和基础设施事件的时间线，寻找“第一处偏离正常路径”的证据。", [0, 1, 2]),
            ("mechanism", "六层地图", "按依赖关系检查：GPU/驱动（Xid、ECC、温度、功耗）→ 节点与 PCIe/NVLink → RDMA/RoCE 网络 → NCCL collective → 存储与数据管线 → 调度与训练框架。每层都记录症状、直接证据、排除实验和 owner；上层超时通常只是下层故障的传播结果。", [0, 1]),
            ("example", "“NCCL hang”其实是单 rank OOM", "若 rank 37 在 step 812 先出现 OOM 并退出，其他 rank 在下一个 all-reduce 等不到它，稍后统一报通信超时。时间顺序是 OOM → rank 缺席 → collective 等待 → watchdog 报错；根因分类应落在内存/框架配置，而不是网络。", [0]),
            ("boundary", "相关不等于根因", "同一时刻出现 PFC pause、GPU 降频和 step time 抖动，不能凭相关性选一个背锅。用对照实验缩小范围：单机/跨机、替换节点、固定数据、nccl-tests、存储基准。每次只改变一个关键条件，并保存运行拓扑与软件版本。", [0, 1, 2]),
            ("practice", "故障卡片", "遇到事故先填六格：影响范围、首个异常时间、首个异常 rank/节点、最后成功 step、跨层证据、最小排除实验。没有这六项之前，不要批量调整 NCCL 环境变量。", [0, 1, 2]),
        ],
        "questions": [
            question("所有 rank 最终都报 NCCL timeout 时，第一步应是什么？", ["立即更换所有交换机", "对齐时间线，找最早异常的 rank 和事件", "提高 timeout", "直接降低模型规模"], [1], "用时间线和第一处异常避免误判", "同步训练的最后症状常由更早的单点异常传播。", core=True),
            question("rank 37 先 OOM，其他 rank 随后卡在 all-reduce。根因应先归到哪层？", ["网络拥塞", "NCCL 算法", "内存/框架配置", "对象存储"], [2], "建立跨层故障地图", "OOM 是时间线上的首个直接异常。"),
            question("以下哪个排查实验最能区分跨机网络与单机问题？", ["重复同一原任务", "对比单机与跨机 nccl-tests", "增加日志保留天数", "更换训练数据名称"], [1], "建立跨层故障地图", "改变跨机边界可直接缩小网络问题范围。"),
            question("为什么不应一开始批量调整 NCCL 环境变量？", ["变量都已废弃", "会同时改变多个条件并掩盖证据", "NCCL 不读取环境变量", "只能由前端设置"], [1], "用时间线和第一处异常避免误判", "无假设地调参会破坏可归因性。"),
            question("哪项最像可审计的故障证据？", ["感觉网络很慢", "聊天里说可能是 GPU", "带时间、rank、拓扑和版本的原始事件", "事故结束后的口头回忆"], [2], "建立跨层故障地图", "可复核证据必须带上下文和来源。"),
        ],
    },
    {
        "chapter": 1,
        "position": 3,
        "title": "NCCL、RDMA 与 GPU：十分钟分诊",
        "question": "all-reduce 变慢或 hang，如何快速分清 rank、GPU 与网络问题？",
        "objectives": ["解释 collective hang 的基本机制", "设计 NCCL、RoCE 与 GPU 的最小证据集"],
        "source_keys": ["nccl_ras", "nccl_troubleshooting", "roce", "dcgm"],
        "blocks": [
            ("conclusion", "先问三个问题", "一问所有 rank 是否进入了同一个 collective；二问最慢/缺席 rank 的 GPU 与进程是否健康；三问通信路径的带宽、丢包/拥塞和拓扑是否异常。按这个顺序可把“应用不一致、GPU/进程故障、网络退化”三类问题快速分开。", [0, 1, 2, 3]),
            ("mechanism", "collective 为什么会挂", "all-reduce 要多个 rank 以一致的顺序参与。某 rank 少调用一次、先崩溃或长期 straggle，其他 rank 就会等待。NCCL RAS 能提供 communicator 的全局状态、操作计数不一致和无响应进程线索；重复查询时计数是否前进，比单次 MISMATCH 更有意义。", [0, 1]),
            ("example", "网络证据看什么", "RoCE 不只看链路 up/down。至少关联 ECN marked、CNP、PFC pause 时长、buffer 使用/丢弃、端口错误和路径带宽。若跨机 nccl-tests 退化而单机正常，并与拥塞计数同步上升，网络假设才变强；PFC 包数量本身不是故障结论。", [1, 2]),
            ("boundary", "GPU 健康也要分层", "DCGM 的被动健康监控适合在线发现，主动诊断适合 readiness、故障后隔离和 post-mortem。Xid、不可纠正显存错误、温度和 PCIe/NVLink 异常要与 workload 时间线对齐。诊断通过不等于能排除所有现场问题，它只是受控测试下的一份证据。", [3]),
            ("practice", "十分钟命令思路", "保存每 rank 最后成功 step 和堆栈；查询 NCCL RAS/日志；对嫌疑节点采集 GPU Xid、ECC、温度与链路；比较单机和跨机通信基线；拉取 RoCE 拥塞计数。最后只写一个当前最强假设和一个可推翻它的实验。", [0, 1, 2, 3]),
        ],
        "questions": [
            question("判断 communicator 是否真的卡住，哪项证据最强？", ["单次看到 MISMATCH", "重复查询时操作计数持续不前进且固定 rank 无响应", "GPU 利用率低一次", "某端口有过 PFC 包"], [1], "解释 collective hang 的基本机制", "静态差异可能是瞬时负载不均，持续不前进更接近 hang。", core=True),
            question("单机 nccl-tests 正常，跨机显著退化且 ECN/CNP 同步上升，最应加强哪个假设？", ["数据解析错误", "跨机网络拥塞", "优化器状态损坏", "checkpoint 文件缺失"], [1], "设计 NCCL、RoCE 与 GPU 的最小证据集", "跨机边界与拥塞信号同时指向网络。"),
            question("PFC pause 包数量增加意味着什么？", ["已经证明根因是交换机", "是需要结合时长、buffer、丢弃和性能的拥塞线索", "GPU 一定坏了", "NCCL 调用顺序正确"], [1], "设计 NCCL、RoCE 与 GPU 的最小证据集", "单个计数是证据，不是独立结论。"),
            question("DCGM 主动诊断通过后，正确结论是什么？", ["排除全部 GPU 现场问题", "说明受控测试未发现对应问题，仍需结合事故证据", "网络一定正常", "训练代码一定正确"], [1], "设计 NCCL、RoCE 与 GPU 的最小证据集", "诊断有范围和运行条件边界。"),
            question("分诊报告最后应优先保留什么？", ["十个并列猜测", "当前最强假设及可推翻它的实验", "所有可用环境变量", "仅一张利用率截图"], [1], "解释 collective hang 的基本机制", "可证伪假设才能驱动下一步。"),
        ],
    },
    {
        "chapter": 2,
        "position": 1,
        "title": "Checkpoint、重启与 Gang Scheduling",
        "question": "怎样把“任务失败”变成可控、低损失的恢复流程？",
        "objectives": ["定义可验证的 checkpoint/restart 契约", "理解全员重启与 gang scheduling 的边界"],
        "source_keys": ["dcp", "elastic", "gang"],
        "blocks": [
            ("conclusion", "恢复能力是系统功能", "checkpoint 不是“目录里有文件”，而是一次可验证事务：模型、优化器、随机状态、数据进度和拓扑元数据一致可读；重启器能选择最后一个完整版本；调度器能一次性给齐所需 worker。三者缺一，自动重启只会自动重复失败。", [0, 1, 2]),
            ("mechanism", "保存与恢复契约", "分布式 checkpoint 常由多个 rank 并行写出多个文件。发布 latest 指针前要确认所有 shard 与元数据完成，最好采用临时版本 → 完整性校验 → 原子发布。恢复时做最小前向/step 校验，并记录 world size、并行策略、代码与配置版本；若允许 reshard，也要单独测试。", [0]),
            ("example", "为什么要全员重启", "同步训练中一个 worker 失败后，其余 worker 的通信组通常已失效。PyTorch Elastic Agent 的容错模型会监控 worker，发现失败或不健康后终止并重启整组 worker。恢复点来自最后一个完整 checkpoint，而不是失败进程的本地内存。", [1]),
            ("boundary", "调度器解决资源齐套，不解决状态正确", "Gang/PodGroup 语义保证一组 worker 同时获得运行资源，避免部分占卡却无法推进；它不能保证 checkpoint 可读，也不能判断某个退出码是否值得重试。可重试基础设施故障、不可重试配置错误和需要隔离节点的硬件故障，应走不同策略。", [1, 2]),
            ("practice", "写一份恢复验收清单", "至少包含：RPO（最多丢多少 step/分钟）、RTO/ETTR、保存完成条件、校验方法、latest 发布规则、重试上限、节点隔离条件、恢复后 loss/step time 健康阈值，以及演练频率。没有定期注入失败验证的 checkpoint，只是希望。", [0, 1, 2]),
        ],
        "questions": [
            question("哪项最能证明 checkpoint 可恢复？", ["目录存在", "所有 shard 完成并通过一次实际加载/step 校验", "文件数量很多", "latest 指针已写入"], [1], "定义可验证的 checkpoint/restart 契约", "真实恢复验证比文件存在性更强。", core=True),
            question("为什么 latest 指针应在完整性确认后发布？", ["减少 GPU 温度", "避免重启器选择半写入版本", "提高 NCCL 带宽", "让 rank 数量变少"], [1], "定义可验证的 checkpoint/restart 契约", "发布顺序决定读者是否会看到不完整状态。"),
            question("同步训练一个 worker 失败后，全员重启的主要原因是什么？", ["每个 worker 都有语法错误", "通信组状态通常已不可继续一致推进", "对象存储只能读一次", "GPU 不能复用"], [1], "理解全员重启与 gang scheduling 的边界", "同步组成员失败会破坏当前通信上下文。"),
            question("Gang scheduling 主要解决什么问题？", ["保证模型收敛", "保证所需 worker 资源成组到位", "验证 checkpoint 内容", "自动修复 GPU"], [1], "理解全员重启与 gang scheduling 的边界", "Gang 语义关注资源齐套和同时推进。"),
            question("下面哪个故障最不应盲目自动重试？", ["短暂节点驱逐", "已知不可重试的错误配置", "偶发可恢复网络中断", "已隔离故障节点后的重调度"], [1], "理解全员重启与 gang scheduling 的边界", "确定性配置错误会消耗资源并重复失败。"),
        ],
    },
    {
        "chapter": 2,
        "position": 2,
        "title": "看懂 Megatron 与 DeepSpeed 的故障语义",
        "question": "为什么训练框架参数会改变故障形态和排障路径？",
        "objectives": ["把并行策略映射到通信组与状态分片", "区分框架配置错误和基础设施故障"],
        "source_keys": ["megatron", "zero", "dcp"],
        "blocks": [
            ("conclusion", "框架配置就是系统拓扑", "TP、PP、DP、CP、EP 决定哪些 rank 共同执行哪些 collective、每卡持有什么状态、哪里可能产生 bubble 或热点。ZeRO/FSDP 又决定参数、梯度和优化器状态如何分片。不了解这些语义，就无法解释“为什么只在某组 rank hang”或“为什么 checkpoint 换 world size 后失败”。", [0, 1, 2]),
            ("mechanism", "并行维度的最短地图", "DP 复制计算并聚合梯度；TP 切分单层矩阵，频繁依赖组内通信；PP 沿层深切 stage，关注流水线 bubble 与 stage 失衡；CP 切长序列上下文；EP 把 token 路由到专家，易出现负载不均。组合后，每个 rank 同时属于多个通信组。", [0]),
            ("example", "ZeRO 改变内存与通信", "ZeRO-1 分片优化器状态，ZeRO-2 再分片梯度，ZeRO-3 进一步分片参数。阶段越高，每卡常驻内存越少，但参数收集、通信和 checkpoint 语义更复杂。OOM 是否只发生在特定阶段、特定 rank、前向还是 optimizer step，是定位的重要线索。", [1]),
            ("boundary", "先验证确定性，再怀疑随机硬件", "固定配置在同一 step、同一张量形状稳定复现，优先检查 batch、序列长度、并行维度整除、激活峰值和框架版本。只在特定节点或链路随机出现，并伴随 Xid/网络错误，基础设施假设更强。两类证据可以共存，但 owner 和修复路径不同。", [0, 1]),
            ("practice", "画 rank 拓扑", "为一次真实任务写下 world size = DP×TP×PP×CP（EP 需按框架语义补充），标出每类 process group、每 rank 的模型/优化器分片、最大激活阶段和 checkpoint shard。然后问：少一个 rank 时，哪些组会先停？", [0, 1, 2]),
        ],
        "questions": [
            question("为什么排查前要知道每个 rank 所属的并行组？", ["决定日志文件颜色", "不同组执行不同 collective，故障传播范围不同", "可以取消 checkpoint", "能保证 GPU 不降频"], [1], "把并行策略映射到通信组与状态分片", "并行组定义通信依赖和故障影响面。", core=True),
            question("ZeRO-3 相比 ZeRO-1 额外分片了什么？", ["只分片数据集", "参数以及前序阶段已有的状态", "CUDA 驱动", "网络端口"], [1], "把并行策略映射到通信组与状态分片", "ZeRO-3 在优化器和梯度之外进一步分片模型参数。"),
            question("PP 最典型的效率风险是什么？", ["流水线 bubble 与 stage 失衡", "RoCE 无法路由", "对象存储无目录", "rank 不需要通信"], [0], "把并行策略映射到通信组与状态分片", "流水线空泡和阶段负载差会拉低吞吐。"),
            question("错误在同一配置、同一 step 稳定复现，优先检查什么？", ["随机更换全部节点", "确定性的形状、并行配置和内存峰值", "关闭所有日志", "只提高网络 timeout"], [1], "区分框架配置错误和基础设施故障", "稳定复现更支持确定性软件/配置假设。"),
            question("EP 特别需要观察哪类现象？", ["专家 token 负载不均与通信热点", "单机文件名", "显示器刷新率", "HTTP 缓存"], [0], "把并行策略映射到通信组与状态分片", "专家路由不均会产生 straggler 和 all-to-all 热点。"),
        ],
    },
    {
        "chapter": 2,
        "position": 3,
        "title": "从 MFU 瓶颈到闭环：一张作战图",
        "question": "训练不再中断后，怎样系统提高 MFU 与有效吞吐？",
        "objectives": ["按计算、通信、I/O、调度分解效率瓶颈", "把优化与稳定性证据合成持续改进闭环"],
        "source_keys": ["nemo_perf", "nsight", "nccl_troubleshooting", "sre_postmortem"],
        "blocks": [
            ("conclusion", "先稳定，再优化最宽的瓶颈", "以稳定窗口中的 tokens/s、step time 分位数和 MFU 为基线，把 step 拆成计算、通信、数据/存储、checkpoint、调度等待与 straggler 尾部。一次只优化占比最大的可控项，并用同模型、同精度、同规模的 A/B 运行验证；不能用更多失败换来漂亮的峰值。", [0, 1, 2, 3]),
            ("mechanism", "瓶颈指纹", "GPU kernel 间大空洞常指向 CPU/数据或同步等待；通信与计算无法重叠、跨机带宽差指向 collective/网络；个别 rank step time 长形成全局尾延迟；周期性长暂停常是 checkpoint/I/O。Nsight Systems 的 CUDA、NVTX、CPU 调度与内存时间线可把这些等待具体化。", [0, 1]),
            ("example", "优化顺序示例", "若 30% step 时间在等待最慢 rank，先定位 straggler，而不是调 GEMM。若所有 rank 同步等待数据，查数据管线和存储。若 GPU 连续计算但 MFU 仍低，再看 kernel shape、精度、并行切分和融合机会。若通信主导，先建立 nccl-tests 基线，再调整拓扑或重叠策略。", [0, 1, 2]),
            ("boundary", "MFU 比较必须同口径", "MFU 的模型 FLOPs 估算、硬件峰值、精度和是否计入稀疏/MoE 都会改变分母。跨模型、跨硬件直接比一个百分比容易误导。生产目标应同时报告稳定窗口、任务规模、tokens/s、step time P50/P99、失败率与回滚损失。", [0]),
            ("practice", "30 分钟事故与效率作战图", "画四列：症状、第一证据、最小实验、自动化动作。至少放入 NCCL hang、GPU Xid、RoCE 拥塞、checkpoint 失败、straggler 与低 MFU。最后选一项写成闭环：发现 → 定位 → 隔离/恢复 → 验证 → 复盘 → 加入预案或自动化。", [0, 1, 2, 3]),
        ],
        "questions": [
            question("稳定窗口中 30% step 时间在等待最慢 rank，首要动作是什么？", ["先增大所有 timeout", "定位 straggler 来源和影响范围", "直接换精度", "只看平均 GPU 利用率"], [1], "按计算、通信、I/O、调度分解效率瓶颈", "尾部 rank 已是当前最宽瓶颈。", core=True),
            question("所有 rank 周期性同时长暂停，且与保存时间一致，优先检查什么？", ["checkpoint 与存储路径", "专家路由", "显示驱动", "告警文本"], [0], "按计算、通信、I/O、调度分解效率瓶颈", "周期与保存一致是强时间相关证据。"),
            question("GPU timeline 中 kernel 之间有大量空洞，最合理的下一步是什么？", ["证明 GPU 损坏", "关联 CPU、数据管线和同步事件", "立即提高 MFU 目标", "删除 NVTX"], [1], "按计算、通信、I/O、调度分解效率瓶颈", "空洞需要用主机与同步时间线解释。"),
            question("跨两种硬件比较 MFU 前，必须先确认什么？", ["日志字体相同", "模型 FLOPs、精度与硬件峰值的口径一致", "任务名称相同", "checkpoint 文件数相同"], [1], "把优化与稳定性证据合成持续改进闭环", "MFU 分母不同会让百分比不可直接比较。"),
            question("哪条优化结论最可信？", ["峰值跑过一次", "同配置 A/B 多次复现，吞吐提升且失败率与回滚损失未恶化", "GPU 利用率截图更高", "调了最多参数"], [1], "把优化与稳定性证据合成持续改进闭环", "可复现且不牺牲稳定性才是有效改进。"),
        ],
    },
]


CHAPTERS = [
    {
        "title": "第一章：从岗位指标到跨层分诊",
        "objective": "建立稳定性指标、六层故障地图，以及 NCCL/RDMA/GPU 的十分钟分诊路径。",
    },
    {
        "title": "第二章：把恢复与效率做成系统",
        "objective": "理解 checkpoint、调度、训练框架与 MFU 的工程闭环。",
    },
]


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def import_book():
    engine, sessions = build_database(settings.database_url)
    Base.metadata.create_all(engine)
    with sessions() as db:
        SlowService(db, LocalDemoAdapter(), AcceptingSourceVerifier()).ensure_seed()
        existing = db.scalar(select(Series).where(Series.id == SERIES_ID))
        if existing:
            book = db.get(Book, BOOK_ID)
            sections = db.scalars(
                select(Section)
                .join(Chapter, Chapter.id == Section.chapter_id)
                .where(Chapter.book_id == BOOK_ID)
            ).all()
            print(dumps({"status": "already_exists", "seriesId": SERIES_ID, "bookId": BOOK_ID, "sections": len(sections), "title": book.title if book else ""}))
            return

        request = {
            "shelf_id": "shelf_technology",
            "topic": "大模型训练稳定性工程",
            "role": "准备或评估大模型训练稳定性工程岗位的技术人员",
            "experience": "默认具备 Python/Linux/分布式训练基础，但不假设有千卡集群实战",
            "purpose": "快速建立岗位全景、排障语言和面试/入职可用的作战框架",
            "depth": "overview",
            "details": "依据岗位截图，覆盖 GPU、RDMA/RoCE、NCCL、存储、调度、checkpoint、Megatron/DeepSpeed、SLO、ETTR/MTTR 与 MFU",
        }
        request_hash = hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        plan = LearningPlan(
            id=PLAN_ID,
            **request,
            assumptions_json=dumps([
                "这是岗位速读教材，不替代真实集群演练",
                "读者能阅读基础 Linux、Python 和分布式训练术语",
                "来源于 2026-07-26 可访问的一手官方资料",
            ]),
            confidence="high",
            status="active",
        )
        series = Series(
            id=SERIES_ID,
            plan_id=PLAN_ID,
            shelf_id="shelf_technology",
            title="大模型训练稳定性工程：120 分钟岗位速读",
            rationale="从岗位结果出发，先学会跨层分诊，再把恢复、框架语义与 MFU 优化连成一个可执行闭环。",
        )
        book = Book(
            id=BOOK_ID,
            series_id=SERIES_ID,
            shelf_id="shelf_technology",
            position=1,
            title="从 NCCL Hang 到 MFU：训练稳定性工程作战手册",
            topic="大模型预训练与后训练的稳定性、可靠性和效率工程",
            description="2 章 6 节的岗位速读教材。每节约 15–20 分钟，读完能用统一指标描述问题、按证据分诊，并设计 checkpoint/restart 与效率优化闭环。",
            estimated_minutes=120,
            status="available",
        )
        db.add_all([plan, series, book])
        db.add(
            PlanCreationRequest(
                idempotency_key=REQUEST_KEY,
                user_id="user_demo",
                request_hash=request_hash,
                status="completed",
                series_id=SERIES_ID,
            )
        )
        db.add(
            BookCapstone(
                id=f"capstone_{BOOK_ID}",
                book_id=BOOK_ID,
                title="30 分钟：训练事故分诊与恢复作战图",
                brief_json=dumps({
                    "goal": "为一个 1024 GPU 训练任务的 hang + 低 MFU 场景产出可复核作战图",
                    "deliverables": ["跨层时间线与最强假设", "最小证据集和排除实验", "checkpoint/restart 契约", "稳定性与效率联合指标"],
                }),
                status="locked",
            )
        )

        chapter_rows = {}
        for position, item in enumerate(CHAPTERS, 1):
            chapter_id = f"chapter_{BOOK_ID}_{position}"
            chapter = Chapter(
                id=chapter_id,
                book_id=BOOK_ID,
                position=position,
                title=item["title"],
                objective=item["objective"],
                status="available" if position == 1 else "locked",
            )
            chapter_rows[position] = chapter
            db.add(chapter)
            db.add(
                ChapterPractice(
                    id=f"practice_{chapter_id}",
                    chapter_id=chapter_id,
                    title=f"{item['title']}：证据卡片",
                    instructions_json=dumps({
                        "objective": item["objective"],
                        "steps": ["画出一张跨层证据图", "写出一个可证伪假设", "给出一个自动化动作及其失败边界"],
                    }),
                    status="locked",
                )
            )

        persisted_at = now()
        for lesson in LESSONS:
            chapter_id = chapter_rows[lesson["chapter"]].id
            section_id = f"section_{BOOK_ID}_{lesson['chapter']}_{lesson['position']}"
            content_id = f"content_{BOOK_ID}_{lesson['chapter']}_{lesson['position']}_v1"
            quiz_id = f"quiz_{BOOK_ID}_{lesson['chapter']}_{lesson['position']}_v1"
            sources = [SOURCES[key] for key in lesson["source_keys"]]
            blocks = [
                {
                    "id": f"block_{content_id}_{position}",
                    "version": 1,
                    "kind": "text",
                    "role": role,
                    "heading": heading,
                    "content": content,
                    "source_indexes": source_indexes,
                }
                for position, (role, heading, content, source_indexes) in enumerate(lesson["blocks"], 1)
            ]
            section = Section(
                id=section_id,
                chapter_id=chapter_id,
                position=lesson["position"],
                title=lesson["title"],
                question=lesson["question"],
                objectives_json=dumps(lesson["objectives"]),
                status="available" if lesson["chapter"] == 1 and lesson["position"] == 1 else "locked",
            )
            db.add_all([
                section,
                ContentVersion(
                    id=content_id,
                    section_id=section_id,
                    version=1,
                    blocks_json=dumps(blocks),
                    sources_json=dumps(sources),
                    confidence="high",
                ),
                QuizSet(
                    id=quiz_id,
                    section_id=section_id,
                    generation=1,
                    questions_json=dumps(lesson["questions"]),
                ),
                GenerationRun(
                    id=f"generation_{BOOK_ID}_{lesson['chapter']}_{lesson['position']}_v1",
                    section_id=section_id,
                    operation="curated_import",
                    attempt=1,
                    status="succeeded",
                    model=MODEL,
                    trace_json=dumps({
                        "stage": "persisted",
                        "mode": "curated_import",
                        "contentVersionId": content_id,
                        "quizSetId": quiz_id,
                        "sourceVerificationMode": "codex_web_research_2026-07-26",
                        "notProviderGenerated": True,
                    }),
                    started_at=persisted_at,
                    finished_at=persisted_at,
                ),
                SourceVerification(
                    id=f"verification_{BOOK_ID}_{lesson['chapter']}_{lesson['position']}_v1",
                    content_version_id=content_id,
                    report_json=dumps([
                        {
                            "url": source["url"],
                            "reachable": True,
                            "statusCode": 200,
                            "pinned": True,
                            "verificationMode": "codex_web_research_2026-07-26",
                        }
                        for source in sources
                    ]),
                    verified_at=persisted_at,
                ),
                LearningNote(
                    id=f"note_{BOOK_ID}_{lesson['chapter']}_{lesson['position']}",
                    section_id=section_id,
                    user_id="user_demo",
                    ai_content_json=dumps({
                        "solved_question": lesson["question"],
                        "core_mechanism": lesson["objectives"],
                        "personal_gaps": ["完成测验后，根据错题补充个人薄弱点"],
                        "boundaries": ["速读教材提供分诊框架，不替代真实集群证据与演练"],
                        "practice_checks": [lesson["blocks"][-1][2]],
                        "sources": [source["title"] for source in sources],
                        "unresolved": [],
                        "provenance": {"mode": "curated_import", "version": MODEL},
                    }),
                    user_content_json="{}",
                ),
            ])

        db.commit()
        print(dumps({
            "status": "created",
            "seriesId": SERIES_ID,
            "bookId": BOOK_ID,
            "title": series.title,
            "chapters": len(CHAPTERS),
            "sections": len(LESSONS),
            "estimatedMinutes": book.estimated_minutes,
        }))
    engine.dispose()


def verify_sources():
    engine, sessions = build_database(settings.database_url)
    with sessions() as db:
        content_rows = db.scalars(
            select(ContentVersion)
            .join(Section, Section.id == ContentVersion.section_id)
            .join(Chapter, Chapter.id == Section.chapter_id)
            .where(Chapter.book_id == BOOK_ID)
        ).all()
        if not content_rows:
            raise SystemExit("book not imported")
        source_by_url = {}
        for content in content_rows:
            for payload in json.loads(content.sources_json):
                source_by_url.setdefault(payload["url"], Source.model_validate(payload))
        results = asyncio.run(HttpSourceVerifier().verify(list(source_by_url.values())))
        result_by_url = {item["url"]: item for item in results}
        for content in content_rows:
            report = db.scalar(
                select(SourceVerification).where(SourceVerification.content_version_id == content.id)
            )
            sources = json.loads(content.sources_json)
            report.report_json = dumps([
                {
                    **result_by_url[source["url"]],
                    "verificationMode": "server_http_2026-07-26",
                }
                for source in sources
            ])
            report.verified_at = now()
            run = db.scalar(
                select(GenerationRun).where(GenerationRun.section_id == content.section_id)
            )
            trace = json.loads(run.trace_json)
            trace["sourceVerificationMode"] = "server_http_2026-07-26"
            run.trace_json = dumps(trace)
        db.commit()
        print(dumps({
            "status": "verified",
            "bookId": BOOK_ID,
            "uniqueSources": len(source_by_url),
            "contentVersions": len(content_rows),
        }))
    engine.dispose()


if __name__ == "__main__":
    verify_sources() if "--verify-sources" in sys.argv else import_book()
