import unittest

from agent.trading_agent import TradingAgent


class TradingAgentTests(unittest.TestCase):
    def test_buy_decision_when_signal_is_strong_and_risk_is_low(self):
        config = {
            "agent": {
                "buy_threshold": 0.6,
                "sell_threshold": 0.3,
                "risk_threshold": 0.4,
            }
        }
        agent = TradingAgent(config)

        decision = agent.make_decision(signal_score=0.8, risk_score=0.2, position=0.0)

        self.assertEqual(decision["action"], "buy")
        self.assertIn("buy", decision["reason"].lower())

    def test_hold_decision_when_risk_is_high(self):
        config = {
            "agent": {
                "buy_threshold": 0.6,
                "sell_threshold": 0.3,
                "risk_threshold": 0.4,
            }
        }
        agent = TradingAgent(config)

        decision = agent.make_decision(signal_score=0.8, risk_score=0.8, position=0.0)

        self.assertEqual(decision["action"], "hold")
        self.assertIn("risk", decision["reason"].lower())


if __name__ == "__main__":
    unittest.main()
