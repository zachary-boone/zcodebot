"""查询重写模块（优化中文查询匹配英文代码）

解决的问题：
  - 用户输入中文："找处理登录的代码"
  - BM25分词后中文token与英文代码token不匹配
  - 导致BM25检索命中率低

解决方案：
  - 用LLM将中文查询重写为英文关键词
  - 扩展同义词，增加匹配概率
  - 缓存重写结果，减少重复调用
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# 中文字符检测正则
_CHINESE_RE = re.compile(r'[\u4e00-\u9fff]')

# 常见编程术语中英文映射（本地快速匹配，无需LLM）
_TERM_MAPPING: dict[str, list[str]] = {
    # 认证相关
    "登录": ["login", "signin", "sign in", "authenticate", "auth", "log in"],
    "登出": ["logout", "signout", "sign out", "log out"],
    "注册": ["register", "signup", "sign up", "create account"],
    "验证": ["verify", "validate", "check", "confirm", "authentication"],
    "鉴权": ["authenticate", "auth", "authorize", "authorization"],
    "权限": ["permission", "access", "privilege", "right", "authorization"],
    "令牌": ["token", "jwt", "access token", "refresh token"],
    
    # 数据操作
    "查询": ["query", "search", "find", "get", "fetch", "retrieve"],
    "创建": ["create", "add", "new", "insert", "generate"],
    "更新": ["update", "modify", "edit", "change", "set"],
    "删除": ["delete", "remove", "drop", "destroy"],
    "保存": ["save", "store", "persist", "write"],
    "加载": ["load", "fetch", "get", "retrieve"],
    
    # 用户相关
    "用户": ["user", "account", "profile", "member", "customer"],
    "管理员": ["admin", "administrator", "manager", "superuser"],
    "账户": ["account", "user account"],
    
    # 文件相关
    "文件": ["file", "document", "attachment", "resource"],
    "目录": ["directory", "dir", "folder", "path"],
    "读取": ["read", "load", "get", "fetch"],
    "写入": ["write", "save", "store", "output"],
    
    # 错误处理
    "错误": ["error", "exception", "fault", "bug", "issue", "failure"],
    "异常": ["exception", "error", "anomaly"],
    "失败": ["fail", "failure", "error", "unsuccessful"],
    "成功": ["success", "succeed", "complete", "ok"],
    
    # 配置相关
    "配置": ["config", "configuration", "settings", "preferences"],
    "设置": ["settings", "config", "setup", "preferences"],
    
    # 数据库相关
    "数据库": ["database", "db", "data store", "repository"],
    "表": ["table", "collection", "model"],
    "字段": ["field", "column", "property", "attribute"],
    
    # 网络相关
    "请求": ["request", "query", "call"],
    "响应": ["response", "reply", "result"],
    "接口": ["api", "endpoint", "interface", "service"],
    
    # 其他常见
    "处理": ["handle", "process", "manage", "deal with"],
    "初始化": ["initialize", "init", "setup", "bootstrap"],
    "销毁": ["destroy", "cleanup", "dispose", "teardown"],
    "缓存": ["cache", "caching"],
    "日志": ["log", "logging", "logger"],
    "监控": ["monitor", "watch", "observe"],
    "测试": ["test", "testing", "spec", "unittest"],
}


@dataclass
class RewriteResult:
    """查询重写结果"""
    original: str           # 原始查询
    rewritten: str          # 重写后的查询（用于BM25）
    method: str             # 重写方法：local_mapping / llm / hybrid
    cached: bool = False    # 是否命中缓存
    rewrite_time_ms: float = 0.0  # 重写耗时


class QueryRewriteCache:
    """查询重写缓存"""
    
    def __init__(self, ttl: int = 3600, max_size: int = 1000):
        """
        Args:
            ttl: 缓存过期时间（秒）
            max_size: 最大缓存条目数
        """
        self._cache: dict[str, dict[str, Any]] = {}
        self._ttl = ttl
        self._max_size = max_size
    
    def _make_key(self, query: str) -> str:
        """生成缓存键"""
        return hashlib.md5(query.strip().lower().encode()).hexdigest()
    
    def get(self, query: str) -> str | None:
        """获取缓存的重写结果"""
        key = self._make_key(query)
        if key in self._cache:
            entry = self._cache[key]
            if time.time() - entry["timestamp"] < self._ttl:
                return entry["rewritten"]
            else:
                # 过期，删除
                del self._cache[key]
        return None
    
    def set(self, query: str, rewritten: str) -> None:
        """缓存重写结果"""
        # 如果缓存满了，删除最旧的条目
        if len(self._cache) >= self._max_size:
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k]["timestamp"])
            del self._cache[oldest_key]
        
        key = self._make_key(query)
        self._cache[key] = {
            "rewritten": rewritten,
            "timestamp": time.time()
        }
    
    def clear(self) -> None:
        """清空缓存"""
        self._cache.clear()


class QueryRewriter:
    """查询重写器
    
    支持三种重写方式：
    1. 本地术语映射（快速，无API调用）
    2. LLM重写（准确，需要API调用）
    3. 混合模式（先本地，再LLM补充）
    """
    
    def __init__(
        self,
        llm_client: Any = None,
        cache_ttl: int = 3600,
        enable_local_mapping: bool = True,
        enable_llm_rewrite: bool = True,
    ):
        """
        Args:
            llm_client: LLM客户端（用于LLM重写）
            cache_ttl: 缓存过期时间（秒）
            enable_local_mapping: 是否启用本地术语映射
            enable_llm_rewrite: 是否启用LLM重写
        """
        self._llm = llm_client
        self._cache = QueryRewriteCache(ttl=cache_ttl)
        self._enable_local = enable_local_mapping
        self._enable_llm = enable_llm_rewrite
    
    def _contains_chinese(self, text: str) -> bool:
        """检测是否包含中文"""
        return bool(_CHINESE_RE.search(text))
    
    def _local_rewrite(self, query: str) -> str:
        """本地术语映射重写
        
        快速将中文术语替换为英文，无需API调用
        """
        expanded_terms = []
        
        # 逐字符检查是否在映射表中
        # 简单实现：检查每个中文词是否在映射表中
        remaining = query
        for chinese, english_list in _TERM_MAPPING.items():
            if chinese in remaining:
                expanded_terms.extend(english_list)
                remaining = remaining.replace(chinese, "")
        
        # 如果没有匹配到任何术语，返回原查询
        if not expanded_terms:
            return query
        
        # 去重并返回
        unique_terms = list(dict.fromkeys(expanded_terms))
        return " ".join(unique_terms)
    
    async def _llm_rewrite(self, query: str) -> str:
        """LLM重写查询
        
        使用LLM将中文查询转换为英文关键词
        """
        if not self._llm:
            return query
        
        prompt = f"""将以下中文代码搜索查询转换为英文关键词，用于代码检索。

