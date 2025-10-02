# backend/networking/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List


class NetworkingError(Exception):
    """Networking backend error."""


class NetworkingBackend(ABC):
    """Common interface for networking backends."""

    def __init__(self, spec, paths):
        self.spec = spec
        self.paths = paths

    @abstractmethod
    def prepare(self) -> List[str]:
        """Create/attach TAPs, configure VLANs/bridge. Returns list of created TAPs."""
        raise NotImplementedError

    @abstractmethod
    def teardown(self) -> None:
        """Teardown TAPs/VLANs/bridge. Must be idempotent."""
        raise NotImplementedError
