"""
learning/model.py
نموذج التعلّم: انحدار لوجستي (Logistic Regression) يتدرّب تدريجيًا.

ليش انحدار لوجستي وليس شبكة عصبية أو XGBoost؟
1. عدد صفقاتك بالسنة بالمئات لا بالملايين — نموذج معقّد على بيانات قليلة
   يحفظ الضجيج ويسميه "نمطًا"، وهذا أسوأ من عدم وجود نموذج أصلًا.
2. مخرجاته احتمال معايَر فعلًا (0.68 تعني فعلاً ~68% نجاح على المدى الطويل)،
   وهذا بالضبط ما نحتاجه لحساب حجم الصفقة بـKelly.
3. قابل للقراءة: تقدر تشوف وزن كل خاصية وتفهم "ليش" رفض الصفقة.
4. يتدرّب تدريجيًا (online) بصفقة واحدة — فالبوت يتعلّم من صفقة أمس بدون
   إعادة تدريب كامل، وهذا هو المطلوب للتشغيل اليومي.

التدريب بـAdaGrad: معدل تعلّم يتكيّف لكل خاصية لحاله، فما يحتاج ضبط يدوي
ويتحمّل اختلاف مقاييس الخصائص.
"""
import json
import math
from pathlib import Path
from typing import Optional

import numpy as np

from .features import FEATURE_NAMES, N_FEATURES, features_to_vector

MODEL_VERSION = 1
CLIP_Z = 5.0  # سقف القيمة المعيارية — يمنع عيّنة شاذة واحدة من قلب النموذج


class RunningScaler:
    """
    تطبيع تدريجي لكل خاصية (متوسط وانحراف معياري) بخوارزمية Welford.

    مهم للأمانة الزمنية: يتحدّث فقط أثناء التدريب، وأبدًا أثناء التنبؤ.
    لو حدّثناه وقت التنبؤ، صار النموذج يستخدم معلومة من عيّنة ما تعلّم منها بعد
    (تسرّب بيانات خفيف لكنه حقيقي).

    القيم المفقودة (NaN) تُتجاهل بالحساب، وتُعامل وقت التحويل كأنها المتوسط
    (أي z = 0) — يعني "لا معلومة" بدل "معلومة خاطئة".
    """

    def __init__(self, n_features: int = N_FEATURES):
        self.n = np.zeros(n_features, dtype=float)
        self.mean = np.zeros(n_features, dtype=float)
        self.m2 = np.zeros(n_features, dtype=float)

    def update(self, x: np.ndarray):
        valid = np.isfinite(x)
        if not valid.any():
            return
        self.n[valid] += 1
        delta = np.where(valid, x - self.mean, 0.0)
        self.mean += np.where(valid, delta / np.maximum(self.n, 1), 0.0)
        delta2 = np.where(valid, x - self.mean, 0.0)
        self.m2 += delta * delta2

    def std(self) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            var = np.where(self.n > 1, self.m2 / np.maximum(self.n - 1, 1), 1.0)
        std = np.sqrt(np.maximum(var, 0.0))
        # خاصية ثابتة تمامًا (انحراف صفري) ما تحمل معلومة — نخليها 1 لتفادي القسمة على صفر
        return np.where(std > 1e-9, std, 1.0)

    def transform(self, x: np.ndarray) -> np.ndarray:
        z = (np.where(np.isfinite(x), x, self.mean) - self.mean) / self.std()
        return np.clip(z, -CLIP_Z, CLIP_Z)

    def to_dict(self) -> dict:
        return {"n": self.n.tolist(), "mean": self.mean.tolist(), "m2": self.m2.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "RunningScaler":
        s = cls(len(d["mean"]))
        s.n = np.array(d["n"], dtype=float)
        s.mean = np.array(d["mean"], dtype=float)
        s.m2 = np.array(d["m2"], dtype=float)
        return s


def _sigmoid(z: float) -> float:
    # صيغة مستقرة عدديًا — exp(710) تطفح بالبايثون وترجع inf
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 500.0)))
    e = math.exp(max(z, -500.0))
    return e / (1.0 + e)


