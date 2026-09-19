from .base import MarketDataProvider

class OfficialTsetmcProvider(MarketDataProvider):
    # Credentials and endpoint mappings must stay server-side.
    # This adapter intentionally raises until the official account schema is configured.
    async def search(self,query):
        raise RuntimeError("Official TSETMC provider is not configured.")
    async def quotes(self,symbols):
        raise RuntimeError("Official TSETMC provider is not configured.")
