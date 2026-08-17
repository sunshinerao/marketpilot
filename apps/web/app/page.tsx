import Workbench, { type PlatformStatus } from "./workbench";

export const dynamic = "force-dynamic";

async function getJson<T>(path: string): Promise<T | null> {
  const baseUrl = process.env.MARKETPILOT_API_URL ?? "http://127.0.0.1:8000";
  try {
    const response = await fetch(`${baseUrl}${path}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(2500),
    });
    if (!response.ok) return null;
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

export default async function Home() {
  const [health, models, market, events, modelHealth, capabilities] = await Promise.all([
    getJson<{ status: string; service: string }>("/health"),
    getJson<Array<{ model_id: string; name: string; version: string }>>("/v1/models"),
    getJson<PlatformStatus["market"]>("/v1/market/state"),
    getJson<PlatformStatus["events"]>("/v1/events/today"),
    getJson<PlatformStatus["modelHealth"]>("/v1/model/health"),
    getJson<PlatformStatus["capabilities"]>("/v1/providers/webull/capabilities"),
  ]);

  return (
    <Workbench
      initialStatus={{
        connected: health?.status === "ok",
        model: models?.[0] ?? null,
        market,
        events,
        modelHealth,
        capabilities,
        checkedAt: new Date().toISOString(),
      }}
    />
  );
}
