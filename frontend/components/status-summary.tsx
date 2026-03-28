"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fetchPanelState } from "@/lib/api";
import { PanelState } from "@/lib/types";

function readinessBadge(ready: boolean): React.JSX.Element {
  return <Badge>{ready ? "Ready" : "Pending"}</Badge>;
}

export function StatusSummary(): React.JSX.Element {
  const [state, setState] = useState<PanelState | null>(null);

  useEffect(() => {
    void fetchPanelState().then(setState).catch(() => setState(null));
  }, []);

  const profileReady = Boolean(state?.profile.name && state.profile.email);
  const preferencesReady = Boolean(state?.preferences.location && state.preferences.keywords.length > 0);
  const aiReady = Boolean(state?.ai.has_api_key);

  return (
    <Card className="bg-card/90">
      <CardHeader>
        <CardTitle>Configuration Summary</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center justify-between rounded-2xl bg-white/65 px-4 py-3">
          <span>Profile</span>
          {readinessBadge(profileReady)}
        </div>
        <div className="flex items-center justify-between rounded-2xl bg-white/65 px-4 py-3">
          <span>Preferences</span>
          {readinessBadge(preferencesReady)}
        </div>
        <div className="flex items-center justify-between rounded-2xl bg-white/65 px-4 py-3">
          <span>AI Setup</span>
          {readinessBadge(aiReady)}
        </div>
      </CardContent>
    </Card>
  );
}
