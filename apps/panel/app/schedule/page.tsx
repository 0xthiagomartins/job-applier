import { PanelShell } from "@/components/panel-shell";
import { ScheduleForm } from "@/components/schedule-form";

export default function SchedulePage(): React.JSX.Element {
  return (
    <PanelShell
      active="/schedule"
      description="Configure when the agent should run and trigger a manual execution when you want to validate the full flow."
      title="Agent Schedule"
    >
      <ScheduleForm />
    </PanelShell>
  );
}
