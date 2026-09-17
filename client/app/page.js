import TaskLauncher from "../components/task-launcher";

export default function HomePage() {
  return (
    <main className="landing-shell">
      <section className="landing-copy">
        <div className="eyebrow">DEVFLOW / CONTROL PLANE</div>
        <h1>Ship changes with an explicit human decision.</h1>
        <p>
          Start a deterministic backend run, inspect its persisted patch and evidence,
          then approve, reject, or cancel without inventing client-side success.
        </p>
        <div className="capability-row" aria-label="Workbench capabilities">
          <span>Durable events</span>
          <span>Revision checks</span>
          <span>Read-only diff</span>
        </div>
      </section>
      <TaskLauncher />
    </main>
  );
}
