import logging
import requests
import json
import re
import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta
from .config import load_config

logger = logging.getLogger(__name__)

class NotionClient:
    """
    Advanced Notion API Client for GitHub Alpha Reports.
    Supports Year/Month/Day hierarchy and rich block types (Callouts, Toggles, etc.)
    """

    def __init__(self):
        self.settings = load_config()
        self.integration_token = self.settings["notion"].get("token", "")
        self.parent_page_id = self.settings["notion"].get("page_id", "")
        self.base_url = "https://api.notion.com/v1"
        self.version = "2022-06-28"

        if not self.integration_token:
            logger.warning("Notion Integration Token missing.")
        if not self.parent_page_id:
            logger.warning("Notion Parent Page ID missing.")

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.integration_token}",
            "Content-Type": "application/json",
            "Notion-Version": self.version
        }

    def _make_request(self, method: str, endpoint: str, data: Dict = None) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        headers = self._get_headers()
        try:
            if method.upper() == "GET":
                response = requests.get(url, headers=headers, timeout=30)
            elif method.upper() == "POST":
                response = requests.post(url, headers=headers, json=data, timeout=30)
            elif method.upper() == "PATCH":
                response = requests.patch(url, headers=headers, json=data, timeout=30)
            else:
                raise ValueError(f"Unsupported method: {method}")

            if response.status_code >= 400:
                logger.error(f"Notion API Error Body: {response.text}")
            response.raise_for_status()
            return {"success": True, "data": response.json()}
        except Exception as e:
            logger.error(f"Notion API Request Failed: {e}")
            return {"success": False, "error": str(e)}

    # --- Hierarchical Page Management ---

    def get_page_children(self, page_id: str) -> Dict[str, Any]:
        return self._make_request("GET", f"blocks/{page_id}/children")

    def create_page(self, parent_id: str, title: str, content_blocks: List[Dict] = None) -> Dict[str, Any]:
        data = {
            "parent": {"page_id": parent_id},
            "properties": {
                "title": {"title": [{"text": {"content": title}}]}
            }
        }
        if content_blocks:
            # Notion limit: 100 blocks per request
            data["children"] = content_blocks[:100]
        return self._make_request("POST", "pages", data)

    def find_or_create_child_page(self, parent_id: str, target_title: str) -> Optional[str]:
        """Generic helper to find or create a nested page by title."""
        res = self.get_page_children(parent_id)
        if res.get("success"):
            for child in res["data"].get("results", []):
                if child.get("type") == "child_page":
                    if child["child_page"].get("title") == target_title:
                        return child["id"]
        
        # Create if not found
        create_res = self.create_page(parent_id, target_title)
        if create_res.get("success"):
            return create_res["data"]["id"]
        return None

    # --- Rich Text & Block Construction ---

    def _parse_rich_text(self, text: str) -> List[Dict]:
        """
        Advanced parsing for links, bold, italic, and inline code.
        Supports nested bold/italic inside links.
        """
        if not text:
            return [{"type": "text", "text": {"content": ""}}]

        if len(text) > 2000:
            text = text[:1997] + "..."

        rich_text = []
        # Combined pattern for link, bold, italic, code
        combined_pattern = r'(\[([^\]]+)\]\((https?://[^)]+)\))|(\*\*(.*?)\*\*)|(\*(.*?)\*)|(`([^`]+)`)'
        last_end = 0

        for match in re.finditer(combined_pattern, text):
            # Text before match
            if match.start() > last_end:
                content = text[last_end:match.start()]
                if content:
                    rich_text.append({"type": "text", "text": {"content": content}})

            if match.group(1):  # Link [text](url)
                link_text = match.group(2)
                url = match.group(3)
                nested_items = self._parse_nested_formatting(link_text)
                for item in nested_items:
                    item["text"]["link"] = {"url": url}
                    rich_text.append(item)
            elif match.group(4):  # Bold **text**
                rich_text.append({"type": "text", "text": {"content": match.group(5)}, "annotations": {"bold": True}})
            elif match.group(6):  # Italic *text*
                rich_text.append({"type": "text", "text": {"content": match.group(7)}, "annotations": {"italic": True}})
            elif match.group(8):  # Code `text`
                rich_text.append({"type": "text", "text": {"content": match.group(9)}, "annotations": {"code": True}})

            last_end = match.end()
            # Notion limit: 100 items per rich_text array
            if len(rich_text) >= 95: break

        if last_end < len(text) and len(rich_text) < 100:
            rich_text.append({"type": "text", "text": {"content": text[last_end:]}})

        return rich_text if rich_text else [{"type": "text", "text": {"content": ""}}]

    def _parse_nested_formatting(self, text: str) -> List[Dict]:
        """Helper to parse bold/italic/code within a string (e.g. inside a link)."""
        items = []
        nested_pattern = r'(\*\*(.*?)\*\*)|(\*(.*?)\*)|(`([^`]+)`)'
        last_end = 0
        for m in re.finditer(nested_pattern, text):
            if m.start() > last_end:
                content = text[last_end:m.start()]
                if content:
                    items.append({"type": "text", "text": {"content": content}})
            
            if m.group(1): # Bold
                items.append({"type": "text", "text": {"content": m.group(2)}, "annotations": {"bold": True}})
            elif m.group(3): # Italic
                items.append({"type": "text", "text": {"content": m.group(4)}, "annotations": {"italic": True}})
            elif m.group(5): # Code
                items.append({"type": "text", "text": {"content": m.group(6)}, "annotations": {"code": True}})
            last_end = m.end()
            if len(items) >= 90: break # Keep safety margin
            
        if last_end < len(text) and len(items) < 100:
            items.append({"type": "text", "text": {"content": text[last_end:]}})
        
        if not items:
            items = [{"type": "text", "text": {"content": text}}]
        return items

    def markdown_to_blocks(self, markdown: str) -> List[Dict]:
        """
        Parses Markdown into basic Notion blocks.
        Supports: H1, H2, H3, Dividers, Quotes, Bulleted Lists, and Tables.
        """
        blocks = []
        lines = markdown.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            
            try:
                if line.startswith('# '):
                    blocks.append({"object": "block", "type": "heading_1", "heading_1": {"rich_text": self._parse_rich_text(line[2:])}})
                elif line.startswith('## '):
                    blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": self._parse_rich_text(line[3:])}})
                elif line.startswith('### '):
                    blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": self._parse_rich_text(line[4:])}})
                elif line.startswith('---'):
                    blocks.append({"object": "block", "type": "divider", "divider": {}})
                elif line.startswith('> '):
                    # Multi-line quote
                    quote_lines = [line[2:]]
                    j = i + 1
                    while j < len(lines):
                        if lines[j].startswith('> '):
                            quote_lines.append(lines[j][2:])
                            j += 1
                        elif not lines[j].strip(): j += 1
                        else: break
                    quote_text = ' '.join(ql.strip() for ql in quote_lines if ql.strip())
                    if quote_text:
                        blocks.append({"object": "block", "type": "quote", "quote": {"rich_text": self._parse_rich_text(quote_text)}})
                    i = j - 1
                elif line.startswith(('- ', '* ')):
                    blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": self._parse_rich_text(line[2:])}})
                elif '|' in line and line.count('|') >= 2:
                    table_lines = []
                    while i < len(lines):
                        curr = lines[i].strip()
                        if '|' in curr and curr.count('|') >= 2: table_lines.append(curr)
                        elif not curr: pass
                        else: break
                        i += 1
                    i -= 1
                    self._process_table_to_blocks(table_lines, blocks)
                else:
                    blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": self._parse_rich_text(line)}})
            except Exception as e:
                logger.warning(f"Failed to parse line: {line[:50]}... Error: {e}")
                blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]}})
            i += 1
        return blocks

    def _process_table_to_blocks(self, table_lines: List[str], blocks: List[Dict]):
        table_rows = []
        headers = None
        for line in table_lines:
            cells = [cell.strip() for cell in line.split('|')[1:-1]]
            if not cells or all(all(c in '-: ' for c in cell) for cell in cells if cell): continue
            if headers is None: headers = cells
            else: table_rows.append(cells)
        
        if not headers: return
        
        # Split large tables
        if len(table_rows) > 99:
            for j in range(0, len(table_rows), 99):
                chunk = table_rows[j:j+99]
                self._create_single_notion_table(headers, chunk, blocks)
        else:
            self._create_single_notion_table(headers, table_rows, blocks)

    def _create_single_notion_table(self, headers: List[str], rows: List[List[str]], blocks: List[Dict]):
        table_children = []
        table_children.append({"type": "table_row", "table_row": {"cells": [self._parse_rich_text(h) for h in headers]}})
        for row in rows:
            row_data = (row + [""] * len(headers))[:len(headers)]
            table_children.append({"type": "table_row", "table_row": {"cells": [self._parse_rich_text(c) for c in row_data]}})
        
        blocks.append({
            "object": "block", "type": "table",
            "table": {
                "table_width": len(headers), "has_column_header": True, "children": table_children
            }
        })

    def _is_valid_block(self, block: Dict) -> bool:
        """检查 Block 字典是否符合 Notion API 的严格规范"""
        if not block or "type" not in block: return False
        block_type = block["type"]
        
        # 核心数据对象必须存在
        if block_type not in block: return False
        content = block[block_type]
        
        # 大多数 Block 类型（段落、标题、列表项等）都需要 rich_text 字段
        rich_text_types = [
            "paragraph", "heading_1", "heading_2", "heading_3", 
            "bulleted_list_item", "numbered_list_item", "quote", "callout", "toggle"
        ]
        
        if block_type in rich_text_types:
            if "rich_text" not in content:
                # 托底处理：如果缺失 rich_text，补一个空的
                content["rich_text"] = [{"type": "text", "text": {"content": ""}}]
            elif not isinstance(content["rich_text"], list):
                return False
        
        # 递归检查折叠模块和提示模块的子节点
        if block_type in ["toggle", "callout"]:
            children = content.get("children", [])
            if children:
                valid_children = [c for c in children if self._is_valid_block(c)]
                # 如果过滤后子节点变少或变空，更新它
                content["children"] = valid_children
                
        # 表格类型的特殊校验
        if block_type == "table":
            if "children" not in content or not content["children"]:
                return False
            for row in content["children"]:
                if row.get("type") != "table_row": return False
        
        return True

    def _append_blocks_to_page(self, page_id: str, blocks: List[Dict]):
        """将 Block 列表追加到页面，采用分批处理以提高速度"""
        # 过滤掉非法的 Block
        valid_blocks = [b for b in blocks if self._is_valid_block(b)]
        if not valid_blocks: return

        # 每批处理 10 个顶级 Block。
        # Notion 的限制是每请求总计 100 个 Block（含嵌套）。
        # 由于我们的折叠模块可能包含 30-40 个子 Block，10 是一个比较安全的数值。
        batch_size = 10
        for i in range(0, len(valid_blocks), batch_size):
            batch = valid_blocks[i:i + batch_size]
            res = self._make_request("PATCH", f"blocks/{page_id}/children", {"children": batch})
            if not res.get("success"):
                logger.error(f"追加第 {i} 批 Block 失败: {res.get('error')}")
                # 如果整批失败，尝试逐个重试，以定位具体有问题的 Block
                for single_block in batch:
                    self._make_request("PATCH", f"blocks/{page_id}/children", {"children": [single_block]})
            time.sleep(0.3) 

    def push_report(self, markdown_content: str, title: str):
        """Traditional Markdown-based push."""
        content_blocks = self.markdown_to_blocks(markdown_content)
        return self.push_blocks(content_blocks, title)

    def push_blocks(self, blocks: List[Dict], title: str):
        """Generic block-based push. Creates page first and then appends content."""
        if not self.integration_token or not self.parent_page_id:
            return False

        now = datetime.now(timezone(timedelta(hours=8)))
        year_id = self.find_or_create_child_page(self.parent_page_id, str(now.year))
        month_id = self.find_or_create_child_page(year_id, f"{now.month:02d}月")
        day_id = self.find_or_create_child_page(month_id, f"{now.day:02d}日")

        report_title = f"{title} [{now.strftime('%H:%M')}]"
        
        # Create an empty page first
        res = self.create_page(day_id, report_title, [])
        
        if res.get("success"):
            page_id = res["data"]["id"]
            if blocks:
                self._append_blocks_to_page(page_id, blocks)
            logger.info(f"Report pushed to Notion: {report_title}")
            return True
        return False

notion_client = NotionClient()
