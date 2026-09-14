"""
learning/dataset.py
يحوّل إشارات الاستراتيجية + نتائجها الفعلية إلى عيّنات تدريب.

كل صفقة مكتملة (دخول ثم خروج) = درس واحد:
    الخصائص = حالة السوق لحظة الدخول (من features.py)
    التسمية  = 1 لو ربحت، 0 لو خسرت
    r_multiple = الربح مقسومًا على المخاطرة الأصلية — أهم من مجرد ربح/خسارة،
                 لأن صفقة تربح 3 أضعاف المخاطرة ما تساوي صفقة تربح 0.2 منها.
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .features import extract_features, features_to_vector, date_to_index


@dataclass
class Sample:
    """درس واحد: حالة السوق لحظة الدخول + ما صار بعدها فعليًا."""
    date: pd.Timestamp
    symbol: str
    strategy: str
    features: dict
    label: int                      # 1 رابحة، 0 خاسرة
    r_multiple: float               # الربح ÷ المخاطرة الأصلية
    entry_price: float
    exit_price: float
    exit_date: Optional[pd.Timestamp] = None
    exit_reason: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None

    def vector(self) -> np.ndarray:
        return features_to_vector(self.features)


def attach_features(df: pd.DataFrame, signals: list) -> list:
    """
    يملأ حقل features لكل إشارة شراء بالقائمة (تعديل بالمكان).

    نفعلها كخطوة منفصلة بدل ما نحشرها داخل كل استراتيجية: الاستراتيجيات
    الأربع تبقى كما هي بدون أي تعديل، وأي استراتيجية جديدة تُضاف مستقبلاً
    تحصل على التعلّم مجانًا بدون كتابة سطر إضافي فيها.
    """
    for sig in signals:
        if sig.side != "BUY" or getattr(sig, "features", None) is not None:
            continue
        idx = date_to_index(df, sig.date)
        if idx is None:
            continue
        sig.features = extract_features(df, idx, stop_loss=sig.stop_loss,
                                        take_profit=sig.take_profit)
    return signals


def build_samples(df: pd.DataFrame, signals: list, symbol: str,
                  strategy: str = "unknown") -> list:
    """
    يزاوج كل إشارة شراء مع إشارة البيع اللي تليها، ويبني منها عيّنة تدريب.

    إشارة شراء بلا بيع بعدها (صفقة ما زالت مفتوحة بنهاية البيانات) تُهمَل —
    ما نقدر نتعلّم من صفقة ما انتهت، وتخمين نتيجتها تزوير مباشر للبيانات.
    """
    attach_features(df, signals)
    samples = []
    open_signal = None

    for sig in signals:
        if sig.side == "BUY":
            open_signal = sig if getattr(sig, "features", None) is not None else None
        elif sig.side == "SELL" and open_signal is not None:
            risk = open_signal.price - (open_signal.stop_loss or np.nan)
            if not np.isfinite(risk) or risk <= 0:
                open_signal = None
                continue
            pnl_per_share = sig.price - open_signal.price
            samples.append(Sample(
                date=open_signal.date,
                symbol=symbol,
                strategy=strategy,
                features=open_signal.features,
                label=1 if pnl_per_share > 0 else 0,
                r_multiple=float(pnl_per_share / risk),
                entry_price=float(open_signal.price),
                exit_price=float(sig.price),
                exit_date=sig.date,
                exit_reason=sig.reason,
                stop_loss=float(open_signal.stop_loss),
                take_profit=float(open_signal.take_profit) if open_signal.take_profit else None,
            ))
            open_signal = None

    return samples


def samples_to_arrays(samples: list):
    """يحوّل قائمة العيّنات إلى مصفوفات جاهزة للتدريب: X, y, r."""
    if not samples:
        return (np.empty((0, 0)), np.empty(0, dtype=int), np.empty(0))
    X = np.vstack([s.vector() for s in samples])
    y = np.array([s.label for s in samples], dtype=int)
    r = np.array([s.r_multiple for s in samples], dtype=float)
    return X, y, r


def sort_by_date(samples: list) -> list:
    """
    ترتيب زمني صارم — شرط أساسي لأي تقييم walk-forward.
    بدونه يتدرّب النموذج على صفقة 2024 ويتنبأ بصفقة 2022، والنتيجة وهمية.
    """
    return sorted(samples, key=lambda s: (pd.Timestamp(s.date), s.symbol))
