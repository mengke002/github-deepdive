import logging
import json
import re
import asyncio
from openai import AsyncOpenAI
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class LLMClient:
    """
    统一的 LLM 请求客户端，支持多模型轮询、重试以及结构化输出解析。
    """
    def __init__(self, api_key: str, base_url: str, model_names: List[str]):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model_names = model_names
        self.timeout = 90.0
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            default_headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json"
            }
        )

    async def chat(
        self, 
        system_prompt: str, 
        user_prompt: str, 
        temperature: float = 0.3,
        json_mode: bool = False,
        max_retries: int = 1
    ) -> Optional[Any]:
        """
        发送聊天请求，支持模型轮询。
        
        :param json_mode: 如果为 True，将尝试解析返回结果为 JSON。
        """
        if not self.api_key or not self.model_names:
            logger.error("LLM 配置缺失: api_key 或 model_names 为空")
            return None

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        for model in self.model_names:
            for attempt in range(max_retries + 1):
                try:
                    logger.info(f"正在使用模型 {model} 发起请求 (尝试 {attempt + 1})...")
                    # 使用 stream=True 可以兼容某些代理端点返回的不规范级联 JSON
                    response = await self.client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=temperature,
                        stream=True
                    )

                    content_chunks = []
                    async for chunk in response:
                        if hasattr(chunk, 'choices') and chunk.choices:
                            delta = chunk.choices[0].delta
                            if hasattr(delta, 'content') and delta.content:
                                content_chunks.append(delta.content)

                    content = "".join(content_chunks).strip()
                        
                    if json_mode:
                        return self._parse_json(content)
                    return content
                        
                except Exception as e:
                    error_msg = str(e)
                    # 截断过长的错误信息 (例如 Render 的 403 HTML 页面)，避免日志刷屏
                    display_msg = error_msg if len(error_msg) < 300 else error_msg[:300] + " ... [截断过多错误信息]"
                    logger.warning(f"模型 {model} 请求出错: {display_msg}")

                    # Basic retry logic for status codes if they are in the error message, or typical network errors
                    if "403" in error_msg or "429" in error_msg or "500" in error_msg or "502" in error_msg or "503" in error_msg or "504" in error_msg:
                        if attempt < max_retries:
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        else:
                            break
                    else:
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue
                        break # 切换下一个模型
            
        return None

    def _parse_json(self, text: str) -> Any:
        """
        从 LLM 返回的内容中提取并解析 JSON。
        """
        try:
            # 1. 尝试直接解析
            return json.loads(text)
        except json.JSONDecodeError:
            # 2. 尝试提取 Markdown JSON 块
            match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass
            
            # 3. 尝试寻找第一个 [ 或 {
            match = re.search(r'([\[{].*[\]}])', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass
            
            logger.error(f"无法从文本中解析 JSON: {text[:200]}...")
            return None
