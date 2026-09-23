import type { ReportExportKind, ReportFeedbackTag, ReportFeedbackVerdict } from "./types";

/** 报告质量反馈的选项；报告页与管理中心共用同一套文案与色调。 */
export const FEEDBACK_VERDICTS: ReadonlyArray<{
  value: ReportFeedbackVerdict;
  label: string;
  tone: "success" | "warning" | "danger";
}> = [
  { value: "usable", label: "可直接使用", tone: "success" },
  { value: "needs_revision", label: "修改后可用", tone: "warning" },
  { value: "unusable", label: "不可用", tone: "danger" },
];

export const FEEDBACK_TAGS: ReadonlyArray<{ value: ReportFeedbackTag; label: string }> = [
  { value: "fact_error", label: "事实错误" },
  { value: "missing_fact", label: "遗漏关键事实" },
  { value: "liability", label: "责任认定不当" },
  { value: "legal_citation", label: "法规引用错误或过时" },
  { value: "reasoning", label: "推理不清" },
  { value: "format", label: "表述格式" },
  { value: "other", label: "其他" },
];

export const verdictOption = (value: ReportFeedbackVerdict) =>
  FEEDBACK_VERDICTS.find((item) => item.value === value) ?? FEEDBACK_VERDICTS[0];

export const tagLabel = (value: ReportFeedbackTag) =>
  FEEDBACK_TAGS.find((item) => item.value === value)?.label ?? value;

/** 下载文件会带的标记说明；正式导出不带标记，因此没有说明。 */
export const EXPORT_NOTICES: Partial<Record<ReportExportKind, string>> = {
  engineering: "演示样本：报告未经质量验收，下载的文件带有“工程验证样本”标记。",
  unreviewed: "独立审查未通过：可下载最后一版候选稿，文件带有“未通过独立审查”标记。",
};
