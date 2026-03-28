import aiohttp

class MistralClient:
    DEFAULT_BASE = "https://api.mistral.ai/v1"

    def __init__(self, api_key: str, model: str = "mistral-large-latest", base_url: str = ""):
        self.api_key = api_key
        self.model = model
        base = base_url.strip().rstrip("/") if base_url.strip() else self.DEFAULT_BASE
        self.chat_url = f"{base}/chat/completions"

    async def chat(self, messages: list[dict], session: aiohttp.ClientSession) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
        }
        async with session.post(self.chat_url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"API错误 {resp.status}: {text[:200]}")
            data = await resp.json()
            return data["choices"][0]["message"]["content"]