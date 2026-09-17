/* =========================================================================
   هوية الموقع — غيّر ما تريد هنا فقط، وسينعكس على كل الصفحات.
   (العنوان في <title> وبطاقات المشاركة داخل ملفات HTML — انظر README.md)
   ========================================================================= */
window.BRAND = {
  name:    "لَبِنة",                  // الاسم بالعربية
  latin:   "Labina",                 // الاسم بالحروف اللاتينية
  tagline: "نبني من أوّل لَبِنة",      // الشعار
  owner:   "اسمك هنا",                // ← ضع اسمك الحقيقي
  role:    "مطوّر واجهات ومواقع",
  email:   "you@example.com",        // ← بريدك
  whatsapp:"9665XXXXXXXX",           // ← رقم واتساب بصيغة دولية بلا + ولا مسافات
  mostaql: "https://mostaql.com/u/USERNAME",  // ← رابط ملفك على مستقل
  khamsat: "https://khamsat.com/user/USERNAME",
  github:  "https://github.com/eprofdev"
};

/* يملأ العناصر التي تحمل data-brand="..." ويبني روابط التواصل تلقائياً */
document.addEventListener("DOMContentLoaded", function () {
  var B = window.BRAND;
  document.querySelectorAll("[data-brand]").forEach(function (el) {
    var key = el.getAttribute("data-brand");
    if (B[key]) el.textContent = B[key];
  });
  document.querySelectorAll("[data-brand-href]").forEach(function (el) {
    var key = el.getAttribute("data-brand-href");
    var map = {
      email:    "mailto:" + B.email,
      whatsapp: "https://wa.me/" + B.whatsapp,
      mostaql:  B.mostaql,
      khamsat:  B.khamsat,
      github:   B.github
    };
    if (map[key]) el.setAttribute("href", map[key]);
  });
});
