import { PanelShell } from "@/components/panel-shell";
import { ProfileForm } from "@/components/profile-form";

export default function ProfilePage(): React.JSX.Element {
  return (
    <PanelShell
      active="/profile"
      description="Capture the core candidate profile used by the automation. The CV upload is stored locally in a gitignored runtime directory for the MVP."
      title="Profile Configuration"
    >
      <ProfileForm />
    </PanelShell>
  );
}
