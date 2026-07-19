import logging
import json
import re
import asyncio
import httpx
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

        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": temperature
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for model in self.model_names:
                payload["model"] = model
                for attempt in range(max_retries + 1):
                    try:
                        logger.info(f"正在使用模型 {model} 发起请求 (尝试 {attempt + 1})...")
                        response = await client.post(
                            f"{self.base_url}/chat/completions",
                            json=payload,
                            headers={"Authorization": f"Bearer {self.api_key}"}
                        )
                        
                        if response.status_code == 200:
                            content = response.json()['choices'][0]['message']['content'].strip()
                            if json_mode:
                                return self._parse_json(content)
                            return content
                        
                        logger.warning(f"模型 {model} 返回错误状态码: {response.status_code}, 内容: {response.text[:200]}...")
                        if response.status_code in (403, 429) or response.status_code >= 500:
                            # 针对 WAF 临时阻断 (403)、限流 (429) 或服务端错误 (5xx) 进行退避重试
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        else:
                            break
                            
                    except Exception as e:
                        logger.error(f"模型 {model} 请求出错: {str(e)}")
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
