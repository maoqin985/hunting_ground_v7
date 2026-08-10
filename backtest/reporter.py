"""
猎场系统 V6.0 — 回测报告生成器
"""

import logging
from typing import Dict, List
from datetime import datetime

logger = logging.getLogger(__name__)


class BacktestReporter:
    """回测报告生成"""

    @staticmethod
    def generate_report(result) -> str:
        """生成文本报告"""
        return f"""
============================================================
  猎场系统 V6.0 — 回测绩效报告
============================================================
  总收益率:     {result.total_return:>8.2f}%
  年化收益率:   {result.annual_return:>8.2f}%
  夏普比率:     {result.sharpe_ratio:>8.2f}
  最大回撤:     {result.max_drawdown:>8.2f}%
  胜率:         {result.win_rate:>8.1f}%
  交易次数:     {result.total_trades:>8d}
  盈亏比:       {result.profit_factor:>8.2f}
============================================================
"""

    @staticmethod
    def export_html(result, filepath: str):
        """导出HTML报告"""
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>回测报告</title>
<style>body{{font-family:Arial;max-width:800px;margin:auto;padding:20px}}
.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}
.card{{background:#1a1a2e;color:#e0e0e0;padding:16px;border-radius:8px;text-align:center}}
.card .label{{font-size:12px;opacity:.7}}.card .value{{font-size:28px;font-weight:bold;margin-top:8px}}
.positive{{color:#4caf50}}.negative{{color:#f44336}}</style></head><body>
<h1>猎场 V6.0 回测报告</h1>
<div class="metrics">
<div class="card"><div class="label">总收益率</div><div class="value {'positive' if result.total_return>0 else 'negative'}">{result.total_return:.2f}%</div></div>
<div class="card"><div class="label">年化收益率</div><div class="value {'positive' if result.annual_return>0 else 'negative'}">{result.annual_return:.2f}%</div></div>
<div class="card"><div class="label">夏普比率</div><div class="value">{result.sharpe_ratio:.2f}</div></div>
<div class="card"><div class="label">最大回撤</div><div class="value negative">{result.max_drawdown:.2f}%</div></div>
<div class="card"><div class="label">胜率</div><div class="value">{result.win_rate:.1f}%</div></div>
<div class="card"><div class="label">交易次数</div><div class="value">{result.total_trades}</div></div>
</div>
<p style="color:#666;text-align:center;margin-top:32px">Generated at {datetime.now().isoformat()}</p>
</body></html>"""
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html)
        logger.info(f"HTML report saved to {filepath}")
