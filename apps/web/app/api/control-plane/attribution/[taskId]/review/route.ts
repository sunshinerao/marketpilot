import { NextRequest, NextResponse } from "next/server";

import { upstreamHeaders, upstreamResponseHeaders } from "../../../../upstream-auth";

const allowedStatuses = new Set([
  "IN_REVIEW",
  "CONFIRMED",
  "REJECTED",
  "INCONCLUSIVE",
]);

export async function POST(
  request: NextRequest,
  context: { params: Promise<{ taskId: string }> },
) {
  const { taskId } = await context.params;
  let status: string;
  try {
    const body = (await request.json()) as { status?: string };
    if (!body.status || !allowedStatuses.has(body.status)) {
      return NextResponse.json({ detail: "Invalid local review status" }, { status: 400 });
    }
    status = body.status;
  } catch {
    return NextResponse.json({ detail: "Invalid JSON body" }, { status: 400 });
  }

  const baseUrl = process.env.MARKETPILOT_API_URL ?? "http://127.0.0.1:8000";
  try {
    const response = await fetch(
      `${baseUrl}/v1/attribution/tasks/${encodeURIComponent(taskId)}/reviews`,
      {
        method: "POST",
        headers: upstreamHeaders(request, { json: true }),
        body: JSON.stringify({
          status,
          reviewer: "local-workbench-operator",
          reviewed_at: new Date().toISOString(),
          note: "Local scenario review from the read-only MarketPilot workbench",
          retain_as_reusable_sample: false,
        }),
        cache: "no-store",
        signal: AbortSignal.timeout(5000),
      },
    );
    const payload = (await response.json()) as unknown;
    return NextResponse.json(payload, {
      status: response.status,
      headers: upstreamResponseHeaders(response.status),
    });
  } catch {
    return NextResponse.json(
      { detail: "Attribution review API unreachable. No external action was taken." },
      { status: 503 },
    );
  }
}
