"""
猎场系统 V6.0 — 回测报告生成器
含换手率、收益来源拆解、样本内/样本外验证（维度六「多维绩效 + 防过拟合」）
"""

import logging
from typing import Dict, List
from datetime import datetime

logger = logging.getLogger(__name__)


def _fmt_pf(profit_factor) -> str:
    """盈亏比可能为 inf，安全格式化"""
    try:
        return f"{profit_factor:.2f}" if profit_factor != float('inf') else "∞"
    except (ValueError, TypeError):
        return "-"


def _metric_block(result, title: str) -> str:
    """生成单段指标文本"""
    attr = result.return_attribution or {}
    return (
        f"  [{title}]\n"
        f"  总收益率:     {result.total_return:>8.2f}%\n"
        f"  年化收益率:   {result.annual_return:>8.2f}%\n"
        f"  夏普比率:     {result.sharpe_ratio:>8.2f}\n"
        f"  最大回撤:     {result.max_drawdown:>8.2f}%\n"
        f"  胜率:         {result.win_rate:>8.1f}%\n"
        f"  交易次数:     {result.total_trades:>8d}\n"
        f"  盈亏比:       {_fmt_pf(result.profit_factor):>8s}\n"
        f"  换手率:       {result.turnover_rate:>8.2f}x\n"
        f"  平均持仓:     {result.avg_hold_days:>8.1f} 天\n"
        f"  毛盈利:       {attr.get('gross_profit', 0):>12.2f}\n"
        f"  毛亏损:       {attr.get('gross_loss', 0):>12.2f}\n"
        f"  总成本:       {attr.get('total_fees', 0):>12.2f}\n"
        f"  净盈亏:       {attr.get('net_pnl', 0):>12.2f}\n"
    )


class BacktestReporter:
    """回测报告生成"""

    @staticmethod
    def generate_report(result) -> str:
        """生成文本报告"""
        lines = [
            "=" * 60,
            "  猎场系统 V6.0 — 回测绩效报告",
            "=" * 60,
        ]
        lines.append(_metric_block(result, "全期"))

        if result.in_sample and result.out_of_sample:
            lines.append("  ---- 样本外验证（防过拟合） ----")
            lines.append(_metric_block(result.in_sample, "样本内 (train)"))
            lines.append(_metric_block(result.out_of_sample, "样本外 (test)"))

        lines.append("=" * 60)
        return "\n".join(lines) + "\n"

    @staticmethod
    def export_html(result, filepath: str):
        """导出HTML报告"""
        def cards(r):
            attr = r.return_attribution or {}
            return f"""
<div class="metrics">
<div class="card"><div class="label">总收益率</div><div class="value {'positive' if r.total_return>0 else 'negative'}">{r.total_return:.2f}%</div></div>
<div class="card"><div class="label">年化收益率</div><div class="value {'positive' if r.annual_return>0 else 'negative'}">{r.annual_return:.2f}%</div></div>
<div class="card"><div class="label">夏普比率</div><div class="value">{r.sharpe_ratio:.2f}</div></div>
<div class="card"><div class="label">最大回撤</div><div class="value negative">{r.max_drawdown:.2f}%</div></div>
<div class="card"><div class="label">胜率</div><div class="value">{r.win_rate:.1f}%</div></div>
<div class="card"><div class="label">交易次数</div><div class="value">{r.total_trades}</div></div>
<div class="card"><div class="label">盈亏比</div><div class="value">{_fmt_pf(r.profit_factor)}</div></div>
<div class="card"><div class="label">换手率</div><div class="value">{r.turnover_rate:.2f}x</div></div>
<div class="card"><div class="label">平均持仓</div><div class="value">{r.avg_hold_days:.1f}天</div></div>
</div>
<div class="attribution">
<div class="card"><div class="label">毛盈利</div><div class="value positive">{attr.get('gross_profit', 0):.0f}</div></div>
<div class="card"><div class="label">毛亏损</div><div class="value negative">{attr.get('gross_loss', 0):.0f}</div></div>
<div class="card"><div class="label">总成本(费+税)</div><div class="value">{attr.get('total_fees', 0):.0f}</div></div>
<div class="card"><div class="label">净盈亏</div><div class="value {'positive' if attr.get('net_pnl',0)>0 else 'negative'}">{attr.get('net_pnl', 0):.0f}</div></div>
</div>"""

        sections = [f"<h1>猎场 V6.0 回测报告</h1><h2>全期</h2>{cards(result)}"]

        if result.in_sample and result.out_of_sample:
            sections.append(
                f"<h2>样本外验证（防过拟合）</h2>"
                f"<h3>样本内 (train)</h3>{cards(result.in_sample)}"
                f"<h3>样本外 (test)</h3>{cards(result.out_of_sample)}"
            )

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>回测报告</title>
<style>body{{font-family:Arial;max-width:900px;margin:auto;padding:20px;background:#111;color:#e0e0e0}}
h1{{border-bottom:1px solid #333;padding-bottom:8px}}h2{{margin-top:32px;color:#8ab4f8}}h3{{color:#aaa}}
.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}
.attribution{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:16px}}
.card{{background:#1a1a2e;color:#e0e0e0;padding:16px;border-radius:8px;text-align:center}}
.card .label{{font-size:12px;opacity:.7}}.card .value{{font-size:24px;font-weight:bold;margin-top:8px}}
.positive{{color:#4caf50}}.negative{{color:#f44336}}</style></head><body>
{''.join(sections)}
<p style="color:#666;text-align:center;margin-top:32px">Generated at {datetime.now().isoformat()}</p>
</body></html>"""
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html)
        logger.info(f"HTML report saved to {filepath}")
