#!/usr/bin/env python3
"""
测试日志系统的功能
"""
import asyncio
import aiohttp
from core.client import MistralClient
from utils import logger

def test_api_logging():
    """测试API日志功能"""
    logger.info("开始测试API日志功能")
    
    # 创建一个假的API密钥用于测试日志功能（这会失败，但会显示日志）
    client = MistralClient(api_key="fake_key_for_testing", model="test-model")
    
    print(f"API端点URL: {client.chat_url}")
    print(f"模型: {client.model}")
    
    logger.info("API客户端配置完成")
    
    # 尝试进行一次API调用（预期会失败，但会记录详细日志）
    async def test_call():
        try:
            async with aiohttp.ClientSession() as session:
                result = await client.chat([{"role": "user", "content": "test"}], session)
                return result
        except Exception as e:
            logger.warning(f"API调用失败，这是预期的测试行为: {e}")
            return None

    # 运行异步测试
    asyncio.run(test_call())
    
    logger.info("API日志功能测试完成")

if __name__ == "__main__":
    test_api_logging()