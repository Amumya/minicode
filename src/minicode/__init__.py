"""minicode —— 一个以治理层为重点的编码智能体。

模块：
    config    读 .env，给出配置
    events    结构化事件记录，指标从事件算
    policy    权限判定（分词 + 参数语义）
    provider  模型通信，归一成 ModelReply
    loop      主循环
    tools     工具注册表与实现（目前只有 bash）
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
