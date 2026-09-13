from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Friend:
    wxid: str
    nick: str = ""
    remark: str = ""
    is_friend: bool = True


class ContactSource(Protocol):
    def list_friends(self) -> list[Friend]: ...


class FakeContactSource:
    def __init__(self, friends: list[Friend]):
        self._friends = list(friends)

    def list_friends(self) -> list[Friend]:
        return list(self._friends)
