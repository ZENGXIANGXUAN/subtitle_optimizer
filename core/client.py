import aiohttp

# 尝试相对导入，如果失败则使用绝对导入
try:
    from ...utils import logger
except ImportError:
    import sys
    import os

    sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
    from utils import logger


class MistralClient:
    DEFAULT_BASE = "https://api.mistral.ai/v1"

    def __init__(self, api_key: str, model: str = "mistral-large-latest", base_url: str = ""):
        self.api_key = api_key
        self.model = model
        base = base_url.strip().rstrip("/") if base_url.strip() else self.DEFAULT_BASE
        self.chat_url = f"{base}/chat/completions"
        logger.debug(f"MistralClient初始化完成，模型: {model}")

    async def chat(self, messages: list[dict], session: aiohttp.ClientSession) -> str:
        logger.debug(f"开始与Mistral API通信，消息数量: {len(messages)}")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.5,
        }

        logger.info(f"发送API请求到: {self.chat_url}，模型: {self.model}")
        try:
            async with session.post(self.chat_url, headers=headers, json=payload) as resp:
                logger.debug(f"API响应状态码: {resp.status}")
                if resp.status != 200:
                    text = await resp.text()
                    error_msg = f"API错误 {resp.status}: {text[:200]}"
                    logger.error(error_msg)
                    logger.error(f"请求URL: {self.chat_url}")
                    logger.error(f"请求头: {headers}")
                    logger.error(f"请求负载: {payload}")
                    raise RuntimeError(error_msg)
                data = await resp.json()
                logger.info(f"成功收到API响应，状态码: {resp.status}")
                content = data["choices"][0]["message"]["content"]
                # content 偶尔以列表形式返回（多模态/thinking格式），统一提取为纯文本
                if isinstance(content, list):
                    content = "\n".join(
                        item.get("text", "") if isinstance(item, dict) else str(item)
                        for item in content
                    ).strip()
                return content
        except Exception as e:
            logger.error(f"Mistral API调用失败: {str(e)}")
            logger.error(f"请求URL: {self.chat_url}")
            raise