原始查询：{query}

要求：
1. 翻译为英文
2. 扩展相关的编程术语和同义词
3. 保留原始语义
4. 返回逗号分隔的关键词列表

示例：
输入："找处理登录的代码"
输出："login, authentication, user login, sign in, auth, verify credentials, session management"

输入："如何处理错误"
输出："error handling, exception handling, error processing, try catch, error recovery"

请直接返回关键词列表，不要解释："""
        
        try:
            response = await self._llm.chat_completion([
                {"role": "user", "content": prompt}
            ])
            return response.content.strip()
        except Exception as e:
            log.warning("LLM查询重写失败: %s", e)
            return query
    
    async def rewrite(self, query: str) -> RewriteResult:
        """重写查询
        
        Args:
            query: 原始查询（可能是中文）
            
        Returns:
            RewriteResult: 重写结果
        """
        start_time = time.time()
        
        # 1. 检查缓存
        cached = self._cache.get(query)
        if cached:
            return RewriteResult(
                original=query,
                rewritten=cached,
                method="cache",
                cached=True,
                rewrite_time_ms=(time.time() - start_time) * 1000
            )
        
        # 2. 检测是否需要重写
        if not self._contains_chinese(query):
            # 不包含中文，无需重写
            return RewriteResult(
                original=query,
                rewritten=query,
                method="no_rewrite",
                cached=False,
                rewrite_time_ms=(time.time() - start_time) * 1000
            )
        
        # 3. 执行重写
        rewritten = query
        method = "no_rewrite"
        
        # 3.1 本地术语映射
        if self._enable_local:
            local_result = self._local_rewrite(query)
            if local_result != query:
                rewritten = local_result
                method = "local_mapping"
        
        # 3.2 LLM重写（如果本地映射结果不理想，或启用LLM）
        if self._enable_llm and self._llm:
            # 如果本地映射没有匹配到足够多的术语，用LLM补充
            if method == "no_rewrite" or len(rewritten.split()) < 3:
                llm_result = await self._llm_rewrite(query)
                if llm_result != query:
                    # 合并本地和LLM结果
                    if method == "local_mapping":
                        all_terms = rewritten.split() + llm_result.split()
                        unique_terms = list(dict.fromkeys(all_terms))
                        rewritten = " ".join(unique_terms)
                        method = "hybrid"
                    else:
                        rewritten = llm_result
                        method = "llm"
        
        # 4. 缓存结果
        self._cache.set(query, rewritten)
        
        return RewriteResult(
            original=query,
            rewritten=rewritten,
            method=method,
            cached=False,
            rewrite_time_ms=(time.time() - start_time) * 1000
        )
    
    def get_stats(self) -> dict[str, Any]:
        """获取重写器统计信息"""
        return {
            "cache_size": len(self._cache._cache),
            "enable_local_mapping": self._enable_local,
            "enable_llm_rewrite": self._enable_llm and self._llm is not None,
            "local_terms_count": len(_TERM_MAPPING),
        }
