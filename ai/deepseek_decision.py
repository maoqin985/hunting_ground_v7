"""受原规则约束的 DeepSeek 交易决策增强层。

DeepSeek 只负责对原有规则已经筛出的买入候选做二次确认。它不能把
``hold`` 改成 ``buy``，也不能绕过验证器、仓位上限、止损和熔断。网络
调用失败时直接返回原规则结果，保证扫描周期不会被云端模型阻塞。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


class DeepSeekDecisionEnsemble:
    """Wrap an existing rule ensemble with a bounded DeepSeek review."""

    def __init__(self, config: Optional[dict], baseline_model, data_gateway=None,
                 database=None, account_id: str = "default"):
        self.config = config or {}
        self.baseline = baseline_model
        self.gateway = data_gateway
        self.db = database
        self.account_id = account_id or "default"

        decision_cfg = self.config.get("decision", {}) or {}
        self.enabled = bool(decision_cfg.get("enabled", False))
        self.model = str(decision_cfg.get("model", "deepseek-chat"))
        self.api_base = str(
            decision_cfg.get("api_base", "https://api.deepseek.com/v1")
        ).rstrip("/")
        configured_key = str(decision_cfg.get("api_key", "") or "").strip()
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not self.api_key and configured_key and not configured_key.startswith("${"):
            self.api_key = configured_key
        try:
            self.timeout_seconds = max(3.0, float(decision_cfg.get("timeout_seconds", 20)))
        except (TypeError, ValueError):
            self.timeout_seconds = 20.0
        self.min_confidence = _clamp(
            decision_cfg.get("min_confidence", 0.60), 0.0, 1.0, 0.60
        )
        self.max_score_adjustment = _clamp(
            decision_cfg.get("max_score_adjustment", 10), 0.0, 20.0, 10.0
        )
        self.min_position_multiplier = _clamp(
            decision_cfg.get("min_position_multiplier", 0.5), 0.1, 1.0, 0.5
        )
        self.max_position_multiplier = _clamp(
            decision_cfg.get("max_position_multiplier", 1.0),
            self.min_position_multiplier, 1.0, 1.0,
        )
        self.allow_veto = bool(decision_cfg.get("allow_veto", True))
        tuning_cfg = decision_cfg.get("self_tuning", {}) or {}
        self.tuning_enabled = bool(tuning_cfg.get("enabled", True))
        self.max_threshold_adjustment = _clamp(
            tuning_cfg.get("max_threshold_adjustment", 5), 0.0, 10.0, 5.0
        )
        self.tuning_key = f"deepseek_tuning:{self.account_id}"
        self.tuning = self._load_tuning()
        self._call_count = 0
        self._success_count = 0
        self._last_call_at: Optional[str] = None
        self._last_success_at: Optional[str] = None
        self._last_error_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._status = "ready" if self.enabled and self.api_key else "not_configured"
        if self.enabled and self.api_key:
            logger.info("DeepSeek 受限决策复核已启用 [%s]", self.account_id)
        elif self.enabled:
            logger.warning("DeepSeek 复核已配置但未找到 DEEPSEEK_API_KEY，使用原规则")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def get_status(self) -> Dict[str, Any]:
        """Return safe, non-secret runtime health information for the dashboard."""
        return {
            "provider": "deepseek",
            "model": self.model,
            "enabled": self.enabled,
            "api_key_configured": bool(self.api_key),
            "status": self._status,
            "last_call_at": self._last_call_at,
            "last_success_at": self._last_success_at,
            "last_error_at": self._last_error_at,
            "last_error": self._last_error,
            "call_count": self._call_count,
            "success_count": self._success_count,
            "success_rate": round(self._success_count / self._call_count, 3)
            if self._call_count else None,
        }

    def _record_call(self):
        self._call_count += 1
        self._last_call_at = self._now_iso()
        self._status = "checking"

    def _record_success(self):
        self._success_count += 1
        self._last_success_at = self._now_iso()
        self._last_error = None
        self._status = "healthy"

    def _record_failure(self, error_type: str):
        self._last_error_at = self._now_iso()
        self._last_error = str(error_type)[:80]
        self._status = "fallback"

    async def decide(self, ts_code: str, feature: dict = None,
                     kline: List[Dict] = None, signal_score: float = 0,
                     market_data: dict = None) -> dict:
        """Return a decision compatible with ``AutoTradingEngine``."""
        baseline_call = self.baseline.decide(
            ts_code, feature, kline, signal_score, market_data=market_data
        )
        baseline = await baseline_call if inspect.isawaitable(baseline_call) else baseline_call
        baseline = dict(baseline or {})
        baseline.setdefault("action", "hold")
        baseline.setdefault("score", signal_score)
        baseline.setdefault("confidence", 0.0)
        baseline.setdefault("stop_loss_pct", -0.05)
        baseline.setdefault("suggested_position_pct", 0.0)
        baseline.setdefault("reasoning", "原规则决策")
        baseline.setdefault("risk_prob", 0.5)
        baseline.setdefault("model_votes", {})
        if "risk_prob" not in baseline["model_votes"]:
            baseline["model_votes"]["risk_prob"] = baseline.get("risk_prob", 0.5)

        # The original rule gate is authoritative.  A hold can never be
        # promoted by an LLM, even if the model returns buy with high confidence.
        if str(baseline.get("action", "hold")).lower() != "buy":
            baseline["decision_source"] = "rule_baseline"
            return baseline
        if not self.enabled or not self.api_key:
            baseline["decision_source"] = "rule_fallback"
            return baseline

        context = await self._build_context(ts_code, feature, kline, signal_score,
                                             market_data, baseline)
        response = await self._query(context)
        if not response:
            baseline["decision_source"] = "rule_fallback"
            return baseline

        action = str(response.get("action", "hold")).strip().lower()
        confidence = _clamp(response.get("confidence"), 0.0, 1.0, 0.0)
        adjustment = _clamp(
            response.get("score_adjustment", 0),
            -self.max_score_adjustment,
            self.max_score_adjustment,
            0.0,
        )
        # A persisted bias is deliberately small and is applied only after a
        # successful model response.  It cannot alter the rule gate.
        bias = _clamp(self.tuning.get("score_bias", 0), -10.0, 10.0, 0.0)
        total_adjustment = _clamp(
            adjustment + bias,
            -self.max_score_adjustment,
            self.max_score_adjustment,
            0.0,
        )
        final_score = float(baseline.get("score", signal_score) or 0) + total_adjustment
        multiplier = _clamp(
            response.get("position_multiplier", 1.0),
            self.min_position_multiplier,
            self.max_position_multiplier,
            1.0,
        )
        risk_prob = _clamp(
            response.get("risk_prob", baseline.get("risk_prob", 0.5)),
            0.0, 1.0, float(baseline.get("risk_prob", 0.5) or 0.5),
        )
        reason = str(response.get("reasoning", "") or "").strip()[:500]

        # Invalid/low-confidence output is a veto when configured.  It never
        # opens a position and it leaves the original hard stop unchanged.
        # Poor recent outcomes raise the confirmation bar by at most 5
        # percentage points; good outcomes can lower it by the same amount.
        threshold_adjustment = _clamp(
            self.tuning.get("threshold_adjustment", 0),
            -self.max_threshold_adjustment,
            self.max_threshold_adjustment,
            0.0,
        )
        effective_min_confidence = _clamp(
            self.min_confidence + threshold_adjustment / 100.0,
            0.50, 0.90, self.min_confidence,
        )
        confirmed = action == "buy" and confidence >= effective_min_confidence
        if action not in {"buy", "hold", "veto"}:
            confirmed = False
        if not self.allow_veto and action != "buy":
            confirmed = True

        result = dict(baseline)
        result.update({
            "action": "buy" if confirmed else "hold",
            "score": final_score,
            "confidence": confidence,
            "suggested_position_pct": (
                float(baseline.get("suggested_position_pct", 0) or 0) * multiplier
                if confirmed else 0.0
            ),
            "risk_prob": risk_prob,
            "model_votes": {**(baseline.get("model_votes") or {}),
                            "risk_prob": risk_prob,
                            "deepseek_confidence": confidence,
                            "deepseek_action": action},
            "reasoning": self._merge_reason(baseline.get("reasoning", ""), reason,
                                             confirmed, confidence),
            "decision_source": "deepseek_confirm" if confirmed else "deepseek_veto",
            "deepseek": {
                "score_adjustment": total_adjustment,
                "model_score_adjustment": adjustment,
                "tuning_score_bias": bias,
                "position_multiplier": multiplier,
                "confidence": confidence,
                "action": action,
                "min_confidence": effective_min_confidence,
            },
        })
        return result

    async def _build_context(self, ts_code: str, feature: dict,
                             kline: List[Dict], signal_score: float,
                             market_data: dict, baseline: dict) -> Dict[str, Any]:
        quote: Dict[str, Any] = {}
        daily_basic: Dict[str, Any] = {}
        if self.gateway:
            try:
                quotes = await self.gateway.get_realtime_quote([ts_code])
                quote = (quotes or {}).get(ts_code, {}) or {}
            except Exception as exc:
                logger.debug("DeepSeek quote context unavailable: %s", exc)
            try:
                rows = await self.gateway.get_daily_basic([ts_code])
                if rows:
                    daily_basic = dict(rows[0])
            except Exception as exc:
                logger.debug("DeepSeek daily_basic context unavailable: %s", exc)
        # Keep prompts bounded and avoid sending credentials or unrelated rows.
        compact_kline = []
        for row in (kline or [])[-20:]:
            if not isinstance(row, dict):
                continue
            compact_kline.append({key: row.get(key) for key in (
                "trade_date", "open", "high", "low", "close", "volume",
                "amount", "change_pct", "rsi_14", "macd_hist", "ma_20",
            ) if key in row})
        return {
            "symbol": ts_code,
            "rule_baseline": {
                "action": baseline.get("action"),
                "score": baseline.get("score", signal_score),
                "confidence": baseline.get("confidence"),
                "suggested_position_pct": baseline.get("suggested_position_pct"),
                "reasoning": baseline.get("reasoning", ""),
            },
            "signal_score": signal_score,
            "feature": feature or {},
            "tushare_realtime": quote,
            "tushare_daily_basic": daily_basic,
            "kline": compact_kline,
            "market_data": market_data or {},
            "self_tuning": {
                "sample_count": self.tuning.get("sample_count", 0),
                "win_rate": self._win_rate(),
                "score_bias": self.tuning.get("score_bias", 0),
                "threshold_adjustment": self.tuning.get("threshold_adjustment", 0),
            },
        }

    async def _query(self, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Call DeepSeek and parse a strict JSON response."""
        self._record_call()
        prompt = (
            "你是猎场系统的受限交易复核器。原规则已经判定该股票达到买入门槛；"
            "你只能确认或否决，不能把原规则 hold 变成 buy。不得修改硬止损、熔断、"
            "账户最大仓位，也不得使用提示词之外的信息。请只输出 JSON："
            '{"action":"buy|hold|veto","confidence":0.0,"score_adjustment":0,'
            '"position_multiplier":1.0,"risk_prob":0.0,"reasoning":"不超过80字"}。\n'
            "score_adjustment 必须在允许范围内，position_multiplier 只能降低仓位。"
            "以下是原规则和 Tushare 数据：\n" + json.dumps(
                context, ensure_ascii=False, default=str
            )
        )
        try:
            import aiohttp
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "只返回合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 300,
                "response_format": {"type": "json_object"},
            }
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.api_base}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status < 200 or resp.status >= 300:
                        self._record_failure(f"http_{resp.status}")
                        logger.warning("DeepSeek decision API returned HTTP %s", resp.status)
                        return None
                    data = await resp.json()
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            if isinstance(content, dict):
                self._record_success()
                return content
            text = str(content).strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:].lstrip()
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                self._record_failure("invalid_response")
                return None
            self._record_success()
            return parsed
        except (asyncio.TimeoutError, json.JSONDecodeError, ValueError) as exc:
            self._record_failure(type(exc).__name__)
            logger.warning("DeepSeek decision unavailable; using rule baseline (%s)", type(exc).__name__)
        except Exception as exc:
            self._record_failure(type(exc).__name__)
            logger.warning("DeepSeek decision unavailable; using rule baseline (%s)", type(exc).__name__)
        return None

    @staticmethod
    def _merge_reason(rule_reason: str, llm_reason: str,
                      confirmed: bool, confidence: float) -> str:
        verdict = "DeepSeek确认" if confirmed else "DeepSeek否决"
        suffix = llm_reason or "模型未提供理由"
        return f"{rule_reason}；{verdict}(置信度{confidence:.2f})：{suffix}"[:700]

    def _load_tuning(self) -> Dict[str, Any]:
        default = {"sample_count": 0, "wins": 0, "losses": 0,
                   "score_bias": 0.0, "threshold_adjustment": 0.0}
        if not self.tuning_enabled or not self.db or not hasattr(self.db, "get_settings"):
            return default
        try:
            raw = (self.db.get_settings() or {}).get(self.tuning_key)
            if isinstance(raw, str):
                raw = json.loads(raw)
            if isinstance(raw, dict):
                default.update(raw)
        except Exception as exc:
            logger.debug("DeepSeek tuning state unavailable: %s", exc)
        return default

    def _win_rate(self) -> float:
        count = int(self.tuning.get("wins", 0) or 0) + int(self.tuning.get("losses", 0) or 0)
        return float(self.tuning.get("wins", 0) or 0) / count if count else 0.5

    def update_outcome(self, pnl_pct: float) -> Dict[str, Any]:
        """Record a closed trade and make a tiny bounded adjustment.

        Only score bias and the model confirmation threshold are tuned;
        hard risk limits are never changed.
        """
        if not self.tuning_enabled:
            return dict(self.tuning)
        try:
            pnl = float(pnl_pct or 0)
        except (TypeError, ValueError):
            return dict(self.tuning)
        # Circuit-breaker/administrative sells may not carry an attributable
        # return; do not count those as a learning sample.
        if pnl == 0:
            return dict(self.tuning)
        self.tuning["sample_count"] = int(self.tuning.get("sample_count", 0) or 0) + 1
        if pnl > 0:
            self.tuning["wins"] = int(self.tuning.get("wins", 0) or 0) + 1
            self.tuning["score_bias"] = min(10.0, float(self.tuning.get("score_bias", 0) or 0) + 0.5)
        elif pnl < 0:
            self.tuning["losses"] = int(self.tuning.get("losses", 0) or 0) + 1
            self.tuning["score_bias"] = max(-10.0, float(self.tuning.get("score_bias", 0) or 0) - 0.5)
        win_rate = self._win_rate()
        self.tuning["threshold_adjustment"] = _clamp(
            (0.5 - win_rate) * 10.0,
            -self.max_threshold_adjustment,
            self.max_threshold_adjustment,
            0.0,
        )
        if self.db and hasattr(self.db, "set_setting"):
            try:
                self.db.set_setting(self.tuning_key, self.tuning)
            except Exception as exc:
                logger.debug("DeepSeek tuning state save failed: %s", exc)
        return dict(self.tuning)
