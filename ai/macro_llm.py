"""
猎场系统 V6.0 — LLM 宏观推理模块
本地 Ollama + Qwen2.5-7B 推理 / 云端 DeepSeek API 备选
功能：市场状态分类、极端事件识别、每日复盘报告
"""

import logging
import json
from typing import Optional, Dict, List
from datetime import datetime

logger = logging.getLogger(__name__)


class MacroLLM:
    """LLM 宏观分析（本地优先，云端备选）"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        llm_cfg = config.get("llm", {})

        # 本地配置
        local_cfg = llm_cfg.get("local", {})
        self._local_provider = local_cfg.get("provider", "ollama")
        self._local_model = local_cfg.get("model", "qwen2.5:7b")
        self._local_url = local_cfg.get("base_url", "http://localhost:11434")

        # 云端配置
        cloud_cfg = llm_cfg.get("cloud", {})
        self._cloud_provider = cloud_cfg.get("provider", "deepseek")
        self._cloud_model = cloud_cfg.get("model", "deepseek-chat")
        self._cloud_api_key = cloud_cfg.get("api_key", "")
        self._cloud_api_base = cloud_cfg.get("api_base", "https://api.deepseek.com/v1")
        self._cloud_fallback = cloud_cfg.get("fallback", True)

        self._local_available = False
        self._cloud_available = bool(self._cloud_api_key)

    async def check_local(self) -> bool:
        """检查本地 LLM 是否可用"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self._local_url}/api/tags", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        self._local_available = True
                        logger.info(f"Local LLM available: {self._local_model} via {self._local_provider}")
                        return True
        except Exception:
            pass
        logger.debug("Local LLM not available")
        return False

    async def analyze_market(self, market_data: Dict) -> Dict:
        """
        宏观市场分析

        Args:
            market_data: {fear_greed, hs300_change, advance_decline, limit_up, limit_down, ...}

        Returns:
            {market_regime, risk_level, recommendation, reasoning}
        """
        prompt = self._build_market_prompt(market_data)
        response = await self._query(prompt)

        try:
            result = json.loads(response)
            return {
                'market_regime': result.get('market_regime', 'unknown'),
                'risk_level': result.get('risk_level', 'medium'),
                'recommendation': result.get('recommendation', 'hold'),
                'reasoning': result.get('reasoning', ''),
                'confidence': result.get('confidence', 0.5),
            }
        except json.JSONDecodeError:
            # 非JSON输出时，基于关键词判断
            return {
                'market_regime': 'unknown',
                'risk_level': 'medium',
                'recommendation': 'hold',
                'reasoning': response[:500] if response else '',
                'confidence': 0.3,
            }

    async def generate_daily_review(self, trades: List[Dict], pnl: Dict,
                                     signals: List[Dict]) -> str:
        """生成每日复盘报告"""
        prompt = self._build_review_prompt(trades, pnl, signals)
        return await self._query(prompt)

    def _build_market_prompt(self, data: Dict) -> str:
        """构造市场分析提示词"""
        return f"""你是一位专业的A股市场分析师。请根据以下数据判断当前市场状态并给出建议。

数据：
- 恐贪指数: {data.get('fear_greed', 50)}
- 沪深300涨跌: {data.get('hs300_change', 0):.2f}%
- 涨跌家数比: {data.get('advance_decline', 1.0):.2f}
- 涨停家数: {data.get('limit_up', 0)}
- 跌停家数: {data.get('limit_down', 0)}
- 成交额: {data.get('total_amount', 0):.0f}亿

请以JSON格式回答：
{{
    "market_regime": "bull/ bear/ range/ volatile/ extreme_fear/ extreme_greed",
    "risk_level": "low/ medium/ high/ extreme",
    "recommendation": "aggressive_buy/ buy/ hold/ reduce/ sell_all",
    "reasoning": "简短的分析理由（50字以内）",
    "confidence": 0.0-1.0
}}

只输出JSON，不要其他内容。"""

    def _build_review_prompt(self, trades: List[Dict], pnl: Dict,
                              signals: List[Dict]) -> str:
        """构造每日复盘提示词"""
        trade_list = '\n'.join([
            f"- {t.get('ts_code','')} {t.get('direction','')} "
            f"价格{t.get('price',0):.2f} 盈亏{t.get('pnl',0):.2f}元({t.get('pnl_pct',0):.2f}%)"
            for t in (trades or [])[-10:]
        ])

        signal_list = '\n'.join([
            f"- {s.get('ts_code','')} 评分{s.get('score',0)} {s.get('reason','')}"
            for s in (signals or [])[-5:]
        ])

        return f"""作为量化交易系统的AI复盘助手，请对今日交易进行分析：

今日盈亏: {pnl.get('daily_pnl', 0):.2f}元 ({pnl.get('daily_pnl_pct', 0):.2f}%)
累计盈亏: {pnl.get('total_pnl', 0):.2f}元 ({pnl.get('total_pnl_pct', 0):.2f}%)
持仓数: {pnl.get('position_count', 0)}

今日交易:
{trade_list or '无交易'}

买入信号:
{signal_list or '无信号'}

请简要分析（200字以内）：
1. 今日得失总结
2. 需要关注的风险点
3. 明日建议"""

    async def _query(self, prompt: str) -> str:
        """查询 LLM（本地优先，失败时切换云端）"""
        # 先试本地
        if self._local_available:
            result = await self._query_ollama(prompt)
            if result:
                return result

        # 再试云端
        if self._cloud_fallback and self._cloud_available:
            result = await self._query_deepseek(prompt)
            if result:
                return result

        logger.warning("Neither local nor cloud LLM available")
        return ""

    async def _query_ollama(self, prompt: str) -> Optional[str]:
        """Ollama 本地推理"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": self._local_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 512},
                }
                timeout = aiohttp.ClientTimeout(total=30)
                async with session.post(
                    f"{self._local_url}/api/generate",
                    json=payload,
                    timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("response", "")
        except Exception as e:
            logger.debug(f"Ollama query error: {e}")
            self._local_available = False
        return None

    async def _query_deepseek(self, prompt: str) -> Optional[str]:
        """DeepSeek 云端备选"""
        try:
            import aiohttp
            headers = {
                "Authorization": f"Bearer {self._cloud_api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self._cloud_model,
                "messages": [
                    {"role": "system", "content": "你是一位专业的A股量化分析师。请用JSON格式回答。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 512,
            }
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._cloud_api_base}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data['choices'][0]['message']['content']
                    else:
                        logger.warning(f"DeepSeek API error: {resp.status}")
        except Exception as e:
            logger.debug(f"DeepSeek query error: {e}")
        return None
