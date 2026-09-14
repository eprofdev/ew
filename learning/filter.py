"""
learning/filter.py
القرار النهائي: ندخل هذي الصفقة أو لا، وبأي حجم.

يأخذ احتمال النجاح من النموذج ويحوّله لقرارين:
  1. قبول/رفض حسب عتبة مُختارة من جدول العتبات بـwalk_forward.py
  2. حجم الصفقة عبر معيار Kelly المجزّأ

قاعدة أمان مركزية بالملف: التعلّم لا يرفع مخاطرتك أبدًا فوق RISK_PCT
اللي حددته أنت. أقصى ما يقدر يسويه هو تقليلها عند ضعف الثقة. السبب
مباشر: النموذج مبني على مئات الصفقات لا ملايين، واحتمالاته تقريبية —
وتقدير احتمال مبالغ فيه مع Kelly كامل طريق سريع لتفجير الحساب.
"""
from dataclasses import dataclass
from typing import Optional

from .features import features_to_vector
from .model import OnlineLogisticModel


@dataclass
class Decision:
    """قرار قابل للتسجيل والمراجعة لاحقًا — نسجّله حتى للصفقات المرفوضة."""
    take: bool
    probability: Optional[float]
    size_mult: float               # مضاعف على RISK_PCT، بين 0 و1
    risk_pct: float                # نسبة المخاطرة الفعلية المقترحة
    reason: str
    explain: list = None           # أكثر الخصائص تأثيرًا على هذا القرار

    def to_dict(self) -> dict:
        return {
            "take": self.take,
            "probability": round(self.probability, 4) if self.probability is not None else None,
            "size_mult": round(self.size_mult, 3),
            "risk_pct": round(self.risk_pct, 5),
            "reason": self.reason,
            "explain": self.explain,
        }


def kelly_fraction(p: float, b: float) -> float:
    """
    معيار Kelly لرهان ثنائي: f* = (p·(b+1) − 1) ÷ b

    p = احتمال الفوز، b = العائد مقسومًا على المخاطرة (مثلاً 2 يعني تربح
    ضعفي ما تخاطر فيه). النتيجة = النسبة المثلى من رأس المال للمخاطرة بها.
    سالب أو صفر = الصفقة خاسرة رياضيًا على المدى الطويل، لا تدخلها.
    """
    if b <= 0 or not (0.0 < p < 1.0):
        return 0.0
    f = (p * (b + 1.0) - 1.0) / b
    return max(0.0, f)


class SignalFilter:
    """
    الحكم بين الاستراتيجية والتنفيذ.

    threshold: أقل احتمال مقبول للدخول. 0.0 = مرّر كل شيء (بلا فلترة).
    kelly_frac: جزء Kelly المستخدم. 0.25 (ربع Kelly) افتراضي — Kelly الكامل
                صحيح رياضيًا فقط لو احتمالاتك دقيقة تمامًا، وهي ليست كذلك.
    base_risk_pct: سقف المخاطرة اللي حددته أنت. لا يُتجاوز أبدًا.
    """

    def __init__(self, model: Optional[OnlineLogisticModel] = None,
                 threshold: float = 0.0, kelly_frac: float = 0.25,
                 base_risk_pct: float = 0.01, min_size_mult: float = 0.25):
        self.model = model
        self.threshold = float(threshold)
        self.kelly_frac = float(kelly_frac)
        self.base_risk_pct = float(base_risk_pct)
        # أقل مضاعف حجم: صفقة مقبولة لكن بحجم 3% من المعتاد ما لها معنى عملي
        # (عمولات + كسور أسهم)، فإما ندخلها بحجم محترم أو نرفضها أصلاً.
        self.min_size_mult = float(min_size_mult)

    @property
    def active(self) -> bool:
        """
        هل التعلّم يؤثر فعليًا على القرارات الآن؟
        يكفي وجود نموذج ناضج: حتى بعتبة 0 يبقى حساب Kelly شغّالًا فيقلّل
        الأحجام ويرفض الصفقات ذات التوقّع السلبي.
        """
        return self.model is not None and self.model.is_ready

    def evaluate(self, features: Optional[dict], rr_ratio: float = 2.0) -> Decision:
        """
        يقيّم إشارة واحدة. يمرّرها بلا تدخّل في كل حالة غير مؤكدة:
        لا نموذج، نموذج غير ناضج، أو خصائص مفقودة.
        الفلسفة: التعلّم إضافة فوق البوت، لا نقطة فشل تعطّله.
        """
        if self.model is None:
            return Decision(True, None, 1.0, self.base_risk_pct,
                            "لا يوجد نموذج — تمرير بلا فلترة")
        if not self.model.is_ready:
            return Decision(True, None, 1.0, self.base_risk_pct,
                            f"النموذج تدرّب على {self.model.n_samples} صفقة فقط "
                            f"(المطلوب {self.model.min_samples}) — تمرير بلا فلترة")
        if not features:
            return Decision(True, None, 1.0, self.base_risk_pct,
                            "خصائص الإشارة غير متوفرة — تمرير بلا فلترة")

        x = features_to_vector(features)
        p = self.model.predict_proba(x)
        explain = self.model.explain(x)

        # نفضّل نسبة العائد/المخاطرة الفعلية للإشارة نفسها على الافتراضية
        b = features.get("rr_ratio")
        if b is None or not (b == b) or b <= 0:  # (b == b) ترفض NaN
            b = rr_ratio

        if self.threshold > 0 and p < self.threshold:
            return Decision(False, p, 0.0, 0.0,
                            f"احتمال النجاح {p:.1%} تحت العتبة {self.threshold:.0%}",
                            explain)

        f_kelly = kelly_fraction(p, b)
        if f_kelly <= 0:
            return Decision(False, p, 0.0, 0.0,
                            f"Kelly = 0 عند احتمال {p:.1%} وعائد/مخاطرة {b:.2f} — "
                            "توقّع سلبي رياضيًا", explain)

        # لا نتجاوز سقفك أبدًا؛ نقلّ عنه فقط عند ضعف الأفضلية
        suggested = min(self.base_risk_pct, self.kelly_frac * f_kelly)
        mult = suggested / self.base_risk_pct if self.base_risk_pct > 0 else 0.0

        if mult < self.min_size_mult:
            return Decision(False, p, 0.0, 0.0,
                            f"الحجم المقترح ({mult:.0%} من المعتاد) أصغر من الحد الأدنى "
                            f"({self.min_size_mult:.0%}) — أفضلية ضعيفة جدًا", explain)

        return Decision(True, p, mult, suggested,
                        f"احتمال {p:.1%} ≥ العتبة {self.threshold:.0%}، "
                        f"حجم {mult:.0%} من المعتاد (ربع Kelly)", explain)

    @classmethod
    def from_path(cls, model_path, **kwargs) -> "SignalFilter":
        """يبني فلترًا من نموذج محفوظ؛ لو الملف مفقود يرجع فلتر ممرِّر."""
        from pathlib import Path
        p = Path(model_path)
        if not p.exists():
            return cls(model=None, **kwargs)
        try:
            return cls(model=OnlineLogisticModel.load(p), **kwargs)
        except Exception:
            # نموذج تالف أو نسخة خصائص قديمة — نمرّر بدل ما نوقف التداول
            return cls(model=None, **kwargs)
