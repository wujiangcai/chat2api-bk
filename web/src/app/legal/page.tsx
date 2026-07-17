import Link from "next/link";

const documents = [
  { href: "/legal/terms", title: "用户协议", description: "账号、额度、API 使用和服务规则" },
  { href: "/legal/privacy", title: "隐私政策", description: "数据收集、使用、存储和用户权利" },
  { href: "/legal/refund", title: "退款说明", description: "充值、套餐、失败任务退款与人工处理流程" },
];

export default function LegalIndexPage() {
  return (
    <section className="mx-auto w-full max-w-4xl space-y-6 py-8">
      <div className="space-y-2 text-center">
        <div className="text-xs font-semibold uppercase tracking-[0.22em] text-stone-400">Legal</div>
        <h1 className="text-3xl font-semibold tracking-tight text-stone-950">服务条款与合规说明</h1>
        <p className="text-sm leading-6 text-stone-500">正式对外服务前，请根据实际公司主体、支付渠道和上游授权情况复核并替换以下模板内容。</p>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        {documents.map((item) => (
          <Link key={item.href} href={item.href} className="rounded-2xl border border-white/80 bg-white/90 p-5 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md">
            <h2 className="text-lg font-semibold text-stone-950">{item.title}</h2>
            <p className="mt-2 text-sm leading-6 text-stone-500">{item.description}</p>
          </Link>
        ))}
      </div>
    </section>
  );
}
