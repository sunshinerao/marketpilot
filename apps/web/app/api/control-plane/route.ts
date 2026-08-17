import { NextRequest, NextResponse } from "next/server";

import { upstreamHeaders, upstreamResponseHeaders } from "../upstream-auth";

const modelId = "strikepilot_spxw_0dte_ic";

type Result = { ok: true; data: unknown } | { ok: false; error: string; status: number };

async function upstream(request: NextRequest, path: string): Promise<Result> {
  const baseUrl = process.env.MARKETPILOT_API_URL ?? "http://127.0.0.1:8000";
  try {
    const response = await fetch(`${baseUrl}${path}`, {
      headers: upstreamHeaders(request),
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    const payload = (await response.json()) as unknown;
    if (!response.ok) {
      const detail =
        typeof payload === "object" && payload !== null && "detail" in payload
          ? String(payload.detail)
          : `${path} returned ${response.status}`;
      return { ok: false, error: detail, status: response.status };
    }
    return { ok: true, data: payload };
  } catch {
    return { ok: false, error: `${path} is unavailable`, status: 503 };
  }
}

export async function GET(request: NextRequest) {
  const [attribution, deliveries, versions, champion, validation, decisions, replays, integrity] = await Promise.all([
    upstream(request, "/v1/attribution/tasks"),
    upstream(request, "/v1/alerts/stream/deliveries"),
    upstream(request, `/v1/governance/models/${modelId}/versions`),
    upstream(request, `/v1/governance/models/${modelId}/champion`),
    upstream(request, `/v1/governance/models/${modelId}/validation`),
    upstream(request, "/v1/history/decisions?limit=12"),
    upstream(request, "/v1/history/replay-manifests"),
    upstream(request, "/v1/audit/integrity"),
  ]);

  const results = { attribution, deliveries, versions, champion, validation, decisions, replays, integrity };
  const authFailure = Object.values(results).find(
    (result) => !result.ok && (result.status === 401 || result.status === 403),
  );
  if (authFailure && !authFailure.ok) {
    return NextResponse.json(
      { detail: authFailure.error },
      {
        status: authFailure.status,
        headers: upstreamResponseHeaders(authFailure.status),
      },
    );
  }
  const errors = Object.entries(results)
    .filter(([, result]) => !result.ok)
    .map(([area, result]) => ({ area, error: (result as { error: string }).error }));

  return NextResponse.json({
    run_mode: "SCENARIO",
    scope: "LOCAL",
    verification: "UNVERIFIED",
    execution_enabled: false,
    checked_at: new Date().toISOString(),
    attribution: attribution.ok ? attribution.data : null,
    deliveries: deliveries.ok ? deliveries.data : null,
    governance: {
      versions: versions.ok ? versions.data : null,
      champion: champion.ok ? champion.data : null,
      validation: validation.ok ? validation.data : null,
    },
    audit: {
      decisions: decisions.ok ? decisions.data : null,
      replays: replays.ok ? replays.data : null,
      integrity: integrity.ok ? integrity.data : null,
    },
    errors,
  });
}
