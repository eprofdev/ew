"""
learning/bandit.py
المستوى الثاني من التعلّم: أي استراتيجية نشغّل أصلاً؟

النموذج بـmodel.py يجاوب "هل هذي الصفقة جيدة؟". هذا الملف يجاوب سؤالاً
أعلى منه: "أي استراتيجية من الأربع تشتغل أحسن بسوق اليوم؟"

الطريقة: Thompson Sampling — بدل ما نشغّل دائمًا الاستراتيجية الأفضل
تاريخيًا (فما نكتشف أبدًا إن غيرها صارت أحسن)، نحتفظ لكل استراتيجية
بتوزيع احتمالي لأدائها، ونسحب منه عشوائيًا كل مرة ونشغّل صاحب أعلى سحبة.
النتيجة سلوك متوازن تلقائيًا: الاستراتيجية القوية تُختار أغلب الوقت،
والضعيفة تُجرَّب أحيانًا — فلو تغيّر السوق لصالحها نكتشف ذلك بدل ما نعلق
على اختيار قديم للأبد.

نكافئ على R (الربح ÷ المخاطرة) لا على نسبة الفوز: استراتيجية تربح 40%
من صفقاتها بـ3R وتخسر 60% بـ1R أفضل بكثير من واحدة تربح 70% بـ0.3R.
"""
import json
import math
import random
from pathlib import Path
from typing import Optional

# تحويل R إلى مكافأة بين 0 و1 (مطلوب لتوزيع Beta).
# -1R (وقف خسارة كامل) → 0، و +3R → 1. الأبعد من ذلك يُقصّ.
R_MIN, R_MAX = -1.0, 3.0


def r_to_reward(r_multiple: float) -> float:
    if r_multiple is None or not math.isfinite(r_multiple):
        return 0.5  # نتيجة مجهولة = محايدة، لا تكافئ ولا تعاقب
    return min(1.0, max(0.0, (r_multiple - R_MIN) / (R_MAX - R_MIN)))


