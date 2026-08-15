const statusRows = [
  ["Data quality", "NOT CONNECTED"],
  ["Model", "StrikePilot 0.1 baseline"],
  ["Calibration", "NOT CALIBRATED"],
  ["Execution", "MANUAL / READ ONLY"],
];

export default function Home() {
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
        <b>NO TRADE</b>
        <p>Reason: DATA_CAPABILITY_NOT_VERIFIED</p>
      </aside>
    </main>
  );
}

