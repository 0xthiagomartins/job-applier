import { ApplicationHistory } from "@/components/application-history";
import { PanelShell } from "@/components/panel-shell";

export default function HistoryPage(): React.JSX.Element {
  return (
    <PanelShell
      active="/history"
      description="Review successful applications, filter by company or date, and inspect exactly what was sent in each submission."
      title="Application History"
    >
      <ApplicationHistory />
    </PanelShell>
  );
}
