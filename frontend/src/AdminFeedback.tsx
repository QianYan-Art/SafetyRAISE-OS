import { useEffect, useState } from "react";
import { Download } from "lucide-react";
import { downloadAdminReportFeedback, formatApiErrorMessage, listAdminReportFeedback } from "./api";
import { EmptyState, formatDateTime, TablePagination } from "./adminTableParts";
import { FEEDBACK_TAGS, FEEDBACK_VERDICTS, tagLabel, verdictOption } from "./feedbackOptions";
import type { AdminReportFeedbackPage, ReportFeedbackTag, ReportFeedbackVerdict } from "./types";

const COMMENT_PREVIEW = 80;

/** 管理员查看各报告的最新一次质量反馈，并按当前筛选导出表格。 */
export function AdminFeedback() {
  const [verdict, setVerdict] = useState<ReportFeedbackVerdict | "all">("all");
  const [tag, setTag] = useState<ReportFeedbackTag | "all">("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [data, setData] = useState<AdminReportFeedbackPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState("");
  const filters = {
    verdict: verdict === "all" ? undefined : verdict,
    tag: tag === "all" ? undefined : tag,
  };

  useEffect(() => { setPage(1); }, [verdict, tag, pageSize]);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError("");
    listAdminReportFeedback({ ...filters, limit: pageSize, offset: (page - 1) * pageSize })
      .then((value) => { if (current) setData(value); })
      .catch((err) => { if (current) setError(formatApiErrorMessage(err, "读取质量反馈失败。")); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [verdict, tag, page, pageSize]);

  async function exportTable() {
    setExporting(true);
    setError("");
    try {
      const result = await downloadAdminReportFeedback(filters);
      const url = URL.createObjectURL(result.blob);
      const link = document.createElement("a");
      link.href = url; link.download = result.fileName; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      setError(formatApiErrorMessage(err, "导出失败。"));
    } finally {
      setExporting(false);
    }
  }

  const total = data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  return (
    <section className="account-admin-surface" aria-labelledby="admin-feedback-title">
      <header className="account-admin-heading">
        <div>
          <h1 id="admin-feedback-title">质量反馈</h1>
          <p>每份报告取最新一次保存的意见</p>
        </div>
        <button type="button" className="account-button account-button-primary" disabled={exporting || total === 0}
          title="CSV 格式，可直接用 Excel 打开" onClick={() => void exportTable()}>
          <Download aria-hidden="true" />
          {exporting ? "正在导出" : "导出表格"}
        </button>
      </header>

      <div className="account-admin-toolbar">
        <select className="account-select" value={verdict} aria-label="筛选总体结论"
          onChange={(event) => setVerdict(event.target.value as ReportFeedbackVerdict | "all")}>
          <option value="all">全部结论</option>
          {FEEDBACK_VERDICTS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        <select className="account-select" value={tag} aria-label="筛选问题类型"
          onChange={(event) => setTag(event.target.value as ReportFeedbackTag | "all")}>
          <option value="all">全部问题类型</option>
          {FEEDBACK_TAGS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        <span className="account-admin-count">{total} 份报告有反馈</span>
      </div>

      {error ? <div className="account-alert account-alert-error" role="alert">{error}</div> : null}
      <div className="account-admin-table-wrap" role="region" aria-label="质量反馈列表" tabIndex={0}>
        <table className="account-admin-table account-feedback-table">
          <thead>
            <tr>
              <th>事故会话</th>
              <th>总体结论</th>
              <th>问题类型</th>
              <th>具体意见</th>
              <th>反馈人</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={5} className="account-admin-empty">正在读取质量反馈...</td></tr>
            ) : !data || data.items.length === 0 ? (
              <tr><td colSpan={5} className="account-admin-empty"><EmptyState title="还没有质量反馈"
                description="用户在报告页保存反馈后会出现在这里；也可以调整上方筛选条件。" /></td></tr>
            ) : data.items.map((item) => {
              const option = verdictOption(item.verdict);
              const context = item.run_context;
              return <tr key={item.run_id}>
                <td data-label="事故会话">
                  <span className="account-feedback-session">
                    <strong title={context.session_title ?? item.run_id}>{context.session_title || "未命名会话"}</strong>
                    <small>报告生成于 {context.run_created_at ? formatDateTime(context.run_created_at) : "-"}</small>
                  </span>
                </td>
                <td data-label="总体结论">
                  <span className="account-feedback-verdict" data-tone={option.tone}>{option.label}</span>
                </td>
                <td data-label="问题类型" className="account-feedback-tags" data-empty={item.issue_tags.length === 0}>
                  {item.issue_tags.length ? item.issue_tags.map(tagLabel).join("、") : "-"}
                </td>
                <td data-label="具体意见" className="account-feedback-comment">
                  {item.comment.length > COMMENT_PREVIEW
                    ? <details><summary>{item.comment.slice(0, COMMENT_PREVIEW)}…</summary><p>{item.comment}</p></details>
                    : item.comment || "-"}
                </td>
                <td data-label="反馈人">
                  <span className="account-feedback-reviewer">
                    <strong>{item.reviewer_name}</strong>
                    <small>账号 {item.author_username}</small>
                    <small>{formatDateTime(item.updated_at)}{item.revision > 1 ? ` · 第 ${item.revision} 版` : ""}</small>
                  </span>
                </td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>
      <TablePagination total={total} page={Math.min(page, pageCount)} pageCount={pageCount}
        pageSize={pageSize} onPageChange={setPage} onPageSizeChange={setPageSize} />
    </section>
  );
}
