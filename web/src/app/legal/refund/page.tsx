import { LegalDocument } from "../legal-document";

export default function RefundPage() {
  return (
    <LegalDocument
      label="Refund"
      title="退款说明"
      updatedAt="2026-07-07"
      intro="本说明用于约定充值、套餐、CDK、失败任务和异常扣费的处理方式。"
      sections={[
        { title: "失败任务退款", items: ["异步图片任务在取消、失败或部分成功时，会按实际未完成数量自动返还预扣额度。", "同步接口在上游失败时会尝试返还预扣额度；返还记录以额度账本为准。"] },
        { title: "订单与套餐", items: ["未支付订单可由用户或管理员取消。", "已支付且已履约的套餐原则上按已使用额度、活动规则和支付渠道政策综合处理。"] },
        { title: "CDK", items: ["CDK 一经兑换会写入兑换记录；已兑换 CDK 不支持二次转让或重复兑换。", "如 CDK 因平台原因无法使用，可联系管理员核验兑换记录后补发额度或重新发码。"] },
        { title: "异常扣费", items: ["用户发现异常扣费、重复支付或额度未到账，可提供订单号、支付流水号、账号邮箱和时间范围申请核查。", "平台会依据 orders、payments、quota_ledger 和 audit_logs 记录进行处理。"] },
        { title: "处理时效", items: ["自动额度返还通常实时完成。", "人工退款或补偿需要结合支付渠道、银行处理时间和客服核验流程，具体时效以实际通知为准。"] },
      ]}
    />
  );
}
