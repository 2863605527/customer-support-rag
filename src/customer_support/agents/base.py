"""Agent 抽象基类（与项目1同一设计）。"""
from abc import ABC, abstractmethod
from typing import Any


class BaseAgent(ABC):
    """Agent 抽象基类。"""

    name: str = "base"
    description: str = ""

    @abstractmethod
    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        """执行 Agent 逻辑，返回对 state 的部分更新。"""

    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        return self.run(state)
