import { NextRequest, NextResponse } from "next/server";

import { upstreamHeaders, upstreamResponseHeaders } from "../../upstream-auth";

const allowedKinds = new Set(["ACKNOWLEDGED", "DISMISSED", "FALSE_POSITIVE"]);

export async function POST(request: NextRequest) {
  let alertId: string;
  let kind: string;
  try {
    const body = (await request.json()) as { alertId?: string; kind?: string };
    if (!body.alertId || !body.kind || !allowedKinds.has(body.kind)) {
      return NextResponse.json({ detail: "Invalid local feedback request" }, { status: 400 });
    }
    alertId = body.alertId;
    kind = body.kind;
  } catch {
    return NextResponse.json({ detail: "Invalid JSON body" }, { status: 400 });
  }

  const baseUrl = process.env.MARKETPILOT_API_URL ?? "http://127.0.0.1:8000";
  try {
    const response = await fetch(
      `${baseUrl}/v1/alerts/${encodeURIComponent(alertId)}/feedback`,
      {
        method: "POST",
        headers: upstreamHeaders(request, { json: true }),
        body: JSON.stringify({
          kind,
          actor: "demo-workbench-operator",
          recorded_at: new Date().toISOString(),
          note: "Process-local UI feedback; no external side effect",
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
      { detail: "Feedback API unreachable. No external action was taken." },
      { status: 503 },
    );
  }
}
