export const dynamic = "force-dynamic";

type Health = { status: string; service: string };
type Model = { model_id: string; name: string; version: string };
type MarketState = { quality: string; reason: string; execution_enabled: boolean };

async function getJson<T>(path: string): Promise<T | null> {
  const baseUrl = process.env.MARKETPILOT_API_URL ?? "http://127.0.0.1:8000";
  try {
    const response = await fetch(`${baseUrl}${path}`, { cache: "no-store" });
    if (!response.ok) return null;
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

export default async function Home() {
  const [health, models, market] = await Promise.all([
    getJson<Health>("/health"),
    getJson<Model[]>("/v1/models"),
    getJson<MarketState>("/v1/market/state"),
  ]);
  const connected = health?.status === "ok";
  const model = models?.[0];
  const statusRows = [
    ["API", connected ? "CONNECTED" : "UNREACHABLE"],
    ["Data quality", market?.quality ?? "NOT CONNECTED"],
    ["Model", model ? `${model.name} ${model.version}` : "UNAVAILABLE"],
    ["Execution", market?.execution_enabled ? "ENABLED" : "MANUAL / READ ONLY"],
  ];
  const action = market?.quality === "GREEN" ? "WAIT" : "NO TRADE";
  const reason = market?.reason ?? "API_UNREACHABLE";

  return (
    <main>
      <header>
        <p className="eyebrow">MARKETPILOT / MODEL 01</p>
        <h1>Decision intelligence, with safety gates first.</h1>
        <p className="lead">
          The repository skeleton is ready. Live market data and executable strikes remain locked
          until provider capabilities and entitlements are verified.
        </p>
      </header>
      <section aria-label="Platform status">
        {statusRows.map(([label, value]) => (
          <article key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
          </article>
        ))}
      </section>
      <aside>
        <span>Current action</span>
        <b>{action}</b>
        <p>Reason: {reason}</p>
      </aside>
    </main>
  );
}
