"""
backtest.py
محرك باكتيست يحاكي تنفيذ إشارات الاستراتيجية على بيانات تاريخية،
ويحسب حجم الصفقة حسب إدارة المخاطر، ويقيس الأداء.
"""
import pandas as pd
from strategy import TradeSignal, position_size


class Backtester:
    def __init__(self, initial_capital: float = 1000.0, risk_pct: float = 0.01):
        self.initial_capital = initial_capital
        self.risk_pct = risk_pct

    def run(self, signals: list[TradeSignal]) -> dict:
        capital = self.initial_capital
        equity_curve = [capital]
        trades = []

        open_trade = None
        for sig in signals:
            if sig.side == "BUY" and open_trade is None:
                shares = position_size(capital, self.risk_pct, sig.price, sig.stop_loss)
                if shares == 0:
                    continue
                open_trade = {
                    "entry_date": sig.date, "entry_price": sig.price,
                    "shares": shares, "stop_loss": sig.stop_loss,
                    "take_profit": sig.take_profit,
                }
            elif sig.side == "SELL" and open_trade is not None:
                pnl = (sig.price - open_trade["entry_price"]) * open_trade["shares"]
                capital += pnl
                trades.append({
                    "entry_date": open_trade["entry_date"],
                    "exit_date": sig.date,
                    "entry_price": round(open_trade["entry_price"], 2),
                    "exit_price": round(sig.price, 2),
                    "shares": open_trade["shares"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / (open_trade["entry_price"] * open_trade["shares"]) * 100, 2),
                    "reason": sig.reason,
                })
                equity_curve.append(capital)
                open_trade = None

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total_return_pct = (capital - self.initial_capital) / self.initial_capital * 100

        results = {
            "initial_capital": self.initial_capital,
            "final_capital": round(capital, 2),
            "total_return_pct": round(total_return_pct, 2),
            "num_trades": len(trades),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
            "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
            "max_drawdown_pct": self._max_drawdown(equity_curve),
            "trades": trades,
        }
        return results

    @staticmethod
    def _max_drawdown(equity_curve: list[float]) -> float:
        peak = equity_curve[0]
        max_dd = 0.0
        for val in equity_curve:
            peak = max(peak, val)
            dd = (peak - val) / peak * 100
            max_dd = max(max_dd, dd)
        return round(max_dd, 2)


def print_report(results: dict, symbol: str):
    print(f"\n{'=' * 50}")
    print(f"  تقرير الباكتيست — {symbol}")
    print(f"{'=' * 50}")
    print(f"رأس المال الابتدائي:   ${results['initial_capital']:.2f}")
    print(f"رأس المال النهائي:     ${results['final_capital']:.2f}")
    print(f"العائد الإجمالي:        {results['total_return_pct']}%")
    print(f"عدد الصفقات:            {results['num_trades']}")
    print(f"نسبة الصفقات الرابحة:   {results['win_rate_pct']}%")
    print(f"متوسط الربح للصفقة:     ${results['avg_win']}")
    print(f"متوسط الخسارة للصفقة:   ${results['avg_loss']}")
    print(f"أقصى تراجع (Drawdown):  {results['max_drawdown_pct']}%")
    print(f"{'=' * 50}\n")

    if results["trades"]:
        df = pd.DataFrame(results["trades"])
        print(df.to_string(index=False))
