import TaskLauncher from "../components/task-launcher";

export default async function HomePage({ searchParams }) {
  const query = await searchParams;
  const initialTask = typeof query?.task === "string" ? query.task.slice(0, 10000) : "";
  return (
    <main className="landing-shell">
      <section className="landing-copy">
        <div className="eyebrow">DEVFLOW / BETA</div>
        <h1>Ship changes with an explicit human decision.</h1>
        <p>
          Connect your provider, consent to sharing committed source, and follow a live run.
          Review the evidence, approve a patch, and apply it yourself.
        </p>
        <div className="capability-row" aria-label="Workbench capabilities">
          <span>Durable events</span>
          <span>Revision checks</span>
          <span>Read-only diff</span>
        </div>
      </section>
      <TaskLauncher key={initialTask} initialTask={initialTask} />
    </main>
  );
}
