import { PanelShell } from "@/components/panel-shell";
import { PreferencesForm } from "@/components/preferences-form";

export default function PreferencesPage(): React.JSX.Element {
  return (
    <PanelShell
      active="/preferences"
      description="Configure the vacancy filters, Easy Apply scope and recruiter toggle used by the scheduled agent."
      title="Search Filters & Preferences"
    >
      <PreferencesForm />
    </PanelShell>
  );
}
