from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StorageBackend(ABC):
    """抽象存储后端基类"""

    @abstractmethod
    def load_accounts(self) -> list[dict[str, Any]]:
        """加载所有账号数据"""
        pass

    @abstractmethod
    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存所有账号数据"""
        pass

    @abstractmethod
    def load_auth_keys(self) -> list[dict[str, Any]]:
        """加载所有鉴权密钥数据"""
        pass

    @abstractmethod
    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存所有鉴权密钥数据"""
        pass

    @abstractmethod
    def load_collection(self, name: str) -> list[dict[str, Any]]:
        """加载命名集合数据"""
        pass

    @abstractmethod
    def save_collection(self, name: str, items: list[dict[str, Any]]) -> None:
        """保存命名集合数据"""
        pass

    def append_collection_item(self, name: str, item: dict[str, Any]) -> None:
        """Append or upsert one item in a named collection.

        Backends can override this for atomic/efficient writes. The default
        implementation preserves compatibility by loading the collection,
        replacing an existing item with the same id, then saving it back.
        """
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            raise ValueError("collection item id is required")
        items = self.load_collection(name)
        replaced = False
        next_items: list[dict[str, Any]] = []
        for existing in items:
            if str(existing.get("id") or "").strip() == item_id:
                next_items.append(item)
                replaced = True
            else:
                next_items.append(existing)
        if not replaced:
            next_items.append(item)
        self.save_collection(name, next_items)

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """健康检查，返回存储后端状态"""
        pass

    @abstractmethod
    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        pass
