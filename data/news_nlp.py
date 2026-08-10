"""
猎场系统 V6.0 — 新闻抓取 + NLP 情感分析
数据源: 东方财富新闻
NLP模型: BERT 中文金融情感分类
"""

import re
import logging
from typing import List, Dict, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 利空关键词（不需要模型也能快速过滤）
NEGATIVE_KEYWORDS = [
    "减持", "立案", "亏损", "退市", "ST", "*ST", "问询函", "监管函",
    "处罚", "诉讼", "债务违约", "商誉减值", "业绩暴雷", "财务造假",
    "停牌", "终止上市", "破产", "重组失败", "大股东质押", "平仓",
    "限售股解禁", "大额计提", "预亏", "修正业绩", "下调评级",
]

POSITIVE_KEYWORDS = [
    "业绩预增", "回购", "增持", "分红", "中标", "战略合作",
    "新产品", "获批", "订单", "扭亏", "重组", "资产注入",
]


class NewsNLPService:
    """新闻抓取与NLP分析服务"""

    def __init__(self, database):
        self.db = database
        self._model = None
        self._tokenizer = None
        self._initialized = False

    async def initialize(self):
        """延迟加载 NLP 模型（可选）"""
        try:
            from transformers import pipeline
            self._model = pipeline(
                "text-classification",
                model="uer/roberta-base-finetuned-jd-binary-chinese",
                top_k=None,
            )
            logger.info("NLP model loaded: roberta-base-chinese-sentiment")
        except ImportError:
            logger.warning("transformers not installed, using keyword matching only")
        except Exception as e:
            logger.warning(f"NLP model load failed: {e}, using keyword matching")

        self._initialized = True

    async def fetch_and_analyze(self, ts_codes: List[str] = None) -> List[Dict]:
        """抓取新闻并进行情感分析"""
        news_items = await self._fetch_eastmoney_news(ts_codes)
        analyzed = []

        for item in news_items:
            sentiment = await self._analyze_sentiment(item.get('title', '') + ' ' + item.get('content', ''))
            is_negative = sentiment < -0.3
            item['sentiment'] = sentiment
            item['is_negative'] = 1 if is_negative else 0
            item['keywords'] = self._extract_keywords(item.get('title', '') + item.get('content', ''))
            analyzed.append(item)

        if analyzed:
            self.db.save_news_sentiment(analyzed)
            neg_count = sum(1 for a in analyzed if a['is_negative'])
            logger.info(f"News analyzed: {len(analyzed)} items, {neg_count} negative")

        return analyzed

    async def _fetch_eastmoney_news(self, ts_codes: List[str] = None) -> List[Dict]:
        """从东方财富抓取新闻"""
        import aiohttp

        news_list = []
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 东财个股新闻API
                if ts_codes:
                    for code in ts_codes[:20]:  # 限制数量
                        try:
                            market = '1' if code.startswith('6') else '0'
                            url = f"https://push2.eastmoney.com/api/qt/stock/news/get"
                            params = {
                                'secid': f"{market}.{code}",
                                'page': 1,
                                'size': 3,
                            }
                            async with session.get(url, params=params) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    items = data.get('data', {}).get('list', [])
                                    for item in items:
                                        news_list.append({
                                            'ts_code': code,
                                            'title': item.get('title', ''),
                                            'content': item.get('digest', ''),
                                            'url': item.get('url', ''),
                                            'source': 'eastmoney',
                                            'pub_time': item.get('show_time', ''),
                                        })
                        except Exception:
                            continue

                # 市场要闻
                url = "https://push2.eastmoney.com/api/qt/stock/news/get"
                params = {'secid': '1.000001', 'page': 1, 'size': 20}
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get('data', {}).get('list', []):
                            news_list.append({
                                'ts_code': '',
                                'title': item.get('title', ''),
                                'content': item.get('digest', ''),
                                'url': item.get('url', ''),
                                'source': 'eastmoney_market',
                                'pub_time': item.get('show_time', ''),
                            })

        except ImportError:
            logger.warning("aiohttp not available for news fetching")
        except Exception as e:
            logger.error(f"News fetch error: {e}")

        return news_list

    async def _analyze_sentiment(self, text: str) -> float:
        """情感分析：-1.0(极度负面) 到 1.0(极度正面)"""
        if not text or len(text) < 10:
            return 0.0

        # NLP 模型分析
        if self._model:
            try:
                result = self._model(text[:512])  # 限制输入长度
                if result and result[0]:
                    scores = {r['label']: r['score'] for r in result[0]}
                    # 映射到 [-1, 1]
                    pos = scores.get('positive', scores.get('POSITIVE', 0))
                    neg = scores.get('negative', scores.get('NEGATIVE', 0))
                    return pos - neg
            except Exception:
                pass

        # 关键词兜底
        text_lower = text
        pos_count = sum(1 for kw in POSITIVE_KEYWORDS if kw in text_lower)
        neg_count = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text_lower)

        if neg_count > pos_count:
            return -0.5 - (neg_count - pos_count) * 0.1
        elif pos_count > neg_count:
            return 0.3 + (pos_count - neg_count) * 0.1
        return 0.0

    def _extract_keywords(self, text: str) -> str:
        """提取文本中的关键词"""
        found = []
        for kw in NEGATIVE_KEYWORDS + POSITIVE_KEYWORDS:
            if kw in text:
                found.append(kw)
        return ','.join(found[:5])

    @staticmethod
    def is_negative_stock(ts_code: str, negative_list: List[str]) -> bool:
        """检查股票是否在负面列表中"""
        return ts_code in negative_list
