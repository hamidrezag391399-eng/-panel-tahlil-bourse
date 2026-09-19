from abc import ABC, abstractmethod

class MarketDataProvider(ABC):
    @abstractmethod
    async def search(self, query: str): ...
    @abstractmethod
    async def quotes(self, symbols: list[str]): ...
    async def close(self): pass
