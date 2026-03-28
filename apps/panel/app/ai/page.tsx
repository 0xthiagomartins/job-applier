import { AiForm } from "@/components/ai-form";
import { PanelShell } from "@/components/panel-shell";

export default function AiPage(): React.JSX.Element {
  return (
    <PanelShell
      active="/ai"
      description="Keep the model selection simple and low-cost. The API key is stored only in the local backend runtime store and is never committed to the repository."
      title="AI Settings"
    >
      <AiForm />
    </PanelShell>
  );
}
