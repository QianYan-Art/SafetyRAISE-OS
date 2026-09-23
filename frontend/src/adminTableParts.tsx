import { ChevronLeft, ChevronRight, FolderOpen } from "lucide-react";

/** 管理中心各列表共用的空状态、时间格式与分页。 */
export const PAGE_SIZE_OPTIONS = [10, 20, 50] as const;

export function EmptyState(props: { title: string; description: string }) {
  return (
    <div className="account-empty-state">
      <FolderOpen aria-hidden="true" />
      <strong>{props.title}</strong>
      <span>{props.description}</span>
    </div>
  );
}

export function formatDateTime(value: string | number) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  const hours = `${date.getHours()}`.padStart(2, "0");
  const minutes = `${date.getMinutes()}`.padStart(2, "0");
  return `${year}-${month}-${day} ${hours}:${minutes}`;
}

export function paginate<T>(items: T[], page: number, pageSize: number): T[] {
  const safePage = Math.max(1, page);
  const start = (safePage - 1) * pageSize;
  return items.slice(start, start + pageSize);
}

export function TablePagination(props: {
  total: number;
  page: number;
  pageCount: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
}) {
  const { total, page, pageCount, pageSize, onPageChange, onPageSizeChange } = props;
  return (
    <div className="account-admin-pagination">
      <span>共 {total} 条 · 每页 {pageSize} 条</span>
      <div className="account-admin-pagination-controls">
        <label className="account-pagination-size">
          <span className="account-sr-only">每页条数</span>
          <select className="account-select" value={pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))} aria-label="每页条数">
            {PAGE_SIZE_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
        </label>
        <button type="button" className="account-icon-button" onClick={() => onPageChange(Math.max(1, page - 1))} disabled={page <= 1} aria-label="上一页" title="上一页"><ChevronLeft aria-hidden="true" /></button>
        <span>{page} / {pageCount}</span>
        <button type="button" className="account-icon-button" onClick={() => onPageChange(Math.min(pageCount, page + 1))} disabled={page >= pageCount} aria-label="下一页" title="下一页"><ChevronRight aria-hidden="true" /></button>
      </div>
    </div>
  );
}
