"""minicode —— 一个以治理层为重点的编码智能体 harness。

包的分层：

    minicode.provider    模型通信（Anthropic / OpenAI 兼容协议）
    minicode.tools       工具注册表与实现
    minicode.policy      权限判定（结构化命令解析）
    minicode.loop        主循环
    minicode.events      结构化事件流（可观测性）
    minicode.cli         命令行入口

阶段 A（当前）：只有骨架与元数据，尚未实现上述模块。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
