import type { ReportReference, ReportReferenceSource, ReportRunMaterials } from "./types";

const SOURCE_LABELS: Record<ReportReferenceSource, string> = {
  cited: "报告引用", read: "读取全文", search: "模型检索命中", initial: "初始检索",
};
const SOURCE_ORDER: ReportReferenceSource[] = ["cited", "read", "search", "initial"];
const ROLE_LABELS: Record<string, string> = { generator: "撰写模型", reviewer: "审查模型" };

/** 片段正文首部带“标题：”元数据时取其作为条目名，否则回退到片段编号。 */
function referenceTitle(item: ReportReference): string {
  const match = /^标题：(.+)$/m.exec(item.text);
  return match ? match[1].trim() : item.id;
}

function rank(item: ReportReference): number {
  const index = SOURCE_ORDER.findIndex((source) => item.sources.includes(source));
  return index === -1 ? SOURCE_ORDER.length : index;
}

/** 参考依据：先给出检索与读取的过程记录，再按“报告引用 > 读取全文 > 检索命中 > 初始检索”列出条目。 */
export function ReportReferences({ materials, active }: { materials: ReportRunMaterials | null; active: boolean }) {
  const references = [...(materials?.references ?? [])].sort((left, right) => rank(left) - rank(right));
  const lookups = materials?.lookups ?? [];
  if (!references.length) {
    return <p className="report-empty">{active ? "知识检索完成后显示在这里。" : "这份报告没有检索或读取知识条目。"}</p>;
  }
  const counts = SOURCE_ORDER
    .map((source) => [source, references.filter((item) => item.sources.includes(source)).length] as const)
    .filter(([, count]) => count > 0)
    .map(([source, count]) => `${SOURCE_LABELS[source]} ${count} 条`);
  return <div className="report-references">
    <p className="report-tab-hint">共 {references.length} 条知识：{counts.join("，")}。同一条可能同时属于多类。</p>
    {lookups.length > 0 && <section className="report-lookups" aria-label="检索与读取记录">
      <h4>检索与读取记录</h4>
      <ol>
        {lookups.map((item, index) => <li key={index}>
          <span className="report-ref-tag" data-source={item.kind}>{item.kind === "read" ? "读取全文" : item.kind === "initial" ? "初始检索" : "模型检索"}</span>
          {item.kind !== "initial" && item.role && <span className="report-lookup-role">{ROLE_LABELS[item.role] ?? item.role}</span>}
          <span className="report-lookup-text">{item.query ? `“${item.query}”` : item.result_ids.join("、")}</span>
          {item.query && <span className="report-lookup-count">命中 {item.result_ids.length} 条</span>}
        </li>)}
      </ol>
    </section>}
    <section aria-label="依据条目">
      <h4>依据条目</h4>
      {references.map((item) => {
        const roles = item.sources.some((source) => source === "read" || source === "search")
          ? item.roles.map((role) => ROLE_LABELS[role] ?? role) : [];
        return <article key={item.id} className="report-reference">
          <header>
            <strong>{referenceTitle(item)}</strong>
            <span className="report-ref-tags">
              {SOURCE_ORDER.filter((source) => item.sources.includes(source)).map((source) =>
                <span key={source} className="report-ref-tag" data-source={source}>{SOURCE_LABELS[source]}</span>)}
            </span>
          </header>
          <small>{item.id}{roles.length ? ` · ${roles.join("、")}` : ""}</small>
          <details className="report-reference-text">
            <summary>查看原文</summary>
            <p>{item.text}</p>
          </details>
        </article>;
      })}
    </section>
  </div>;
}
