import { describe, expect, it } from "vitest";

import {
  buildAuthorizationPayload,
  canExecuteReportRun,
  canExportReportRun,
  createEmptyEvidenceRecord,
  createReportRunAfterDraftSave,
  parseAccidentData,
  toggleEvidenceConflict,
  updateEvidenceFieldConflict,
  RUN_TERMINAL_STATES,
} from "./ReportHarnessPanel";
import type { ReportAuthorizationPreview, ReportRunView } from "./types";
import { evidenceWriteRecord } from "./api";

function run(overrides: Partial<ReportRunView> = {}): ReportRunView {
  return {
    run_id: "run-1",
    session_id: "session-1",
    state: "queued",
    state_version: 1,
    snapshot_digest: "snapshot",
    candidate_version: 0,
    review_status: "pending",
    terminal_reason: null,
    budget: { remaining: 1000 },
    last_event_seq: 0,
    quality_gate: "engineering_only",
    formal_export_eligible: false,
    release_binding_status: "unapproved",
    execution_profile: "outbound",
    ...overrides,
  };
}

describe("报告 harness 纯逻辑", () => {
  it("需人工复核是终态，不能展示取消或同运行重启", () => {
    expect(RUN_TERMINAL_STATES.has("needs_review")).toBe(true);
    expect(canExecuteReportRun(run({ state: "needs_review" }), true)).toBe(false);
  });
  it("刷新后的证据保存只发送可编辑字段，不回传服务器审计字段", () => {
    const record = {
      ...createEmptyEvidenceRecord(), text: "合成文本",
      recorded_by: "server-owner", updated_at: "2026-09-17T00:00:00Z",
    };
    expect(evidenceWriteRecord(record)).not.toHaveProperty("recorded_by");
    expect(evidenceWriteRecord(record)).not.toHaveProperty("updated_at");
    expect(evidenceWriteRecord(record).text).toBe("合成文本");
  });
  it("使用确认时传入的当前 JSON，而不是初始草稿", () => {
    expect(parseAccidentData('{"事故类型":"追尾","速度":20}')).toEqual({
      事故类型: "追尾",
      速度: 20,
    });
    expect(() => parseAccidentData("{}" as string)).toThrow("不能为空对象");
  });

  it("只有事故输入保存成功后才创建报告运行", async () => {
    const events: string[] = [];
    const result = await createReportRunAfterDraftSave(
      async () => {
        events.push("save");
      },
      async () => {
        events.push("create");
        return "run-1";
      },
    );

    expect(result).toBe("run-1");
    expect(events).toEqual(["save", "create"]);
  });

  it("事故输入保存失败时不创建报告运行", async () => {
    let createCalled = false;
    await expect(
      createReportRunAfterDraftSave(
        async () => {
          throw new Error("409");
        },
        async () => {
          createCalled = true;
          return "run-1";
        },
      ),
    ).rejects.toThrow("409");
    expect(createCalled).toBe(false);
  });

  it("保守区分 synthetic_test 与 outbound 的执行资格", () => {
    expect(canExecuteReportRun(run({ execution_profile: "synthetic_test" }), false)).toBe(true);
    expect(canExecuteReportRun(run({ execution_profile: "outbound" }), false)).toBe(false);
    expect(canExecuteReportRun(run({ execution_profile: "outbound" }), true)).toBe(true);
    expect(canExecuteReportRun(run({ state: "active", execution_profile: "synthetic_test" }), false)).toBe(false);
  });

  it("只允许 published 运行导出，正式导出还要通过质量资格", () => {
    expect(canExportReportRun(run({ state: "queued" }), "formal")).toBe(false);
    expect(canExportReportRun(run({ state: "published", quality_gate: "engineering_only" }), "formal")).toBe(false);
    expect(canExportReportRun(run({ state: "published", quality_gate: "quality_validated", formal_export_eligible: true }), "formal")).toBe(true);
    expect(canExportReportRun(run({ state: "published" }), "engineering")).toBe(true);
  });

  it("授权请求严格复用服务端预览的三个 digest", () => {
    const preview: ReportAuthorizationPreview = {
      available: true,
      reason: null,
      snapshot_digest: "s",
      endpoint_profile_digest: "e",
      approved_knowledge_manifest_digest: "k",
      snapshot: {
        canonicalization_version: 1,
        accident_data: { type: "追尾" },
        supplemental_records: [],
        revision: 2,
        knowledge_manifest_digest: "k",
        fact_obligations: [],
        source_digests: {},
      },
      endpoints: [],
      knowledge_collections: [],
    };
    expect(buildAuthorizationPayload(preview)).toEqual({
      snapshot_digest: "s",
      endpoint_profile_digest: "e",
      approved_knowledge_manifest_digest: "k",
      confirmed: true,
    });
  });

  it("证据冲突和 JSON Pointer 字段冲突保持不可变更新", () => {
    const record = createEmptyEvidenceRecord();
    const withConflict = toggleEvidenceConflict(record, "other", true);
    expect(withConflict.conflicts_with).toEqual(["other"]);
    expect(record.conflicts_with).toEqual([]);

    const withField = {
      ...withConflict,
      field_conflicts: [{ accident_field: "/车辆/速度", explanation: "记录不一致" }],
    };
    const updated = updateEvidenceFieldConflict(withField, 0, { explanation: "人工复核不一致" });
    expect(updated.field_conflicts[0].explanation).toBe("人工复核不一致");
    expect(withField.field_conflicts[0].explanation).toBe("记录不一致");
  });
});
