type LegalSection = {
  title: string;
  items: string[];
};

export function LegalDocument({
  label,
  title,
  updatedAt,
  intro,
  sections,
}: {
  label: string;
  title: string;
  updatedAt: string;
  intro: string;
  sections: LegalSection[];
}) {
  return (
    <article className="mx-auto w-full max-w-4xl space-y-6 py-8">
      <header className="rounded-[28px] border border-white/80 bg-white/90 p-6 shadow-sm sm:p-8">
        <div className="text-xs font-semibold uppercase tracking-[0.22em] text-stone-400">{label}</div>
        <h1 className="mt-3 text-3xl font-semibold tracking-tight text-stone-950">{title}</h1>
        <p className="mt-3 text-sm leading-7 text-stone-500">{intro}</p>
        <p className="mt-4 text-xs text-stone-400">更新日期：{updatedAt}</p>
      </header>
      <div className="space-y-4">
        {sections.map((section, index) => (
          <section key={section.title} className="rounded-2xl border border-white/80 bg-white/90 p-5 shadow-sm sm:p-6">
            <h2 className="text-lg font-semibold text-stone-950">{index + 1}. {section.title}</h2>
            <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-7 text-stone-600">
              {section.items.map((item) => <li key={item}>{item}</li>)}
            </ul>
          </section>
        ))}
      </div>
      <p className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-xs leading-6 text-amber-800">
        本页面为商业化上线模板，正式发布前应由运营主体结合公司名称、联系方式、支付渠道、上游模型服务授权和当地法律要求进行复核。
      </p>
    </article>
  );
}
