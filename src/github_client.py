import requests
import time
import logging
import random
from .config import load_config

logger = logging.getLogger(__name__)

class GitHubClient:
    """
    GitHub API 客户端，支持多 Token 轮询、指数退避重试和速率限制处理。
    """
    def __init__(self):
        self.settings = load_config()
        self.tokens = [token for token in self.settings["github"].get("tokens", []) if token]
        self.max_concurrent_requests = self.settings["github"].get("max_concurrent_requests", 10)
        self.session = requests.Session()
        if not self.tokens:
            logger.warning("配置文件中未找到 GitHub Token。")
        
    def _get_headers(self):
        """构造请求头，随机选择一个 Token 以分散速率限制压力"""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-deepdive/1.0",
        }
        if self.tokens:
            token = random.choice(self.tokens)
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def request(self, method, url, **kwargs):
        """执行带重试逻辑的 API 请求"""
        max_retries = kwargs.pop("max_retries", 3)
        timeout = kwargs.pop("timeout", 30)
        for i in range(max_retries):
            try:
                headers = self._get_headers()
                if "headers" in kwargs:
                    headers.update(kwargs.pop("headers"))
                
                response = self.session.request(method, url, headers=headers, timeout=timeout, **kwargs)
                
                if response.status_code in (200, 201, 202):
                    if response.status_code == 202 and not response.text.strip():
                        return None
                    content_type = response.headers.get("Content-Type", "")
                    if "application/json" in content_type:
                        return response.json()
                    return response.text
                elif response.status_code == 204:
                    return None
                elif response.status_code in (403, 429):
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    retry_after = response.headers.get("Retry-After")
                    
                    if retry_after and retry_after.isdigit():
                        sleep_duration = min(int(retry_after), 120)
                    elif remaining == "0":
                        reset_time = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
                        sleep_duration = min(max(reset_time - time.time(), 10), 120)
                    else:
                        # Secondary Rate Limit (Abuse Prevention)
                        sleep_duration = min(2 ** (i + 1) * 5, 120)
                    
                    logger.warning(f"GitHub API 限流/阻断 (HTTP {response.status_code})。重试 {i+1}/{max_retries}，等待 {sleep_duration} 秒: {url}")
                    time.sleep(sleep_duration)
                    continue
                elif response.status_code == 404:
                    logger.info(f"未找到资源: {url}")
                    return None
                else:
                    logger.error(f"GitHub API 错误 {response.status_code}: {response.text}")
                    if i < max_retries - 1:
                        time.sleep(2 ** i) # 指数退避
                        continue
                    return None
            except Exception as e:
                logger.error(f"GitHub API 请求异常: {e}")
                if i < max_retries - 1:
                    time.sleep(2 ** i)
                    continue
                return None

    def test_connection(self):
        """测试连接和速率限制状态"""
        return self.request("GET", "https://api.github.com/rate_limit")

    def get_repo(self, full_name):
        """获取仓库基础信息"""
        return self.request("GET", f"https://api.github.com/repos/{full_name}")

    def get_contributors(self, full_name, per_page=30):
        """获取仓库贡献者列表"""
        return self.request("GET", f"https://api.github.com/repos/{full_name}/contributors?per_page={per_page}")

    def get_user_starred(self, username, per_page=30):
        """获取用户 Star 过的仓库列表"""
        return self.request("GET", f"https://api.github.com/users/{username}/starred?per_page={per_page}")

    def get_user_following(self, username, per_page=100):
        """获取用户关注的人员列表"""
        return self.request("GET", f"https://api.github.com/users/{username}/following?per_page={per_page}")

    def get_latest_release(self, full_name):
        """获取仓库最新的 Release 信息"""
        return self.request("GET", f"https://api.github.com/repos/{full_name}/releases/latest")

    def get_issues(self, full_name, state="open", sort="comments", per_page=30):
        """获取仓库的 Issue 列表"""
        return self.request("GET", f"https://api.github.com/repos/{full_name}/issues?state={state}&sort={sort}&per_page={per_page}")

    def get_content(self, full_name, path):
        """获取仓库中指定路径的文件内容"""
        import base64
        res = self.request("GET", f"https://api.github.com/repos/{full_name}/contents/{path}")
        if res and isinstance(res, dict) and "content" in res:
            try:
                content = res["content"].replace("\n", "")
                return base64.b64decode(content).decode("utf-8", errors="ignore")
            except Exception as e:
                logger.error(f"解析文件内容失败 {full_name}/{path}: {e}")
        return None

    def get_readme(self, full_name):
        """获取仓库 README 内容 (经过 Base64 解码)"""
        import base64
        res = self.request("GET", f"https://api.github.com/repos/{full_name}/readme")
        if res and isinstance(res, dict) and "content" in res:
            try:
                # 兼容不同换行符并解码
                content = res["content"].replace("\n", "")
                return base64.b64decode(content).decode("utf-8", errors="ignore")
            except Exception as e:
                logger.error(f"解码 README 失败 {full_name}: {e}")
        return ""

github_client = GitHubClient()
