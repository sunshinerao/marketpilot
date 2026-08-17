import { NextRequest, NextResponse } from "next/server";

import {
  UpstreamHttpError,
  upstreamHeaders,
  upstreamResponseHeaders,
} from "../upstream-auth";

async function upstream(request: NextRequest, path: string, body?: unknown) {
  const baseUrl = process.env.MARKETPILOT_API_URL ?? "http://127.0.0.1:8000";
  const response = await fetch(`${baseUrl}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: upstreamHeaders(request, { json: body !== undefined }),
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) throw new UpstreamHttpError(response.status, `${path} returned ${response.status}`);
  return response.json() as Promise<unknown>;
}

export async function GET(request: NextRequest) {
  try {
    const [overview, scenarios, alerts, equitySession, quoteQuality, economics] = await Promise.all([
      upstream(request, "/v1/overview"),
      upstream(request, "/v1/demo/scenarios"),
      upstream(request, "/v1/alerts"),
      upstream(request, "/v1/scenario/session-quality/equity-session", {
        run_mode: "SCENARIO",
        session_date: "2026-11-27",
        verified_from: "2026-01-01",
        verified_through: "2026-12-31",
        holidays: ["2026-12-25"],
        early_closes: [{ session_date: "2026-11-27", closes_at: "13:00:00" }],
      }),
      upstream(request, "/v1/scenario/session-quality/quote-quality", {
        run_mode: "SCENARIO",
        as_of: "2026-08-16T12:00:00Z",
        policy: {
          green_max_age_seconds: 2,
          amber_max_age_seconds: 5,
          max_receive_latency_seconds: 1,
          conflict_absolute_tolerance: "0.50",
          conflict_relative_tolerance: "0.0001",
          require_two_sources: true,
        },
        observations: ["licensed-a", "licensed-b"].map((source, index) => ({
          source,
          instrument_id: "ESU6@XCME",
          source_ts: "2026-08-16T11:59:59Z",
          received_ts: "2026-08-16T11:59:59.100Z",
          delayed: false,
          entitlement: "VERIFIED",
          bid: index === 0 ? "6399.875" : "6401.875",
          ask: index === 0 ? "6400.125" : "6402.125",
          bid_size: "10",
          ask_size: "12",
          field_timestamps: {
            bid: "2026-08-16T11:59:59Z",
            ask: "2026-08-16T11:59:59Z",
            bid_size: "2026-08-16T11:59:59Z",
            ask_size: "2026-08-16T11:59:59Z",
          },
        })),
      }),
      upstream(request, "/v1/scenario/economics/assess", {
        run_mode: "SCENARIO",
        scope: "LOCAL",
        valued_at: "2026-08-16T14:30:01Z",
        quotes: [
          { leg_id: "short-call", quantity: -1, multiplier: 100, bid: 5, ask: 5.2, bid_size: 10, ask_size: 10, quoted_at: "2026-08-16T14:30:00Z" },
          { leg_id: "long-call", quantity: 1, multiplier: 100, bid: 2, ask: 2.2, bid_size: 10, ask_size: 10, quoted_at: "2026-08-16T14:30:00Z" },
        ],
        assumptions: { max_quote_age_seconds: 2, fee_per_contract: 0.5, slippage_per_contract: 0.05, max_size_participation: 0.5 },
        scenarios: [
          { name: "base", probability: 0.95, conservative_pnl: 30 },
          { name: "tail", probability: 0.05, conservative_pnl: -400 },
        ],
        risk: { max_loss: 400, risk_budget: 500, cvar_budget: 400, cvar_confidence: 0.95 },
      }),
    ]);
    return NextResponse.json({
      overview,
      scenarios,
      alerts,
      assurance: { equitySession, quoteQuality, economics },
    });
  } catch (error) {
    if (error instanceof UpstreamHttpError && (error.status === 401 || error.status === 403)) {
      return NextResponse.json(
        { detail: "Upstream authorization failed" },
        {
          status: error.status,
          headers: upstreamResponseHeaders(error.status),
        },
      );
    }
    return NextResponse.json(
      { detail: "Operations API unreachable. No external action was taken." },
      { status: 503 },
    );
  }
}
