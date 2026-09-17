import RunWorkbench from "../../../components/run-workbench";

export default async function RunPage({ params }) {
  const { runId } = await params;
  return <RunWorkbench key={runId} runId={runId} />;
}
