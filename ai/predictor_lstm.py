"""
猎场系统 V6.0 — LSTM 短期涨跌预测模型
输入: 60天 OHLCV 序列 → 输出: 未来 1/3/5 日预期收益率 + 置信度
"""

import logging
import numpy as np
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from datetime import datetime
import pickle
import json

logger = logging.getLogger(__name__)


class LSTMPredictor:
    """LSTM/Transformer 价格预测器"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._model = None
        self._scaler = None
        self._model_path = Path("data/models/lstm_predictor.pt")
        self._model_path.parent.mkdir(parents=True, exist_ok=True)
        self._trained = False

        cfg = self.config.get("ai_models", {}).get("lstm", {})
        self._seq_len = cfg.get("sequence_length", 60)
        self._horizons = cfg.get("prediction_horizons", [1, 3, 5])
        self._conf_threshold = cfg.get("confidence_threshold", 0.55)

    @property
    def is_trained(self) -> bool:
        return self._trained

    def predict(self, kline: List[Dict]) -> Dict:
        """
        预测未来收益率

        Args:
            kline: 日K线数据（至少60条）

        Returns:
            {
                'predictions': {1: +0.02, 3: +0.05, 5: +0.08},
                'confidence': 0.65,
                'direction': 'up',
                'signal': True/False
            }
        """
        if not self._trained or len(kline) < self._seq_len:
            return self._rule_based_predict(kline)

        try:
            # 提取序列特征
            closes = np.array([r['close'] for r in kline[-self._seq_len:]], dtype=np.float32)
            volumes = np.array([r.get('volume', 0) or 0 for r in kline[-self._seq_len:]], dtype=np.float32)

            # 归一化
            close_mean, close_std = closes.mean(), closes.std() + 1e-8
            closes_norm = (closes - close_mean) / close_std

            vol_mean, vol_std = volumes.mean(), volumes.std() + 1e-8
            volumes_norm = (volumes - vol_mean) / vol_std

            # 构造输入
            X = np.stack([closes_norm, volumes_norm], axis=-1)
            X = X.reshape(1, self._seq_len, 2)

            # PyTorch 预测
            import torch
            with torch.no_grad():
                X_tensor = torch.FloatTensor(X)
                preds = self._model(X_tensor).numpy()[0]

            predictions = {}
            for i, horizon in enumerate(self._horizons):
                if i < len(preds):
                    predictions[horizon] = float(preds[i])

            direction = 'up' if predictions.get(3, 0) > 0 else 'down'
            confidence = min(abs(predictions.get(3, 0)) * 10 + 0.4, 0.9)

            return {
                'predictions': predictions,
                'confidence': confidence,
                'direction': direction,
                'signal': direction == 'up' and confidence >= self._conf_threshold,
            }

        except Exception as e:
            logger.error(f"LSTM predict error: {e}")
            return self._rule_based_predict(kline)

    def _rule_based_predict(self, kline: List[Dict]) -> Dict:
        """规则fallback预测"""
        if len(kline) < 5:
            return {'predictions': {}, 'confidence': 0, 'direction': 'unknown', 'signal': False}

        closes = [r['close'] for r in kline]
        changes = np.diff(closes[-10:]) / closes[-11:-1]

        # 简单趋势判断
        ma5 = np.mean(closes[-5:])
        ma20 = np.mean(closes[-20:]) if len(closes) >= 20 else np.mean(closes)
        trend = 1 if ma5 > ma20 else -1

        # 动量
        momentum = np.mean(changes[-5:]) if len(changes) >= 5 else 0

        prediction = (trend * 0.02 + momentum * 0.5) * 0.5

        return {
            'predictions': {1: prediction * 0.3, 3: prediction * 0.7, 5: prediction},
            'confidence': 0.4,
            'direction': 'up' if prediction > 0 else 'down',
            'signal': prediction > 0.01,
        }

    def train(self, X_train: np.ndarray, y_train: np.ndarray,
              X_val=None, y_val=None, epochs: int = 50):
        """训练LSTM模型"""
        try:
            import torch
            import torch.nn as nn
            from torch.utils.data import DataLoader, TensorDataset

            class PricePredictor(nn.Module):
                def __init__(self, input_dim=2, hidden_dim=64, output_dim=3, num_layers=2):
                    super().__init__()
                    self.lstm = nn.LSTM(input_dim, hidden_dim, num_layers,
                                        batch_first=True, dropout=0.2)
                    self.attention = nn.Linear(hidden_dim, 1)
                    self.fc = nn.Sequential(
                        nn.Linear(hidden_dim, 32),
                        nn.ReLU(),
                        nn.Dropout(0.2),
                        nn.Linear(32, output_dim),
                    )

                def forward(self, x):
                    lstm_out, _ = self.lstm(x)
                    attn_weights = torch.softmax(self.attention(lstm_out), dim=1)
                    context = torch.sum(attn_weights * lstm_out, dim=1)
                    return self.fc(context)

            device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
            self._model = PricePredictor(
                input_dim=X_train.shape[2],
                output_dim=len(self._horizons)
            ).to(device)

            optimizer = torch.optim.Adam(self._model.parameters(), lr=0.001)
            criterion = nn.MSELoss()

            dataset = TensorDataset(
                torch.FloatTensor(X_train),
                torch.FloatTensor(y_train)
            )
            loader = DataLoader(dataset, batch_size=32, shuffle=True)

            self._model.train()
            for epoch in range(epochs):
                total_loss = 0
                for batch_X, batch_y in loader:
                    batch_X, batch_y = batch_X.to(device), batch_y.to(device)
                    optimizer.zero_grad()
                    output = self._model(batch_X)
                    loss = criterion(output, batch_y)
                    loss.backward()
                    optimizer.step()
                    total_loss += loss.item()

                if (epoch + 1) % 10 == 0:
                    logger.debug(f"LSTM Epoch {epoch + 1}/{epochs}, Loss: {total_loss / len(loader):.6f}")

            self._trained = True
            self.save()
            logger.info(f"LSTM trained: {len(X_train)} samples, {epochs} epochs")

        except ImportError:
            logger.warning("torch not installed, LSTM will use rule-based fallback")
        except Exception as e:
            logger.error(f"LSTM training error: {e}")

    def save(self):
        """保存模型"""
        try:
            import torch
            torch.save({
                'model_state': self._model.state_dict(),
                'seq_len': self._seq_len,
                'horizons': self._horizons,
                'trained_at': datetime.now().isoformat(),
            }, self._model_path)
        except Exception:
            pass

    def load(self) -> bool:
        """加载模型"""
        if not self._model_path.exists():
            return False
        try:
            import torch
            from .predictor_lstm import PricePredictor
            checkpoint = torch.load(self._model_path, map_location='cpu')
            self._model = PricePredictor(input_dim=2, output_dim=len(self._horizons))
            self._model.load_state_dict(checkpoint['model_state'])
            self._model.eval()
            self._trained = True
            logger.info(f"LSTM model loaded from {self._model_path}")
            return True
        except Exception as e:
            logger.error(f"LSTM load error: {e}")
            return False
