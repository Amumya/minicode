"""读环境变量，给出一份不可变的配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-flash"


def _load_dotenv(path: Path) -> None:
    """有 python-dotenv 就用它，没有就什么都不做。

    dotenv 是可选依赖（装不上也能跑测试），所以这里要能降级。
    """
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    max_steps: int = 8

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        if env_file is not None:
            _load_dotenv(env_file)
        return cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
            max_steps=int(os.environ.get("MAX_STEPS", "8")),
        )

    def require_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "没有 API key。把 .env.example 复制成 .env 再填 DEEPSEEK_API_KEY。"
            )
        return self.api_key
