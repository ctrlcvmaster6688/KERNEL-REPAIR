# utils/llm_interface.py
import time
import sys
import os
import logging
import traceback
from openai import OpenAI, APIError, RateLimitError, APIConnectionError, APITimeoutError

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import Config

logger = logging.getLogger("LLM_Interface")

_client = None
_token_stats = {
    "total_prompt_tokens": 0,
    "total_completion_tokens": 0,
    "total_requests": 0,
    "by_model": {}
}

def _get_client():
    """获取或创建 OpenAI 客户端"""
    global _client
    if _client is None:
        if not Config.LLM_API_KEY:
            raise RuntimeError(
                "LLM_API_KEY is not set. Please export LLM_API_KEY before running the agent."
            )
        _client = OpenAI(
            api_key=Config.LLM_API_KEY,
            base_url=Config.LLM_BASE_URL,
            timeout=300.0
        )
    return _client

def get_token_stats():
    """获取 token 统计信息"""
    return _token_stats.copy()

def reset_token_stats():
    """重置 token 统计"""
    global _token_stats
    _token_stats = {
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_requests": 0,
        "by_model": {}
    }

def call_llm(prompt: str, system_prompt: str = None, temperature: float = 1.0) -> str:
    """
    调用 LLM API,包含简单的重试逻辑。
    """
    global _token_stats
    
    client = _get_client()
    model_name = Config.LLM_MODEL
    
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    
    total_chars = len(str(messages))
    logger.info(f"[LLM] Calling API: model={model_name}, temp={temperature}, prompt_len={total_chars}")

    max_retries = 3
    for attempt in range(max_retries):
        try:
            logger.info(f"[LLM] Attempt {attempt + 1}/{max_retries}...")
            
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temperature
            )
            
            logger.info(f"[LLM] Response received: id={getattr(response, 'id', 'N/A')}")
            
            if hasattr(response, 'usage') and response.usage:
                prompt_tokens = response.usage.prompt_tokens
                completion_tokens = response.usage.completion_tokens
                
                _token_stats["total_prompt_tokens"] += prompt_tokens
                _token_stats["total_completion_tokens"] += completion_tokens
                _token_stats["total_requests"] += 1
                
                if model_name not in _token_stats["by_model"]:
                    _token_stats["by_model"][model_name] = {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "requests": 0
                    }
                _token_stats["by_model"][model_name]["prompt_tokens"] += prompt_tokens
                _token_stats["by_model"][model_name]["completion_tokens"] += completion_tokens
                _token_stats["by_model"][model_name]["requests"] += 1
                
                logger.info(f"[LLM] Token usage: prompt={prompt_tokens}, completion={completion_tokens}")
            
            if not response.choices:
                logger.warning(f"[LLM] No choices in response")
                continue
            
            content = response.choices[0].message.content
            logger.info(f"[LLM] Content length: {len(content)} chars")
            
            return content

        except RateLimitError as e:
            wait_time = 2 ** (attempt + 1)
            logger.warning(f"[LLM] Rate limit hit, retry in {wait_time}s: {e}")
            time.sleep(wait_time)
            
        except APIConnectionError as e:
            logger.error(f"[LLM] Connection error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return ""
            time.sleep(2)
            
        except APITimeoutError as e:
            logger.error(f"[LLM] Timeout error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return ""
            time.sleep(2)
            
        except APIError as e:
            logger.error(f"[LLM] API error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return ""
            time.sleep(2)
            
        except Exception as e:
            logger.error(f"[LLM] Unknown error (attempt {attempt + 1}/{max_retries}): {type(e).__name__}: {e}")
            logger.error(f"[LLM] Traceback: {traceback.format_exc()}")
            if attempt == max_retries - 1:
                return ""
            time.sleep(1)

    logger.error(f"[LLM] All {max_retries} attempts failed")
    return ""
