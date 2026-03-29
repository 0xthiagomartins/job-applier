import { OperationalDashboard } from "@/components/operational-dashboard";
import { PanelShell } from "@/components/panel-shell";

export default function HomePage(): React.JSX.Element {
  return (
    <PanelShell
      active="/"
      description="Track the last execution, trigger manual runs and jump quickly into schedule, filters and history without leaving the panel."
      title="Operations"
    >
      <OperationalDashboard />
    </PanelShell>
  );
}
