import { PanelShell } from "@/components/panel-shell";
import { OverviewCards } from "@/components/overview-cards";

export default function HomePage(): React.JSX.Element {
  return (
    <PanelShell
      active="/"
      description="The panel stays intentionally small. Use it to configure the candidate profile, search filters, AI setup and recruiter toggle without pulling UI concerns into the Python backend."
      title="Overview"
    >
      <OverviewCards />
    </PanelShell>
  );
}