class OnlineLogisticModel:
    """
    احتمال نجاح الصفقة = sigmoid(w · z + b) حيث z الخصائص بعد التطبيع.

    min_samples: أقل عدد صفقات قبل ما نثق بالنموذج إطلاقًا. تحته يرجع
    is_ready = False، وطبقة الفلترة تمرّر كل الإشارات بدون تدخّل. هذي
    الحماية الأهم بالملف كله: بوت يرفض صفقات بناءً على 9 عيّنات أسوأ من
    بوت بلا تعلّم.
    """

    def __init__(self, l2: float = 1e-3, lr: float = 0.15,
                 min_samples: int = 40, feature_names: Optional[list] = None):
        self.feature_names = list(feature_names) if feature_names else list(FEATURE_NAMES)
        n = len(self.feature_names)
        self.w = np.zeros(n, dtype=float)
        self.b = 0.0
        self.scaler = RunningScaler(n)
        self.l2 = float(l2)
        self.lr = float(lr)
        self.min_samples = int(min_samples)
        self.n_samples = 0
        self.n_positive = 0
        self._g2 = np.zeros(n, dtype=float)  # مجموع مربعات التدرّج (AdaGrad)
        self._g2_b = 0.0

    # ------------------------------------------------------------------ حالة
    @property
    def is_ready(self) -> bool:
        """هل تدرّب على عيّنات كافية لنثق بمخرجاته؟"""
        return self.n_samples >= self.min_samples

    @property
    def base_rate(self) -> float:
        """نسبة الصفقات الرابحة بكل ما تعلّمه — خط الأساس للمقارنة."""
        return self.n_positive / self.n_samples if self.n_samples else 0.0

    # ----------------------------------------------------------------- تدريب
    def partial_fit(self, x: np.ndarray, y: int, sample_weight: float = 1.0):
        """خطوة تدريب واحدة من صفقة واحدة — هذي هي آلية التعلّم اليومي."""
        x = np.asarray(x, dtype=float)
        self.scaler.update(x)          # التطبيع يتعلّم أولاً من العيّنة
        z = self.scaler.transform(x)
        p = _sigmoid(float(np.dot(self.w, z) + self.b))
        err = (p - float(y)) * float(sample_weight)

        grad_w = err * z + self.l2 * self.w
        grad_b = err

        self._g2 += grad_w ** 2
        self._g2_b += grad_b ** 2
        self.w -= self.lr * grad_w / (np.sqrt(self._g2) + 1e-8)
        self.b -= self.lr * grad_b / (math.sqrt(self._g2_b) + 1e-8)

        self.n_samples += 1
        self.n_positive += int(y)

    def fit(self, X: np.ndarray, y: np.ndarray, epochs: int = 12,
            seed: int = 0, balance: bool = True):
        """
        تدريب دفعة كاملة بعدة تمريرات مع خلط عشوائي ثابت البذرة.

        balance: يعطي الفئة الأقل عددًا وزنًا أكبر. بدونه، لو 70% من صفقاتك
        خاسرة، النموذج يتعلّم الحل الكسول "توقّع الخسارة دائمًا" — دقة 70%
        وفائدة صفر.
        """
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=int)
        if len(X) == 0:
            return self

        n_pos = int(y.sum())
        n_neg = len(y) - n_pos
        if balance and n_pos > 0 and n_neg > 0:
            w_pos = len(y) / (2.0 * n_pos)
            w_neg = len(y) / (2.0 * n_neg)
        else:
            w_pos = w_neg = 1.0

        rng = np.random.RandomState(seed)
        for epoch in range(epochs):
            order = rng.permutation(len(X))
            for i in order:
                weight = w_pos if y[i] == 1 else w_neg
                self.partial_fit(X[i], int(y[i]), sample_weight=weight)

        # كل تمريرة تزيد n_samples، وهذا يضخّم عدد العيّنات الحقيقي زورًا
        # ويخلي min_samples بلا معنى. نصحّحه للعدد الفعلي بعد التدريب.
        self.n_samples = len(X)
        self.n_positive = n_pos
        return self

    # ----------------------------------------------------------------- تنبؤ
    def predict_proba(self, x: np.ndarray) -> float:
        """احتمال نجاح الصفقة. لا يعدّل حالة النموذج إطلاقًا."""
        z = self.scaler.transform(np.asarray(x, dtype=float))
        return _sigmoid(float(np.dot(self.w, z) + self.b))

    def predict_proba_features(self, feats: dict) -> float:
        return self.predict_proba(features_to_vector(feats))

    def explain(self, x: np.ndarray, top: int = 5) -> list:
        """
        أكثر الخصائص تأثيرًا على هذا القرار تحديدًا (وزن × قيمة معيارية).
        موجب = يدفع نحو "صفقة رابحة"، سالب = يدفع نحو الرفض.
        """
        z = self.scaler.transform(np.asarray(x, dtype=float))
        contrib = self.w * z
        order = np.argsort(-np.abs(contrib))[:top]
        return [(self.feature_names[i], round(float(contrib[i]), 3)) for i in order]

    def weights_report(self) -> list:
        """أوزان النموذج مرتبة بالأهمية — لفهم ما تعلّمه فعليًا."""
        order = np.argsort(-np.abs(self.w))
        return [(self.feature_names[i], round(float(self.w[i]), 4)) for i in order]

    # ------------------------------------------------------------ حفظ/تحميل
    def to_dict(self) -> dict:
        return {
            "version": MODEL_VERSION,
            "feature_names": self.feature_names,
            "w": self.w.tolist(),
            "b": self.b,
            "scaler": self.scaler.to_dict(),
            "l2": self.l2,
            "lr": self.lr,
            "min_samples": self.min_samples,
            "n_samples": self.n_samples,
            "n_positive": self.n_positive,
            "g2": self._g2.tolist(),
            "g2_b": self._g2_b,
        }

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # كتابة ذرّية: لو انقطع التشغيل بالنص، ما نخرب النموذج القديم
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
        tmp.replace(path)

    @classmethod
    def from_dict(cls, d: dict) -> "OnlineLogisticModel":
        if d.get("version") != MODEL_VERSION:
            raise ValueError(
                f"نسخة النموذج {d.get('version')} لا تطابق النسخة الحالية {MODEL_VERSION} — "
                "أعد التدريب بـ train_model.py"
            )
        if list(d["feature_names"]) != list(FEATURE_NAMES):
            raise ValueError(
                "خصائص النموذج المحفوظ لا تطابق features.py الحالي — "
                "تغيّرت قائمة الخصائص، لازم إعادة تدريب من الصفر."
            )
        m = cls(l2=d["l2"], lr=d["lr"], min_samples=d["min_samples"],
                feature_names=d["feature_names"])
        m.w = np.array(d["w"], dtype=float)
        m.b = float(d["b"])
        m.scaler = RunningScaler.from_dict(d["scaler"])
        m.n_samples = int(d["n_samples"])
        m.n_positive = int(d["n_positive"])
        m._g2 = np.array(d["g2"], dtype=float)
        m._g2_b = float(d["g2_b"])
        return m

    @classmethod
    def load(cls, path) -> "OnlineLogisticModel":
        return cls.from_dict(json.loads(Path(path).read_text()))

    @classmethod
    def load_or_new(cls, path, **kwargs) -> "OnlineLogisticModel":
        """يحمّل النموذج إن وُجد وكان صالحًا، وإلا يرجع نموذجًا جديدًا فارغًا."""
        p = Path(path)
        if p.exists():
            try:
                return cls.load(p)
            except (ValueError, KeyError, json.JSONDecodeError):
                pass  # ملف تالف أو نسخة قديمة — نبدأ من جديد بدل ما نتوقف
        return cls(**kwargs)
