import { NextRequest, NextResponse } from "next/server";

import { upstreamHeaders, upstreamResponseHeaders } from "../upstream-auth";

const scenarios = {
  live: {
    values: {},
  },
  cpi: {
    run_mode: "SCENARIO",
    scenario_session_id: "workbench-cpi",
    values: { center: 7812.4, up_tail: 46.5, down_tail: 53.1, joint_buffer: 7.5 },
    gates: {
      data_quality: "GREEN",
      event_cleared: false,
      option_chain_usable: true,
      tail_expanding: true,
      next_major_event_in_holding_period: true,
      edge_ok: true,
    },
  },
  cleared: {
    run_mode: "SCENARIO",
    scenario_session_id: "workbench-cleared",
    values: { center: 7812.4, up_tail: 28.6, down_tail: 34.2, joint_buffer: 3.5 },
    gates: {
      data_quality: "GREEN",
      event_cleared: true,
      option_chain_usable: true,
      edge_ok: true,
    },
  },
} as const;

type ScenarioId = keyof typeof scenarios;

export async function POST(request: NextRequest) {
  let scenario: ScenarioId;
  try {
    const body = (await request.json()) as { scenario?: string };
    if (!body.scenario || !(body.scenario in scenarios)) {
      return NextResponse.json({ detail: "Unknown demo scenario" }, { status: 400 });
    }
    scenario = body.scenario as ScenarioId;
  } catch {
    return NextResponse.json({ detail: "Invalid JSON body" }, { status: 400 });
  }

  const baseUrl = process.env.MARKETPILOT_API_URL ?? "http://127.0.0.1:8000";
  try {
    const response = await fetch(`${baseUrl}/v1/decision/run`, {
      method: "POST",
      headers: upstreamHeaders(request, { json: true }),
      body: JSON.stringify({
        model_id: "strikepilot_spxw_0dte_ic",
        ...(scenario === "live" ? {} : { as_of: new Date().toISOString() }),
        ...scenarios[scenario],
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    const payload = (await response.json()) as unknown;
    return NextResponse.json(payload, {
      status: response.status,
      headers: upstreamResponseHeaders(response.status),
    });
  } catch {
    return NextResponse.json(
      {
        detail: "Decision API unreachable. Fail-closed: NO_TRADE.",
        action: "NO_TRADE",
        reasons: ["API_UNREACHABLE"],
      },
      { status: 503 },
    );
  }
}