class StrategySelector:
    """
    يختار استراتيجية عبر Thompson Sampling على توزيعات Beta.

    decay: معامل نسيان لكل تحديث (0.99 افتراضيًا). يخلي الأداء الأخير أثقل
    من أداء قبل سنتين. بدونه تبقى استراتيجية نجحت في سوق 2021 مسيطرة على
    الاختيار حتى لو توقفت عن العمل من زمان.
    prior_strength: قوة الافتراض المبدئي (α=β=1 يعني "ما نعرف شيئًا").
    """

    def __init__(self, strategies: list, decay: float = 0.99,
                 seed: Optional[int] = None):
        self.strategies = list(strategies)
        self.decay = float(decay)
        self.alpha = {s: 1.0 for s in self.strategies}
        self.beta = {s: 1.0 for s in self.strategies}
        self.trials = {s: 0 for s in self.strategies}
        self.sum_r = {s: 0.0 for s in self.strategies}
        self.wins = {s: 0 for s in self.strategies}
        self._rng = random.Random(seed)

    # ------------------------------------------------------------- تحديث
    def update(self, strategy: str, r_multiple: float):
        """يسجّل نتيجة صفقة واحدة لاستراتيجية."""
        if strategy not in self.alpha:      # استراتيجية جديدة تنضاف تلقائيًا
            self.strategies.append(strategy)
            self.alpha[strategy] = 1.0
            self.beta[strategy] = 1.0
            self.trials[strategy] = 0
            self.sum_r[strategy] = 0.0
            self.wins[strategy] = 0

        reward = r_to_reward(r_multiple)
        # النسيان يُطبَّق على هذي الاستراتيجية فقط، لأن الاستراتيجيات اللي
        # ما اشتغلت اليوم ما وصلتها معلومة جديدة تستدعي إضعاف يقيننا فيها
        self.alpha[strategy] = 1.0 + (self.alpha[strategy] - 1.0) * self.decay + reward
        self.beta[strategy] = 1.0 + (self.beta[strategy] - 1.0) * self.decay + (1.0 - reward)
        self.trials[strategy] += 1
        self.sum_r[strategy] += float(r_multiple) if r_multiple is not None else 0.0
        if r_multiple is not None and r_multiple > 0:
            self.wins[strategy] += 1

    # ------------------------------------------------------------- اختيار
    def select(self) -> str:
        """
        يسحب عيّنة من توزيع كل استراتيجية ويرجّح صاحب أعلى سحبة.
        أي استراتيجية ما جُرّبت إطلاقًا لها الأولوية — لازم نشوفها تشتغل
        مرة واحدة على الأقل قبل ما نحكم عليها.
        """
        untried = [s for s in self.strategies if self.trials[s] == 0]
        if untried:
            return self._rng.choice(untried)
        draws = {s: self._rng.betavariate(max(self.alpha[s], 1e-6),
                                          max(self.beta[s], 1e-6))
                 for s in self.strategies}
        return max(draws, key=draws.get)

    def best(self) -> str:
        """أفضل استراتيجية حسب المتوسط المتوقع (بلا عشوائية) — للتقارير."""
        return max(self.strategies,
                   key=lambda s: self.alpha[s] / (self.alpha[s] + self.beta[s]))

    def report(self) -> list:
        """حالة كل استراتيجية: عدد الصفقات، نسبة الفوز، متوسط R، وفترة الثقة."""
        rows = []
        for s in self.strategies:
            a, b = self.alpha[s], self.beta[s]
            mean = a / (a + b)
            var = (a * b) / ((a + b) ** 2 * (a + b + 1))
            n = self.trials[s]
            rows.append({
                "strategy": s,
                "trials": n,
                "win_rate": round(self.wins[s] / n * 100, 1) if n else None,
                "avg_r": round(self.sum_r[s] / n, 3) if n else None,
                # نرجّع المكافأة المتوقعة إلى وحدات R ليصير الرقم مفهومًا
                "expected_r": round(mean * (R_MAX - R_MIN) + R_MIN, 3),
                "uncertainty": round(math.sqrt(var) * (R_MAX - R_MIN), 3),
            })
        return sorted(rows, key=lambda r: -r["expected_r"])

    def format_report(self) -> str:
        lines = ["", "اختيار الاستراتيجية (Thompson Sampling):", "",
                 f"   {'الاستراتيجية':<26}{'صفقات':<9}{'فوز٪':<9}{'متوسط R':<11}{'R متوقع':<11}{'±':<8}"]
        for r in self.report():
            win = f"{r['win_rate']}" if r["win_rate"] is not None else "-"
            avg = f"{r['avg_r']}" if r["avg_r"] is not None else "-"
            lines.append(f"   {r['strategy']:<26}{r['trials']:<9}{win:<9}{avg:<11}"
                         f"{r['expected_r']:<11}{r['uncertainty']:<8}")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------ حفظ/تحميل
    def to_dict(self) -> dict:
        return {
            "strategies": self.strategies, "decay": self.decay,
            "alpha": self.alpha, "beta": self.beta, "trials": self.trials,
            "sum_r": self.sum_r, "wins": self.wins,
        }

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
        tmp.replace(path)

    @classmethod
    def from_dict(cls, d: dict, seed: Optional[int] = None) -> "StrategySelector":
        sel = cls(d["strategies"], decay=d.get("decay", 0.99), seed=seed)
        sel.alpha = {k: float(v) for k, v in d["alpha"].items()}
        sel.beta = {k: float(v) for k, v in d["beta"].items()}
        sel.trials = {k: int(v) for k, v in d["trials"].items()}
        sel.sum_r = {k: float(v) for k, v in d.get("sum_r", {}).items()}
        sel.wins = {k: int(v) for k, v in d.get("wins", {}).items()}
        return sel

    @classmethod
    def load_or_new(cls, path, strategies: list, **kwargs) -> "StrategySelector":
        p = Path(path)
        if p.exists():
            try:
                sel = cls.from_dict(json.loads(p.read_text()), seed=kwargs.get("seed"))
                # استراتيجية جديدة أُضيفت للكود بعد آخر حفظ — نضمّها بحالة بكر
                for s in strategies:
                    if s not in sel.alpha:
                        sel.alpha[s] = sel.beta[s] = 1.0
                        sel.trials[s] = 0
                        sel.sum_r[s] = 0.0
                        sel.wins[s] = 0
                        sel.strategies.append(s)
                return sel
            except (json.JSONDecodeError, KeyError):
                pass
        return cls(strategies, **kwargs)
