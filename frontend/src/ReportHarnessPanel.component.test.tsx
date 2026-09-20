// @vitest-environment jsdom

import { createRef } from "react";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReportHarnessPanel, type ReportHarnessPanelHandle } from "./ReportHarnessPanel";
import type { ReportRunView } from "./types";

const api = vi.hoisted(() => ({
  fetchReportEvidence: vi.fn(),
  listReportRuns: vi.fn(),
  fetchReportRun: vi.fn(),
  fetchReportRunCandidate: vi.fn(),
  fetchReportRunEvents: vi.fn(),
}));
vi.mock("./api", async () => ({
  ...(await vi.importActual<typeof import("./api")>("./api")),
  ...api,
}));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe("开发证据模式导航保护", () => {
  it.each([
    ["generating", true], ["checking", true], ["queued", false], ["suspended", false], ["failed", false],
  ] as const)("%s状态离开保护为%s", async (state, blocked) => {
    const run: ReportRunView = {
      run_id: "run-1", session_id: "session-1", state, state_version: 1,
      snapshot_digest: "synthetic", candidate_version: 0, review_status: "pending",
      terminal_reason: null, budget: { remaining: 1000 }, last_event_seq: 0,
      quality_gate: "engineering_only", formal_export_eligible: false,
      release_binding_status: "unapproved", execution_profile: "outbound",
    };
    api.fetchReportEvidence.mockResolvedValue({ revision: 0, records: [] });
    api.listReportRuns.mockResolvedValue({ runs: [run], next_cursor: null });
    api.fetchReportRun.mockResolvedValue(run);
    api.fetchReportRunCandidate.mockResolvedValue(null);
    api.fetchReportRunEvents.mockResolvedValue({ events: [], next_seq: 0 });
    const ref = createRef<ReportHarnessPanelHandle>();
    render(<ReportHarnessPanel ref={ref} sessionId="session-1" initialDraftJson='{"事故":"合成测试"}' onPersistDraft={vi.fn()} />);
    await waitFor(() => expect(api.fetchReportRunEvents).toHaveBeenCalled());
    await waitFor(() => expect(ref.current?.isTransitionBlocked()).toBe(blocked));
  });
});
