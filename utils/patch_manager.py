
import os
import json
from typing import Dict, Optional
from datetime import datetime
from config import Config
from agent.log_manager import get_logger

logger = get_logger("PatchManager")

def save_patch_immediately(
    syzid: str,
    patch_id: str,
    func_name: str,
    file_path: str,
    modified_code: str,
    reasoning: str = "",
    temperature: float = 0.0,
    generation_round: int = 0
) -> Optional[str]:
    """
    立即保存生成的补丁到文件系统。
    
    Args:
        syzid: Syzbot 案例 ID
        patch_id: 补丁 ID
        func_name: 目标函数名
        file_path: 目标文件路径
        modified_code: 修改后的代码
        reasoning: 推理过程
        temperature: 生成温度
        generation_round: 生成轮次
    
    Returns:
        保存的补丁文件路径，失败返回 None
    """
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        patch_filename = f"{syzid}_{func_name}_{patch_id}_{timestamp}.patch"
        patch_dir = os.path.join(Config.PATCH_ARCHIVE_DIR, syzid)
        os.makedirs(patch_dir, exist_ok=True)
        patch_filepath = os.path.join(patch_dir, patch_filename)
        
        patch_content = f"""From: AutoPatchAgent
Date: {timestamp}
Subject: [PATCH] Fix for {func_name} in {file_path}

syzid: {syzid}
patch_id: {patch_id}
temperature: {temperature}
generation_round: {generation_round}
reasoning:
{reasoning}

---
{file_path}
"""
        patch_content += modified_code
        
        with open(patch_filepath, 'w', encoding='utf-8') as f:
            f.write(patch_content)
        
        logger.info(f"💾 Patch saved immediately: {patch_filepath}")
        
        metadata = {
            "syzid": syzid,
            "patch_id": patch_id,
            "func_name": func_name,
            "file_path": file_path,
            "timestamp": timestamp,
            "temperature": temperature,
            "generation_round": generation_round,
            "patch_file": patch_filepath
        }
        
        metadata_file = os.path.join(patch_dir, f"{patch_filename}.json")
        with open(metadata_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        
        return patch_filepath
        
    except Exception as e:
        logger.error(f"❌ Failed to save patch: {e}")
        return None

def save_patch_metadata(
    syzid: str,
    patch_data: Dict
) -> Optional[str]:
    """
    保存补丁元数据。
    
    Args:
        syzid: Syzbot 案例 ID
        patch_data: 补丁数据字典
    
    Returns:
        保存的元数据文件路径
    """
    return save_patch_immediately(
        syzid=syzid,
        patch_id=patch_data.get("patch_id", "unknown"),
        func_name=patch_data.get("func_name", "unknown"),
        file_path=patch_data.get("file_path", "unknown"),
        modified_code=patch_data.get("modified_code", ""),
        reasoning=patch_data.get("reasoning", ""),
        temperature=patch_data.get("temperature", 0.0),
        generation_round=patch_data.get("generation_round", 0)
    )

