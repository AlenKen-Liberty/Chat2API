"""
Chat2API - Perplexity Provider
Cookie-based HTTP client for Perplexity AI
"""

import json
import uuid
from pathlib import Path
from typing import Dict, Generator, Optional
import httpx


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
            raise ValueError("Must provide either cookies dict or cookies_file")
        
        # Create HTTP client
        self.client = httpx.Client(
            cookies=self.cookies,
            headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
                'Accept': 'text/event-stream',
                'Accept-Language': 'en-US,en;q=0.9',
                'Content-Type': 'application/json',
                'Origin': 'https://www.perplexity.ai',
                'Referer': 'https://www.perplexity.ai/',
                'sec-ch-ua': '"Not(A:Brand";v="8", "Chromium";v="144"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Linux"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
            },
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
        # Generate UUIDs
        frontend_uuid = str(uuid.uuid4())
        backend_uuid = str(uuid.uuid4())
        read_write_token = str(uuid.uuid4())
        
        # Build request payload
        payload = {
            "params": {
                "last_backend_uuid": backend_uuid,
                "read_write_token": read_write_token,
                "attachments": [],
                "language": "en-US",
                "timezone": "America/New_York",
                "search_focus": "internet",
                "sources": ["web"],
                "frontend_uuid": frontend_uuid,
                "mode": "copilot",
                "model_preference": model,
                "is_related_query": False,
                "is_sponsored": False,
                "prompt_source": "user",
                "query_source": "default",
                "is_incognito": False,
                "use_schematized_api": True,
                "send_back_text_in_streaming_api": False,
                "supported_block_use_cases": [
                    "answer_modes", "media_items", "knowledge_cards",
                    "inline_entity_cards", "search_result_widgets"
                ],
                "version": "2.18"
            },
            "query_str": query
        }
        
        # Send request
        with self.client.stream('POST', self.API_URL, json=payload) as response:
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
