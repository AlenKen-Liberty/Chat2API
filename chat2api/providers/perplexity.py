"""
Chat2API - Perplexity Provider
Cookie-based HTTP client for Perplexity AI
"""

import json
import uuid
import os
from pathlib import Path
from typing import Dict, Generator, Optional
import httpx


class CookieAuthError(Exception):
    """Raised when Perplexity cookie is missing or no longer valid."""


class PerplexityClient:
    """HTTP client for Perplexity AI using cookie-based authentication"""
    
    API_URL = "https://www.perplexity.ai/rest/sse/perplexity_ask"
    
    def __init__(self, cookies: Optional[Dict[str, str]] = None, cookies_file: Optional[Path] = None):
        """
        Initialize Perplexity client
        
        Args:
            cookies: Dictionary of cookie name->value pairs
            cookies_file: Path to JSON file containing cookies
        """
        if cookies:
            self.cookies = cookies
        elif cookies_file and cookies_file.exists():
            with open(cookies_file, 'r') as f:
                cookie_list = json.load(f)
            self.cookies = {c['name']: c['value'] for c in cookie_list}
        else:
            raise CookieAuthError(
                "未找到可用 cookie。请打开浏览器登录 Perplexity 后重新导出 cookies.json。"
            )
        
        # Create HTTP client
        user_agent = os.getenv(
            "PPLX_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/144.0.0.0 Safari/537.36",
        )
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/event-stream",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://www.perplexity.ai",
            "Referer": "https://www.perplexity.ai/",
        }
        self.client = httpx.Client(
            cookies=self.cookies,
            headers=headers,
            timeout=60.0,
            follow_redirects=True
        )
    
    def ask(self, query: str, model: str = "pplx_pro", stream: bool = True) -> Generator[Dict, None, None]:
        """
        Send a query to Perplexity
        
        Args:
            query: The question to ask
            model: Model to use (pplx_pro, turbo, etc)
            stream: Whether to stream the response
        
        Yields:
            Response chunks as dictionaries
        """
        # Build payload matching current web client shape.
        payload = {
            "version": "2.18",
            "source": "default",
            "frontend_uuid": str(uuid.uuid4()),
            "language": "en-US",
            "timezone": "America/New_York",
            "search_focus": "internet",
            "mode": "concise",
            "is_related_query": False,
            "is_default_related_query": False,
            "visitor_id": "",
            "query_str": query,
            "base_model": model,
            "is_vscode_extension": False,
            "supported_block_use_cases": [
                "answer_modes",
                "media_items",
                "knowledge_cards",
                "inline_entity_cards",
                "search_result_widgets",
            ],
        }
        
        # Send request
        with self.client.stream('POST', self.API_URL, json=payload) as response:
            if response.status_code in (401, 403):
                raise CookieAuthError(
                    "Perplexity cookie 可能已过期或失效。请打开浏览器重新登录 Perplexity，"
                    "然后重新导出 cookies.json。"
                )
            response.raise_for_status()
            
            # Parse SSE stream
            for line in response.iter_lines():
                if line.startswith('data: '):
                    data_str = line[6:]  # Remove 'data: ' prefix
                    if data_str.strip():
                        try:
                            data = json.loads(data_str)
                            yield data
                        except json.JSONDecodeError:
                            yield {'raw': data_str}
    
    def extract_answer(self, chunks: Generator[Dict, None, None]) -> Generator[str, None, None]:
        """
        Extract answer text from response chunks
        
        Args:
            chunks: Generator of response chunks
            
        Yields:
            Text chunks from the answer
        """
        seen_chunks = set()
        
        for chunk in chunks:
            # Look for blocks with markdown content
            if isinstance(chunk, dict) and 'blocks' in chunk:
                for block in chunk['blocks']:
                    # Check for markdown blocks with "ask_text" usage (main answer)
                    if 'markdown_block' in block and block.get('intended_usage') == 'ask_text':
                        markdown = block['markdown_block']
                        
                        # Get chunks from this block
                        text_chunks = markdown.get('chunks', [])
                        for text_chunk in text_chunks:
                            # Only yield if we haven't seen this chunk yet
                            if text_chunk not in seen_chunks:
                                yield text_chunk
                                seen_chunks.add(text_chunk)
    
    def close(self):
        """Close the HTTP client"""
        self.client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
