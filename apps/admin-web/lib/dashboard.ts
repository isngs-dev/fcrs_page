/**
 * Server-only dashboard data composition. The lead API owns tenant scoping
 * from the authenticated cookie; this helper deliberately accepts no tenant
 * identifier and never fabricates a partial pipeline when a stage fails.
 */
import "server-only";

import { listLeads, type LeadListItem, type LeadsResult } from "@/lib/leads";

export const DASHBOARD_STAGES = [
  { key: "captured", label: "Captured" },
  { key: "qualified", label: "Qualified" },
  { key: "contacted", label: "Contacted" },
  { key: "converted", label: "Converted" },
] as const;

export type DashboardStage = (typeof DASHBOARD_STAGES)[number]["key"];

export interface PipelineColumn {
  key: DashboardStage;
  label: string;
  total: number;
  items: LeadListItem[];
}

export interface PipelineMetrics {
  total: number;
  active: number;
  converted: number;
  qualificationRate: number | null;
}

export type DashboardPipelineResult =
  | { status: "ok"; columns: PipelineColumn[]; metrics: PipelineMetrics }
  | { status: "error"; message: string; correlationId: string };

/**
 * Creates the visible pipeline from four canonical live lead-stage lists.
 * A stage is the lead's current state, so totals are mutually exclusive and
 * can safely be summed. The qualification rate counts every lead that made
 * it past capture (qualified, contacted, or converted); `null` preserves the
 * unknown zero-denominator case rather than rendering a fabricated 0%.
 */
export function buildPipelineDashboard(results: LeadsResult[]): DashboardPipelineResult {
  const failed = results.find((result) => result.status === "error");
  if (failed?.status === "error") {
    return failed;
  }

  const columns = DASHBOARD_STAGES.map((stage, index) => {
    const result = results[index];
    if (!result || result.status !== "ok") {
      // The result array is produced from the fixed stage list below. This is
      // a defensive expected-error state, not a data fallback.
      return null;
    }
    return { ...stage, total: result.total, items: result.items };
  });

  if (columns.some((column) => column === null)) {
    return {
      status: "error",
      message: "Unable to load the complete lead pipeline. Please try again.",
      correlationId: "",
    };
  }

  const completeColumns = columns as PipelineColumn[];
  const totals = Object.fromEntries(completeColumns.map((column) => [column.key, column.total]));
  const total = completeColumns.reduce((sum, column) => sum + column.total, 0);
  const progressed = totals.qualified + totals.contacted + totals.converted;

  return {
    status: "ok",
    columns: completeColumns,
    metrics: {
      total,
      active: totals.captured + totals.qualified + totals.contacted,
      converted: totals.converted,
      qualificationRate: total === 0 ? null : progressed / total,
    },
  };
}

// SR-16 D1: as of the dashboard rebuild, no call site in the app invokes
// this function -- the landing page no longer renders a lead-pipeline
// kanban. Left in place deliberately (not deleted) because SR-18's real
// Leads board is its intended future consumer; treat this as orphaned but
// expected to be re-wired, not dead code to remove.
export async function getDashboardPipeline(): Promise<DashboardPipelineResult> {
  const results = await Promise.all(
    DASHBOARD_STAGES.map((stage) => listLeads({ page: 1, stage: stage.key }))
  );
  return buildPipelineDashboard(results);
}
